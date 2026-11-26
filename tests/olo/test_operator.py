"""Operator authorization and audit (olo.operator, post-audit batch 3).

The authorized resolution path stands beside the DenyAll OperatorToken
stubs, which stay DenyAll (tests/olo/m3/test_identity_safety_authz.py,
tests/olo/test_ledger.py).
"""
from __future__ import annotations

import sqlite3

import pytest

from olo.adapters.contacts import OperatorToken, create_operator_token
from olo.ledger import AuthorizationError, LedgerEvent, apply_event, resolve_unresolved_match
from olo.operator import (
    OperatorGrant,
    assign_signal_identity,
    authorize,
    enroll,
    migrate,
    resolve_commitment,
    resolve_identifier,
    revoke,
)
from olo.store import connect
from olo.store import migrate as migrate_store
from tests.olo.test_s5_pipeline import Pipeline, content

NOW = "2026-11-26T12:00:00.000000Z"
LATER = "2026-11-26T12:16:00.000000Z"
PASS = "correct horse battery"


@pytest.fixture()
def db(tmp_path) -> sqlite3.Connection:
    conn = connect(tmp_path / "olo.db")
    migrate_store(conn)
    migrate(conn)
    return conn


def decisions(db) -> list[tuple]:
    return [tuple(r) for r in db.execute(
        "SELECT actor, subject_kind, decision, result FROM operator_decisions ORDER BY id")]


def unresolved_commit(db, doc="d2", contact="cid_alice") -> None:
    for d in ("d1", doc):
        apply_event(db, LedgerEvent(
            event_type="delivery", contact_id=contact, business_key="", source_doc_id=d,
            source_kind="email.content", occurred_at="2024-03-01T09:00:00+00:00",
            actor_role="business", action="commit", confirmed_date="2024-04-10",
            thread_id=f"t-{d}"), recorded_at=NOW)
    db.commit()


class TestAuthorization:
    def test_the_first_operator_bootstraps_and_later_ones_need_a_grant(self, db) -> None:
        enroll(db, "ops", PASS, now=NOW)
        with pytest.raises(AuthorizationError):
            enroll(db, "intruder", PASS, now=NOW)
        g = authorize(db, "ops", PASS, now=NOW)
        enroll(db, "second", "another long passphrase", now=NOW, grant=g)
        assert [d[:3] for d in decisions(db)] == [
            ("bootstrap", "operator", "enroll"), ("ops", "operator", "authorize"),
            ("ops", "operator", "enroll")]

    def test_short_passphrases_are_refused(self, db) -> None:
        with pytest.raises(ValueError):
            enroll(db, "ops", "short", now=NOW)

    def test_a_wrong_passphrase_is_refused_and_audited(self, db) -> None:
        enroll(db, "ops", PASS, now=NOW)
        with pytest.raises(AuthorizationError):
            authorize(db, "ops", "wrong passphrase!", now=NOW)
        with pytest.raises(AuthorizationError):
            authorize(db, "nobody", PASS, now=NOW)
        assert [d[3] for d in decisions(db)][-2:] == [
            "refused: unknown operator or wrong passphrase"] * 2
        assert db.execute("SELECT COUNT(*) FROM operator_grants").fetchone()[0] == 0

    @pytest.mark.parametrize("forged", [
        None, "ops", OperatorToken(actor="ops"), OperatorGrant("ops", "made-up", "made-up")])
    def test_forged_or_foreign_tokens_cannot_decide(self, db, forged) -> None:
        enroll(db, "ops", PASS, now=NOW)
        unresolved_commit(db)
        with pytest.raises(AuthorizationError):
            resolve_commitment(db, forged, "d2", "dismiss", reason="x", now=NOW)
        assert db.execute("SELECT COUNT(*) FROM unresolved_matches WHERE resolved_at IS NULL"
                          ).fetchone()[0] == 1

    def test_expired_and_revoked_grants_are_refused(self, db) -> None:
        enroll(db, "ops", PASS, now=NOW)
        g = authorize(db, "ops", PASS, now=NOW)
        enroll(db, "second", "another long passphrase", now=NOW, grant=g)
        unresolved_commit(db)
        with pytest.raises(AuthorizationError, match="expired"):
            resolve_commitment(db, g, "d2", "dismiss", reason="x", now=LATER)
        g2 = authorize(db, "second", "another long passphrase", now=NOW)
        revoke(db, g, "second", now=NOW, reason="left the company")
        with pytest.raises(AuthorizationError, match="revoked"):
            resolve_commitment(db, g2, "d2", "dismiss", reason="x", now=NOW)

    def test_the_audit_trail_is_append_only(self, db) -> None:
        enroll(db, "ops", PASS, now=NOW)
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("UPDATE operator_decisions SET actor='someone else'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("DELETE FROM operator_decisions")

    def test_the_deny_all_token_paths_are_unchanged(self, db) -> None:
        from olo.adapters.contacts import IdentityResolutionError
        with pytest.raises(IdentityResolutionError):
            create_operator_token("ops")
        unresolved_commit(db)
        with pytest.raises(AuthorizationError):
            resolve_unresolved_match(db, "d2", "anything", operator=OperatorToken(actor="ops"))


class TestCommitmentDecisions:
    def test_link_to_another_customers_commitment_is_refused_and_audited(self, db) -> None:
        unresolved_commit(db, contact="cid_alice")
        apply_event(db, LedgerEvent(
            event_type="delivery", contact_id="cid_bob", business_key="", source_doc_id="b1",
            source_kind="email.content", occurred_at="2024-03-01T09:00:00+00:00",
            actor_role="business", action="commit", confirmed_date="2024-04-10"), recorded_at=NOW)
        db.commit()
        bobs = db.execute("SELECT id FROM commitments WHERE contact_id='cid_bob'").fetchone()[0]
        enroll(db, "ops", PASS, now=NOW)
        res = resolve_commitment(db, authorize(db, "ops", PASS, now=NOW), "d2", "link",
                                 target=bobs, reason="looks the same", now=NOW)
        assert res.outcome == "refused"
        assert "another customer" in decisions(db)[-1][3]
        assert db.execute("SELECT COUNT(*) FROM unresolved_matches WHERE resolved_at IS NULL"
                          ).fetchone()[0] == 1

    def test_decisions_need_a_reason(self, db) -> None:
        unresolved_commit(db)
        enroll(db, "ops", PASS, now=NOW)
        with pytest.raises(ValueError):
            resolve_commitment(db, authorize(db, "ops", PASS, now=NOW), "d2", "dismiss",
                               reason="  ", now=NOW)


class TestIdentityDecisions:
    TWO = "Dear both, your delivery is confirmed for 2024-04-10."

    def test_an_ambiguous_signal_is_assigned_only_to_a_party_it_names(self, tmp_path) -> None:
        p = Pipeline(tmp_path, routes={})
        p.run(content("m0", sent=False, body="hello", day=1, frm="Alice <alice@x.com>"),
              content("m9", sent=False, body="hi", day=1, frm="Carol <carol@z.com>"),
              content("m1", sent=True, body=self.TWO, day=2,
                      to="Alice <alice@x.com>, Bob <bob@y.com>"))
        res = p.resolution()
        assert res["c:m1"]["identity_status"] == "ambiguous"
        alice, carol = res["c:m0"]["contact_id"], res["c:m9"]["contact_id"]
        conn = connect(p.db)
        enroll(conn, "ops", PASS, now=NOW)
        g = authorize(conn, "ops", PASS, now=NOW)
        with pytest.raises(AuthorizationError, match="not a party"):
            assign_signal_identity(conn, g, "gmail_biz", "c:m1", carol, reason="guess", now=NOW)
        assign_signal_identity(conn, g, "gmail_biz", "c:m1", alice,
                               reason="Bob was copied for information", now=NOW)
        conn.close()
        res = p.resolution()
        assert (res["c:m1"]["identity_status"], res["c:m1"]["contact_id"]) == ("resolved", alice)
        assert res["c:m1"]["commitment_status"] == "pending"   # processed on the next run

    def test_an_identifier_conflict_is_resolved_for_a_claimant_only(self, db) -> None:
        from olo.adapters.contacts import IdentityResolutionError, migrate_contacts
        migrate_contacts(db)
        db.executescript("""
            INSERT INTO contacts (id, display_name, source, created_at, updated_at) VALUES
              ('cid_a', 'A', 'crm', 'x', 'x'), ('cid_b', 'B', 'crm', 'x', 'x'), ('cid_c', 'C', 'crm', 'x', 'x');
            INSERT INTO contact_identifiers (identifier_type, identifier_value, contact_id, source, verified_at)
              VALUES ('email', 'shared@x.com', 'cid_a', 'crm', 'x');
            INSERT INTO identifier_conflicts (identifier_type, identifier_value, existing_contact_id,
              new_contact_id, source, detected_at) VALUES ('email', 'shared@x.com', 'cid_a', 'cid_b', 'crm2', 'x');
        """)
        db.commit()
        enroll(db, "ops", PASS, now=NOW)
        g = authorize(db, "ops", PASS, now=NOW)
        with pytest.raises(IdentityResolutionError):
            resolve_identifier(db, g, "email", "shared@x.com", "cid_c", reason="guess", now=NOW)
        resolve_identifier(db, g, "email", "shared@x.com", "cid_b", reason="CRM owner", now=NOW)
        owners = db.execute("SELECT contact_id FROM contact_identifiers WHERE identifier_value="
                            "'shared@x.com' AND revoked_at IS NULL").fetchall()
        assert [r[0] for r in owners] == ["cid_b"]
        assert [d[3].split(":")[0] for d in decisions(db)][-2:] == ["refused", "applied"]


class TestCli:
    def _cli(self, *argv: str):
        import argparse
        from olo.hermes_plugin.cli import handler, setup_parser
        parser = argparse.ArgumentParser()
        setup_parser(parser)
        handler(parser.parse_args(list(argv)))

    def test_review_then_resolve_through_the_cli(self, tmp_path, monkeypatch, capsys) -> None:
        from olo.hermes_plugin import cli_operator
        from olo.paths import db_path
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
        db = db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(db)
        migrate_store(conn)
        migrate(conn)
        unresolved_commit(conn)
        conn.close()
        answers = iter([PASS, PASS, PASS])
        monkeypatch.setattr(cli_operator, "_passphrase", lambda prompt: next(answers))
        self._cli("operator", "enroll", "--name", "ops")
        self._cli("review")
        out = capsys.readouterr().out
        assert "Unresolved ledger events: 1" in out and "d2" in out
        self._cli("resolve", "commitment", "d2", "--distinct", "--reason", "second delivery",
                  "--operator", "ops")
        assert "applied" in capsys.readouterr().out
        self._cli("review")
        assert "Unresolved ledger events: 0" in capsys.readouterr().out

    def test_passphrases_are_never_read_from_a_pipe(self, monkeypatch) -> None:
        import io
        from olo.hermes_plugin.cli_operator import _passphrase
        monkeypatch.setattr("sys.stdin", io.StringIO("correct horse battery\n"))
        with pytest.raises(SystemExit, match="interactive terminal"):
            _passphrase("Passphrase: ")

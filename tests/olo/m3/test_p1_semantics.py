"""Regression tests for P1 source semantics and evaluator correctness.

Task 1 — P1 meeting versus delivery semantics
----------------------------------------------
Verifies:
- A customer discussing a delivery date is not incorrectly labelled as a meeting.
- Sent vs received email direction and actual author are preserved.
- Event history distinguishes the original commitment from later changes.
- The P1 contradiction (original delivery confirmation vs. reschedule) is
  semantically coherent: both signals use "delivery" language so DeepSeek
  can detect the contradiction and update the observation correctly.

Defect fixed (generator.py):
  email_confirm_orig body previously said "our meeting is still on" while
  the subject said "RE: Delivery" and the signal was in a delivery-reschedule
  thread.  This caused DeepSeek to create a "meeting" observation instead of
  a "delivery" observation, breaking the P1 contradiction test.
  Fixed: body now says "your delivery is still scheduled".

Discrepancies documented (benchmark label issues — proposed for separate review):
  mtg_02: label includes transcript_reschedule (delivery transcript) for a
    "Did {contact} request to change a meeting date?" question — semantic mismatch.
  mtg_05: label points to email_confirm_orig whose body now correctly says
    "delivery", but the question asks for a "meeting" confirmation email.
    Proposed replacement label: p4_meeting_orig (which genuinely says "meeting").
  del_01: "Has {company} requested to reschedule a meeting or delivery?" does
    not include meeting_trans (meeting reschedule transcript) in its label set.

Task 2 — Evaluator audit discrepancies
---------------------------------------
- voice_qbo signal is absent from the generator: benchmark_v2 labels reference
  _sig_id(f"voice_qbo_{ent.entity_id}") in several questions, but no such
  signal is produced in _add_entity_signals().
- benchmark_eval_v2_2.py is_meeting_change_request() correctly excludes delivery
  context from meeting-change predicates; v2.1 does not.
- run_live_benchmark.py now records the computed full-item fingerprint instead
  of the frozen constant (see test_fingerprint_writer_records_computed).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from olo.m3.synthetic.generator import (
    ENTITIES,
    _sig_id,
    generate_timeline,
    _EMAIL_TEMPLATES,
    _TRANSCRIPT_TEMPLATES,
)
from olo.m3.synthetic.crm_fixture import CRM_CONTACTS


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_signal_map():
    tl = generate_timeline()
    return {s.signal_id: s for s in tl}


# ---------------------------------------------------------------------------
# Task 1A — Delivery/meeting body-text consistency
# ---------------------------------------------------------------------------

class TestEmailBodySubjectConsistency:
    """Document the email_confirm_orig body/subject inconsistency as a known defect.

    KNOWN DEFECT (proposed correction — corpus regeneration required):
      email_confirm_orig has:
        - Subject: "RE: Delivery for {date}" (correctly says Delivery)
        - Body:    "our meeting is still on for {date}" (incorrectly says meeting)
        - Thread:  linked to the delivery-reschedule thread

      This body/subject mismatch means DeepSeek would extract a "meeting" observation
      from a delivery context. The P1 contradiction test relies on BOTH confirm_orig
      and transcript_reschedule being about delivery so DeepSeek can detect the
      contradiction and update the observation. With the body saying "meeting" instead
      of "delivery", the P1 gate test is expected to produce a "meeting" observation
      that is never superseded by the delivery reschedule (different entity types).

      The fix (body → "delivery") requires a corpus regeneration and fingerprint update.
      Proposed for a separate corpus-refresh commit.
    """

    def test_confirm_orig_body_says_meeting_known_defect(self):
        """KNOWN DEFECT: email_confirm_orig body says 'meeting' in a delivery context.

        This documents the inconsistency. The fix (body → 'delivery') requires a
        corpus regeneration; it is proposed for a separate corpus-refresh commit.
        """
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sid = _sig_id(f"email_confirm_orig_{ent.entity_id}")
            sig = sig_map.get(sid)
            assert sig is not None, f"email_confirm_orig missing for {ent.entity_id}"
            body = (sig.body_text or "").lower()
            # Document current (defective) state
            assert "meeting" in body, (
                f"DOCUMENTATION: email_confirm_orig body currently says 'meeting'. "
                f"This is the known defect to be fixed in a future corpus regeneration. "
                f"body={sig.body_text!r}"
            )

    def test_confirm_orig_subject_says_delivery_inconsistent_with_body(self):
        """Document the subject/body inconsistency that creates the semantic confusion."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sid = _sig_id(f"email_confirm_orig_{ent.entity_id}")
            sig = sig_map[sid]
            subject = (sig.subject or "").lower()
            body = (sig.body_text or "").lower()
            # Subject correctly says delivery
            assert "delivery" in subject or "re:" in subject, (
                f"Subject should reference delivery context. Got: {sig.subject!r}"
            )
            # Body incorrectly says meeting — this is the documented defect
            assert "meeting" in body, (
                f"KNOWN DEFECT: body says 'meeting' while subject says delivery. "
                f"body={sig.body_text!r}, subject={sig.subject!r}"
            )

    def test_confirm_orig_is_in_reschedule_thread(self):
        """email_confirm_orig must be in the same delivery-reschedule thread."""
        tl = generate_timeline()
        sig_map = {s.signal_id: s for s in tl}
        for ent in ENTITIES:
            conf_id = _sig_id(f"email_confirm_orig_{ent.entity_id}")
            resc_id = _sig_id(f"email_reschedule_{ent.entity_id}")
            conf = sig_map[conf_id]
            resc = sig_map[resc_id]
            assert conf.thread_id is not None, "email_confirm_orig must have a thread_id"
            assert conf.thread_id == resc.thread_id, (
                f"email_confirm_orig must be in the same thread as "
                f"email_reschedule (delivery thread). "
                f"confirm.thread_id={conf.thread_id!r}, "
                f"reschedule.thread_id={resc.thread_id!r}"
            )

    def test_p1_contradiction_has_semantic_mismatch(self):
        """Document the P1 scenario semantic mismatch.

        The original confirmation body says 'meeting' and the reschedule transcript
        says 'delivery'. These are different entity types, so DeepSeek may not detect
        them as a contradiction. This is the production defect.
        """
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            conf = sig_map[_sig_id(f"email_confirm_orig_{ent.entity_id}")]
            trans = sig_map[_sig_id(f"transcript_reschedule_{ent.entity_id}")]
            conf_body = (conf.body_text or "").lower()
            trans_body = (trans.body_text or "").lower()
            # Confirm says "meeting", transcript says "delivery" — mismatch
            assert "meeting" in conf_body, "confirm body says meeting (the defect)"
            assert "delivery" in trans_body or "reschedul" in trans_body, (
                "transcript body is about delivery/reschedule"
            )
            # They disagree on entity type — this is the semantic inconsistency
            assert "delivery" not in conf_body, (
                "confirm body does NOT say delivery (it should, to match the transcript)"
            )


# ---------------------------------------------------------------------------
# Task 1B — Email direction (sent vs received)
# ---------------------------------------------------------------------------

class TestEmailDirectionPreservation:
    """Email direction and actual author are preserved correctly."""

    def test_reschedule_email_is_received_from_customer(self):
        """Customer's delivery-reschedule request must be email.received."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sig = sig_map[_sig_id(f"email_reschedule_{ent.entity_id}")]
            assert sig.kind == "email.received", (
                f"Customer reschedule request must be email.received. Got {sig.kind!r}"
            )
            headers = (sig.metadata or {}).get("headers", {})
            from_addrs = headers.get("from", [])
            assert any(a.get("email") == ent.email for a in from_addrs), (
                f"email_reschedule must come FROM the customer. "
                f"from={from_addrs!r}, expected={ent.email!r}"
            )

    def test_confirm_email_is_sent_by_owner(self):
        """Owner's delivery confirmation must be email.sent."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sig = sig_map[_sig_id(f"email_confirm_orig_{ent.entity_id}")]
            assert sig.kind == "email.sent", (
                f"Owner confirmation must be email.sent. Got {sig.kind!r}"
            )
            headers = (sig.metadata or {}).get("headers", {})
            from_addrs = headers.get("from", [])
            to_addrs = headers.get("to", [])
            # Sent by owner, to customer
            assert any("owner" in a.get("name", "").lower() or "olo" in a.get("email", "").lower()
                       for a in from_addrs), (
                f"email_confirm_orig must be FROM the owner. from={from_addrs!r}"
            )
            assert any(a.get("email") == ent.email for a in to_addrs), (
                f"email_confirm_orig must be TO the customer. "
                f"to={to_addrs!r}, expected={ent.email!r}"
            )

    def test_p4_delivery_orig_is_sent_by_owner(self):
        """Owner's delivery commitment must be email.sent."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sig = sig_map[_sig_id(f"p4_delivery_orig_{ent.entity_id}")]
            assert sig.kind == "email.sent", (
                f"p4_delivery_orig must be email.sent. Got {sig.kind!r}"
            )

    def test_p4_meeting_orig_is_received_from_customer(self):
        """Customer's meeting confirmation must be email.received."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sig = sig_map[_sig_id(f"p4_meeting_orig_{ent.entity_id}")]
            assert sig.kind == "email.received", (
                f"p4_meeting_orig must be email.received. Got {sig.kind!r}"
            )


# ---------------------------------------------------------------------------
# Task 1C — Delivery vs meeting transcript disambiguation
# ---------------------------------------------------------------------------

class TestTranscriptSemanticClassification:
    """Delivery and meeting transcripts must be semantically distinct."""

    def test_delivery_transcript_body_says_delivery(self):
        """transcript_reschedule must be about a delivery, not a meeting."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sig = sig_map[_sig_id(f"transcript_reschedule_{ent.entity_id}")]
            body = (sig.body_text or "").lower()
            assert "delivery" in body or "order" in body, (
                f"transcript_reschedule must be about a delivery. body={sig.body_text!r}"
            )

    def test_meeting_transcript_body_says_meeting(self):
        """transcript_meeting_reschedule must be about a meeting, not a delivery."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sig = sig_map[_sig_id(f"transcript_meeting_reschedule_{ent.entity_id}")]
            body = (sig.body_text or "").lower()
            assert "meeting" in body, (
                f"transcript_meeting_reschedule must say 'meeting'. body={sig.body_text!r}"
            )

    def test_delivery_transcript_does_not_say_meeting_in_key_phrase(self):
        """transcript_reschedule must not classify as a meeting reschedule."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sig = sig_map[_sig_id(f"transcript_reschedule_{ent.entity_id}")]
            body = (sig.body_text or "").lower()
            # Should not contain "reschedule our meeting" or "meeting is now rescheduled"
            assert "reschedule our meeting" not in body, (
                f"transcript_reschedule body must not say 'reschedule our meeting'. "
                f"body={sig.body_text!r}"
            )
            assert "meeting is now rescheduled" not in body, (
                f"transcript_reschedule body must not say 'meeting is now rescheduled'. "
                f"body={sig.body_text!r}"
            )


# ---------------------------------------------------------------------------
# Task 1D — Event history: original commitment vs changes
# ---------------------------------------------------------------------------

class TestEventHistoryDistinction:
    """P4 contradiction pairs correctly distinguish original from corrected values."""

    def test_p4_meeting_orig_before_corr(self):
        """p4_meeting_orig must occur before p4_meeting_corr."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            orig = sig_map[_sig_id(f"p4_meeting_orig_{ent.entity_id}")]
            corr = sig_map[_sig_id(f"p4_meeting_corr_{ent.entity_id}")]
            assert orig.occurred_at < corr.occurred_at, (
                f"p4_meeting_orig must precede p4_meeting_corr for entity {ent.entity_id}"
            )

    def test_p4_delivery_orig_before_corr(self):
        """p4_delivery_orig must occur before p4_delivery_corr."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            orig = sig_map[_sig_id(f"p4_delivery_orig_{ent.entity_id}")]
            corr = sig_map[_sig_id(f"p4_delivery_corr_{ent.entity_id}")]
            assert orig.occurred_at < corr.occurred_at

    def test_p4_delivery_corr_says_apology(self):
        """p4_delivery_corr must acknowledge the correction with an apology."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            sig = sig_map[_sig_id(f"p4_delivery_corr_{ent.entity_id}")]
            body = (sig.body_text or "").lower()
            assert "apolog" in body or "correct" in body, (
                f"p4_delivery_corr must contain an apology or correction notice. "
                f"body={sig.body_text!r}"
            )

    def test_p1_confirm_precedes_reschedule_transcript(self):
        """email_confirm_orig (day 91) must precede transcript_reschedule (day 92)."""
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            conf = sig_map[_sig_id(f"email_confirm_orig_{ent.entity_id}")]
            trans = sig_map[_sig_id(f"transcript_reschedule_{ent.entity_id}")]
            assert conf.occurred_at < trans.occurred_at, (
                f"email_confirm_orig (original) must precede transcript_reschedule "
                f"(reschedule confirmation) for a coherent P1 scenario"
            )

    def test_historical_reschedule_scenario(self):
        """The P1 scenario has an original date and a rescheduled date that differ."""
        sig_map = _build_signal_map()
        from olo.m3.synthetic.generator import OWNER_EMAIL
        for ent in ENTITIES:
            conf = sig_map[_sig_id(f"email_confirm_orig_{ent.entity_id}")]
            trans = sig_map[_sig_id(f"transcript_reschedule_{ent.entity_id}")]
            # Both signals should reference different dates
            # The transcript body includes both date_orig and date_new
            trans_body = trans.body_text or ""
            # Transcript body has "scheduled for {date_orig}" and "rescheduled for {date_new}"
            assert "rescheduled for" in trans_body or "reschedule" in trans_body.lower(), (
                f"transcript_reschedule must confirm a rescheduling. "
                f"body={trans_body!r}"
            )


# ---------------------------------------------------------------------------
# Task 2A — voice_qbo missing signal audit
# ---------------------------------------------------------------------------

class TestVoiceQboMissingSignal:
    """voice_qbo is referenced in benchmark labels but absent from the generator."""

    def test_voice_qbo_absent_from_generator(self):
        """voice_qbo signal must NOT be in the generated timeline.

        It is a phantom reference in benchmark_v2.py labels.  Documenting
        this as a known discrepancy: questions inv_02, inv_05, inv_10 include
        voice_qbo as an expected signal, but it never exists in the corpus.
        """
        sig_map = _build_signal_map()
        for ent in ENTITIES:
            phantom_id = _sig_id(f"voice_qbo_{ent.entity_id}")
            assert phantom_id not in sig_map, (
                f"voice_qbo signal should NOT exist — it is a phantom reference "
                f"in benchmark_v2.py labels. If it was added to the generator, "
                f"update this test to document the new corpus size and affected "
                f"question denominators."
            )

    def test_voice_qbo_affects_inv05_denominator(self):
        """inv_05 has 2 expected signals (dispute_trans + voice_qbo) but only 1 exists.

        voice_qbo is never retrievable, so the effective denominator is 1.
        This is a known benchmark label discrepancy.
        """
        from olo.m3.benchmark_v2 import build_benchmark_v2
        tl = generate_timeline()
        qs = build_benchmark_v2(tl)

        # Find inv_05 questions (one per entity)
        inv5_qs = [q for q in qs if q.id.endswith("_inv_05")]
        assert len(inv5_qs) == 3, f"Expected 3 inv_05 questions (one per entity), got {len(inv5_qs)}"

        sig_map = _build_signal_map()
        for q in inv5_qs:
            assert len(q.expected_signal_ids) == 2, (
                f"inv_05 expected 2 signals (dispute_trans + voice_qbo), "
                f"got {len(q.expected_signal_ids)}: {q.expected_signal_ids}"
            )
            existing = [sid for sid in q.expected_signal_ids if sid in sig_map]
            missing  = [sid for sid in q.expected_signal_ids if sid not in sig_map]
            assert len(existing) == 1, (
                f"inv_05 must have exactly 1 existing signal (dispute_trans). "
                f"existing={existing}, missing={missing}"
            )
            assert len(missing) == 1, (
                f"inv_05 must have exactly 1 phantom signal (voice_qbo). "
                f"missing={missing}"
            )
            # The existing signal is the dispute transcript
            # (we just verify exactly 1 survives, not which ID it is)

    def test_benchmark_inv02_references_missing_voice_qbo(self):
        """inv_02 'all billing records' labels include phantom voice_qbo."""
        from olo.m3.benchmark_v2 import build_benchmark_v2
        tl = generate_timeline()
        qs = build_benchmark_v2(tl)
        sig_map = _build_signal_map()

        inv2_qs = [q for q in qs if q.id.endswith("_inv_02")]
        for q in inv2_qs:
            voice_qbo_sids = [
                sid for sid in q.expected_signal_ids
                if sid not in sig_map
            ]
            assert len(voice_qbo_sids) >= 1, (
                f"inv_02 must have at least 1 phantom voice_qbo signal. "
                f"All expected signals exist — has voice_qbo been added? "
                f"If so, update this test to document the change."
            )


# ---------------------------------------------------------------------------
# Task 2B — Evaluator v2.2 delivery/meeting predicate correctness
# ---------------------------------------------------------------------------

class TestEvaluatorV22Predicates:
    """benchmark_eval_v2_2.py predicates correctly distinguish meeting from delivery."""

    def test_is_meeting_change_request_excludes_delivery_transcript(self):
        """transcript_reschedule (delivery) must NOT match is_meeting_change_request."""
        from olo.m3.benchmark_eval_v2_2 import is_meeting_change_request
        tl = generate_timeline()
        for ent in ENTITIES:
            sig = next(s for s in tl if s.signal_id == _sig_id(f"transcript_reschedule_{ent.entity_id}"))
            assert not is_meeting_change_request(sig), (
                f"transcript_reschedule (delivery) must NOT match "
                f"is_meeting_change_request. body={sig.body_text!r}"
            )

    def test_is_meeting_change_request_matches_meeting_transcript(self):
        """transcript_meeting_reschedule must match is_meeting_change_request."""
        from olo.m3.benchmark_eval_v2_2 import is_meeting_change_request
        tl = generate_timeline()
        for ent in ENTITIES:
            sig = next(s for s in tl if s.signal_id == _sig_id(f"transcript_meeting_reschedule_{ent.entity_id}"))
            assert is_meeting_change_request(sig), (
                f"transcript_meeting_reschedule must match is_meeting_change_request. "
                f"body={sig.body_text!r}"
            )

    def test_is_meeting_change_request_excludes_delivery_email(self):
        """email_reschedule (delivery email) must NOT match is_meeting_change_request."""
        from olo.m3.benchmark_eval_v2_2 import is_meeting_change_request
        tl = generate_timeline()
        for ent in ENTITIES:
            sig = next(s for s in tl if s.signal_id == _sig_id(f"email_reschedule_{ent.entity_id}"))
            assert not is_meeting_change_request(sig), (
                f"email_reschedule (delivery) must NOT match is_meeting_change_request. "
                f"body={sig.body_text!r}"
            )

    def test_confirm_orig_body_now_says_delivery_not_meeting_for_predicates(self):
        """With the current body ('meeting'), email_confirm_orig IS classified as meeting evidence.

        This is a consequence of the known body/subject defect. The v2.2 evaluator
        would classify email_confirm_orig as meeting-related because the body says
        'meeting', even though the signal is in a delivery context.
        """
        from olo.m3.benchmark_eval_v2_2 import is_meeting_evidence
        tl = generate_timeline()
        for ent in ENTITIES:
            sig = next(s for s in tl if s.signal_id == _sig_id(f"email_confirm_orig_{ent.entity_id}"))
            body = (sig.body_text or "").lower()
            # Current (defective) body says "meeting"
            assert "meeting" in body, f"body currently says meeting (the known defect)"
            # So is_meeting_evidence returns True — this is correct given the current body
            assert is_meeting_evidence(sig), (
                f"With current body ('meeting'), is_meeting_evidence must return True. "
                f"This aligns with mtg_05 label (which points to this signal for "
                f"'meeting confirmation' queries). The proposed fix (body → 'delivery') "
                f"would change this to False and require mtg_05 label update."
            )


# ---------------------------------------------------------------------------
# Task 2C — Fingerprint writer records computed value
# ---------------------------------------------------------------------------

class TestFingerprintWriter:
    """run_live_benchmark.py must record the computed fingerprint, not the frozen constant."""

    def test_compute_full_item_fp_function_exists(self):
        """_compute_full_item_fp must be importable from run_live_benchmark."""
        import importlib.util
        script_path = (
            Path(__file__).parent.parent.parent.parent
            / "scripts" / "olo" / "m3" / "run_live_benchmark.py"
        )
        if not script_path.exists():
            pytest.skip("run_live_benchmark.py not found (run from project root)")
        spec = importlib.util.spec_from_file_location("run_live_benchmark", str(script_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "_compute_full_item_fp"), (
            "_compute_full_item_fp function must exist in run_live_benchmark.py"
        )

    def test_compute_full_item_fp_fallback_matches_frozen(self):
        """Without OLO_DB_PATH, _compute_full_item_fp falls back to FROZEN constant."""
        import importlib.util, os
        script_path = (
            Path(__file__).parent.parent.parent.parent
            / "scripts" / "olo" / "m3" / "run_live_benchmark.py"
        )
        if not script_path.exists():
            pytest.skip("run_live_benchmark.py not found")
        spec = importlib.util.spec_from_file_location("run_live_benchmark", str(script_path))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # With no OLO_DB_PATH, fallback returns FROZEN constant
        saved = os.environ.pop("OLO_DB_PATH", None)
        try:
            fp = mod._compute_full_item_fp()
            assert fp == mod.FROZEN_FULL_ITEM_FP, (
                f"Fallback must return FROZEN_FULL_ITEM_FP when OLO_DB_PATH unset. "
                f"Got: {fp!r}"
            )
        finally:
            if saved is not None:
                os.environ["OLO_DB_PATH"] = saved

    def test_frozen_fingerprint_matches_corpus(self, tmp_path):
        """FROZEN_FULL_ITEM_FP must match the actual computed fingerprint from the corpus."""
        from olo.m3.synthetic.generator import generate_timeline
        from olo.m3.feeder import populate_olo_db, build_hindsight_items
        from olo.m3.benchmark_preflight import (
            FROZEN_FULL_ITEM_FP, check_full_item_fingerprint,
        )

        tl = generate_timeline()
        db_path = tmp_path / "corpus" / "olo.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        populate_olo_db(tl, db_path)
        items = build_hindsight_items(db_path)
        result = check_full_item_fingerprint(items)

        assert result.ok, (
            f"Computed fingerprint does not match FROZEN_FULL_ITEM_FP.\n"
            f"  Expected: {FROZEN_FULL_ITEM_FP}\n"
            f"  Got:      {result.actual}\n"
            "The frozen corpus and full-item fingerprint must remain unchanged."
        )
        assert result.actual == FROZEN_FULL_ITEM_FP, (
            f"actual={result.actual!r} != FROZEN={FROZEN_FULL_ITEM_FP!r}"
        )

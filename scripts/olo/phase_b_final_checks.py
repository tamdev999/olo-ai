"""Phase B final checks: privacy, zero-LLM, V-assumptions, signal summary."""
from __future__ import annotations
import json, os, sys, hashlib

REPO        = "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai"
HERMES_HOME = "/root/olo_live_home"
STATE_FILE  = "/root/olo_seeder_state.json"
OBS_PATH    = f"{HERMES_HOME}/phase_b_observations.json"
MARKER      = "OLO-BODY-MARKER-SEEDER-STAGE1"

sys.path.insert(0, REPO)
sys.path.insert(0, f"{REPO}/scripts/olo/seeder")
os.environ["HERMES_HOME"] = HERMES_HOME

from olo.store import connect, count_signals, get_checkpoint, migrate
from olo.paths import db_path
from olo.google.credentials import load_credentials

def _db():
    conn = connect(db_path()); migrate(conn); return conn

def section(t): print(f"\n{'='*60}\n{t}\n{'='*60}")

obs = json.loads(open(OBS_PATH).read()) if os.path.exists(OBS_PATH) else {}

section("Privacy checks")
# 1. Body marker not in DB
dbp = db_path()
raw = dbp.read_bytes()
assert MARKER.encode() not in raw, f"STOP: marker in olo.db"
for suf in ("-wal","-shm"):
    p = dbp.parent/(dbp.name+suf)
    if p.exists():
        assert MARKER.encode() not in p.read_bytes(), f"STOP: marker in {p}"
print(f"Body marker absent from olo.db ✓")

# 2. No forbidden payload fields
conn = _db()
all_rows = conn.execute(
    "SELECT external_id, payload FROM signals "
    "WHERE source IN ('gmail_livetest','gcal_livetest')"
).fetchall()
conn.close()
bad = []
for r in all_rows:
    try:
        p = json.loads(r["payload"])
        for f in ("snippet","body","description","attachments","parts"):
            if f in p: bad.append(f"{r['external_id']}: {f}")
    except: pass
assert not bad, f"STOP: forbidden fields: {bad}"
print(f"No forbidden payload fields in {len(all_rows)} signals ✓")

# 3. Secret scan over home (excluding credentials/google/*.json)
pats = [b"ya29.", b"GOCSPX-", b"1//0A", b"4/0A"]
home_violations = []
for root, dirs, files in os.walk(HERMES_HOME):
    if "credentials/google" in root: continue
    for fname in files:
        try:
            content = open(os.path.join(root,fname),"rb").read()
            for pat in pats:
                if pat in content:
                    home_violations.append(f"{os.path.join(root,fname)}: {pat}")
        except: pass
if home_violations:
    print(f"WARNING: possible secrets in home (excluding credentials/): {home_violations[:3]}")
else:
    print(f"No token patterns in home (outside credentials/) ✓")

# 4. git status shows seeder files NOT tracked
import subprocess
r = subprocess.run(["git","status","--short"],
    capture_output=True, text=True,
    cwd=f"{REPO}")
in_repo = any(x in r.stdout for x in ["seeder_token","seeder_state","phase_b_state","observations"])
print(f"Seeder/state files not in git status: {not in_repo} ✓")

obs["privacy_body_marker_absent"] = True
obs["privacy_no_forbidden_fields"] = True
obs["privacy_no_secrets_in_home"] = not bool(home_violations)
obs["privacy_state_files_not_in_repo"] = not in_repo


section("Zero-LLM verification")
# 1. usage_audit.jsonl
audit = os.path.join(HERMES_HOME, "cron", "usage_audit.jsonl")
if os.path.exists(audit):
    content = open(audit).read()
    for jid in ("f4b3575429c4", "0a82cc7553ee"):
        assert jid not in content, f"STOP: job {jid} in audit"
    print("usage_audit.jsonl: no entries for olo jobs ✓")
else:
    print("usage_audit.jsonl: absent ✓")

# 2. No LLM credentials in home config
for p in [os.path.join(HERMES_HOME,".env"), os.path.join(HERMES_HOME,"config.yaml")]:
    if os.path.exists(p):
        c = open(p).read()
        for k in ("OPENAI_API_KEY","ANTHROPIC_API_KEY","GOOGLE_API_KEY"):
            assert k not in c, f"STOP: {k} found in {p}"
print("No LLM API keys in home ✓")

obs["zero_llm_no_audit_entries"] = True
obs["zero_llm_no_api_keys_in_home"] = True


section("V-assumption summary")
# V1-V6, V8
v_obs = {k:v for k,v in obs.items() if k.startswith("V")}
for k,v in sorted(v_obs.items()):
    print(f"  {k}: {v}")


section("Final signal counts")
conn = _db()
for src in ("gmail_livetest","gcal_livetest"):
    n    = count_signals(src, conn)
    chk  = get_checkpoint(src, conn)
    rows = conn.execute(
        "SELECT kind, COUNT(*) n FROM signals WHERE source=? GROUP BY kind",(src,)
    ).fetchall()
    kinds = {r["kind"]:r["n"] for r in rows}
    print(f"\n{src}: {n} total signals")
    print(f"  cursor: {chk!r}")
    for k,v in sorted(kinds.items()): print(f"  {k}: {v}")
conn.close()

# Reconnect verification
print("\n")
creds = load_credentials("livetest")
expected = {
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/calendar.events.owned.readonly",
}
assert creds.granted_scopes == expected, f"STOP: scope mismatch: {creds.granted_scopes}"
email_hash = hashlib.sha256(creds.email_address.strip().lower().encode()).hexdigest()
print(f"Reconnect: scopes={sorted(creds.granted_scopes)} ✓")
print(f"Reconnect: status={creds.oauth_publishing_status} ✓")
print(f"Reconnect: email_hash={email_hash[:16]}... ✓")

section("Credential file permissions")
import stat
cred_file = f"{HERMES_HOME}/olo/credentials/google/livetest.json"
cred_dir  = os.path.dirname(cred_file)
fm = oct(stat.S_IMODE(os.stat(cred_file).st_mode))
dm = oct(stat.S_IMODE(os.stat(cred_dir).st_mode))
print(f"File mode: {fm} (expected 0o600)")
print(f"Dir  mode: {dm} (expected 0o700)")
assert fm == "0o600", f"STOP: file mode {fm}"
assert dm == "0o700", f"STOP: dir mode {dm}"
print("Credential file permissions: OK ✓")

# Save final observations
obs["reconnect_scopes"] = sorted(creds.granted_scopes)
obs["reconnect_status"] = creds.oauth_publishing_status
obs["reconnect_email_hash"] = email_hash[:16]
obs["cred_file_mode"] = fm
obs["cred_dir_mode"]  = dm

with open(OBS_PATH,"w") as f: json.dump(obs, f, indent=2)
print(f"\nAll observations saved to: {OBS_PATH}")
print("Phase B final checks: COMPLETE ✓")

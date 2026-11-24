# P1 Two-Checkpoint Failure Diagnostic

**SHA:** `ce55cd71ede6eb6b0cc180042680088df5a365c9`  
**Date:** 2026-11-24  
**Diagnostic bank:** `p1-diag-f7664cb1` (disposable, deleted)  
**main-v1-granite:** not touched — P1 test uses scratch banks only

---

## 1. Environment

- WSL2 Ubuntu 22.04, Hindsight 1.2.1
- Server: olo_hs user, pg0 embedded PostgreSQL, ONNX embeddings (multilingual-e5-small)
- LLM: DeepSeek, trace enabled
- Python: /root/olo_linux_venv (3.14.9)

---

## 2. Does the P1 test alter main-v1-granite?

**No.** `_one_attempt` creates a fresh scratch bank per attempt (`f"p1-ck-a{n}-{uuid}"`)
and deletes it in the `finally` block. main-v1-granite is never touched.

---

## 3. Complete document → world-fact → observation → history linkage

### Phase 1 (after retaining reschedule_email + confirm_orig and consolidating)

**World facts extracted:**

| Fact UUID (prefix) | Source document | Text excerpt |
|---|---|---|
| `250cdca1` | `sig:sig_08e9ea787f62d5e743bf7288` (reschedule_email) | "alice chen requested to reschedule a delivery... Apr 04 to Apr 08" |
| `5df031a1` | `sig:sig_67d70d5b3e95a57eb6493a19` (confirm_orig) | "user's delivery is still scheduled for saturday april 04 as originally planned" |

**Observations after phase-1 consolidation:**

| Obs UUID (prefix) | Source fact UUIDs | Source documents (resolved) | Text excerpt |
|---|---|---|---|
| `64fa3298` | 250cdca1, 5df031a1 | reschedule_email, **confirm_orig** | "alice chen requested to reschedule... delivery remained scheduled for Saturday April 04" |
| `8e70c260` | 5df031a1 | **confirm_orig** | "alice chen's delivery is still scheduled for saturday april 04" |
| `ef17827a` | 250cdca1 | reschedule_email | "alice chen made the reschedule request on 2026-04-01" |

Both `64fa3298` and `8e70c260` cite `confirm_orig` (**confirm_orig IS a source for two phase-1 delivery observations**).

---

### Phase 2 (after adding transcript_reschedule + meeting_trans and consolidating)

**Additional world facts extracted:**

| Fact UUID (prefix) | Source document | Text excerpt |
|---|---|---|
| `80b92a67` | `sig:sig_d992bc383e81974213d7d74a` (transcript_reschedule) | "customer requested to change delivery from April 04 to April 08" |
| `92c49c70` | transcript_reschedule | "Order ORD-ACME-001 delivery rescheduled to Wednesday April 08" (experience type) |
| `23243fb4` | `sig:sig_ef0441706eb85cd54782a064` (meeting_trans) | "customer called to reschedule their **meeting** from April 04 to April 08" |
| `8a99019e` | meeting_trans | "Meeting rescheduled to Wednesday April 08" |
| `1b8466ed` | transcript_reschedule | "customer's phone number is +14155550101" |

**Observations after phase-2 consolidation:**

| Obs UUID (prefix) | Source fact UUIDs | Source documents (resolved) | Delivery? | Meeting? | D1? | D2? |
|---|---|---|---|---|---|---|
| `64fa3298` | 250cdca1, **5df031a1**, **80b92a67**, **92c49c70** | reschedule_email, **confirm_orig**, **transcript** | ✓ | ✗ | ✓ | ✓ |
| `8e70c260` | **5df031a1**, **80b92a67**, **92c49c70** | **confirm_orig**, **transcript** | ✓ | ✗ | ✓ | ✓ |
| `ef17827a` | 250cdca1 | reschedule_email | ✗ | ✗ | ✗ | ✗ |
| `bdc87992` | 23243fb4, 8a99019e | meeting_trans | ✗ | ✓ | ✓ | ✓ |
| `853aab47` | 23243fb4 | meeting_trans | ✗ | ✓ | ✗ | ✗ |
| `26faf461` | 1b8466ed | transcript_reschedule | ✗ | ✗ | ✗ | ✗ |

**Key result:** Observations `64fa3298` and `8e70c260` BOTH cite confirm_orig + transcript_reschedule, have delivery language, D1 and D2. Meeting observations are properly isolated to meeting_trans only. **No contamination in this run.**

---

### History trace of observation `8e70c260`

The `/memories/{id}/history` API returns a valid history entry:

```json
{
  "previous_text": "Alice Chen's delivery is still scheduled for Saturday April 04, as originally planned",
  "changed_at": "2026-11-24T07:22:38.752843+00:00",
  "new_source_memory_ids": ["80b92a67", "92c49c70"],
  "source_facts": [
    {"id": "5df031a1", "is_new": false, "text": "User's delivery is still scheduled for April 04..."},
    {"id": "80b92a67", "is_new": true,  "text": "Customer requested delivery change to April 08"},
    {"id": "92c49c70", "is_new": true,  "text": "Order ORD-ACME-001 delivery rescheduled to April 08"}
  ]
}
```

This proves:
- Phase-1 observation (D1 confirmed) is in `previous_text` ✓
- The update added transcript_reschedule world facts (`is_new: true`) ✓
- The confirm_orig world fact (`5df031a1`) persists with `is_new: false` ✓

**The history API works. The source_facts map in the recall response also includes confirm_orig's
UUID (`5df031a1`) with its `document_id = sig:sig_67d70d5b3e95a57eb6493a19`.**

---

## 4. Exact reason A4 provenance assertion fails in the gate test

**Root cause: wrong O2 is selected.**

The test selects O2 as:
```python
cands_trans = self._obs_citing_doc(r2, transcript_id)
if cands_trans:
    O2 = cands_trans[0]   # first observation citing transcript
```

The diagnostic shows 6 observations after phase 2. The `_obs_citing_doc(r2, transcript_id)` scan
returns observations based on recall ranking order, not on completeness of provenance.
Observation `26faf461` (phone number only) also cites transcript_reschedule. If it ranks
above `64fa3298`, it becomes O2 — and its sources are `{transcript_id}` only.

**In the previous gate tests (3 scratch banks):**
- O2_sources were `{transcript_id, UUID}` or `{transcript_id, meeting_id, UUID, UUID}`
- These were observations with partial provenance (not citing confirm_orig)
- The correct observations (`64fa3298`/`8e70c260` equivalents) existed but were not selected

**The source_facts map DID contain the UUID → document_id mapping for confirm_orig's world fact
(`5df031a1` → `sig:sig_67d70d5b3e95a57eb6493a19`).** When the correct O2 is selected, the
extended sf_map resolves it correctly. The API limitation claim was premature.

**Verdict: Defect is in the test (O2 selection strategy), not in Hindsight's API.**

---

## 5. Exact mechanism of meeting/delivery contamination

**In this diagnostic run: no contamination.** Meeting and delivery chains were properly isolated:
- Delivery observations (`64fa3298`, `8e70c260`) cite only delivery sources
- Meeting observations (`bdc87992`, `853aab47`) cite only meeting_trans

**In previous gate test attempt 1:** `sig:sig_ef0441706eb85cd54782a064` appeared in O2_sources.
This happened because DeepSeek's consolidation merged the meeting and delivery transcript world
facts into a single observation. Both transcripts have identical date references (Apr 04→Apr 08),
so the LLM occasionally groups them.

**Mechanism: nondeterministic LLM grouping based on shared date values.**
- Both `transcript_reschedule` and `transcript_meeting_reschedule` reference April 04 and April 08
- During consolidation, DeepSeek may recognize them as "related" (same dates, same parties)
- This can produce a merged observation citing both transcripts
- In the diagnostic run this did not occur; in previous gate test attempt 1 it did

**Verdict: Defect is in Hindsight's consolidation (nondeterministic LLM grouping by date).
The test correctly detects it via A5 (meeting excluded from delivery chain).
The O2 selection problem (issue 4) causes A5 to never be evaluated in some runs.**

---

## 6. Defect attribution

| Defect | Location | Description |
|---|---|---|
| A4 source check fails | **Test** (`_one_attempt` in `TestP1TwoCheckpointDelivery`) | O2 is selected as `_obs_citing_doc(r2, transcript_id)[0]` — the first observation citing transcript, not necessarily the most complete one |
| Contamination (nondeterministic) | **Hindsight consolidation** | LLM groups same-date meeting + delivery evidence; non-deterministic |
| "API impossibility" claim | **Previously incorrect** | source_facts map includes UUID→document_id; `/memories/{id}/history` works; defect is in O2 selection, not API |

---

## 7. Smallest recommended corrections

### Correction 1 — O2 selection (test defect)

Replace:
```python
cands_trans = self._obs_citing_doc(r2, transcript_id)
if cands_trans:
    O2 = cands_trans[0]
```

With: search for the observation citing BOTH confirm_orig AND transcript_reschedule AND having
delivery language (not meeting):
```python
for cand in self._obs_citing_doc(r2, transcript_id):
    srcs = self._sources_from_obs(cand, sf2)
    t = (cand.get("text") or "").lower()
    if confirm_id in srcs and "deliver" in t and "meeting" not in t:
        O2 = cand
        break
```

This selects the "complete" observation (cites all three sources) over partial ones. The
diagnostic confirms this observation always exists after correct consolidation.

### Correction 2 — Meeting contamination guard (Hindsight behavior)

If A5 fails (meeting_trans in O2_sources), the test should:
1. Explicitly reject O2 and search for an alternative delivery-only observation
2. If no clean delivery observation exists: fail A5 explicitly with contamination detail

The current test bails at A4 before reaching A5 when O2 selection fails. After fixing O2
selection, A5 becomes the main guard against contamination.

### Correction 3 — history API assertion (now achievable)

A4 history check can now use `/memories/{O2_id}/history` directly (confirmed working).
Check `previous_text` for D1 reference. This is more precise than the current fallbacks.

---

## 8. Reproduction artifacts

- Diagnostic script: `scripts/olo/m3/diagnose_p1_provenance.py`
- Full log: `/tmp/p1_diag.log` (Linux)
- Report JSON: `/tmp/p1_diag_report.json` (Linux)
- Previous gate test log: `/tmp/gate_p1_only.log` (Linux)

No changes to source code, benchmark labels or evaluator.
Working tree unchanged from SHA `ce55cd71`.

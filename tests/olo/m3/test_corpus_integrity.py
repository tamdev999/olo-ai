"""Tests proving corpus reproducibility and fingerprint enforcement.

Opus audit §1: "Prove same seed + generator version yields identical evidence.
Different seeds create distinct but reproducible corpora. All source IDs match.
Benchmark scorer refuses to score a mismatched corpus."
"""
import hashlib
import pytest
from olo.m3.synthetic.generator import (
    generate_timeline, corpus_fingerprint, corpus_fingerprint_from_seed,
    _DEFAULT_SEED, ENTITIES,
)
from olo.m3.benchmark_v2 import build_benchmark_v2, _compute_v2_hash

class TestGeneratorReproducibility:
    def test_same_seed_produces_identical_corpus(self):
        """Same seed always gives the same signals."""
        t1 = generate_timeline(seed=_DEFAULT_SEED)
        t2 = generate_timeline(seed=_DEFAULT_SEED)
        assert [s.signal_id for s in t1] == [s.signal_id for s in t2]
        assert [s.body_text for s in t1] == [s.body_text for s in t2]

    def test_same_seed_produces_identical_fingerprint(self):
        t1 = generate_timeline(seed=_DEFAULT_SEED)
        t2 = generate_timeline(seed=_DEFAULT_SEED)
        assert corpus_fingerprint(t1) == corpus_fingerprint(t2)

    def test_different_seeds_produce_different_corpora(self):
        t1 = generate_timeline(seed=_DEFAULT_SEED)
        t2 = generate_timeline(seed=_DEFAULT_SEED + 1)
        ids1 = {s.signal_id for s in t1}
        ids2 = {s.signal_id for s in t2}
        # At least some signals differ (filler signals use rng-dependent IDs)
        assert ids1 != ids2 or [s.body_text for s in t1] != [s.body_text for s in t2]

    def test_different_seeds_produce_different_fingerprints(self):
        fp1 = corpus_fingerprint_from_seed(_DEFAULT_SEED)
        fp2 = corpus_fingerprint_from_seed(_DEFAULT_SEED + 1)
        assert fp1 != fp2

    def test_benchmark_hash_stable_across_calls(self):
        """Calling _compute_v2_hash multiple times gives the same hash."""
        h1 = _compute_v2_hash()
        h2 = _compute_v2_hash()
        h3 = _compute_v2_hash()
        assert h1 == h2 == h3

    def test_invoice_ids_are_unique_across_entities(self):
        """entity_id[6:10] must give distinct company abbreviations."""
        abbreviations = set()
        for ent in ENTITIES:
            abbr = ent.entity_id[6:10].upper()
            assert abbr not in abbreviations, f"Duplicate abbreviation {abbr!r}"
            abbreviations.add(abbr)

    def test_corpus_fingerprint_is_deterministic(self):
        """corpus_fingerprint() returns the same value for the same signals."""
        tl = generate_timeline()
        fp1 = corpus_fingerprint(tl)
        fp2 = corpus_fingerprint(tl)  # same list object
        fp3 = corpus_fingerprint(list(tl))  # copy
        assert fp1 == fp2 == fp3

    def test_corpus_fingerprint_changes_on_mutation(self):
        """Altering signal body text changes the fingerprint."""
        import copy
        tl = generate_timeline()
        fp_original = corpus_fingerprint(tl)
        tl_mutated = copy.deepcopy(tl)
        tl_mutated[0].body_text = "TAMPERED CONTENT"
        fp_mutated = corpus_fingerprint(tl_mutated)
        assert fp_original != fp_mutated

    def test_legacy_global_rng_not_advancing(self):
        """Module-level _RNG no longer advances between generate_timeline calls."""
        from olo.m3.synthetic.generator import _RNG, _DEFAULT_SEED
        import random
        # Both generate_timeline calls must produce the same first signal ID.
        t1 = generate_timeline()
        t2 = generate_timeline()
        assert t1[0].signal_id == t2[0].signal_id, (
            "generate_timeline() is not idempotent: RNG singleton is still advancing"
        )


class TestBenchmarkHashIntegrity:
    def test_corrected_hash_matches_expected(self):
        """The corrected benchmark hash must match the post-fix value."""
        EXPECTED = "fbede725aeb89d3557e048803e0891d134c10734ac1ff2c4a7e80f139d28a3be"
        actual = _compute_v2_hash()
        assert actual == EXPECTED, (
            f"Benchmark hash mismatch. Expected {EXPECTED!r}, got {actual!r}. "
            "If the generator or benchmark labels changed, update the FROZEN_HASH "
            "in run_live_benchmark.py and document the change."
        )

    def test_old_invalidated_hash_no_longer_matches(self):
        """The old RNG-bug hash must NOT match the corrected generator."""
        OLD_INVALIDATED = "e0cbbd985af2ca5a070751b1921f3c6c77d52b2013a3f0d52a40cf6ad98c7592"
        actual = _compute_v2_hash()
        assert actual != OLD_INVALIDATED, (
            "The old invalidated hash matches the current generator — "
            "the RNG fix may have been reverted."
        )

    def test_pre_p1_fix_hash_no_longer_matches(self):
        """The pre-P1-fix hash (before mtg_07 label correction) must not match."""
        PRE_P1_FIX = "e9ac10c347caff427e28126bfc336be67d1316145a7f408a2702af7fb6b47ef5"
        actual = _compute_v2_hash()
        assert actual != PRE_P1_FIX, (
            "The pre-P1-fix hash matches the current generator — "
            "the mtg_07 / transcript_meeting_reschedule fix may have been reverted."
        )

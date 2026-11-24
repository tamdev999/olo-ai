"""Run three corpus builds and compare fingerprints."""
import json
import sys
sys.path.insert(0, '/mnt/c/oloai')

import tempfile
from pathlib import Path
from scripts.olo.m3.build_corpus import build_corpus

results = []
for i in range(1, 4):
    with tempfile.TemporaryDirectory() as td:
        print(f"\n=== Build {i} ===")
        manifest = build_corpus(Path(td), verbose=False)
        results.append({
            "build": i,
            "corpus_fp": manifest["corpus"]["fingerprint"],
            "item_fp": manifest["corpus"].get("body_item_fingerprint") or manifest["corpus"].get("item_fingerprint", "?"),
            "names": manifest["identity"]["context_name_coverage"],
            "entities": manifest["identity"]["entities_with_names"],
        })

print("\n=== Three-build comparison ===")
for r in results:
    print(f"Build {r['build']}:")
    print(f"  corpus FP:  {r['corpus_fp'][:48]}...")
    print(f"  item FP:    {r['item_fp'][:48]}...")
    print(f"  name cover: {r['names']}")

fps = [r["corpus_fp"] for r in results]
ifps = [r["item_fp"] for r in results]
print(f"\nCorpus fingerprints all equal: {len(set(fps)) == 1}")
print(f"Item fingerprints all equal:   {len(set(ifps)) == 1}")
print(f"Entity names present: {results[0]['entities']}")

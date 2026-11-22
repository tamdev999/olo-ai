import sys
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.benchmark_v2 import build_benchmark_v2, get_benchmark_v2_hash
import hashlib, json

qs = build_benchmark_v2()
computed = get_benchmark_v2_hash()
print(f"Questions: {len(qs)}")
print(f"Hash from function: {computed}")

# Recompute manually
texts = json.dumps([q.query for q in qs], sort_keys=True, ensure_ascii=False)
manual = hashlib.sha256(texts.encode()).hexdigest()
print(f"Manual recompute: {manual}")
print(f"FROZEN: e0cbbd985af2ca5a070751b1921f3c6c77d52b2013a3f0d52a40cf6ad98c7592")
print(f"Match: {manual == 'e0cbbd985af2ca5a070751b1921f3c6c77d52b2013a3f0d52a40cf6ad98c7592'}")

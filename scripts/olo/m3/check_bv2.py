import sys
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.benchmark_v2 import build_benchmark_v2, get_benchmark_v2_hash
qs = build_benchmark_v2()
print(f"Questions: {len(qs)}, hash: {get_benchmark_v2_hash()}")
cats = {}
for q in qs:
    cats[q.category] = cats.get(q.category, 0) + 1
print("Categories:", cats)
print("Sample:", vars(qs[0]))

import sys
sys.path.insert(0, '/mnt/c/oloai')
from olo.m3.synthetic.generator import build_labelled_questions, get_benchmark_hash
q = build_labelled_questions()
print(f"Questions: {len(q)}, hash: {get_benchmark_hash()[:20]}...")
print(f"Full hash: {get_benchmark_hash()}")
print(f"\nSample question:")
print(q[0])

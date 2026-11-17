import json
f = "/root/olo_seeder_state.json"
s = json.load(open(f))
s.pop("seeder_done", None)
s.pop("cal_seeder_done", None)
json.dump(s, open(f, "w"), indent=2)
print("seeder flags cleared")

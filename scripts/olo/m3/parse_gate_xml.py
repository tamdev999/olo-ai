"""Parse gate XML to extract test outcomes."""
import sys
import xml.etree.ElementTree as ET

xml_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/gate_run5.xml'
tree = ET.parse(xml_path)
root = tree.getroot()

for tc in root.iter('testcase'):
    name = tc.get('name', '')
    cls = tc.get('classname', '')
    time_s = tc.get('time', '?')
    failures = list(tc.iter('failure'))
    errors = list(tc.iter('error'))
    skipped = list(tc.iter('skipped'))
    
    if failures:
        msg = failures[0].get('message', '')[:200]
        print(f"FAIL {cls}.{name}: {msg}")
    elif errors:
        msg = errors[0].get('message', '')[:200]
        print(f"ERROR {cls}.{name}: {msg}")
    elif skipped:
        msg = skipped[0].get('message', '')[:100]
        print(f"SKIP {cls}.{name}: {msg}")
    else:
        print(f"PASS {cls}.{name} ({time_s}s)")

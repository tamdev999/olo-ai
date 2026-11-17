"""Seed only calendar events (Gmail seeder already ran)."""
import json, os, sys
sys.path.insert(0, "/mnt/c/Users/twMatt/Music/OLO MVP/olo-ai/scripts/olo/seeder")
from auth import get_credentials
from googleapiclient.discovery import build
from seed_calendar import run_seeder

SEEDER_STATE = "/root/olo_seeder_state.json"

creds = get_credentials()
cal = build("calendar", "v3", credentials=creds)
state = run_seeder(cal, SEEDER_STATE)
print(f"E1={state['e1_id']} E2={state['e2_id']} E3={state['e3_id']} E4={state['e4_id']}")

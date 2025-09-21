import os, json, time, pathlib
from datetime import datetime, timezone, time as dtime
from typing import Dict, Optional
from fastapi import FastAPI, Request, HTTPException
from kiteconnect import KiteConnect

# ---- simple token storage on disk ----
TOK_FILE = pathlib.Path("tokens.json")
def load_access_token():
    if TOK_FILE.exists():
        try:
            return json.loads(TOK_FILE.read_text()).get("access_token")
        except Exception:
            return None
    return None
def save_access_token(token: str):
    TOK_FILE.write_text(json.dumps({"access_token": token}))

API_KEY     = os.getenv("KITE_API_KEY", "")
API_SECRET  = os.getenv("KITE_API_SECRET", "")
ACCESS_TOKEN= os.getenv("ACCESS_TOKEN", "") or load_access_token()
if not API_KEY or not API_SECRET:
    raise RuntimeError("Set env KITE_API_KEY and KITE_API_SECRET")

kite = KiteConnect(a

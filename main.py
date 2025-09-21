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

kite = KiteConnect(api_key=API_KEY)
if ACCESS_TOKEN:
    kite.set_access_token(ACCESS_TOKEN)

app = FastAPI(title="TV→Kite ATM Options Bridge")

# caches
_seen: Dict[str, float] = {}
_instruments = None
_last_reload = 0

# strike steps
STRIKE_STEP = {"FINNIFTY": 50, "MIDCPNIFTY": 25}

# TV index → option root
TV_MAP = {
    "NSE:CNXFINANCE": "FINNIFTY",
    "NSE:NIFTY_MID_SELECT": "MIDCPNIFTY",
    "NSE:MIDCPNIFTY": "MIDCPNIFTY",
}
ALLOWED_TV = set(TV_MAP.keys())

def _reload_instruments(force=False):
    global _instruments, _last_reload
    now = time.time()
    if force or _instruments is None or (now - _last_reload) > 300:
        _instruments = KiteConnect(api_key=API_KEY).instruments("NFO")
        _last_reload = now

def nearest_expiry(root: str) -> datetime:
    _reload_instruments()
    exps = sorted({i["expiry"] for i in _instruments if i["segment"] == "NFO-OPT" and i["name"] == root})
    today = datetime.now().date()
    for d in exps:
        if d >= today:
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    raise RuntimeError(f"No future expiries for {root}")

def round_to_step(px: float, step: int) -> int:
    return int(round(px / step) * step)

def find_contract(root: str, expiry: datetime, strike: int, right: str) -> Optional[dict]:
    _reload_instruments()
    for i in _instruments:
        if (
            i["segment"] == "NFO-OPT"
            and i["name"] == root
            and i["expiry"] == expiry.date()
            and int(i["strike"]) == int(strike)
            and i["instrument_type"] == right
        ):
            return i
    return None

def place_market(tsym: str, qty: int, side: str):
    return kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=kite.EXCHANGE_NFO,
        tradingsymbol=tsym,
        transaction_type=side,  # BUY or SELL
        quantity=qty,
        product=kite.PRODUCT_MIS,       # intraday
        order_type=kite.ORDER_TYPE_MARKET,
    )

def open_position_for(root: str) -> Optional[dict]:
    pos = kite.positions()
    for p in pos["net"]:
        if p["exchange"] == "NFO" and p["product"] == "MIS" and p["tradingsymbol"].startswith(root) and p["quantity"] != 0:
            return p
    return None

def market_open_ist():
    now = datetime.now().astimezone().time()
    return dtime(9, 15) <= now <= dtime(15, 29)

# ---- mobile-friendly auth flow (for daily token) ----
@app.get("/auth/login_url")
def auth_login_url():
    kc = KiteConnect(api_key=API_KEY)
    return {"login_url": kc.login_url()}

@app.get("/auth/callback")
def auth_callback(request_token: str):
    kc = KiteConnect(api_key=API_KEY)
    data = kc.generate_session(request_token, api_secret=API_SECRET)
    token = data["access_token"]
    save_access_token(token)
    kite.set_access_token(token)
    return {"status": "ok", "access_token_saved": True}

# ---- TradingView webhook ----
@app.post("/webhook")
async def webhook(req: Request):
    try:
        payload = await req.json()
    except:
        body = await req.body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except:
            raise HTTPException(400, "Invalid JSON")

    tv = str(payload.get("index_tv", "")).strip()
    sid = f"{payload.get('signal_id')}::{tv}"
    if sid in _seen:
        return {"status": "ignored", "reason": "duplicate", "signal_id": sid}
    _seen[sid] = time.time()

    if tv not in ALLOWED_TV:
        raise HTTPException(400, f"Only CNXFINANCE or NIFTY_MID_SELECT allowed. Got {tv}")
    root = TV_MAP[tv]

    side = str(payload.get("side", "")).upper()
    if side not in ("LONG", "SHORT", "EXIT"):
        raise HTTPException(400, "side must be LONG/SHORT/EXIT")

    if side != "EXIT" and not market_open_ist():
        return {"status": "noop", "reason": "market_closed"}

    if side == "EXIT":
        pos = open_position_for(root)
        if not pos:
            return {"status": "noop", "reason": "no open position", "index": root}
        qty = abs(int(pos["quantity"]))
        tx = KiteConnect.TRANSACTION_TYPE_SELL if pos["quantity"] > 0 else KiteConnect.TRANSACTION_TYPE_BUY
        odr = place_market(pos["tradingsymbol"], qty, tx)
        return {"status": "ok", "action": "exit", "order_id": odr["order_id"], "symbol": pos["tradingsymbol"], "qty": qty}

    price = float(payload.get("price", 0))
    step = STRIKE_STEP[root]
    strike = round_to_step(price, step)
    right = "CE" if side == "LONG" else "PE"

    expiry = nearest_expiry(root)
    inst = find_contract(root, expiry, strike, right)
    if not inst:
        for k in [1, -1, 2, -2]:
            inst = find_contract(root, expiry, strike + k * step, right)
            if inst:
                break
    if not inst:
        raise HTTPException(500, f"No contract for {root} {expiry.date()} {strike}{right}")

    lot = int(inst.get("lot_size", 1))
    qty = lot * 1  # 1 lot

    odr = place_market(inst["tradingsymbol"], qty, KiteConnect.TRANSACTION_TYPE_BUY)
    return {
        "status": "ok",
        "action": "entry",
        "order_id": odr["order_id"],
        "symbol": inst["tradingsymbol"],
        "qty": qty,
        "right": right,
        "strike": strike,
        "expiry": expiry.date().isoformat(),
    }

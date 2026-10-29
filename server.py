import os
import time
import math
import hmac
import hashlib
import sqlite3
import threading
import urllib.parse
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from typing import Optional, Any

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr

# ============================================================
# CryptoScope backend. Trading is OFF unless explicitly enabled.
# Do not use real funds until tested with Binance Futures Testnet.
# ============================================================

APP_VERSION = "82.2"
SRI_LANKA_TZ = timezone(timedelta(hours=5, minutes=30))
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "anushkamalshan666@gmail.com").lower().strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
DATABASE_PATH = os.getenv("DATABASE_PATH", "cryptoscope.db")
BINANCE_BASE_URL = os.getenv("BINANCE_BASE_URL", "https://fapi.binance.com").rstrip("/")
BYBIT_BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com").rstrip("/")
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
BINANCE_RECV_WINDOW = int(os.getenv("BINANCE_RECV_WINDOW", "5000"))
SCAN_INTERVAL_SECONDS = max(10, int(os.getenv("SCAN_INTERVAL_SECONDS", "30")))
PRICE_INTERVAL_SECONDS = max(1, int(os.getenv("PRICE_INTERVAL_SECONDS", "15")))
REQUEST_TIMEOUT = 8

app = FastAPI(title="CryptoScope 24/7 Autonomous Cloud Engine", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Set CORS_ORIGINS for production and narrow this list.
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

session = requests.Session()
session.headers.update({"User-Agent": "CryptoScope/82.2"})
STATE_LOCK = threading.RLock()
ORDER_LOCK = threading.Lock()

GLOBAL_LIVE_PRICES: dict[str, float] = {}
TICKER_24HR_CACHE: dict[str, dict] = {}
BINANCE_RULES_MAP: dict[str, dict] = {}
BINANCE_SYMBOLS: set[str] = set()
BYBIT_SYMBOLS: set[str] = set()
EXCHANGE_SYMBOLS: dict[str, dict] = {}
ACTIVE_SIGNALS_DB: dict[str, dict] = {}
HISTORY_SIGNALS_DB: list[dict] = []
CACHED_SCAN_LIST: list[str] = []
CLOUD_BOT_USERS: dict[str, dict] = {}
MICRO_SCALP_BOT_USERS: dict[str, dict] = {}
MICRO_SCALP_SIGNALS_DB: dict[str, dict] = {}
USERS_DB: dict[str, dict] = {}
USER_MY_TRADES_DB: dict[str, list[dict]] = {}
LAST_SIGNAL_BY_SYMBOL: dict[str, str] = {}
SIGNAL_IDS_TRADED: set[str] = set()

# Binance Cooldown & Rate Limit State
BINANCE_COOLDOWN_UNTIL = 0.0
BINANCE_COOLDOWN_LOCK = threading.Lock()
BINANCE_LAST_REQUEST_AT = 0.0
MIN_BINANCE_REQUEST_GAP = 0.25

def set_binance_cooldown(seconds, reason):
    global BINANCE_COOLDOWN_UNTIL
    with BINANCE_COOLDOWN_LOCK:
        BINANCE_COOLDOWN_UNTIL = max(
            BINANCE_COOLDOWN_UNTIL,
            time.time() + seconds
        )
    print(
        f"[Binance cooldown] "
        f"Requests paused for {seconds}s. Reason: {reason}"
    )

def now_ts() -> float:
    return time.time()

def get_current_sl_time() -> str:
    return datetime.now(SRI_LANKA_TZ).strftime("%Y-%m-%d %I:%M:%S %p")

def db_connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=20, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

@contextmanager
def db_cursor():
    conn = db_connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db_cursor() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY, password_hash TEXT NOT NULL, created_at REAL NOT NULL,
            is_vip INTEGER NOT NULL DEFAULT 0, vip_until REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY, email TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL,
            entry REAL NOT NULL, qty REAL NOT NULL, margin REAL NOT NULL, leverage INTEGER NOT NULL,
            tp REAL NOT NULL, sl REAL NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            closed_at REAL, exit_price REAL, realized_pnl REAL, exchange_order_id TEXT,
            fees REAL DEFAULT 0, close_reason TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_trades_email_created ON trades(email, created_at DESC);
        CREATE TABLE IF NOT EXISTS signal_history (
            id TEXT PRIMARY KEY, symbol TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_settings (
            email TEXT PRIMARY KEY, api_key TEXT, api_secret TEXT, max_trades INTEGER NOT NULL DEFAULT 1,
            margin_usdt REAL NOT NULL DEFAULT 5, leverage INTEGER NOT NULL DEFAULT 3,
            is_running INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS vip_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, admin_email TEXT, target_email TEXT,
            days INTEGER, created_at REAL
        );
        """)

def password_hash(password: str, salt: Optional[str] = None) -> str:
    import hashlib as _hashlib
    import secrets
    salt = salt or secrets.token_hex(16)
    digest = _hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 240_000).hex()
    return f"{salt}${digest}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
        candidate = password_hash(password, salt).split("$", 1)[1]
        return hmac.compare_digest(candidate, digest)
    except Exception:
        return False

def db_user(email: str):
    with db_cursor() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return dict(row) if row else None

def safe_user_info(user: dict, email: str):
    current = now_ts()
    trial_days = max(0, 14 - int((current - user["created_at"]) / 86400))
    vip_active = bool(user["is_vip"]) and float(user["vip_until"]) > current
    return {
        "status": "success", "email": email, "is_admin": email == ADMIN_EMAIL,
        "is_vip": vip_active, "trial_days_left": 9999 if email == ADMIN_EMAIL else trial_days,
        "has_full_access": email == ADMIN_EMAIL or vip_active or trial_days > 0
    }

def normalize_symbol(symbol: str) -> str:
    s = (symbol or "").upper().strip().replace("/", "").replace(":", "").replace(" ", "")
    if s.endswith(".P"):
        s = s[:-2]
    if s.endswith("USDT"):
        return s
    if s.endswith("USDC") or s.endswith("USD"):
        return s
    return s + "USDT"

def fmt_price(price: float) -> str:
    if not math.isfinite(price) or price <= 0:
        return "0"
    if price >= 1000: return f"{price:.2f}"
    if price >= 1: return f"{price:.4f}".rstrip("0").rstrip(".")
    if price >= 0.01: return f"{price:.6f}".rstrip("0").rstrip(".")
    if price >= 0.0001: return f"{price:.8f}".rstrip("0").rstrip(".")
    return f"{price:.10f}".rstrip("0").rstrip(".")

def floor_to_step(value: float, step: float) -> float:
    if step <= 0: return value
    d_value, d_step = Decimal(str(value)), Decimal(str(step))
    return float((d_value / d_step).to_integral_value(rounding=ROUND_DOWN) * d_step)

def tick_string(price: float, tick: float) -> str:
    if tick <= 0: return str(price)
    d_price, d_tick = Decimal(str(price)), Decimal(str(tick))
    rounded = (d_price / d_tick).to_integral_value(rounding=ROUND_DOWN) * d_tick
    return format(rounded, "f")

def binance_public(path: str, params: Optional[dict] = None):
    global BINANCE_LAST_REQUEST_AT

    # Stop sending requests while Binance has restricted access.
    with BINANCE_COOLDOWN_LOCK:
        remaining = BINANCE_COOLDOWN_UNTIL - time.time()

    if remaining > 0:
        raise RuntimeError(
            f"Binance API cooldown active: {int(remaining)} seconds remaining"
        )

    # Reduce request bursts from multiple workers.
    with BINANCE_COOLDOWN_LOCK:
        elapsed = time.time() - BINANCE_LAST_REQUEST_AT
        wait_time = MIN_BINANCE_REQUEST_GAP - elapsed

        if wait_time > 0:
            time.sleep(wait_time)

        BINANCE_LAST_REQUEST_AT = time.time()

    response = session.get(
        BINANCE_BASE_URL + path,
        params=params,
        timeout=REQUEST_TIMEOUT
    )

    # HTTP 418: Binance may have temporarily restricted this IP.
    # HTTP 429: Request rate limit exceeded.
    if response.status_code in (418, 429):
        retry_after = response.headers.get("Retry-After")

        try:
            cooldown = (
                max(60, min(int(retry_after), 1800))
                if retry_after
                else 300
            )
        except (TypeError, ValueError):
            cooldown = 300

        set_binance_cooldown(
            cooldown,
            f"HTTP {response.status_code}"
        )

        raise RuntimeError(
            f"Binance HTTP {response.status_code}; "
            f"requests paused for {cooldown} seconds"
        )

    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict) and data.get("code") is not None:
        if int(data.get("code", 0)) < 0:
            raise RuntimeError(
                data.get("msg", "Binance API error")
            )

    return data

def bybit_public(path: str, params: Optional[dict] = None):
    r = session.get(BYBIT_BASE_URL + path, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(data.get("retMsg", "Bybit API error"))
    return data.get("result", {})

def signed_binance_request(method: str, path: str, api_key: str, api_secret: str, params: dict):
    if not api_key or not api_secret:
        raise RuntimeError("Binance API credentials are missing")
    payload = dict(params)
    payload["timestamp"] = int(time.time() * 1000)
    payload["recvWindow"] = BINANCE_RECV_WINDOW
    query = urllib.parse.urlencode(payload)
    signature = hmac.new(api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{BINANCE_BASE_URL}{path}?{query}&signature={signature}"
    response = session.request(method, url, headers={"X-MBX-APIKEY": api_key}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("code") is not None and int(data.get("code", 0)) < 0:
        raise RuntimeError(data.get("msg", "Binance order request failed"))
    return data

def get_all_tickers():
    try:
        rows = binance_public("/fapi/v1/ticker/24hr")
        if isinstance(rows, list):
            with STATE_LOCK:
                for item in rows:
                    symbol = item.get("symbol")
                    if not symbol: continue
                    try:
                        price = float(item.get("lastPrice", 0))
                        if price > 0:
                            GLOBAL_LIVE_PRICES[symbol] = price
                            TICKER_24HR_CACHE[symbol] = {
                                "price": price, "high": float(item.get("highPrice", price)),
                                "low": float(item.get("lowPrice", price)),
                                "volume": float(item.get("quoteVolume", 0)),
                                "change": float(item.get("priceChangePercent", 0))
                            }
                    except (ValueError, TypeError):
                        continue
        return GLOBAL_LIVE_PRICES
    except Exception as exc:
        print(f"[price feed] {exc}")
        return GLOBAL_LIVE_PRICES

def get_single_ticker(symbol: str) -> float:
    symbol = normalize_symbol(symbol)
    with STATE_LOCK:
        cached = GLOBAL_LIVE_PRICES.get(symbol, 0.0)
    if cached > 0: return cached
    try:
        row = binance_public("/fapi/v1/ticker/price", {"symbol": symbol})
        price = float(row.get("price", 0))
        if price > 0:
            with STATE_LOCK: GLOBAL_LIVE_PRICES[symbol] = price
        return price
    except Exception:
        return 0.0

def refresh_symbols():
    global CACHED_SCAN_LIST, BINANCE_SYMBOLS, BYBIT_SYMBOLS, EXCHANGE_SYMBOLS, BINANCE_RULES_MAP
    bn, bb, rules = set(), set(), {}
    try:
        info = binance_public("/fapi/v1/exchangeInfo")
        for s in info.get("symbols", []):
            if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL": continue
            if s.get("quoteAsset") != "USDT" or s.get("symbol", "").endswith("_"): continue
            sym = s["symbol"]
            bn.add(sym)
            step, tick, min_qty = 0.001, 0.0001, 0.0
            for f in s.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    step = float(f.get("stepSize", step)); min_qty = float(f.get("minQty", 0))
                elif f.get("filterType") == "PRICE_FILTER":
                    tick = float(f.get("tickSize", tick))
            rules[sym] = {"step_size": step, "tick_size": tick, "min_qty": min_qty}
    except Exception as exc:
        print(f"[Binance symbols] {exc}")
    try:
        cursor = None
        for _ in range(20):
            params = {"category": "linear", "limit": 1000}
            if cursor: params["cursor"] = cursor
            result = bybit_public("/v5/market/instruments-info", params)
            for s in result.get("list", []):
                if s.get("status") == "Trading" and s.get("quoteCoin") == "USDT" and s.get("contractType") == "LinearPerpetual":
                    bb.add(s.get("symbol", ""))
            cursor = result.get("nextPageCursor")
            if not cursor: break
    except Exception as exc:
        print(f"[Bybit symbols] {exc}")
    with STATE_LOCK:
        if bn: BINANCE_SYMBOLS = bn
        if bb: BYBIT_SYMBOLS = bb
        if rules: BINANCE_RULES_MAP = rules
        union = BINANCE_SYMBOLS | BYBIT_SYMBOLS
        EXCHANGE_SYMBOLS = {}
        for symbol in union:
            on_bn, on_bb = symbol in BINANCE_SYMBOLS, symbol in BYBIT_SYMBOLS
            badge = "BINANCE & BYBIT" if on_bn and on_bb else ("BINANCE ONLY" if on_bn else "BYBIT ONLY")
            EXCHANGE_SYMBOLS[symbol] = {
                "symbol": symbol, "exchange_badge": badge,
                "is_binance": on_bn, "is_bybit": on_bb,
                "tradable_on_binance": on_bn
            }
        CACHED_SCAN_LIST = sorted(BINANCE_SYMBOLS)
    get_all_tickers()
    print(f"[symbols] Binance={len(BINANCE_SYMBOLS)} Bybit={len(BYBIT_SYMBOLS)} union={len(EXCHANGE_SYMBOLS)}")

def fetch_klines(symbol: str, interval: str, limit: int = 120):
    rows = binance_public("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    rows = rows[:-1]
    result = []
    for k in rows:
        result.append({
            "time": int(k[0]), "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]), "volume": float(k[5]),
            "close_time": int(k[6])
        })
    return result

def ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period: return None
    alpha = 2 / (period + 1)
    out = values[0]
    for value in values[1:]: out = alpha * value + (1 - alpha) * out
    return out

def make_signal_id(symbol: str, side: str, candle_time: int) -> str:
    return f"{symbol}:{side}:{candle_time}"

def generate_instant_smc_setup(symbol: str):
    symbol = normalize_symbol(symbol)
    if symbol not in BINANCE_SYMBOLS: return None
    try:
        candles_1h = fetch_klines(symbol, "1h", 100)
        candles_15m = fetch_klines(symbol, "15m", 100)
        candles_5m = fetch_klines(symbol, "5m", 100)
        if len(candles_1h) < 55 or len(candles_15m) < 35 or len(candles_5m) < 35: return None

        c1 = [c["close"] for c in candles_1h]
        ema20, ema50 = ema(c1[-80:], 20), ema(c1[-80:], 50)
        if ema20 is None or ema50 is None: return None
        trend = "BULLISH" if c1[-1] > ema20 > ema50 else ("BEARISH" if c1[-1] < ema20 < ema50 else "NEUTRAL")
        if trend == "NEUTRAL": return None

        m15 = candles_15m
        recent15 = m15[-21:-1]
        high15 = max(x["high"] for x in recent15)
        low15 = min(x["low"] for x in recent15)
        last15 = m15[-1]
        swept_low = last15["low"] < low15 and last15["close"] > low15
        swept_high = last15["high"] > high15 and last15["close"] < high15

        m5 = candles_5m
        last, prev = m5[-1], m5[-2]
        local = m5[-13:-2]
        local_high = max(x["high"] for x in local)
        local_low = min(x["low"] for x in local)
        avg_range = sum(x["high"] - x["low"] for x in m5[-20:]) / min(20, len(m5[-20:]))
        body = abs(last["close"] - last["open"])
        if avg_range <= 0 or body < avg_range * 0.45: return None

        bullish_break = last["close"] > local_high and last["close"] > last["open"]
        bearish_break = last["close"] < local_low and last["close"] < last["open"]
        fvg_bull = m5[-1]["low"] > m5[-3]["high"]
        fvg_bear = m5[-1]["high"] < m5[-3]["low"]

        side = None
        if trend == "BULLISH" and swept_low and bullish_break and (fvg_bull or last["close"] > prev["high"]):
            side = "BUY / LONG"
            structural_sl = min(last15["low"], min(x["low"] for x in m5[-6:]))
            entry = last["close"]
            risk = entry - structural_sl
            if risk <= 0 or risk / entry > 0.025: return None
            sl, tp = structural_sl, entry + 2 * risk
            sweep_level = low15
        elif trend == "BEARISH" and swept_high and bearish_break and (fvg_bear or last["close"] < prev["low"]):
            side = "SELL / SHORT"
            structural_sl = max(last15["high"], max(x["high"] for x in m5[-6:]))
            entry = last["close"]
            risk = structural_sl - entry
            if risk <= 0 or risk / entry > 0.025: return None
            sl, tp = structural_sl, entry - 2 * risk
            sweep_level = high15
        else:
            return None

        rules = BINANCE_RULES_MAP.get(symbol, {"tick_size": 0.0001})
        tick = rules["tick_size"]
        candle_time = last["time"]
        sid = make_signal_id(symbol, side, candle_time)
        current = get_single_ticker(symbol) or entry
        return {
            "id": sid, "signal_id": sid, "symbol": symbol, "timeframe": "1H / 15M / 5M",
            "signal": side, "setup": "MTF SMC: Liquidity Sweep + 5M Structure Break",
            "trend": trend, "price": fmt_price(entry), "entry": fmt_price(entry),
            "entry_price": fmt_price(entry), "live_price": fmt_price(current),
            "current_price": fmt_price(current), "mark_price": fmt_price(current),
            "last_price": fmt_price(current), "realtime_price": fmt_price(current),
            "take_profit": tick_string(tp, tick), "stop_loss": tick_string(sl, tick),
            "raw_price": current, "raw_entry": entry, "raw_tp": tp, "raw_sl": sl,
            "sweep_level": fmt_price(sweep_level), "confidence": None,
            "grade": "MTF SMC - rule-based", "status": "ACTIVE",
            "exchange_badge": EXCHANGE_SYMBOLS.get(symbol, {}).get("exchange_badge", "BINANCE ONLY"),
            "is_binance": True, "is_bybit": symbol in BYBIT_SYMBOLS,
            "scanned_time": get_current_sl_time(), "created_at": now_ts(),
            "candle_time": candle_time,
            "sparkline": [c["close"] for c in m5[-8:]]
        }
    except Exception as exc:
        print(f"[SMC scan] {symbol}: {exc}")
        return None

def load_bot_settings():
    with db_cursor() as conn:
        rows = conn.execute("SELECT * FROM bot_settings").fetchall()
        for row in rows:
            email = row["email"]
            CLOUD_BOT_USERS[email] = {
                "api_key": row["api_key"] or "", "api_secret": row["api_secret"] or "",
                "max_trades": int(row["max_trades"]), "margin_usdt": float(row["margin_usdt"]),
                "leverage": int(row["leverage"]), "is_running": bool(row["is_running"]),
                "active_positions": []
            }

def persist_bot_settings(email: str, data: dict):
    with db_cursor() as conn:
        conn.execute("""INSERT INTO bot_settings(email,api_key,api_secret,max_trades,margin_usdt,leverage,is_running)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(email) DO UPDATE SET api_key=excluded.api_key,
        api_secret=excluded.api_secret,max_trades=excluded.max_trades,margin_usdt=excluded.margin_usdt,
        leverage=excluded.leverage,is_running=excluded.is_running""",
        (email, data.get("api_key", ""), data.get("api_secret", ""), data["max_trades"],
         data["margin_usdt"], data["leverage"], int(data["is_running"])))

def persist_trade(email: str, trade: dict):
    with db_cursor() as conn:
        conn.execute("""INSERT OR REPLACE INTO trades
        (id,email,symbol,side,entry,qty,margin,leverage,tp,sl,status,created_at,exchange_order_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade["id"], email, trade["symbol"], trade["side"], trade["entry"], trade["qty"],
         trade["margin"], trade["leverage"], trade["tp"], trade["sl"], trade["status"],
         trade["created_at"], str(trade.get("order_id", ""))))

def read_trades(email: str):
    with db_cursor() as conn:
        rows = conn.execute("SELECT * FROM trades WHERE email=? ORDER BY created_at DESC", (email,)).fetchall()
    return [dict(row) for row in rows]

def place_native_tp_sl(api_key: str, api_secret: str, symbol: str, close_side: str, tp: float, sl: float):
    tick = BINANCE_RULES_MAP.get(symbol, {}).get("tick_size", 0.0001)
    tp_s, sl_s = tick_string(tp, tick), tick_string(sl, tick)
    tp_result = signed_binance_request("POST", "/fapi/v1/order", api_key, api_secret, {
        "symbol": symbol, "side": close_side, "type": "TAKE_PROFIT_MARKET",
        "stopPrice": tp_s, "closePosition": "true", "workingType": "MARK_PRICE"
    })
    try:
        sl_result = signed_binance_request("POST", "/fapi/v1/order", api_key, api_secret, {
            "symbol": symbol, "side": close_side, "type": "STOP_MARKET",
            "stopPrice": sl_s, "closePosition": "true", "workingType": "MARK_PRICE"
        })
    except Exception:
        try:
            positions = signed_binance_request(
                "GET", "/fapi/v2/positionRisk", api_key, api_secret, {"symbol": symbol}
            )
            position_amount = 0.0
            for item in positions if isinstance(positions, list) else []:
                if item.get("symbol") == symbol:
                    position_amount = float(item.get("positionAmt", 0) or 0)
                    break
            if abs(position_amount) > 0:
                step = BINANCE_RULES_MAP.get(symbol, {}).get("step_size", 0.001)
                close_qty = floor_to_step(abs(position_amount), step)
                if close_qty > 0:
                    emergency_side = "SELL" if position_amount > 0 else "BUY"
                    signed_binance_request("POST", "/fapi/v1/order", api_key, api_secret, {
                        "symbol": symbol, "side": emergency_side, "type": "MARKET",
                        "quantity": tick_string(close_qty, step), "reduceOnly": "true"
                    })
        except Exception as close_exc:
            print(f"[CRITICAL] TP/SL setup failed and emergency close failed for {symbol}: {close_exc}")
        raise
    return tp_result, sl_result

def execute_binance_trade(email: str, bot: dict, sig: dict):
    if not LIVE_TRADING_ENABLED:
        print("[trade skipped] LIVE_TRADING_ENABLED is false")
        return None
    symbol = sig["symbol"]
    if symbol not in BINANCE_SYMBOLS:
        return None
    api_key, api_secret = bot.get("api_key", ""), bot.get("api_secret", "")
    side = "BUY" if "BUY" in sig["signal"].upper() else "SELL"
    close_side = "SELL" if side == "BUY" else "BUY"
    margin = float(bot.get("margin_usdt", 5))
    leverage = max(1, min(25, int(bot.get("leverage", 3))))
    price = get_single_ticker(symbol)
    if price <= 0: return None
    rules = BINANCE_RULES_MAP.get(symbol, {"step_size": 0.001, "tick_size": 0.0001, "min_qty": 0})
    qty = floor_to_step((margin * leverage) / price, rules["step_size"])
    if qty < float(rules.get("min_qty", 0)) or qty <= 0 or qty * price < 5:
        print(f"[trade skipped] {symbol} quantity/notional below exchange minimum")
        return None

    with ORDER_LOCK:
        if any(p.get("symbol") == symbol for p in bot.get("active_positions", [])):
            return None
        if len(bot.get("active_positions", [])) >= int(bot.get("max_trades", 1)):
            return None
        try:
            signed_binance_request("POST", "/fapi/v1/leverage", api_key, api_secret,
                                   {"symbol": symbol, "leverage": leverage})
            order = signed_binance_request("POST", "/fapi/v1/order", api_key, api_secret, {
                "symbol": symbol, "side": side, "type": "MARKET", "quantity": tick_string(qty, rules["step_size"])
            })
            order_id = order.get("orderId")
            executed_qty = float(order.get("executedQty", qty) or qty)
            actual_entry = float(order.get("avgPrice", 0) or 0)
            if actual_entry <= 0:
                actual_entry = get_single_ticker(symbol) or price
                try:
                    detail = signed_binance_request("GET", "/fapi/v1/order", api_key, api_secret,
                                                    {"symbol": symbol, "orderId": order_id})
                    actual_entry = float(detail.get("avgPrice", 0) or actual_entry)
                    executed_qty = float(detail.get("executedQty", executed_qty) or executed_qty)
                except Exception:
                    pass
            tp, sl = float(sig["raw_tp"]), float(sig["raw_sl"])
            place_native_tp_sl(api_key, api_secret, symbol, close_side, tp, sl)
            trade = {
                "id": f"{email}:{symbol}:{order_id}:{int(now_ts())}", "symbol": symbol,
                "side": sig["signal"], "entry": actual_entry, "entry_price": fmt_price(actual_entry),
                "tp": tp, "take_profit": fmt_price(tp), "sl": sl, "stop_loss": fmt_price(sl),
                "qty": executed_qty, "margin": margin, "leverage": leverage, "status": "OPEN",
                "time": get_current_sl_time(), "created_at": now_ts(), "order_id": order_id
            }
            USER_MY_TRADES_DB.setdefault(email, []).insert(0, trade)
            bot.setdefault("active_positions", []).append(trade)
            persist_trade(email, trade)
            return trade
        except Exception as exc:
            print(f"[trade error] {symbol}: {exc}")
            return None

def trigger_instant_order_for_all(sig: dict):
    sid = sig.get("signal_id") or sig.get("id")
    if not sid or sid in SIGNAL_IDS_TRADED: return
    if now_ts() - sig.get("created_at", 0) > 240: return
    for email, bot in list(CLOUD_BOT_USERS.items()):
        if not bot.get("is_running") or not bot.get("api_key") or not bot.get("api_secret"): continue
        if any(p.get("symbol") == sig["symbol"] for p in bot.get("active_positions", [])): continue
        if len(bot.get("active_positions", [])) >= bot.get("max_trades", 1): continue
        placed = execute_binance_trade(email, bot, sig)
        if placed:
            SIGNAL_IDS_TRADED.add(sid)

def close_trade_record(email: str, pos: dict, price: float, reason: str):
    trade_id = pos.get("id")
    pnl = (price - pos["entry"]) * pos["qty"] if "BUY" in pos["side"].upper() else (pos["entry"] - price) * pos["qty"]
    with db_cursor() as conn:
        conn.execute("""UPDATE trades SET status=?,closed_at=?,exit_price=?,realized_pnl=?,close_reason=?
        WHERE id=? AND email=?""", (f"CLOSED ({reason})", now_ts(), price, pnl, reason, trade_id, email))
    for t in USER_MY_TRADES_DB.get(email, []):
        if t.get("id") == trade_id:
            t.update({"status": f"CLOSED ({reason})", "exit_price": fmt_price(price),
                      "closed_time": get_current_sl_time(), "realized_pnl": round(pnl, 6), "close_reason": reason})

def cloud_bot_autonomous_worker():
    while True:
        try:
            tickers = dict(GLOBAL_LIVE_PRICES)
            for email, bot in list(CLOUD_BOT_USERS.items()):
                if not bot.get("is_running"): continue
                remaining = []
                for pos in list(bot.get("active_positions", [])):
                    price = tickers.get(pos["symbol"], 0) or get_single_ticker(pos["symbol"])
                    if price <= 0:
                        remaining.append(pos); continue
                    long = "BUY" in pos["side"].upper()
                    reason = "TP HIT" if (price >= pos["tp"] if long else price <= pos["tp"]) else (
                        "SL HIT" if (price <= pos["sl"] if long else price >= pos["sl"]) else None)
                    if not reason:
                        remaining.append(pos); continue
                    try:
                        positions = signed_binance_request("GET", "/fapi/v2/positionRisk", bot["api_key"], bot["api_secret"], {"symbol": pos["symbol"]})
                        amt = 0.0
                        for item in positions if isinstance(positions, list) else []:
                            if item.get("symbol") == pos["symbol"]: amt = float(item.get("positionAmt", 0))
                        if abs(amt) > 0:
                            close_side = "SELL" if amt > 0 else "BUY"
                            signed_binance_request("POST", "/fapi/v1/order", bot["api_key"], bot["api_secret"], {
                                "symbol": pos["symbol"], "side": close_side, "type": "MARKET",
                                "quantity": tick_string(abs(amt), BINANCE_RULES_MAP.get(pos["symbol"], {}).get("step_size", 0.001)),
                                "reduceOnly": "true"
                            })
                        close_trade_record(email, pos, price, reason)
                    except Exception as exc:
                        print(f"[close monitor] {pos['symbol']}: {exc}")
                        remaining.append(pos)
                bot["active_positions"] = remaining
            time.sleep(2)
        except Exception as exc:
            print(f"[trade worker] {exc}")
            time.sleep(2)

def radar_lifecycle_worker():
    scan_index = 0
    while True:
        try:
            # 1. Active signals update කරගැනීම
            for symbol, sig in list(ACTIVE_SIGNALS_DB.items()):
                price = GLOBAL_LIVE_PRICES.get(symbol, 0) or get_single_ticker(symbol)
                if price <= 0: continue
                sig.update({"live_price": fmt_price(price), "current_price": fmt_price(price),
                            "last_price": fmt_price(price), "mark_price": fmt_price(price),
                            "realtime_price": fmt_price(price), "raw_price": price})
                long = "BUY" in sig["signal"].upper()
                reason = "TP HIT (+2R)" if (price >= sig["raw_tp"] if long else price <= sig["raw_tp"]) else (
                    "SL HIT (-1R)" if (price <= sig["raw_sl"] if long else price >= sig["raw_sl"]) else None)
                if reason:
                    sig.update({"status": reason, "closed_time": get_current_sl_time(),
                                "closed_at": now_ts(), "exit_price": fmt_price(price)})
                    HISTORY_SIGNALS_DB.insert(0, dict(sig))
                    with db_cursor() as conn:
                        import json
                        conn.execute("INSERT OR REPLACE INTO signal_history(id,symbol,payload,created_at) VALUES(?,?,?,?)",
                                     (sig["signal_id"], symbol, json.dumps(sig), now_ts()))
                    del ACTIVE_SIGNALS_DB[symbol]

            # 2. විනාඩියකට කොයින් 2ක් බැගින් (තත්පර 30කට වරක් එකක්) ආරක්ෂිතව ස්කෑන් කිරීම
            pool = list(CACHED_SCAN_LIST)
            if pool:
                symbol = pool[scan_index % len(pool)]
                scan_index += 1
                if symbol not in ACTIVE_SIGNALS_DB:
                    sig = generate_instant_smc_setup(symbol)
                    if sig and LAST_SIGNAL_BY_SYMBOL.get(symbol) != sig["signal_id"]:
                        LAST_SIGNAL_BY_SYMBOL[symbol] = sig["signal_id"]
                        ACTIVE_SIGNALS_DB[symbol] = sig
                        trigger_instant_order_for_all(sig)

            time.sleep(30)

        except Exception as exc:
            print(f"[radar worker] {exc}")
            time.sleep(30)

def price_worker():
    while True:
        try:
            get_all_tickers()
        except Exception as exc:
            print(f"[Price worker] {exc}")
        time.sleep(15)

@app.on_event("startup")
def startup():
    init_db()
    load_bot_settings()
    with db_cursor() as conn:
        for row in conn.execute("SELECT * FROM users").fetchall():
            USERS_DB[row["email"]] = dict(row)
        for row in conn.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY created_at DESC").fetchall():
            trade = dict(row)
            email = trade["email"]
            USER_MY_TRADES_DB.setdefault(email, []).append({
                **trade, "entry_price": fmt_price(trade["entry"]), "take_profit": fmt_price(trade["tp"]),
                "stop_loss": fmt_price(trade["sl"]), "time": datetime.fromtimestamp(trade["created_at"], SRI_LANKA_TZ).strftime("%Y-%m-%d %I:%M:%S %p")
            })
    refresh_symbols()
    threading.Thread(target=price_worker, daemon=True).start()
    threading.Thread(target=radar_lifecycle_worker, daemon=True).start()
    threading.Thread(target=cloud_bot_autonomous_worker, daemon=True).start()

class RegisterModel(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

class LoginModel(BaseModel):
    email: EmailStr
    password: str

class AdminGrantVipModel(BaseModel):
    admin_email: EmailStr
    admin_password: str
    target_email: EmailStr
    days: int = Field(ge=1, le=3650)

class CloudBotConfigModel(BaseModel):
    email: EmailStr
    api_key: str = ""
    api_secret: str = ""
    max_trades: int = Field(default=1, ge=1, le=20)
    margin_usdt: float = Field(default=5, ge=2, le=100000)
    leverage: int = Field(default=3, ge=1, le=25)
    is_running: bool = False

@app.get("/")
def root():
    return {"status": "online", "version": APP_VERSION, "live_trading_enabled": LIVE_TRADING_ENABLED,
            "active_signals": len(ACTIVE_SIGNALS_DB), "history_count": len(HISTORY_SIGNALS_DB),
            "active_swing_bots": sum(1 for b in CLOUD_BOT_USERS.values() if b.get("is_running")),
            "binance_symbols": len(BINANCE_SYMBOLS), "bybit_symbols": len(BYBIT_SYMBOLS),
            "combined_symbols": len(EXCHANGE_SYMBOLS)}

@app.post("/api/register")
def register(data: RegisterModel):
    email = data.email.lower().strip()
    with db_cursor() as conn:
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "This email is already registered")
        conn.execute("INSERT INTO users(email,password_hash,created_at,is_vip,vip_until) VALUES(?,?,?,?,?)",
                     (email, password_hash(data.password), now_ts(), int(email == ADMIN_EMAIL),
                      now_ts() + (999999999 if email == ADMIN_EMAIL else 0)))
    user = db_user(email)
    USERS_DB[email] = user
    return {"status": "success", "message": "Welcome!", **safe_user_info(user, email)}

@app.post("/api/login")
def login(data: LoginModel):
    email = data.email.lower().strip()
    user = db_user(email)
    if not user or not verify_password(data.password, user["password_hash"]):
        raise HTTPException(401, "Invalid Email or Password")
    USERS_DB[email] = user
    return safe_user_info(user, email)

@app.post("/api/admin/grant-vip")
def admin_grant_vip(data: AdminGrantVipModel):
    admin_email = data.admin_email.lower().strip()
    if admin_email != ADMIN_EMAIL or not ADMIN_PASSWORD or not hmac.compare_digest(data.admin_password, ADMIN_PASSWORD):
        raise HTTPException(403, "Forbidden: invalid admin credentials")
    target = data.target_email.lower().strip()
    user = db_user(target)
    if not user:
        raise HTTPException(404, "Target user must register first")
    new_expiry = max(now_ts(), float(user["vip_until"])) + data.days * 86400
    with db_cursor() as conn:
        conn.execute("UPDATE users SET is_vip=1,vip_until=? WHERE email=?", (new_expiry, target))
        conn.execute("INSERT INTO vip_audit(admin_email,target_email,days,created_at) VALUES(?,?,?,?)",
                     (admin_email, target, data.days, now_ts()))
    USERS_DB[target] = db_user(target)
    return {"status": "success", "target_email": target, "vip_expires_at": datetime.fromtimestamp(new_expiry, SRI_LANKA_TZ).strftime("%Y-%m-%d %I:%M %p")}

@app.get("/api/admin/users-list")
def admin_users_list(admin_email: str = Query(""), admin_password: str = Query("")):
    if admin_email.lower().strip() != ADMIN_EMAIL or not ADMIN_PASSWORD or not hmac.compare_digest(admin_password, ADMIN_PASSWORD):
        raise HTTPException(403, "Access denied")
    with db_cursor() as conn:
        rows = conn.execute("SELECT email,created_at,is_vip,vip_until FROM users ORDER BY created_at DESC").fetchall()
    users = []
    for row in rows:
        users.append({"email": row["email"], "is_vip": bool(row["is_vip"] and row["vip_until"] > now_ts()),
                      "vip_days_remaining": max(0, int((row["vip_until"] - now_ts()) / 86400)),
                      "has_bot_synced": bool(CLOUD_BOT_USERS.get(row["email"], {}).get("is_running"))})
    return {"status": "success", "total_users": len(users), "users": users}

@app.post("/api/bot/save-settings")
def save_bot_settings(data: CloudBotConfigModel):
    email = data.email.lower().strip()
    user = db_user(email)
    if not user:
        raise HTTPException(401, "Please register/login first")
    old = CLOUD_BOT_USERS.get(email, {})
    bot = {"api_key": data.api_key.strip() or old.get("api_key", ""),
           "api_secret": data.api_secret.strip() or old.get("api_secret", ""),
           "max_trades": data.max_trades, "margin_usdt": data.margin_usdt,
           "leverage": data.leverage, "is_running": bool(data.is_running),
           "active_positions": old.get("active_positions", [])}
    if bot["is_running"] and not (bot["api_key"] and bot["api_secret"]):
        raise HTTPException(400, "Binance API key and secret are required")
    if bot["is_running"] and not LIVE_TRADING_ENABLED:
        bot["is_running"] = False
        CLOUD_BOT_USERS[email] = bot
        persist_bot_settings(email, bot)
        return {"status": "success", "message": "Settings saved. Live trading is disabled on server; bot remains stopped.",
                "live_trading_enabled": False}
    CLOUD_BOT_USERS[email] = bot
    persist_bot_settings(email, bot)
    return {"status": "success", "message": "Bot settings saved", "live_trading_enabled": LIVE_TRADING_ENABLED,
            "max_trades": bot["max_trades"], "margin_usdt": bot["margin_usdt"], "leverage": bot["leverage"],
            "is_running": bot["is_running"]}

@app.get("/api/bot/status")
def get_bot_status(email: str = Query("")):
    email = email.lower().strip()
    bot = CLOUD_BOT_USERS.get(email, {})
    return {"status": "success", "bot": {"is_running": bool(bot.get("is_running")),
            "active_positions": bot.get("active_positions", []), "max_trades": bot.get("max_trades", 1),
            "margin_usdt": bot.get("margin_usdt", 5), "leverage": bot.get("leverage", 3),
            "has_api_key": bool(bot.get("api_key")), "live_trading_enabled": LIVE_TRADING_ENABLED}}

@app.get("/api/coins")
def get_coin_list(exchange: str = Query("all")):
    exchange = exchange.lower()
    symbols = []
    for sym, meta in EXCHANGE_SYMBOLS.items():
        if exchange == "binance" and not meta["is_binance"]: continue
        if exchange == "bybit" and not meta["is_bybit"]: continue
        price = GLOBAL_LIVE_PRICES.get(sym, 0.0)
        symbols.append({**meta, "price": fmt_price(price) if price else None,
                        "change_24h": TICKER_24HR_CACHE.get(sym, {}).get("change")})
    symbols.sort(key=lambda x: (x["symbol"]))
    return {"status": "success", "count": len(symbols), "coins": symbols}

@app.get("/api/radar")
def get_live_radar():
    signals = []
    for symbol, sig in list(ACTIVE_SIGNALS_DB.items()):
        price = GLOBAL_LIVE_PRICES.get(symbol, 0.0)
        if price > 0:
            sig.update({"live_price": fmt_price(price), "current_price": fmt_price(price),
                        "mark_price": fmt_price(price), "last_price": fmt_price(price),
                        "realtime_price": fmt_price(price), "raw_price": price})
        signals.append(sig)
    signals.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return {"status": "success", "active_signals_count": len(signals), "signals": signals}

@app.get("/api/radar-history")
def get_radar_history():
    tp = sum(1 for s in HISTORY_SIGNALS_DB if "TP HIT" in s.get("status", ""))
    sl = sum(1 for s in HISTORY_SIGNALS_DB if "SL HIT" in s.get("status", ""))
    total = tp + sl
    win_rate = round(tp / total * 100, 1) if total else 0.0
    return {"status": "success", "total_history": len(HISTORY_SIGNALS_DB), "tp_hits": tp,
            "sl_hits": sl, "win_rate": f"{win_rate}%", "history": HISTORY_SIGNALS_DB}

@app.get("/api/signal")
def get_smc_signal(symbol: str = Query("BTCUSDT"), timeframe: str = Query("5m")):
    symbol = normalize_symbol(symbol)
    if symbol in ACTIVE_SIGNALS_DB:
        return {"status": "success", **ACTIVE_SIGNALS_DB[symbol]}
    signal = generate_instant_smc_setup(symbol)
    if signal:
        return {"status": "success", **signal}
    price = get_single_ticker(symbol)
    return {"status": "success", "symbol": symbol, "timeframe": timeframe, "signal": "NEUTRAL",
            "setup": "No confirmed MTF SMC setup", "trend": "NEUTRAL", "price": fmt_price(price),
            "confidence": None, "grade": "NO TRADE", "entry": "N/A", "take_profit": "N/A",
            "stop_loss": "N/A", "sweep_level": "N/A",
            **EXCHANGE_SYMBOLS.get(symbol, {"exchange_badge": "NOT LISTED", "is_binance": False, "is_bybit": False}),
            "scanned_time": get_current_sl_time(), "sparkline": []}

@app.get("/api/refresh-coin-price")
def refresh_single_coin_price(symbol: str = Query("")):
    if not symbol: raise HTTPException(400, "Symbol parameter is required")
    sym = normalize_symbol(symbol)
    price = get_single_ticker(sym)
    formatted = fmt_price(price)
    return {"status": "success", "symbol": sym, "price": formatted, "live_price": formatted,
            "current_price": formatted, "mark_price": formatted, "last_price": formatted,
            "realtime_price": formatted, "raw_price": price, "updated_at": get_current_sl_time()}

@app.get("/api/crypto/live-price/{symbol}")
def get_coin_live_price_path(symbol: str):
    return refresh_single_coin_price(symbol=symbol)

@app.get("/api/live-tickers")
def get_live_tickers_fast():
    return {"status": "success", "prices": GLOBAL_LIVE_PRICES}

@app.get("/api/prices")
def get_live_prices(symbols: str = Query("")):
    result = {}
    for raw in symbols.split(","):
        if not raw.strip(): continue
        sym = normalize_symbol(raw)
        price = GLOBAL_LIVE_PRICES.get(sym, 0.0) or get_single_ticker(sym)
        if price > 0: result[sym] = fmt_price(price)
    return {"status": "success", "prices": result}

@app.get("/api/news-alerts")
def get_news_alerts():
    return {"status": "success", "has_high_impact_news": False, "event_name": None,
            "note": "No real news provider is connected."}

@app.get("/api/my-trades")
def get_user_my_trades(email: str = Query("")):
    email = email.lower().strip()
    trades = read_trades(email)
    for trade in trades:
        price = GLOBAL_LIVE_PRICES.get(trade["symbol"], 0.0)
        if price <= 0: price = get_single_ticker(trade["symbol"])
        trade["entry_price"] = fmt_price(trade["entry"])
        trade["take_profit"] = fmt_price(trade["tp"])
        trade["stop_loss"] = fmt_price(trade["sl"])
        trade["live_price"] = fmt_price(price) if price else None
        if trade["status"] == "OPEN" and price and trade["entry"]:
            diff = (price - trade["entry"]) if "BUY" in trade["side"].upper() else (trade["entry"] - price)
            trade["unrealized_pnl"] = round(diff * trade["qty"], 6)
            trade["live_pnl"] = f"${trade['unrealized_pnl']:+.2f}"
    return {"status": "success", "count": len(trades), "trades": trades}

@app.get("/api/binance-history")
def get_binance_history(email: str = Query("")):
    trades = read_trades(email.lower().strip())
    closed = [t for t in trades if str(t.get("status", "")).startswith("CLOSED")]
    realized = sum(float(t.get("realized_pnl") or 0) for t in closed)
    return {"status": "success", "count": len(closed), "realized_pnl": round(realized, 6), "history": closed}

@app.get("/api/history")
def get_history_alias(email: str = Query("")):
    return get_binance_history(email=email)

@app.on_event("shutdown")
def shutdown():
    try: session.close()
    except Exception: pass

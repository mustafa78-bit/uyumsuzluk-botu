#!/usr/bin/env python3
import hashlib
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
import urllib.error
import socket

BOT_NAME = "Uyumsuzluk Botu"
HL_URL = "https://api.hyperliquid.xyz/info"

ENV_PATH = Path("/home/f_nisaakk529/.env")


def load_env_file(path=ENV_PATH):
    if not path.exists():
        return

    try:
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        logging.exception("Env dosyasi okunamadi.")


load_env_file()

TG_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TG_TOKEN") or ""
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TG_CHAT_ID") or ""

DB = "elite_divergence_seen.db"
LOG = "elite_divergence_v3.log"
LOCK = "elite_divergence.lock"
_LOCK_HANDLE = None

DEFAULT_TIMEFRAMES = ["4h", "1d", "1w"]
SUPPORTED_TIMEFRAMES = set(DEFAULT_TIMEFRAMES)


def parse_timeframes(raw_value):
    if not raw_value:
        return DEFAULT_TIMEFRAMES.copy()

    parsed = []
    for item in raw_value.split(","):
        tf = item.strip()
        if tf and tf in SUPPORTED_TIMEFRAMES and tf not in parsed:
            parsed.append(tf)

    return parsed or DEFAULT_TIMEFRAMES.copy()


def parse_int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_bool_env(name, default=False):
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


# 15m tamamen iptal. Sadece HTF tarama.
TIMEFRAMES = parse_timeframes(os.getenv("DIVERGENCE_TIMEFRAMES"))

LOOKBACK = {
    "4h": 4 * 60 * 60 * 1000 * 300,
    "1d": 24 * 60 * 60 * 1000 * 300,
    "1w": 7 * 24 * 60 * 60 * 1000 * 300,
}

SLEEP_SCAN = parse_int_env("SCAN_INTERVAL", 14400)
RUN_ONCE = parse_bool_env("RUN_ONCE")
SLEEP_CALL = 0.25

RSI_LEN = 14
RSI_BULL_MAX = 35
RSI_BEAR_MIN = 65

# Divergence pivot/filter defaults added by safe fix
PIVOT_LEN = 3
EXTREME_LOOKBACK = 80
MIN_RSI_DIFF = 3

# Public API pacing defaults added by safe fix
REQUEST_SLEEP_SEC = 1.25
HTTP_429_BACKOFF_SEC = 45
HTTP_429_RETRIES = 2
NETWORK_RETRIES = 4
NETWORK_BACKOFF_SEC = 3
OUTCOME_TP_PCT = 3.0
OUTCOME_SL_PCT = 2.0
OUTCOME_TIMEOUT_BARS = {"4h": 12, "1d": 7, "1w": 4}

# GLOBAL_URLLIB_RATE_LIMIT_PATCH
# Catch every urllib.request.urlopen call in this file, including old direct call paths.
REQUEST_SLEEP_SEC = 1.25
HTTP_429_BACKOFF_SEC = 45
HTTP_429_RETRIES = 2

_ORIGINAL_URLOPEN = urllib.request.urlopen

def _rate_limited_urlopen(*args, **kwargs):
    last_exc = None
    max_retries = max(HTTP_429_RETRIES, NETWORK_RETRIES)
    for attempt in range(max_retries + 1):
        try:
            time.sleep(REQUEST_SLEEP_SEC)
            return _ORIGINAL_URLOPEN(*args, **kwargs)
        except urllib.error.HTTPError as e:
            last_exc = e
            if getattr(e, "code", None) == 429 and attempt < HTTP_429_RETRIES:
                logging.warning(f"HTTP 429 rate limit; backoff {HTTP_429_BACKOFF_SEC * (attempt + 1)}s then retry")
                time.sleep(HTTP_429_BACKOFF_SEC * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            last_exc = e
            if attempt < NETWORK_RETRIES:
                delay = NETWORK_BACKOFF_SEC * (2 ** attempt)
                logging.warning("NETWORK_RETRY attempt=%s delay=%ss error=%s", attempt + 1, delay, str(e)[:180])
                time.sleep(delay)
                continue
            raise
    raise last_exc

urllib.request.urlopen = _rate_limited_urlopen


COINS = [
    "BTC","ETH","SOL","BNB","XRP","DOGE","HYPE","ZEC","TAO","SUI",
    "ENA","VVV","ASTER","MON","PUMP","LIT","ZRO","WIF","FARTCOIN",
    "MEGA","LINEA","GRASS","CHIP","VIRTUAL","APEX","CKB","MOVE",
    "INIT","BANK","BILL","IO","NEAR","TON","ADA","AVAX","LINK",
    "DOT","LTC","BCH","UNI","ATOM","APT","ARB","OP","FIL","INJ",
    "SEI","TIA","WLD","RENDER","FET","JUP","JTO","PYTH","BONK",
    "FLOKI","PEPE","SHIB","NOT","ZK","ETHFI","STRK","MANTA",
    "PENDLE","AAVE","MKR","COMP","CRV","DYDX","GMX","SNX","LDO",
    "RUNE","CAKE","SUSHI","GRT","CHZ","APE","SAND","MANA","AXS",
    "GALA","IMX","MAGIC","GMT","BLUR","MASK","ENS","CYBER","ROSE",
    "KAVA","MINA","ALGO","XLM","HBAR","ICP","ETC","EGLD","FLOW"
]

logging.basicConfig(
    filename=LOG,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


def post_json(url, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", errors="ignore"))


def acquire_single_instance_lock():
    global _LOCK_HANDLE
    _LOCK_HANDLE = open(LOCK, "a+", encoding="utf-8")
    try:
        fcntl.flock(_LOCK_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"Another elite_divergence instance is already running: {LOCK}")
    _LOCK_HANDLE.seek(0)
    _LOCK_HANDLE.truncate()
    _LOCK_HANDLE.write(str(os.getpid()))
    _LOCK_HANDLE.flush()


def tg_send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        logging.error("Telegram token/chat_id eksik.")
        return False

    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            code = r.getcode()
            if 200 <= code < 300:
                logging.info("TG_STATUS %s OK", code)
            else:
                logging.warning("TG_STATUS %s FAIL", code)
        return True
    except urllib.error.HTTPError as e:
        logging.warning("TG_STATUS %s FAIL", getattr(e, "code", "UNKNOWN"))
        return False
    except Exception as e:
        logging.error("Telegram hata: %s: %s", e.__class__.__name__, str(e)[:160])
        return False


def now_ms():
    return int(time.time() * 1000)


def fmt(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def init_db():
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            id TEXT PRIMARY KEY,
            coin TEXT,
            tf TEXT,
            side TEXT,
            t INTEGER,
            created INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            id TEXT PRIMARY KEY,
            coin TEXT NOT NULL,
            tf TEXT NOT NULL,
            side TEXT NOT NULL,
            signal_t INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            stop_price REAL NOT NULL,
            target_price REAL NOT NULL,
            status TEXT NOT NULL,
            exit_price REAL,
            exit_reason TEXT,
            result TEXT,
            r_multiple REAL,
            opened_at INTEGER NOT NULL,
            closed_at INTEGER,
            last_checked_t INTEGER
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_health (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated INTEGER NOT NULL
        )
    """)
    con.commit()
    con.close()


def health_set(key, value):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute(
        """
        INSERT INTO bot_health(key, value, updated)
        VALUES (?, ?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value, updated=excluded.updated
        """,
        (key, json.dumps(value, ensure_ascii=True, sort_keys=True), int(time.time())),
    )
    con.commit()
    con.close()


def seen_new(coin, tf, side, t):
    raw = f"{coin}|{tf}|{side}|{t}"
    sid = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM seen WHERE id=?", (sid,))
    if cur.fetchone():
        con.close()
        return False

    cur.execute(
        "INSERT INTO seen VALUES (?, ?, ?, ?, ?, ?)",
        (sid, coin, tf, side, t, int(time.time()))
    )
    con.commit()
    con.close()
    return True


def outcome_id(coin, tf, side, t):
    return hashlib.sha256(f"{coin}|{tf}|{side}|{t}".encode("utf-8")).hexdigest()


def create_outcome_if_missing(coin, tf, side, t, entry_price):
    oid = outcome_id(coin, tf, side, t)
    if side == "BULLISH":
        stop = entry_price * (1.0 - OUTCOME_SL_PCT / 100.0)
        target = entry_price * (1.0 + OUTCOME_TP_PCT / 100.0)
    else:
        stop = entry_price * (1.0 + OUTCOME_SL_PCT / 100.0)
        target = entry_price * (1.0 - OUTCOME_TP_PCT / 100.0)
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO signal_outcomes(
            id, coin, tf, side, signal_t, entry_price, stop_price, target_price,
            status, opened_at, last_checked_t
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
        """,
        (oid, coin, tf, side, t, entry_price, stop, target, int(time.time()), t),
    )
    con.commit()
    con.close()

def hl_coins():
    data = post_json(HL_URL, {"type": "meta"})
    return set(
        x["name"].upper()
        for x in data.get("universe", [])
        if x.get("name")
    )


def candles(coin, tf):
    end = now_ms()
    start = end - LOOKBACK[tf]
    data = post_json(HL_URL, {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": tf,
            "startTime": start,
            "endTime": end,
        }
    })

    out = []
    if not isinstance(data, list):
        return out

    for c in data:
        try:
            out.append({
                "t": int(c["t"]),
                "h": float(c["h"]),
                "l": float(c["l"]),
                "c": float(c["c"]),
            })
        except Exception:
            pass

    return sorted(out, key=lambda x: x["t"])


def close_outcome(row, exit_price, exit_reason, close_t):
    if str(exit_reason).startswith("AMBIGUOUS"):
        r_multiple = None
        result = "AMBIGUOUS"
    else:
        risk = abs(float(row["entry_price"]) - float(row["stop_price"]))
        if risk == 0:
            r_multiple = 0.0
        elif row["side"] == "BULLISH":
            r_multiple = (exit_price - float(row["entry_price"])) / risk
        else:
            r_multiple = (float(row["entry_price"]) - exit_price) / risk
        result = "WIN" if r_multiple > 0 else "LOSS"
    con = sqlite3.connect(DB)
    cur = con.cursor()
    cur.execute(
        """
        UPDATE signal_outcomes
        SET status='CLOSED', exit_price=?, exit_reason=?, result=?, r_multiple=?,
            closed_at=?, last_checked_t=?
        WHERE id=?
        """,
        (exit_price, exit_reason, result, round(r_multiple, 6) if r_multiple is not None else None, int(time.time()), close_t, row["id"]),
    )
    con.commit()
    con.close()


def update_open_outcomes():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT * FROM signal_outcomes WHERE status='OPEN'").fetchall()
    con.close()
    closed = 0
    for row in rows:
        try:
            cs = candles(row["coin"], row["tf"])
            relevant = [c for c in cs if c["t"] > int(row["signal_t"])]
            timeout_bars = OUTCOME_TIMEOUT_BARS.get(row["tf"], 12)
            for idx, candle in enumerate(relevant, start=1):
                if row["side"] == "BULLISH":
                    hit_stop = candle["l"] <= float(row["stop_price"])
                    hit_target = candle["h"] >= float(row["target_price"])
                else:
                    hit_stop = candle["h"] >= float(row["stop_price"])
                    hit_target = candle["l"] <= float(row["target_price"])
                if hit_stop and hit_target:
                    close_outcome(row, float(candle["c"]), "AMBIGUOUS_TP_SL_SAME_CANDLE", candle["t"])
                    closed += 1
                    break
                if hit_target:
                    close_outcome(row, float(row["target_price"]), "TP", candle["t"])
                    closed += 1
                    break
                if hit_stop:
                    close_outcome(row, float(row["stop_price"]), "SL", candle["t"])
                    closed += 1
                    break
                if idx >= timeout_bars:
                    close_outcome(row, float(candle["c"]), "TIMEOUT", candle["t"])
                    closed += 1
                    break
        except Exception as e:
            logging.exception("outcome_update_error %s %s %s", row["coin"], row["tf"], e)
    health_set("outcomes", {"open_checked": len(rows), "closed_now": closed})


def rsi(closes, n=14):
    if len(closes) < n + 2:
        return []

    arr = [None] * len(closes)
    gains, losses = [], []

    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(abs(min(d, 0)))

    ag = sum(gains) / n
    al = sum(losses) / n
    arr[n] = 100 - (100 / (1 + (ag / al if al else 999)))

    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = ((ag * (n - 1)) + max(d, 0)) / n
        al = ((al * (n - 1)) + abs(min(d, 0))) / n
        arr[i] = 100 - (100 / (1 + (ag / al if al else 999)))

    return arr

# Pivot helper defaults added by safe fix
def _candle_low(row):
    if isinstance(row, (int, float)):
        return float(row)
    if isinstance(row, dict):
        return float(row.get("l", row.get("low")))
    return float(row[3])

def _candle_high(row):
    if isinstance(row, (int, float)):
        return float(row)
    if isinstance(row, dict):
        return float(row.get("h", row.get("high")))
    return float(row[2])

def is_pivot_low(candles_, idx, pivot_len=PIVOT_LEN):
    if idx - pivot_len < 0 or idx + pivot_len >= len(candles_):
        return False
    center = _candle_low(candles_[idx])
    for j in range(idx - pivot_len, idx + pivot_len + 1):
        if j == idx:
            continue
        if _candle_low(candles_[j]) <= center:
            return False
    return True

def is_pivot_high(candles_, idx, pivot_len=PIVOT_LEN):
    if idx - pivot_len < 0 or idx + pivot_len >= len(candles_):
        return False
    center = _candle_high(candles_[idx])
    for j in range(idx - pivot_len, idx + pivot_len + 1):
        if j == idx:
            continue
        if _candle_high(candles_[j]) >= center:
            return False
    return True

def scan_div(c):
    if len(c) < 120:
        return []

    closes = [x["c"] for x in c]
    lows = [x["l"] for x in c]
    highs = [x["h"] for x in c]
    rs = rsi(closes, RSI_LEN)

    if not rs:
        return []

    out = []

    last_confirmed = len(c) - 1 - PIVOT_LEN
    start = max(RSI_LEN + PIVOT_LEN, len(c) - EXTREME_LOOKBACK)

    low_pivots = []
    high_pivots = []

    for i in range(start, last_confirmed + 1):
        if i >= len(rs) or rs[i] is None:
            continue

        if is_pivot_low(lows, i):
            if not low_pivots or i - low_pivots[-1] >= PIVOT_LEN:
                low_pivots.append(i)

        if is_pivot_high(highs, i):
            if not high_pivots or i - high_pivots[-1] >= PIVOT_LEN:
                high_pivots.append(i)

    # BULLISH: fiyat daha düşük dip, RSI daha yüksek dip
    if len(low_pivots) >= 2:
        b = low_pivots[-1]

        for a in reversed(low_pivots[:-1][-6:]):
            if rs[a] is None or rs[b] is None:
                continue

            recent_lowest = lows[b] <= min(lows[max(0, b - EXTREME_LOOKBACK + 1): b + 1])
            price_lower_low = lows[b] < lows[a]
            rsi_higher_low = rs[b] >= rs[a] + MIN_RSI_DIFF
            rsi_deep = min(rs[a], rs[b]) <= RSI_BULL_MAX

            if recent_lowest and price_lower_low and rsi_higher_low and rsi_deep:
                out.append(("BULLISH", c[b]["t"], lows[a], lows[b], rs[a], rs[b]))
                break

    # BEARISH: fiyat daha yüksek tepe, RSI daha düşük tepe
    if len(high_pivots) >= 2:
        b = high_pivots[-1]

        for a in reversed(high_pivots[:-1][-6:]):
            if rs[a] is None or rs[b] is None:
                continue

            recent_highest = highs[b] >= max(highs[max(0, b - EXTREME_LOOKBACK + 1): b + 1])
            price_higher_high = highs[b] > highs[a]
            rsi_lower_high = rs[b] <= rs[a] - MIN_RSI_DIFF
            rsi_hot = max(rs[a], rs[b]) >= RSI_BEAR_MIN

            if recent_highest and price_higher_high and rsi_lower_high and rsi_hot:
                out.append(("BEARISH", c[b]["t"], highs[a], highs[b], rs[a], rs[b]))
                break

    return out
def run_once():
    h = hl_coins()
    coins = sorted(set(x.upper() for x in COINS) & h)
    print(f"{BOT_NAME} | Taranacak coin: {len(coins)}", flush=True)
    health_set("scan_start", {"time": int(time.time()), "coins": len(coins)})
    update_open_outcomes()

    total = 0
    errors = 0

    for coin in coins:
        for tf in TIMEFRAMES:
            try:
                cs = candles(coin, tf)
                divs = scan_div(cs)

                for side, t, p1, p2, r1, r2 in divs:
                    if not seen_new(coin, tf, side, t):
                        continue
                    create_outcome_if_missing(coin, tf, side, t, float(p2))

                    total += 1
                    title = "Bullish Uyumsuzluk" if side == "BULLISH" else "Bearish Uyumsuzluk"
                    symbol = f"{coin}USDT"

                    msg = (
                        "UYUMSUZLUK SIGNAL\n"
                        f"Symbol: {symbol}\n"
                        f"Timeframe: {tf}\n"
                        f"Side: {side}\n"
                        f"Title: {title}\n\n"
                        f"Price: {p1:.6g} → {p2:.6g}\n"
                        f"RSI: {r1:.2f} → {r2:.2f}\n"
                        f"Zaman: {fmt(t)}\n\n"
                        f"Not: İşlem sinyali değil, uyumsuzluk tespitidir."
                    )

                    logging.info(
                        "UYUMSUZLUK SIGNAL Symbol=%s Timeframe=%s Side=%s Time=%s",
                        symbol,
                        tf,
                        side,
                        fmt(t),
                    )
                    print(msg, flush=True)
                    tg_send(msg)
                    time.sleep(SLEEP_CALL)

            except Exception as e:
                errors += 1
                logging.exception(f"{coin} {tf} hata: {e}")

    print("Yeni divergence:", total, flush=True)
    health_set("last_scan", {"time": int(time.time()), "new_divergence": total, "errors": errors})


def main():
    acquire_single_instance_lock()
    init_db()

    if RUN_ONCE:
        run_once()
        return

    while True:
        run_once()
        print(f"Uyku: {SLEEP_SCAN} saniye", flush=True)
        time.sleep(SLEEP_SCAN)


if __name__ == "__main__":
    main()

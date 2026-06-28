cd /workspace 2>/dev/null || cd /home/f_nisaakk529/uyumsuzluk-botu || cd /home/f_nisaakk529 || exit 1

cp main.py "main.py.bak.$(date +%Y%m%d_%H%M%S)"

python3 - <<'PY'
from pathlib import Path

p = Path("main.py")
s = p.read_text()

s = s.replace(
'''def parse_int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
''',
'''def parse_int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def parse_float_env(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
'''
)

s = s.replace(
'''OUTCOME_TIMEOUT_BARS = {"4h": 12, "1d": 7, "1w": 4}
''',
'''OUTCOME_TIMEOUT_BARS = {"4h": 12, "1d": 7, "1w": 4}

USE_SCORE_FILTER = parse_bool_env("USE_SCORE_FILTER", True)
MIN_SIGNAL_SCORE = parse_float_env("MIN_SIGNAL_SCORE", 5.0)

EMA_TREND_MODE = os.getenv("EMA_TREND_MODE", "soft").strip().lower()
if EMA_TREND_MODE not in {"off", "soft", "strict"}:
    EMA_TREND_MODE = "soft"

EMA_FAST_LEN = parse_int_env("EMA_FAST_LEN", 20)
EMA_SLOW_LEN = parse_int_env("EMA_SLOW_LEN", 50)
EMA_TOUCH_PCT = parse_float_env("EMA_TOUCH_PCT", 3.0)

ATR_LEN = parse_int_env("ATR_LEN", 14)
ATR_MIN_PCT = parse_float_env("ATR_MIN_PCT", 0.5)
ATR_MAX_PCT = parse_float_env("ATR_MAX_PCT", 12.0)

MOMENTUM_LOOKBACK = parse_int_env("MOMENTUM_LOOKBACK", 3)

RVOL_LEN = parse_int_env("RVOL_LEN", 20)
RVOL_MIN = parse_float_env("RVOL_MIN", 1.2)
USE_RVOL_FILTER = parse_bool_env("USE_RVOL_FILTER", False)
'''
)

s = s.replace(
'''"c": float(c["c"]),
            })
''',
'''"c": float(c["c"]),
                "v": float(c.get("v", 0) or 0),
            })
'''
)

helper = r'''

def ema_last(values, n):
    if len(values) < n or n <= 0:
        return None
    k = 2 / (n + 1)
    ema = sum(values[:n]) / n
    for value in values[n:]:
        ema = value * k + ema * (1 - k)
    return ema


def atr_pct(candles_, n=14):
    if len(candles_) < n + 1:
        return None

    trs = []
    for i in range(len(candles_) - n, len(candles_)):
        high = float(candles_[i]["h"])
        low = float(candles_[i]["l"])
        prev_close = float(candles_[i - 1]["c"])
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    close = float(candles_[-1]["c"])
    if close <= 0:
        return None
    return sum(trs) / len(trs) / close * 100


def rvol_value(candles_, n=20):
    if len(candles_) < n + 1:
        return None

    vols = [float(x.get("v", 0) or 0) for x in candles_[-n - 1:]]
    if not any(v > 0 for v in vols):
        return None

    avg = sum(vols[:-1]) / len(vols[:-1])
    if avg <= 0:
        return None

    return vols[-1] / avg


def score_divergence_signal(candles_, idx, side, r1, r2):
    score = 3.0
    details = []

    window = candles_[:idx + 1]
    close = float(candles_[idx]["c"])
    closes = [float(x["c"]) for x in window]

    atrp = atr_pct(window, ATR_LEN)
    if atrp is not None:
        details.append(f"ATR%={atrp:.2f}")
        if atrp < ATR_MIN_PCT or atrp > ATR_MAX_PCT:
            return None
        score += 0.5
    else:
        details.append("ATR%=NA")

    rsi_gap = abs(float(r2) - float(r1))
    if rsi_gap >= MIN_RSI_DIFF * 2:
        score += 1.0
        details.append("RSI=strong")
    else:
        details.append("RSI=normal")

    ema_fast = ema_last(closes, EMA_FAST_LEN)
    ema_slow = ema_last(closes, EMA_SLOW_LEN)

    if ema_fast is not None and ema_slow is not None and close > 0:
        if side == "BULLISH":
            trend_ok = close >= ema_slow or ema_fast >= ema_slow
            strict_ok = close >= ema_slow
            ema_near = abs(close - ema_fast) / close * 100 <= EMA_TOUCH_PCT or close >= ema_fast
        else:
            trend_ok = close <= ema_slow or ema_fast <= ema_slow
            strict_ok = close <= ema_slow
            ema_near = abs(close - ema_fast) / close * 100 <= EMA_TOUCH_PCT or close <= ema_fast

        if EMA_TREND_MODE == "strict" and not strict_ok:
            return None

        if EMA_TREND_MODE != "off":
            if trend_ok:
                score += 1.0
                details.append("trend=ok")
            else:
                details.append("trend=weak")

        if ema_near:
            score += 0.5
            details.append("ema_zone=ok")
    else:
        details.append("ema=NA")

    if len(closes) > MOMENTUM_LOOKBACK:
        prev = closes[-1 - MOMENTUM_LOOKBACK]
        if side == "BULLISH" and close > prev:
            score += 1.0
            details.append("momentum=up")
        elif side == "BEARISH" and close < prev:
            score += 1.0
            details.append("momentum=down")
        else:
            details.append("momentum=weak")

    rv = rvol_value(window, RVOL_LEN)
    if rv is not None:
        details.append(f"RVOL={rv:.2f}")
        if rv >= RVOL_MIN:
            score += 1.0
        elif USE_RVOL_FILTER:
            return None
    elif USE_RVOL_FILTER:
        return None
    else:
        details.append("RVOL=NA")

    if USE_SCORE_FILTER and score < MIN_SIGNAL_SCORE:
        return None

    return round(score, 2), ", ".join(details[:7])
'''

if "def score_divergence_signal" not in s:
    s = s.replace("\ndef scan_div(c):", helper + "\ndef scan_div(c):")

s = s.replace(
'''                out.append(("BULLISH", c[b]["t"], lows[a], lows[b], rs[a], rs[b]))
''',
'''                score_info = score_divergence_signal(c, b, "BULLISH", rs[a], rs[b])
                if score_info:
                    score, score_details = score_info
                    out.append(("BULLISH", c[b]["t"], lows[a], lows[b], rs[a], rs[b], score, score_details))
'''
)

s = s.replace(
'''                out.append(("BEARISH", c[b]["t"], highs[a], highs[b], rs[a], rs[b]))
''',
'''                score_info = score_divergence_signal(c, b, "BEARISH", rs[a], rs[b])
                if score_info:
                    score, score_details = score_info
                    out.append(("BEARISH", c[b]["t"], highs[a], highs[b], rs[a], rs[b], score, score_details))
'''
)

s = s.replace(
'''                for side, t, p1, p2, r1, r2 in divs:
''',
'''                for side, t, p1, p2, r1, r2, score, score_details in divs:
'''
)

s = s.replace(
'''                        f"RSI: {r1:.2f} → {r2:.2f}\\n"
                        f"Zaman: {fmt(t)}\\n\\n"
''',
'''                        f"RSI: {r1:.2f} → {r2:.2f}\\n"
                        f"Score: {score:.2f}\\n"
                        f"Filtre: {score_details}\\n"
                        f"Zaman: {fmt(t)}\\n\\n"
'''
)

p.write_text(s)
print("OK: main.py guncellendi")
PY

python3 -m py_compile main.py && echo "OK: syntax temiz"

grep -q '^USE_SCORE_FILTER=' .env 2>/dev/null || cat >> .env <<'ENVADD'

USE_SCORE_FILTER=true
MIN_SIGNAL_SCORE=5
EMA_TREND_MODE=soft
EMA_FAST_LEN=20
EMA_SLOW_LEN=50
EMA_TOUCH_PCT=3.0
ATR_LEN=14
ATR_MIN_PCT=0.5
ATR_MAX_PCT=12.0
MOMENTUM_LOOKBACK=3
RVOL_LEN=20
RVOL_MIN=1.2
USE_RVOL_FILTER=false
ENVADD

echo "OK: env parametreleri eklendi"

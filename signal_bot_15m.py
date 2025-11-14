#!/usr/bin/env python3
# strict_signal_with_indicators.py
# Multi-pair 15m scanner:
# - Checks only CLOSED 15m candles
# - Detects HAMMER and SHOOTING STAR
# - Requires trend confirmation on previous 5 candles
# - Adds indicators: volume filter, RSI (14), EMA20/EMA50
# - Sends Telegram notifications and logs signals

import ccxt
import pandas as pd
import time
import requests
from datetime import datetime, timezone

# ========== CONFIG ==========
BOT_TOKEN = "8438864481:AAFOZFAZq1KqiVdU-rE3SxMrlCvNaHaf79A"
CHAT_ID = "903610526"

PAIRS = ["ETH/USDT", "BCH/USDT", "SOL/USDT", "TON/USDT", "LINK/USDT"]
TIMEFRAME = "15m"
FETCH_LIMIT = 50         # need enough candles for indicators
INTER_DELAY = 1.0        # seconds between pair fetches
LOGFILE = "signals.log"

# Indicator thresholds (tuneable)
VOLUME_MULTIPLIER = 0.9  # require last vol > avg_vol * multiplier
RSI_PERIOD = 14
RSI_OVERSOLD = 40       # allow hammer when RSI below this (less strict than 30)
RSI_OVERBOUGHT = 60     # allow shooting star when RSI above this
EMA_FAST = 20
EMA_SLOW = 50

exchange = ccxt.binance({"enableRateLimit": True})


# ========== UTILITIES ==========
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print("Telegram send failed:", r.status_code, r.text)
    except Exception as e:
        print("Telegram error:", e)


def log_signal(text):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {text}\n")


def fetch_ohlcv_df(symbol, timeframe=TIMEFRAME, limit=FETCH_LIMIT):
    data = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "close", "volume"])
    df["time"] = pd.to_datetime(df["ts"], unit="ms")
    df.set_index("time", inplace=True)
    df = df.astype(float)
    return df


# ========== INDICATORS ==========
def compute_rsi(series, period=RSI_PERIOD):
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    ma_up = up.rolling(period, min_periods=period).mean()
    ma_down = down.rolling(period, min_periods=period).mean()
    rs = ma_up / ma_down
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


# ========== CANDLE PATTERN LOGIC ==========
def is_hammer_candle(c):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    body = abs(cl - o)
    if body == 0:
        return False
    lower = min(o, cl) - l
    upper = h - max(o, cl)
    # strict: long lower shadow, small upper shadow
    return (lower > body * 1.8) and (upper < body * 0.6)


def is_shooting_star_candle(c):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    body = abs(cl - o)
    if body == 0:
        return False
    upper = h - max(o, cl)
    lower = min(o, cl) - l
    return (upper > body * 1.8) and (lower < body * 0.6)


# trend confirmation on previous N candles (5 by default)
def trend_confirmation(candles, direction="down", required_count=3):
    # candles: list of dict-like rows (older -> newer)
    # direction = "down" for hammer, "up" for shooting star
    closes = [c["close"] for c in candles]
    # requirement 1: at least required_count bearish/bullish among them
    if direction == "down":
        count_bear = sum(1 for c in candles if c["close"] < c["open"])
        falling = closes[-1] < closes[0]  # end lower than start
        return (count_bear >= required_count) and falling
    else:
        count_bull = sum(1 for c in candles if c["close"] > c["open"])
        rising = closes[-1] > closes[0]
        return (count_bull >= required_count) and rising


# full analysis for a pair
def analyze_pair(pair):
    try:
        df = fetch_ohlcv_df(pair)
    except Exception as e:
        print(f"[{pair}] fetch error: {e}")
        return []

    # Use last 6 closed candles: previous 5 for trend, last one = pattern candle (closed)
    if len(df) < 6:
        return []

    last6 = df.iloc[-6:]
    trend_candles = last6.iloc[0:5].to_dict("records")   # older -> newer, 5 candles
    pattern_candle = last6.iloc[-1].to_dict()            # closed pattern candle
    last_vol = pattern_candle["volume"]
    avg_vol = df["volume"][-20:].mean() if len(df) >= 20 else df["volume"].mean()

    # indicators (on full df)
    df["rsi"] = compute_rsi(df["close"], RSI_PERIOD)
    df["ema_fast"] = compute_ema(df["close"], EMA_FAST)
    df["ema_slow"] = compute_ema(df["close"], EMA_SLOW)

    pattern_time = last6.index[-1].strftime("%Y-%m-%d %H:%M UTC")
    signals = []

    # Volume filter
    vol_ok = (avg_vol is not None) and (last_vol >= avg_vol * VOLUME_MULTIPLIER)

    # EMA trend confirmation (use EMA slope or cross)
    ema_fast = df["ema_fast"].iloc[-2]   # use previous value (closed)
    ema_slow = df["ema_slow"].iloc[-2]

    # RSI value for pattern candle (use closed candle's rsi)
    rsi_val = df["rsi"].iloc[-2]  # rsi of the closed pattern candle

    # HAMMER logic
    if is_hammer_candle(pattern_candle):
        trend_ok = trend_confirmation(trend_candles, direction="down", required_count=3)
        ema_ok = ema_fast < ema_slow  # short-term under long-term => confirms downtrend
        rsi_ok = (rsi_val <= RSI_OVERSOLD)  # e.g., oversold or near
        if trend_ok and ema_ok and vol_ok and rsi_ok:
            signals.append(("HAMMER", "LONG", pattern_candle["close"], pattern_time,
                            {"rsi": float(rsi_val), "avg_vol": float(avg_vol), "last_vol": float(last_vol)}))

    # SHOOTING STAR logic
    if is_shooting_star_candle(pattern_candle):
        trend_ok = trend_confirmation(trend_candles, direction="up", required_count=3)
        ema_ok = ema_fast > ema_slow  # short-term above long-term => confirms uptrend
        rsi_ok = (rsi_val >= RSI_OVERBOUGHT)
        if trend_ok and ema_ok and vol_ok and rsi_ok:
            signals.append(("SHOOTING_STAR", "SHORT", pattern_candle["close"], pattern_time,
                            {"rsi": float(rsi_val), "avg_vol": float(avg_vol), "last_vol": float(last_vol)}))

    return signals


# ========== MAIN LOOP ==========
def seconds_to_next_15min():
    now = int(time.time())
    rem = now % (15 * 60)
    return (15 * 60) - rem if rem != 0 else 0


def main():
    print("STRICT+INDICATORS Hammer/ShootingStar BOT STARTED")
    wait = seconds_to_next_15min()
    if wait:
        print(f"Aligning to next 15m close: sleeping {wait+1} sec")
        time.sleep(wait + 1)

    while True:
        cycle_start = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        print(f"[{cycle_start}] Scanning pairs: {', '.join(PAIRS)}")
        for pair in PAIRS:
            try:
                signals = analyze_pair(pair)
                if signals:
                    for typ, direction, price, tme, meta in signals:
                        if typ == "HAMMER":
                            title = "🟢 Hammer (confirmed)"
                        else:
                            title = "🔴 Shooting Star (confirmed)"

                        msg = (f"{title}\nPair: {pair}\nPattern time (closed): {tme}\n"
                               f"Direction: {direction}\nPrice: {price}\n"
                               f"RSI: {meta['rsi']:.1f} | vol: {meta['last_vol']:.3f} (avg {meta['avg_vol']:.3f})\nTF: {TIMEFRAME}")
                        send_telegram(msg)
                        log_signal(f"{pair} | {typ} | {direction} | price={price} | time={tme} | rsi={meta['rsi']:.1f}")
                        print(f"[{pair}] SIGNAL -> {typ} @ {price} (rsi {meta['rsi']:.1f})")
                else:
                    print(f"[{pair}] No confirmed pattern.")
            except Exception as e:
                print(f"[{pair}] error:", e)
            time.sleep(INTER_DELAY)

        to_sleep = seconds_to_next_15min()
        if to_sleep == 0:
            to_sleep = 15 * 60
        print(f"Cycle complete. Sleeping {to_sleep+1} sec until next 15m candle close...")
        time.sleep(to_sleep + 1)


if __name__ == "__main__":
    main()
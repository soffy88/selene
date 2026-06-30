import argparse
import requests
import psycopg2
import os
from datetime import datetime, timezone, timedelta
import time

DB_URL = os.environ.get("DB_URL", "postgresql://selene_app:sel_app_2026@localhost:5434/selene")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTC-USDT",
                        help="stored symbol (base) — downstream joins on this")
    parser.add_argument("--inst-id", default=None,
                        help="OKX instId to fetch candles from; default {symbol}-SWAP (the "
                             "PERPETUAL). The live tick/LOB feed is the perp (P0-2), so the "
                             "historical bars must be the perp too, not spot BTC-USDT, or the "
                             "bar series has a spot/perp seam.")
    parser.add_argument("--bar", default="4H")
    parser.add_argument("--years", type=int, default=2)
    args = parser.parse_args()
    inst_id = args.inst_id or f"{args.symbol}-SWAP"

    conn = psycopg2.connect(DB_URL)
    cur = conn.cursor()

    limit = 100
    end_ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ts = int((datetime.now(timezone.utc) - timedelta(days=365 * args.years)).timestamp() * 1000)

    total_inserted = 0

    while end_ts > start_ts:
        url = f"https://www.okx.com/api/v5/market/history-candles?instId={inst_id}&bar={args.bar}&after={end_ts}&limit={limit}"
        
        proxies = {}
        if os.environ.get("HTTPS_PROXY"):
            proxies["https"] = os.environ.get("HTTPS_PROXY")
        
        resp = requests.get(url, proxies=proxies)
        if resp.status_code != 200:
            print(f"Error: {resp.status_code}")
            time.sleep(1)
            continue
            
        data = resp.json()
        if data["code"] != "0" or not data["data"]:
            break
            
        candles = data["data"]
        for c in candles:
            ts = datetime.fromtimestamp(int(c[0])/1000, tz=timezone.utc)
            o, h, l, cl, vol = float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])
            # History candles don't carry VWAP; store NULL (unknown), not a fake 0.0 that a
            # reader can't tell apart from a real VWAP (audit P2-5).
            vwap = None
            tick_count = 0
            
            cur.execute("""
                INSERT INTO v2_bars_4h (time, symbol, open, high, low, close, volume, vwap, tick_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (time, symbol) DO NOTHING
            """, (ts, args.symbol, o, h, l, cl, vol, vwap, tick_count))
            total_inserted += 1
            
        conn.commit()
        end_ts = int(candles[-1][0])
        print(f"Inserted {len(candles)} bars, total {total_inserted}, min_ts: {datetime.fromtimestamp(end_ts/1000, tz=timezone.utc)}")
        time.sleep(0.2)
        
        if len(candles) < limit:
            break

    cur.close()
    conn.close()
    print(f"Backfill complete. Total inserted: {total_inserted}")

if __name__ == "__main__":
    main()

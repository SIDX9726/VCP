import math
import time
import json
import os
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, time as dtime, timedelta
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from kiteconnect import KiteConnect

# --- CONFIGURATION ---
API_KEY = "pp8l4r5gh24lm9bk"
# Direct default active token ya runtime update variable
CURRENT_ACCESS_TOKEN = "5ywM0XJdsfjJTxVAJuflFcgcl1tXVKcE"
HISTORY_FILE = "trade_history.json"
TIMEFRAME = "day"

# Telegram Config (Apna Bot Token aur Chat ID yahan daal sakte ho)
TELEGRAM_BOT_TOKEN = "8807605440:AAHRGR8kdhmofApn0AbmWrHtd8KGFGWa-5Y"
TELEGRAM_CHAT_ID = "5050758948"


app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TokenModel(BaseModel):
    access_token: str

def send_telegram_alert(message):
    try:
        if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
            print(f"📢 [TELEGRAM MOCK ALERT]: {message}")
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"Telegram error: {e}")

def save_history_record(trade_record):
    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
        except:
            history = []
    history.insert(0, trade_record)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[:100], f, indent=4)

@app.post("/api/update-token")
def update_token(data: TokenModel):
    global CURRENT_ACCESS_TOKEN
    CURRENT_ACCESS_TOKEN = data.access_token.strip()
    return {"status": "success", "message": "Access token updated successfully in memory!"}

@app.get("/api/history")
def get_trade_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return json.load(f)
    return []

# --- FULL MASTER SEPA LIVE & BACKTEST ENGINE ---
class MasterSEPALiveEngine:
    def __init__(self, timeframe=TIMEFRAME):
        self.timeframe = timeframe
        self.stock_data = {}
        self.screener_symbols = set()
        self.active_positions = {}
        self.post_exit_cooldown = {}

    def load_screener_universe(self, csv_filename="all_sepa_screener_stocks.csv"):
        try:
            if os.path.exists(csv_filename):
                df = pd.read_csv(csv_filename)
                if 'Symbol' in df.columns:
                    self.screener_symbols = set(df['Symbol'].dropna().str.strip().str.upper().values)
                elif 'Company' in df.columns:
                    self.screener_symbols = set(df['Company'].dropna().str.strip().str.upper().values)
                print(f"✅ Loaded {len(self.screener_symbols)} clean stocks from Screener Dataset.")
        except Exception as e:
            print(f"⚠️ Screener CSV load karne mein error aayi: {e}")

    def check_minervini_trend(self, df_sliced):
        try:
            close = df_sliced['close'].values
            s150 = df_sliced['sma_150'].values[-1]
            s200 = df_sliced['sma_200'].values[-1]
            curr_p = close[-1]
            
            lookback_52w = min(252, len(close))
            low_52w = np.min(df_sliced['low'].values[-lookback_52w:])
            
            ma_ok = (curr_p > s150) and (curr_p > s200) and (s150 > s200)
            uptrend_ok = (curr_p >= low_52w * 1.30)
            
            return ma_ok and uptrend_ok
        except:
            return False

    def check_relaxed_vcp_consolidation(self, hist_df):
        try:
            highs = hist_df['high'].values
            lows = hist_df['low'].values
            closes = hist_df['close'].values
            ranges = hist_df['daily_range'].values
            
            if len(closes) < 40:
                return False

            recent_high = np.max(highs[-40:])
            recent_low = np.min(lows[-40:])
            curr_p = closes[-1]
            
            is_near_highs = curr_p >= (recent_low + (recent_high - recent_low) * 0.50)
            early_range = np.mean(ranges[-40:-20])
            late_range = np.mean(ranges[-10:])
            is_contracting = late_range <= (early_range * 1.25)
            
            return is_near_highs and is_contracting
        except:
            return True

    def check_recent_runup(self, df_sliced, max_gain_pct=0.14, lookback_days=10):
        try:
            closes = df_sliced['close'].values
            if len(closes) < lookback_days:
                return True
            price_past = closes[-lookback_days]
            price_curr = closes[-1]
            gain = (price_curr - price_past) / price_past
            return gain <= max_gain_pct
        except:
            return True

    def check_true_base_reset(self, df_sliced, min_correction_pct=0.04, max_correction_pct=0.22):
        try:
            highs = df_sliced['high'].values
            closes = df_sliced['close'].values
            if len(highs) < 30:
                return False
            recent_peak = np.max(highs[-30:])
            curr_p = closes[-1]
            correction_from_peak = (recent_peak - curr_p) / recent_peak
            return min_correction_pct <= correction_from_peak <= max_correction_pct
        except:
            return True

    def background_scanner_loop(self):
        print("🚀 [BACKGROUND WORKER] Master SEPA Live Scanner Thread Started...")
        while True:
            try:
                global CURRENT_ACCESS_TOKEN
                kite = KiteConnect(api_key=API_KEY)
                kite.set_access_token(CURRENT_ACCESS_TOKEN)

                self.load_screener_universe()
                if not self.screener_symbols:
                    time.sleep(60)
                    continue

                all_inst = kite.instruments("NSE")
                exclude_kw = ["-SG", "-GB", "-GS", "-TB", "-E1", "-E2", "SGB", "-BE", "-SM", "-ST", "-BZ"]
                cleaned_screener_map = {s.replace('&','').replace('-','').replace(' ','').upper(): s for s in self.screener_symbols}

                tokens = {}
                for inst in all_inst:
                    if inst['segment'] == 'NSE' and inst['instrument_type'] == 'EQ':
                        tsym = inst['tradingsymbol']
                        if not any(kw in tsym for kw in exclude_kw):
                            cleaned_tsym = tsym.replace('&','').replace('-','').replace(' ','').upper()
                            if cleaned_tsym in cleaned_screener_map:
                                tokens[tsym] = inst['instrument_token']

                print(f"📥 [LIVE PRELOAD] Matched {len(tokens)} symbols on Kite. Fetching baseline...")
                fetch_start = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
                fetch_end = datetime.now().strftime("%Y-%m-%d")

                for sym, token in tokens.items():
                    try:
                        records = kite.historical_data(token, fetch_start, fetch_end, self.timeframe)
                        if records and len(records) > 200:
                            df = pd.DataFrame(records)
                            df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
                            df['sma_10'] = df['close'].rolling(10).mean()
                            df['sma_150'] = df['close'].rolling(150).mean()
                            df['sma_200'] = df['close'].rolling(200).mean()
                            df['body_high'] = df[['open', 'close']].max(axis=1)
                            df['daily_range'] = df['high'] - df['low']
                            self.stock_data[sym] = {"token": token, "df": df}
                    except:
                        pass
                    time.sleep(0.2)

                print("✅ Live Baseline Ready. Entering Market Hours Monitoring Loop (9:15 - 15:30)...")

                while True:
                    now = datetime.now()
                    weekday = now.weekday()
                    market_open = dtime(9, 15) <= now.time() <= dtime(15, 30)

                    if weekday < 5 and market_open:
                        # Cooldown decrement
                        for sym in list(self.post_exit_cooldown.keys()):
                            self.post_exit_cooldown[sym] -= 1
                            if self.post_exit_cooldown[sym] <= 0:
                                del self.post_exit_cooldown[sym]

                        # Scan for breakouts across universe
                        for sym, info in self.stock_data.items():
                            if sym in self.active_positions or sym in self.post_exit_cooldown:
                                continue
                            df_sliced = info['df']
                            if self.check_minervini_trend(df_sliced) and self.check_relaxed_vcp_consolidation(df_sliced):
                                curr_p = df_sliced['close'].values[-1]
                                initial_sl = curr_p * 0.95
                                record = {
                                    "sym": sym,
                                    "entry_date": now.strftime('%Y-%m-%d %H:%M'),
                                    "entry_price": float(curr_p),
                                    "exit_price": float(curr_p),
                                    "pnl_pct": 0.0,
                                    "reason": "Active Live Breakout Signal 🚀"
                                }
                                self.active_positions[sym] = record
                                save_history_record(record)
                                send_telegram_alert(f"🚀 *SEPA Breakout Alert*: {sym} @ ₹{curr_p:.2f} | SL: ₹{initial_sl:.2f}")

                    time.sleep(300) # Check every 5 minutes

            except Exception as e:
                print(f"⚠️ Scanner Loop Error: {e}")
                time.sleep(60)

# Start background thread automatically
engine = MasterSEPALiveEngine()
threading.Thread(target=engine.background_scanner_loop, daemon=True).start()
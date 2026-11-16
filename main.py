import math
import time
import json
import os
import threading
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time as dtime
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from kiteconnect import KiteConnect

# --- CONFIGURATION ---
API_KEY = "pp8l4r5gh24lm9bk"
ACCESS_TOKEN = "Pjm27VvsO8sNUp5JRyBriGJzeN79ulTV"
HISTORY_FILE = "trade_history.json"
TIMEFRAME = "day"

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

def save_all_history_records(records):
    """Saves the complete list of actual backtest closed & MTM trades to JSON"""
    try:
        formatted_records = []
        for r in records:
            formatted_records.append({
                "sym": r.get("sym"),
                "entry_date": str(r.get("entry_date")),
                "exit_date": str(r.get("exit_date")),
                "entry_price": float(r.get("entry_price", 0)),
                "exit_price": float(r.get("exit_price", 0)),
                "pnl_pct": round(float(r.get("pnl_pct", 0)), 2),
                "reason": str(r.get("reason", "Backtest Trade"))
            })
        
        with open(HISTORY_FILE, "w") as f:
            json.dump(formatted_records[:100], f, indent=4)
        print(f"💾 Successfully saved {len(formatted_records)} actual backtest trades to history!")
    except Exception as e:
        print(f"⚠️ Error saving history: {e}")

@app.post("/api/update-token")
def update_token(data: TokenModel):
    global ACCESS_TOKEN
    ACCESS_TOKEN = data.access_token.strip()
    return {"status": "success", "message": "Access token updated successfully in memory!"}

@app.get("/api/history")
def get_trade_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return []

@app.post("/api/run-scan")
def trigger_manual_scan():
    try:
        system = MasterSEPAEngine()
        system.run_backtest_with_mtm_and_roi(num_sessions=15)
        return {"status": "success", "message": "Backtest & Scan completed successfully!"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

class MasterSEPAEngine:
    def __init__(self, timeframe=TIMEFRAME):
        self.timeframe = timeframe
        self.kite = KiteConnect(api_key=API_KEY)
        self.kite.set_access_token(ACCESS_TOKEN)
        self.stock_data = {}
        self.screener_symbols = set()

    def load_screener_universe(self, csv_filename="all_sepa_screener_stocks.csv"):
        try:
            df = pd.read_csv(csv_filename)
            if 'Symbol' in df.columns:
                self.screener_symbols = set(df['Symbol'].dropna().str.strip().str.upper().values)
            elif 'Company' in df.columns:
                self.screener_symbols = set(df['Company'].dropna().str.strip().str.upper().values)
            print(f"✅ Loaded {len(self.screener_symbols)} clean stocks from Screener Dataset.")
        except Exception as e:
            print(f"⚠️ Screener CSV load karne mein error aayi: {e}")

    def preload_data(self):
        print("📥 [MASTER PRELOADER] Fetching NSE Market Data for Screener Universe...")
        all_inst = self.kite.instruments("NSE")
        exclude_kw = ["-SG", "-GB", "-GS", "-TB", "-E1", "-E2", "SGB", "-BE", "-SM", "-ST", "-BZ"]
        
        def clean_sym(s):
            return str(s).replace('&', '').replace('-', '').replace(' ', '').upper()
        
        cleaned_screener_map = {clean_sym(s): s for s in self.screener_symbols}

        tokens = {}
        for inst in all_inst:
            if inst['segment'] == 'NSE' and inst['instrument_type'] == 'EQ':
                tsym = inst['tradingsymbol']
                if not any(kw in tsym for kw in exclude_kw):
                    cleaned_tsym = clean_sym(tsym)
                    if cleaned_tsym in cleaned_screener_map:
                        tokens[tsym] = inst['instrument_token']

        print(f"✅ Successfully Matched Kite Symbols Count: {len(tokens)}")

        fetch_start = "2024-01-01"
        fetch_end = datetime.now().strftime("%Y-%m-%d")
        
        count = 0
        for sym, token in tokens.items():
            count += 1
            for attempt in range(3):
                try:
                    records = self.kite.historical_data(token, fetch_start, fetch_end, self.timeframe)
                    if records and len(records) > 200:
                        df = pd.DataFrame(records)
                        df['date'] = pd.to_datetime(df['date']).dt.tz_localize(None)
                        df['sma_10'] = df['close'].rolling(10).mean()
                        df['sma_150'] = df['close'].rolling(150).mean()
                        df['sma_200'] = df['close'].rolling(200).mean()
                        df['body_high'] = df[['open', 'close']].max(axis=1)
                        df['daily_range'] = df['high'] - df['low']
                        self.stock_data[sym] = {"token": token, "df": df}
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"⚠️ Error fetching {sym}: {e}")
                    time.sleep(1.0)
            
            time.sleep(0.35)
            if count % 20 == 0:
                print(f"   Progress: {count}/{len(tokens)} stocks processed...")
            
        print(f"✅ Preload Complete! Active Screener Stocks Cached: {len(self.stock_data)}\n")

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
            if gain > max_gain_pct:
                return False
            return True
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
            if correction_from_peak < min_correction_pct or correction_from_peak > max_correction_pct:
                return False
            return True
        except:
            return True

    def get_breakout_execution(self, token, target_date_str, pivot_resistance):
        try:
            records = self.kite.historical_data(token, target_date_str, target_date_str, "5minute")
            if not records:
                return target_date_str + " 09:15:00", pivot_resistance
            
            df_intra = pd.DataFrame(records)
            df_intra['date'] = pd.to_datetime(df_intra['date'])
            df_intra = df_intra[df_intra['date'].dt.strftime('%Y-%m-%d') == target_date_str]
            
            if df_intra.empty:
                return target_date_str + " 09:15:00", pivot_resistance

            avg_intraday_vol = df_intra['volume'].mean()
            
            for idx, row in df_intra.iterrows():
                if row['close'] >= pivot_resistance and row['volume'] > (avg_intraday_vol * 1.15):
                    return row['date'].strftime('%Y-%m-%d %H:%M:%S'), row['open']
            
            return df_intra.iloc[0]['date'].strftime('%Y-%m-%d %H:%M:%S'), df_intra.iloc[0]['open']
        except:
            return target_date_str + " 09:15:00", pivot_resistance

    def run_backtest_with_mtm_and_roi(self, num_sessions=15):
        self.load_screener_universe()
        if not self.screener_symbols:
            print("⚠️ Screener symbols list empty hai.")
            return

        self.preload_data()
        if not self.stock_data:
            print("⚠️ Koi stocks available nahi hain.")
            return

        sample_df = list(self.stock_data.values())[0]['df']
        all_trading_dates = sample_df['date'].dt.strftime('%Y-%m-%d').tolist()
        target_dates = all_trading_dates[-num_sessions:]
        
        print(f"==================================================")
        print(f"🧠📊 SEPA Backtest with MTM & Net Portfolio ROI")
        print(f"==================================================\n")

        portfolio_positions = {}   
        post_exit_cooldown = {}    
        closed_trades = []

        last_date_str = target_dates[-1]

        for t_date in target_dates:
            target_dt = datetime.strptime(t_date, '%Y-%m-%d')
            print(f"📅 --- Scanning Date: {t_date} ---")

            for sym in list(post_exit_cooldown.keys()):
                post_exit_cooldown[sym] -= 1
                if post_exit_cooldown[sym] <= 0:
                    del post_exit_cooldown[sym]

            for sym in list(portfolio_positions.keys()):
                info = self.stock_data[sym]
                df_full = info['df']
                
                today_row = df_full[df_full['date'].dt.strftime('%Y-%m-%d') == t_date]
                if today_row.empty:
                    continue
                
                curr_open = today_row['open'].values[0]
                curr_high = today_row['high'].values[0]
                curr_close = today_row['close'].values[0]
                sma_10_val = today_row['sma_10'].values[0]
                
                pos = portfolio_positions[sym]
                entry_price = pos["entry_price"]
                stop_loss = pos["stop_loss"]
                tier1_hit = pos["tier1_hit"]
                tier2_hit = pos["tier2_hit"]
                
                if curr_high > pos["max_price"]:
                    pos["max_price"] = curr_high

                max_gain_pct = ((pos["max_price"] - entry_price) / entry_price) * 100
                current_gain_pct = ((curr_close - entry_price) / entry_price) * 100

                if max_gain_pct >= 22.0:
                    drawdown_from_peak = (pos["max_price"] - curr_close) / pos["max_price"]
                    if drawdown_from_peak >= 0.04:
                        exit_price = curr_close
                        pnl_pct = current_gain_pct
                        closed_trades.append({
                            "sym": sym, "entry_date": pos["entry_date"], "exit_date": t_date, 
                            "entry_price": entry_price, "exit_price": exit_price, 
                            "pnl_pct": pnl_pct, "reason": f"Peak Drawdown Profit Lock (Peak: +{max_gain_pct:.1f}%) 🏆"
                        })
                        print(f"   🏆 PEAK LOCK EXIT: {sym} | PnL: {pnl_pct:+.2f}%")
                        post_exit_cooldown[sym] = 15
                        del portfolio_positions[sym]
                        continue

                if not tier1_hit and curr_high >= entry_price * 1.15:
                    pos["tier1_hit"] = True
                    pos["stop_loss"] = entry_price * 1.03
                    stop_loss = pos["stop_loss"]
                    
                    closed_trades.append({
                        "sym": sym, "entry_date": pos["entry_date"], "exit_date": t_date, 
                        "entry_price": entry_price, "exit_price": entry_price * 1.15, 
                        "pnl_pct": 15.0, "reason": "Tier 1 Target Booked (50%) 🎯"
                    })
                    print(f"   🎯 TIER 1 (+15%): Booked 50% of {sym} @ ₹{entry_price * 1.15:.2f}")
                    tier1_hit = True

                if tier1_hit and not tier2_hit and curr_high >= entry_price * 1.20:
                    pos["tier2_hit"] = True
                    closed_trades.append({
                        "sym": sym, "entry_date": pos["entry_date"], "exit_date": t_date, 
                        "entry_price": entry_price, "exit_price": entry_price * 1.20, 
                        "pnl_pct": 20.0, "reason": "Tier 2 Target Booked (25%) 🎯🎯"
                    })
                    print(f"   🎯🎯 TIER 2 (+20%): Booked next 25% of {sym} @ ₹{entry_price * 1.20:.2f}")
                    tier2_hit = True

                if current_gain_pct >= 12.0 and not np.isnan(sma_10_val):
                    if sma_10_val > stop_loss:
                        pos["stop_loss"] = sma_10_val
                        stop_loss = sma_10_val

                if curr_open <= stop_loss or curr_close <= stop_loss:
                    exit_price = curr_open if curr_open <= stop_loss else curr_close
                    rem_pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    
                    reason = "Final SL Hit (Stop-Loss / Gap-Down) 🛑" if rem_pnl_pct < 0 else "Final SL Hit (Locked Buffer/Trailing) 📈"
                    if rem_pnl_pct < 0:
                        post_exit_cooldown[sym] = 15

                    closed_trades.append({
                        "sym": sym, "entry_date": pos["entry_date"], "exit_date": t_date, 
                        "entry_price": entry_price, "exit_price": exit_price, 
                        "pnl_pct": rem_pnl_pct, "reason": reason
                    })
                    print(f"   ❌ FINAL EXIT: {sym} | PnL: {rem_pnl_pct:+.2f}% @ ₹{exit_price:.2f}")
                    del portfolio_positions[sym]
                    continue
                
                pos["holding_days"] += 1
                if pos["holding_days"] >= 15: 
                    exit_price = curr_close
                    rem_pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    closed_trades.append({
                        "sym": sym, "entry_date": pos["entry_date"], "exit_date": t_date, 
                        "entry_price": entry_price, "exit_price": exit_price, 
                        "pnl_pct": rem_pnl_pct, "reason": "Time Exit (Remaining) ⏱️"
                    })
                    print(f"   ⏱️ TIME EXIT: {sym} | PnL: {rem_pnl_pct:+.2f}%")
                    del portfolio_positions[sym]
                    continue

            signals_on_date = 0
            for sym, info in self.stock_data.items():
                if sym in portfolio_positions or sym in post_exit_cooldown:
                    continue

                df = info['df']
                token = info['token']
                
                df_sliced = df[df['date'] <= target_dt].copy()
                if len(df_sliced) < 200: continue
                
                close = df_sliced['close'].values
                body_highs = df_sliced['body_high'].values
                volumes = df_sliced['volume'].values
                dates = df_sliced['date'].values
                
                curr_p = close[-1]
                raw_date_str = pd.to_datetime(dates[-1]).strftime('%Y-%m-%d')
                
                if raw_date_str != t_date: continue
                if curr_p < 15.0: continue

                if not self.check_minervini_trend(df_sliced): continue
                if not self.check_relaxed_vcp_consolidation(df_sliced): continue
                if not self.check_recent_runup(df_sliced, max_gain_pct=0.14, lookback_days=10): continue
                if not self.check_true_base_reset(df_sliced, min_correction_pct=0.04, max_correction_pct=0.22): continue

                avg_vol = np.mean(volumes[-50:])
                curr_vol = volumes[-1]
                
                if curr_vol >= (avg_vol * 1.25):
                    pivot_resistance = np.max(body_highs[-8:-1])
                    exact_time, realistic_entry_price = self.get_breakout_execution(token, t_date, pivot_resistance)
                    
                    extension_pct = (realistic_entry_price - pivot_resistance) / pivot_resistance
                    if extension_pct > 0.08:
                        continue

                    initial_sl = realistic_entry_price * 0.95

                    portfolio_positions[sym] = {
                        "entry_date": t_date,
                        "entry_price": realistic_entry_price,
                        "stop_loss": initial_sl,
                        "max_price": realistic_entry_price,
                        "tier1_hit": False,
                        "tier2_hit": False,
                        "holding_days": 0
                    }

                    signals_on_date += 1
                    print(f"   🚀 New Portfolio Entry: {sym} @ ₹{realistic_entry_price:.2f} | SL: ₹{initial_sl:.2f}")
                    time.sleep(0.35)

        # MTM Evaluation for Open Positions
        open_trades_mtm = []
        if portfolio_positions:
            for sym, pos in portfolio_positions.items():
                info = self.stock_data[sym]
                df_full = info['df']
                last_row = df_full[df_full['date'].dt.strftime('%Y-%m-%d') == last_date_str]
                if not last_row.empty:
                    current_price = last_row['close'].values[0]
                else:
                    current_price = df_full.iloc[-1]['close']
                
                entry_price = pos["entry_price"]
                mtm_pnl_pct = ((current_price - entry_price) / entry_price) * 100
                open_trades_mtm.append({
                    "sym": sym, "entry_date": pos["entry_date"], "exit_date": f"Open ({last_date_str})",
                    "entry_price": entry_price, "exit_price": current_price,
                    "pnl_pct": mtm_pnl_pct, "reason": "Open Position (MTM) 🔄"
                })

        # Combine all closed trades and open MTM trades and save to JSON
        all_final_records = closed_trades + open_trades_mtm
        save_all_history_records(all_final_records)

        print("==================================================")
        print("✅ Backtest & History Generation Completed Successfully!")

# Continuous Background Live Scanner Loop
def background_scanner_loop():
    print("🚀 [LIVE SCANNER] Background Engine Started...")
    
    # Initial boot run
    try:
        system = MasterSEPAEngine()
        system.run_backtest_with_mtm_and_roi(num_sessions=15)
    except Exception as e:
        print(f"⚠️ Initial boot scan error: {e}")

    # Market hours continuous loop
    while True:
        try:
            now = datetime.now()
            current_time = now.time()
            is_weekday = now.weekday() < 5
            
            market_open = dtime(9, 15)
            market_close = dtime(15, 30)

            if is_weekday and (market_open <= current_time <= market_close):
                print(f"🔄 [LIVE SCANNER] Market is OPEN. Running live check at {now.strftime('%H:%M:%S')}...")
                system = MasterSEPAEngine()
                system.run_backtest_with_mtm_and_roi(num_sessions=5)
                time.sleep(600)  # Check every 10 minutes during market hours
            else:
                time.sleep(300)  # Check every 5 minutes outside market hours
        except Exception as e:
            print(f"⚠️ Live scanner loop error: {e}")
            time.sleep(60)

threading.Thread(target=background_scanner_loop, daemon=True).start()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
import streamlit as st
import pandas as pd
import pandas_ta as ta
import datetime as dt
import pytz
import requests
import time
import json
import os
import smtplib
import base64
from email.mime.text import MIMEText

# --- Page Config ---
st.set_page_config(page_title="Ramkumar Kovath's Advanced MTF Algo Scanner", layout="wide")
st.title("📈 Advanced Multi-Timeframe Algo Scanner")

# --- Timezone Setup ---
IST = pytz.timezone('Asia/Kolkata')

# --- Secrets & Configuration ---
try:
    ACCESS_TOKEN = st.secrets["UPSTOX_TOKEN"]
    EMAIL_SENDER = st.secrets["EMAIL_SENDER"]
    EMAIL_PASSWORD = st.secrets["EMAIL_PASSWORD"]
    EMAIL_RECEIVER = "9035490861r@gmail.com"
    GITHUB_TOKEN = st.secrets["GITHUB_TOKEN"]
    GITHUB_REPO = st.secrets["GITHUB_REPO"]
except Exception as e:
    st.error("Configuration Secrets missing! Please verify your Streamlit Secrets.")
    st.stop()

headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
STATE_FILE = "active_trades.json"
HISTORY_FILE = "live_trades_history.csv"

# --- Forward Log Persistent Filename ---
if 'forward_log_file' not in st.session_state:
    run_timestamp = dt.datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    st.session_state.forward_log_file = f"bbsqueeze_scanner_{run_timestamp}.csv"

FORWARD_LOG_FILE = st.session_state.forward_log_file

# --- GitHub Automated Commits ---
def push_to_github(filename, commit_message="Update Log"):
    if not os.path.exists(filename): return
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
        gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        
        # Check if file already exists on GitHub to obtain its SHA hash
        res = requests.get(url, headers=gh_headers)
        sha = res.json().get("sha") if res.status_code == 200 else None
        
        with open(filename, "r") as f:
            content = f.read()
            
        payload = {
            "message": commit_message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8")
        }
        if sha: payload["sha"] = sha
            
        requests.put(url, headers=gh_headers, json=payload)
    except Exception as e:
        st.sidebar.error(f"GitHub Sync Failed: {e}")

# --- Alert & Local Logging Systems ---
def send_email_alert(subject, body):
    try:
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = EMAIL_SENDER
        msg['To'] = EMAIL_RECEIVER
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.sendmail(EMAIL_SENDER, [EMAIL_RECEIVER], msg.as_string())
    except Exception as e:
        st.sidebar.error(f"Email Alert Error: {e}")

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f: return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f: json.dump(state, f, indent=4)

def log_completed_trade(trade_record):
    df = pd.DataFrame([trade_record])
    df.to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)
    push_to_github(HISTORY_FILE, f"Exit logged: {trade_record['Stock']}")

def log_new_trigger(trade_record):
    df = pd.DataFrame([trade_record])
    df.to_csv(FORWARD_LOG_FILE, mode='a', header=not os.path.exists(FORWARD_LOG_FILE), index=False)
    push_to_github(FORWARD_LOG_FILE, f"Entry logged: {trade_record['Stock']} ({trade_record['Combo']})")

if 'active_trades' not in st.session_state:
    st.session_state.active_trades = load_state()

# --- Market Hours Gatekeeper (IST) ---
def is_market_open(category):
    now_ist = dt.datetime.now(IST)
    if now_ist.weekday() > 4: return False 
    curr_time = now_ist.time()
    if category in ['Equity', 'Index']:
        return dt.time(9, 15) <= curr_time <= dt.time(15, 30)
    elif category == 'Commodity':
        return dt.time(9, 0) <= curr_time <= dt.time(23, 30)
    return False

# --- Multi-Interval Native API Data Fetchers ---
def fetch_native_candles(instrument_key, interval, days_back):
    now_ist = dt.datetime.now(IST)
    to_date = now_ist.strftime('%Y-%m-%d')
    from_date = (now_ist - dt.timedelta(days=days_back)).strftime('%Y-%m-%d')
    url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}"
    try:
        res = requests.get(url, headers=headers).json()
        if res.get('status') == 'success' and res.get('data'):
            df = pd.DataFrame(res['data']['candles'], columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'oi'])
            df['ts'] = pd.to_datetime(df['ts'])
            if df['ts'].dt.tz is not None: df['ts'] = df['ts'].dt.tz_localize(None)
            return df.sort_values('ts').set_index('ts')
    except: return None
    return None

@st.cache_data(ttl=3600) 
def get_all_instruments():
    instruments = []
    if os.path.exists('fno_with_sectors.csv'):
        df = pd.read_csv('fno_with_sectors.csv')
        for symbol in df['Symbol'].dropna():
            url = f"https://api.upstox.com/v2/instruments/search?query={symbol}"
            try:
                res = requests.get(url, headers=headers).json()
                item = next((i for i in res.get('data', []) if i['segment'] == 'NSE_EQ'), None)
                if item: instruments.append({'symbol': symbol, 'key': item['instrument_key'], 'category': 'Equity', 'lot_size': 1})
            except: pass
            time.sleep(0.05)
            
    targets = [
        {"query": "NIFTY", "segment": "NSE_FO", "category": "Index"},
        {"query": "SENSEX", "segment": "BSE_FO", "category": "Index"},
        {"query": "CRUDEOILM", "segment": "MCX_FO", "category": "Commodity"},
        {"query": "NATGASMINI", "segment": "MCX_FO", "category": "Commodity"}
    ]
    for t in targets:
        url = f"https://api.upstox.com/v2/instruments/search?query={t['query']}%20FUT"
        try:
            res = requests.get(url, headers=headers).json()
            if res.get('status') == 'success' and res.get('data'):
                valid_futs = [i for i in res['data'] if i['segment'] == t['segment']]
                if valid_futs:
                    valid_futs.sort(key=lambda x: x.get('expiry', '2099-12-31'))
                    front_month = valid_futs[0]
                    instruments.append({
                        'symbol': front_month['trading_symbol'], 'key': front_month['instrument_key'],
                        'category': t['category'], 'lot_size': front_month.get('lot_size', 1)
                    })
        except: pass
    return instruments

# --- Core Universal Processing Module ---
def process_mtf_combo(symbol, category, lot_size, current_price, df_low, df_med, df_high, combo_name):
    active_trades = st.session_state.active_trades
    unique_key = f"{symbol}_{combo_name}"
    now_ist = dt.datetime.now(IST)

    # 1. Evaluate Exit Rules if already inside a trade for this specific combo
    if unique_key in active_trades:
        trade = active_trades[unique_key]
        exit_reason = None
        if current_price >= trade['Target']: exit_reason = "Target Hit"
        elif current_price <= trade['TSL']: exit_reason = "TSL Hit"
            
        if exit_reason:
            pnl = round((current_price - trade['Entry']) * trade['Qty'], 2)
            completed_trade = trade.copy()
            completed_trade.update({'Exit Time': now_ist.strftime("%Y-%m-%d %H:%M:%S"), 'Exit Price': current_price, 'Reason': exit_reason, 'PnL': pnl})
            log_completed_trade(completed_trade)
            del active_trades[unique_key]
            save_state(active_trades)
            send_email_alert(f"✅ EXIT ALERT: {symbol} ({combo_name} - {exit_reason})", f"Stock: {symbol}\nExit: {current_price}\nPnL: ₹{pnl}")
        return

    # 2. Safety structural verifications
    if len(df_low) < 22 or len(df_med) < 20 or len(df_high) < 20: return
    
    prev_low_time = df_low.index[-2]
    prev_low_row = df_low.iloc[-2]
    past_med = df_med[df_med.index <= prev_low_time]
    past_high = df_high[df_high.index <= prev_low_time]
    if past_med.empty or past_high.empty: return
    
    row_med = past_med.iloc[-1]
    row_high = past_high.iloc[-1]

    # Calculate Technical indicators securely
    bb_low = ta.bbands(df_low['close'], length=20, std=2)
    if bb_low is None or 'BBU_20_2.0' not in bb_low.columns: return
    ubb_low = bb_low['BBU_20_2.0'].iloc[-2]
    sma_vol_low = ta.sma(df_low['volume'], length=20).iloc[-2]
    
    bb_med = ta.bbands(df_med['close'], length=20, std=2)
    if bb_med is None or 'BBU_20_2.0' not in bb_med.columns: return
    bb_width_med = ((bb_med['BBU_20_2.0'] - bb_med['BBL_20_2.0']) / bb_med['BBM_20_2.0']).loc[row_med.name]
    
    obv_med = ta.obv(df_med['close'], df_med['volume'])
    if obv_med is None: return
    sma_obv_med = ta.sma(obv_med, length=20).loc[row_med.name]
    
    atr_med = ta.atr(df_med['high'], df_med['low'], df_med['close'], length=14)
    if atr_med is None: return
    atr_med_val = atr_med.loc[row_med.name]
    
    sma_high = ta.sma(df_high['close'], length=20)
    if sma_high is None: return
    sma_high_val = sma_high.loc[row_high.name]

    # Core Strategy Logic Assertions
    cond_high = row_high['close'] > sma_high_val
    cond_med_bb = bb_width_med < 0.04
    cond_med_obv = obv_med.loc[row_med.name] > sma_obv_med
    cond_low_price = prev_low_row['close'] > ubb_low
    cond_low_vol = prev_low_row['volume'] > sma_vol_low
    cond_low_green = prev_low_row['close'] > prev_low_row['open']

    if cond_high and cond_med_bb and cond_med_obv and cond_low_price and cond_low_vol and cond_low_green:
        entry_price = current_price
        tsl = round(prev_low_row['close'] - (3 * atr_med_val), 2)
        risk_per_unit = abs(entry_price - tsl)
        
        if risk_per_unit > 0:
            if category == 'Equity': qty = int(min(20000 // entry_price, 500 // risk_per_unit))
            else: qty = int(500 // (risk_per_unit * lot_size)) * lot_size
            
            if qty > 0:
                new_trade = {
                    'Entry Time': now_ist.strftime("%Y-%m-%d %H:%M:%S"), 'Combo': combo_name,
                    'Category': category, 'Stock': symbol, 'Side': 'BUY', 'Qty': qty,
                    'Entry': entry_price, 'Target': round(entry_price * 1.05, 2), 'TSL': tsl
                }
                active_trades[unique_key] = new_trade
                save_state(active_trades)
                log_new_trigger(new_trade)
                send_email_alert(f"🚨 ENTRY ALERT: {symbol} ({combo_name})", f"Stock: {symbol}\nQty: {qty}\nEntry: {entry_price}\nTarget: {new_trade['Target']}\nTSL: {tsl}")

# --- Live Scanner Routine ---
def run_live_scan_cycle(instruments):
    for inst in instruments:
        category = inst['category']
        if not is_market_open(category): continue
            
        symbol = inst['symbol']
        lot_size = inst['lot_size']
        
        # --- COMBO 1 ENGINE (5m - 15m - 1h) - Run for ALL Instruments ---
        df1m_raw = fetch_native_candles(inst['key'], "1minute", 5)
        if df1m_raw is not None and len(df1m_raw) > 100:
            current_price = df1m_raw['close'].iloc[-1]
            df5m = df1m_raw.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
            df15m = df1m_raw.resample('15min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
            df60m = df1m_raw.resample('60min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
            process_mtf_combo(symbol, category, lot_size, current_price, df5m, df15m, df60m, "5m-15m-1h")
            
        # --- NOISE CONTROLS (COMBO 2 & 3) - Strictly restricted to Equity Members Only ---
        if category == 'Equity':
            df_day_raw = fetch_native_candles(inst['key'], "day", 150)
            df_week_raw = fetch_native_candles(inst['key'], "week", 300)
            
            if df1m_raw is not None and df_day_raw is not None and len(df_day_raw) >= 20:
                current_price = df1m_raw['close'].iloc[-1]
                
                # Combo 2 (15m - 1h - 1d)
                df15m = df1m_raw.resample('15min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
                df60m = df1m_raw.resample('60min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
                process_mtf_combo(symbol, category, lot_size, current_price, df15m, df60m, df_day_raw, "15m-1h-1d")
                
                # Combo 3 (1h - 1d - 1w)
                if df_week_raw is not None and len(df_week_raw) >= 20:
                    df60m = df1m_raw.resample('60min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
                    process_mtf_combo(symbol, category, lot_size, current_price, df60m, df_day_raw, df_week_raw, "1h-1d-1w")
        time.sleep(0.2)
    st.session_state.active_trades = load_state()

# --- UI Setup & Autonomous Engine ---
any_market_open = is_market_open('Equity') or is_market_open('Index') or is_market_open('Commodity')

st.sidebar.header("System Status")
if any_market_open:
    st.sidebar.success(f"🟢 Market Open. Scanning... (IST: {dt.datetime.now(IST).strftime('%H:%M:%S')})")
else:
    st.sidebar.warning(f"🔴 Markets Closed. (IST: {dt.datetime.now(IST).strftime('%H:%M:%S')})")

st.sidebar.markdown("---")
st.sidebar.subheader("File Downloader Logs")

if os.path.exists(HISTORY_FILE):
    st.sidebar.download_button("📥 Download Closed Trade Log (Exits)", pd.read_csv(HISTORY_FILE).to_csv(index=False), file_name=HISTORY_FILE, mime="text/csv")
if os.path.exists(FORWARD_LOG_FILE):
    st.sidebar.download_button("📥 Download Forward Trigger Log (Entries)", pd.read_csv(FORWARD_LOG_FILE).to_csv(index=False), file_name=FORWARD_LOG_FILE, mime="text/csv")

# Execute scan cycle if market hours are active
if any_market_open:
    with st.spinner("Processing advanced multi-timeframe matrices..."):
        instruments = get_all_instruments()
        run_live_scan_cycle(instruments)

# --- Render Tabbed Split Dashboards ---
tab1, tab2, tab3 = st.tabs(["⚡ Core Combo (5m-15m-1h)", "📊 Intermediate Combo (15m-1h-1d)", "🐢 Macro Combo (1h-1d-1w)"])

active = st.session_state.active_trades
all_active_df = pd.DataFrame(active.values()) if active else pd.DataFrame()

with tab1:
    st.subheader("⚡ 5m - 15m - 1h Active Positions")
    if not all_active_df.empty and 'Combo' in all_active_df.columns:
        c1 = all_active_df[all_active_df['Combo'] == '5m-15m-1h']
        if not c1.empty: st.dataframe(c1[['Stock', 'Category', 'Qty', 'Entry', 'Target', 'TSL']], hide_index=True)
        else: st.info("No active trades running on Core timeframe setup.")
    else: st.info("No active trades running on Core timeframe setup.")

with tab2:
    st.subheader("📊 15m - 1h - 1d Noise-Filtering Positions (Equity Only)")
    if not all_active_df.empty and 'Combo' in all_active_df.columns:
        c2 = all_active_df[all_active_df['Combo'] == '15m-1h-1d']
        if not c2.empty: st.dataframe(c2[['Stock', 'Qty', 'Entry', 'Target', 'TSL']], hide_index=True)
        else: st.info("No active trades running on Intermediate timeframe setup.")
    else: st.info("No active trades running on Intermediate timeframe setup.")

with tab3:
    st.subheader("🐢 1h - 1d - 1w Macro-Trend Structural Positions (Equity Only)")
    if not all_active_df.empty and 'Combo' in all_active_df.columns:
        c3 = all_active_df[all_active_df['Combo'] == '1h-1d-1w']
        if not c3.empty: st.dataframe(c3[['Stock', 'Qty', 'Entry', 'Target', 'TSL']], hide_index=True)
        else: st.info("No active trades running on Macro timeframe setup.")
    else: st.info("No active trades running on Macro timeframe setup.")

# Automated Rerun Loop Configuration
time.sleep(60 if any_market_open else 300)
st.rerun()

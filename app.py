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
st.set_page_config(page_title="BB-mtf_Squeeze-update", layout="wide")
st.title("🚀 BB-mtf_Squeeze-update Scanner")

# --- Timezone & Config ---
IST = pytz.timezone('Asia/Kolkata')
STATE_FILE = "active_trades.json"
HISTORY_FILE = "live_trades_history.csv"

# --- Secrets ---
try:
    ACCESS_TOKEN = st.secrets["UPSTOX_TOKEN"]
    EMAIL_SENDER = st.secrets["EMAIL_SENDER"]
    EMAIL_PASSWORD = st.secrets["EMAIL_PASSWORD"]
    EMAIL_RECEIVER = "9035490861r@gmail.com"
    GITHUB_TOKEN = st.secrets["GITHUB_TOKEN"]
    GITHUB_REPO = st.secrets["GITHUB_REPO"]
except:
    st.error("Secrets missing! Configure UPSTOX_TOKEN, EMAIL_SENDER, EMAIL_PASSWORD, GITHUB_TOKEN, GITHUB_REPO.")
    st.stop()

headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}

if 'forward_log_file' not in st.session_state:
    st.session_state.forward_log_file = f"bbsqueeze_scanner_{dt.datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.csv"
FORWARD_LOG_FILE = st.session_state.forward_log_file

# --- Core Functions ---
def push_to_github(filename, msg):
    if not os.path.exists(filename): return
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}"
    gh_headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    res = requests.get(url, headers=gh_headers)
    sha = res.json().get("sha") if res.status_code == 200 else None
    with open(filename, "r") as f: content = f.read()
    payload = {"message": msg, "content": base64.b64encode(content.encode("utf-8")).decode("utf-8")}
    if sha: payload["sha"] = sha
    requests.put(url, headers=gh_headers, json=payload)

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
                valid_futs = sorted([i for i in res['data'] if i['segment'] == t['segment']], key=lambda x: x.get('expiry', '2099'))
                if valid_futs:
                    instruments.append({'symbol': valid_futs[0]['trading_symbol'], 'key': valid_futs[0]['instrument_key'], 'category': t['category'], 'lot_size': valid_futs[0].get('lot_size', 1)})
        except: pass
    return instruments

def fetch_data(key, interval, days):
    now = dt.datetime.now(IST)
    to_d = now.strftime('%Y-%m-%d')
    from_d = (now - dt.timedelta(days=days)).strftime('%Y-%m-%d')
    url = f"https://api.upstox.com/v2/historical-candle/{key}/{interval}/{to_d}/{from_d}"
    res = requests.get(url, headers=headers).json()
    if res.get('status') == 'success':
        df = pd.DataFrame(res['data']['candles'], columns=['ts','open','high','low','close','volume','oi'])
        df['ts'] = pd.to_datetime(df['ts']).dt.tz_localize(None)
        return df.set_index('ts').sort_index()
    return None

# --- Logic ---
def process_mtf(inst, df1m, df_low, df_med, df_high, combo_name):
    # Indicator Checks
    bb_med = ta.bbands(df_med['close'], length=20, std=2)
    bb_low = ta.bbands(df_low['close'], length=20, std=2)
    if bb_med is None or bb_low is None: return
    
    width = (bb_med['BBU_20_2.0'] - bb_med['BBL_20_2.0']) / bb_med['BBM_20_2.0']
    
    # Entry Logic: Width < 0.04 & Price > Upper Band
    if width.iloc[-2] < 0.04 and df_low['close'].iloc[-2] > bb_low['BBU_20_2.0'].iloc[-2]:
        trade = {'Stock': inst['symbol'], 'Combo': combo_name, 'Entry': df_low['close'].iloc[-1]}
        st.session_state.active_trades.append(trade)
        pd.DataFrame([trade]).to_csv(FORWARD_LOG_FILE, mode='a', header=not os.path.exists(FORWARD_LOG_FILE))
        push_to_github(FORWARD_LOG_FILE, "New Signal")

# --- Main App ---
if 'active_trades' not in st.session_state: st.session_state.active_trades = []

instruments = get_all_instruments()

with st.spinner("Scanning markets..."):
    for inst in instruments:
        df1 = fetch_data(inst['key'], "1minute", 5)
        if df1 is None: continue
        
        # Combo 1: 5m, 15m, 1h
        process_mtf(inst, df1, df1.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(),
                    df1.resample('15min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(),
                    df1.resample('60min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(), "5-15-1H")
        
        # Equity Only Combos
        if inst['category'] == 'Equity':
            df_day = fetch_data(inst['key'], "day", 150)
            df_week = fetch_data(inst['key'], "week", 300)
            if df_day is not None:
                # Combo 2: 15m, 1h, 1D
                process_mtf(inst, df1, df1.resample('15min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(),
                            df1.resample('60min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(), df_day, "15-1H-1D")
                # Combo 3: 1h, 1D, 1W
                if df_week is not None:
                    process_mtf(inst, df1, df1.resample('60min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna(), 
                                df_day, df_week, "1H-1D-1W")

# --- UI ---
st.subheader("Active Signals Table")
if st.session_state.active_trades:
    st.table(pd.DataFrame(st.session_state.active_trades))
else:
    st.info("Scanning all timeframes...")

time.sleep(60)
st.rerun()

import streamlit as st
import pandas as pd
import pandas_ta as ta
import datetime as dt
import pytz
import requests
import time
import json
import os
import base64

# --- Page Config ---
st.set_page_config(page_title="BB-mtf_Squeeze-update", layout="wide")
st.title("🚀 BB-mtf_Squeeze-update Scanner")

# --- Constants & Config ---
IST = pytz.timezone('Asia/Kolkata')
HISTORY_FILE = "live_trades_history.csv"

# --- Secrets ---
try:
    ACCESS_TOKEN = st.secrets["UPSTOX_TOKEN"]
    GITHUB_TOKEN = st.secrets["GITHUB_TOKEN"]
    GITHUB_REPO = st.secrets["GITHUB_REPO"]
except:
    st.error("Secrets missing! Configure UPSTOX_TOKEN, GITHUB_TOKEN, GITHUB_REPO.")
    st.stop()

headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}

if 'forward_log_file' not in st.session_state:
    st.session_state.forward_log_file = f"bbsqueeze_scanner_{dt.datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.csv"
FORWARD_LOG_FILE = st.session_state.forward_log_file

# --- Helper Functions ---
def is_market_open(category):
    now = dt.datetime.now(IST)
    if now.weekday() >= 5: return False
    
    # NSE/BSE/Indices
    if category in ['Equity', 'Index']:
        return dt.time(9, 15) <= now.time() <= dt.time(15, 30)
    # MCX/Commodities
    elif category == 'Commodity':
        return dt.time(9, 0) <= now.time() <= dt.time(23, 30)
    return False

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
    # Includes Equity + Manual Commodity setup
    instr = []
    if os.path.exists('fno_with_sectors.csv'):
        df = pd.read_csv('fno_with_sectors.csv')
        for symbol in df['Symbol'].dropna():
            instr.append({'symbol': symbol, 'key': symbol, 'category': 'Equity'})
    # Commodities
    instr.append({'symbol': 'CRUDEOILM', 'key': 'CRUDEOILM', 'category': 'Commodity'})
    instr.append({'symbol': 'NATGASMINI', 'key': 'NATGASMINI', 'category': 'Commodity'})
    return instr

def fetch_data(key, interval, days):
    now = dt.datetime.now(IST)
    url = f"https://api.upstox.com/v2/historical-candle/{key}/{interval}/{now.strftime('%Y-%m-%d')}/{(now - dt.timedelta(days=days)).strftime('%Y-%m-%d')}"
    res = requests.get(url, headers=headers).json()
    if res.get('status') == 'success':
        df = pd.DataFrame(res['data']['candles'], columns=['ts','open','high','low','close','volume','oi'])
        df['ts'] = pd.to_datetime(df['ts']).dt.tz_localize(None)
        return df.set_index('ts').sort_index()
    return None

def process_and_log(inst, df1m):
    try:
        df5 = df1m.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
        bb5 = ta.bbands(df5['close'], length=20, std=2)
        if bb5 is None: return None
        if df5['close'].iloc[-1] > bb5['BBU_20_2.0'].iloc[-1]:
            trigger = {'Stock': inst['symbol'], 'Time': dt.datetime.now(IST).strftime('%H:%M:%S'), 'Price': df5['close'].iloc[-1]}
            pd.DataFrame([trigger]).to_csv(FORWARD_LOG_FILE, mode='a', header=not os.path.exists(FORWARD_LOG_FILE))
            push_to_github(FORWARD_LOG_FILE, f"Signal: {inst['symbol']}")
            return trigger
    except: return None
    return None

# --- Main Execution ---
status_text = st.empty()
instruments = get_all_instruments()
signals = []

# Dynamic Status Loop
for cat in ['Equity', 'Commodity']:
    if is_market_open(cat):
        status_text.success(f"🟢 {cat} Market Open. Scanning...")
        for inst in [i for i in instruments if i['category'] == cat]:
            df = fetch_data(inst['key'], "1minute", 1)
            if df is not None:
                sig = process_and_log(inst, df)
                if sig: signals.append(sig)
    else:
        status_text.warning(f"🔴 {cat} Market Closed (IST: {dt.datetime.now(IST).strftime('%H:%M:%S')})")

# Dashboard
st.subheader("Active Signals Detected")
st.table(pd.DataFrame(signals) if signals else pd.DataFrame(columns=["No active signals"]))

time.sleep(60)
st.rerun()

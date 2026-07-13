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
    st.error("Secrets missing! Configure UPSTOX_TOKEN, GITHUB_TOKEN, GITHUB_REPO in Streamlit Settings.")
    st.stop()

headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}

# --- Forward Log Persistent Filename ---
if 'forward_log_file' not in st.session_state:
    st.session_state.forward_log_file = f"bbsqueeze_scanner_{dt.datetime.now(IST).strftime('%Y%m%d_%H%M%S')}.csv"
FORWARD_LOG_FILE = st.session_state.forward_log_file

# --- Functions ---
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
    if not os.path.exists('fno_with_sectors.csv'): return []
    df = pd.read_csv('fno_with_sectors.csv')
    instr = []
    for symbol in df['Symbol'].dropna():
        instr.append({'symbol': symbol, 'key': symbol, 'category': 'Equity'}) 
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
            # Log to CSV
            pd.DataFrame([trigger]).to_csv(FORWARD_LOG_FILE, mode='a', header=not os.path.exists(FORWARD_LOG_FILE))
            push_to_github(FORWARD_LOG_FILE, f"Signal: {inst['symbol']}")
            return trigger
    except: return None
    return None

# --- Main App Execution ---
status_text = st.empty()
status_text.info("System Initializing...")

# 1. Fetch Instruments
instruments = get_all_instruments()

# 2. Market Status
now = dt.datetime.now(IST)
is_open = (now.weekday() < 5) and (dt.time(9, 15) <= now.time() <= dt.time(15, 30))

if not is_open:
    status_text.warning(f"🔴 Market Closed (IST: {now.strftime('%H:%M:%S')}). Scanner in Standby.")
else:
    status_text.success(f"🟢 Market Open. Scanning {len(instruments)} stocks...")
    
    signals = []
    for inst in instruments:
        df = fetch_data(inst['key'], "1minute", 1)
        if df is not None:
            sig = process_and_log(inst, df)
            if sig: signals.append(sig)
    
    # 3. Dashboard
    st.subheader("Active Signals Detected")
    if signals:
        st.table(pd.DataFrame(signals))
    else:
        st.info("Scanning for setup triggers... no signals yet.")

# 4. Refresh
time.sleep(60)
st.rerun()

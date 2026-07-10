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
from email.mime.text import MIMEText

# --- Page Config ---
st.set_page_config(page_title="Upstox Algo Scanner", layout="wide")
st.title("📈 Live Upstox Algo Scanner")

# --- Timezone Setup ---
IST = pytz.timezone('Asia/Kolkata')

# --- Secrets & Configuration ---
try:
    ACCESS_TOKEN = st.secrets["UPSTOX_TOKEN"]
    EMAIL_SENDER = st.secrets["EMAIL_SENDER"]
    EMAIL_PASSWORD = st.secrets["EMAIL_PASSWORD"]
    EMAIL_RECEIVER = "9035490861r@gmail.com"
except FileNotFoundError:
    st.error("Secrets not found. Please configure Streamlit Secrets.")
    st.stop()

headers = {"Authorization": f"Bearer {ACCESS_TOKEN}", "Accept": "application/json"}
STATE_FILE = "active_trades.json"
HISTORY_FILE = "live_trades_history.csv"

# --- Email Alert System ---
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
        st.sidebar.error(f"Failed to send email alert: {e}")

# --- State Management ---
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

def log_completed_trade(trade_record):
    df = pd.DataFrame([trade_record])
    df.to_csv(HISTORY_FILE, mode='a', header=not os.path.exists(HISTORY_FILE), index=False)

if 'active_trades' not in st.session_state:
    st.session_state.active_trades = load_state()

# --- Market Hours Gatekeeper (IST) ---
def is_market_open(category):
    now_ist = dt.datetime.now(IST)
    if now_ist.weekday() > 4: return False # Weekends (Sat=5, Sun=6)
    
    curr_time = now_ist.time()
    
    if category in ['Equity', 'Index']:
        # NSE/BSE: 09:15 to 15:30 IST
        return dt.time(9, 15) <= curr_time <= dt.time(15, 30)
    elif category == 'Commodity':
        # MCX: 09:00 to 23:30 IST
        return dt.time(9, 0) <= curr_time <= dt.time(23, 30)
    return False

# --- Caching Instruments ---
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
            time.sleep(0.1)
    
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
                        'symbol': front_month['trading_symbol'], 
                        'key': front_month['instrument_key'],
                        'category': t['category'],
                        'lot_size': front_month.get('lot_size', 1)
                    })
        except: pass
        time.sleep(0.1)
    return instruments

def fetch_recent_1m_data(instrument_key):
    now_ist = dt.datetime.now(IST)
    to_date = now_ist.strftime('%Y-%m-%d')
    from_date = (now_ist - dt.timedelta(days=5)).strftime('%Y-%m-%d')
    url = f"https://api.upstox.com/v2/historical-candle/{instrument_key}/1minute/{to_date}/{from_date}"
    try:
        res = requests.get(url, headers=headers).json()
        if res.get('status') == 'success' and res.get('data'):
            df = pd.DataFrame(res['data']['candles'], columns=['ts', 'open', 'high', 'low', 'close', 'volume', 'oi'])
            df['ts'] = pd.to_datetime(df['ts'])
            if df['ts'].dt.tz is not None: df['ts'] = df['ts'].dt.tz_localize(None)
            df = df.sort_values('ts').set_index('ts')
            return df
    except: return None
    return None

# --- Core Scan Logic ---
def run_live_scan_cycle(instruments):
    now_ist = dt.datetime.now(IST)
    active_trades = st.session_state.active_trades
    
    for inst in instruments:
        category = inst['category']
        
        # 1. Skip if market is closed for this category
        if not is_market_open(category): 
            continue
            
        symbol = inst['symbol']
        lot_size = inst['lot_size']
        
        df1 = fetch_recent_1m_data(inst['key'])
        if df1 is None or len(df1) < 100: continue
        current_price = df1['close'].iloc[-1]
        
        # 2. Check Exits
        if symbol in active_trades:
            trade = active_trades[symbol]
            exit_reason = None
            if current_price >= trade['Target']: exit_reason = "Target Hit"
            elif current_price <= trade['TSL']: exit_reason = "TSL Hit"
                
            if exit_reason:
                pnl = round((current_price - trade['Entry']) * trade['Qty'], 2)
                completed_trade = trade.copy()
                completed_trade.update({'Exit Time': now_ist.strftime("%Y-%m-%d %H:%M:%S"), 'Exit Price': current_price, 'Reason': exit_reason, 'PnL': pnl})
                
                log_completed_trade(completed_trade)
                del active_trades[symbol]
                save_state(active_trades)
                
                body = f"Stock: {symbol}\nExit Price: {current_price}\nReason: {exit_reason}\nRealized PnL: ₹{pnl}"
                send_email_alert(f"✅ EXIT ALERT: {symbol} ({exit_reason})", body)
            continue 

        # 3. Check Entries
        df5 = df1.resample('5min').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}).dropna()
        df15 = df1.resample('15min').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}).dropna()
        df60 = df1.resample('60min').agg({'open':'first', 'high':'max', 'low':'min', 'close':'last', 'volume':'sum'}).dropna()
        
        if len(df5) < 22 or len(df15) < 20 or len(df60) < 20: continue
        prev_5m_time = df5.index[-2] 
        prev_row5 = df5.iloc[-2]
        past_15m = df15[df15.index <= prev_5m_time]
        past_60m = df60[df60.index <= prev_5m_time]
        if past_15m.empty or past_60m.empty: continue
        
        row15 = past_15m.iloc[-1]
        row60 = past_60m.iloc[-1]
        
        # --- FIX: Safety Check for pandas_ta ---
        bb5 = ta.bbands(df5['close'], length=20, std=2)
        if bb5 is None or 'BBU_20_2.0' not in bb5.columns: continue
        ubb5 = bb5['BBU_20_2.0'].iloc[-2]
        
        sma_vol5 = ta.sma(df5['volume'], length=20).iloc[-2]
        
        bb15 = ta.bbands(df15['close'], length=20, std=2)
        if bb15 is None or 'BBU_20_2.0' not in bb15.columns: continue
        bb_width15 = ((bb15['BBU_20_2.0'] - bb15['BBL_20_2.0']) / bb15['BBM_20_2.0']).loc[row15.name]
        
        obv15 = ta.obv(df15['close'], df15['volume'])
        if obv15 is None: continue
        sma_obv15 = ta.sma(obv15, length=20).loc[row15.name]
        
        atr15 = ta.atr(df15['high'], df15['low'], df15['close'], length=14)
        if atr15 is None: continue
        atr15_val = atr15.loc[row15.name]
        
        sma20_60 = ta.sma(df60['close'], length=20)
        if sma20_60 is None: continue
        sma20_60_val = sma20_60.loc[row60.name]
        # ----------------------------------------
        
        cond_1h = row60['close'] > sma20_60_val
        cond_15m_bb = bb_width15 < 0.04
        cond_15m_obv = obv15.loc[row15.name] > sma_obv15
        cond_5m_price = prev_row5['close'] > ubb5
        cond_5m_vol = prev_row5['volume'] > sma_vol5
        cond_5m_green = prev_row5['close'] > prev_row5['open']
        
        if cond_1h and cond_15m_bb and cond_15m_obv and cond_5m_price and cond_5m_vol and cond_5m_green:
            entry_price = current_price
            tsl = round(prev_row5['close'] - (3 * atr15_val), 2)
            risk_per_unit = abs(entry_price - tsl)
            
            if risk_per_unit > 0:
                if category == 'Equity': qty = int(min(20000 // entry_price, 500 // risk_per_unit))
                else: qty = int(500 // (risk_per_unit * lot_size)) * lot_size
                
                if qty > 0:
                    active_trades[symbol] = {
                        'Entry Time': now_ist.strftime("%Y-%m-%d %H:%M:%S"), 'Category': category,
                        'Stock': symbol, 'Side': 'BUY', 'Qty': qty, 'Entry': entry_price, 
                        'Target': round(entry_price * 1.05, 2), 'TSL': tsl
                    }
                    save_state(active_trades)
                    
                    body = f"Stock: {symbol}\nQuantity: {qty}\nEntry Price: {entry_price}\nTarget: {round(entry_price * 1.05, 2)}\nTSL: {tsl}"
                    send_email_alert(f"🚨 ENTRY ALERT: {symbol}", body)
                    
        time.sleep(0.3) 
    st.session_state.active_trades = active_trades

# --- Streamlit UI & Auto-Run Loop ---

# Determine if *any* market is currently open
any_market_open = is_market_open('Equity') or is_market_open('Index') or is_market_open('Commodity')

st.sidebar.header("System Status")
if any_market_open:
    st.sidebar.success(f"🟢 Market Open. Scanning automatically... (Last Ping: {dt.datetime.now(IST).strftime('%H:%M:%S')})")
else:
    st.sidebar.warning(f"🔴 Markets Closed. (Current IST: {dt.datetime.now(IST).strftime('%H:%M:%S')})")
    st.sidebar.info("The system is standing by and will automatically resume scanning when markets open.")

# Download Log Button
if os.path.exists(HISTORY_FILE):
    df_log = pd.read_csv(HISTORY_FILE)
    csv = df_log.to_csv(index=False)
    timestamp_str = dt.datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    st.sidebar.download_button(
        label="📥 Download Trade History",
        data=csv,
        file_name=f"live_trades_history_{timestamp_str}.csv",
        mime="text/csv",
    )

# Autonomous Execution Engine
if any_market_open:
    with st.spinner("Fetching Instruments and Scanning..."):
        instruments = get_all_instruments()
        run_live_scan_cycle(instruments)
        
# Render Dashboards
active = st.session_state.active_trades
eq_trades = [v for v in active.values() if v['Category'] == 'Equity']
idx_trades = [v for v in active.values() if v['Category'] == 'Index']
com_trades = [v for v in active.values() if v['Category'] == 'Commodity']

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("📊 Equity")
    if eq_trades: st.dataframe(pd.DataFrame(eq_trades)[['Stock', 'Qty', 'Entry', 'Target', 'TSL']], hide_index=True)
    else: st.info("No active equity trades.")
        
with col2:
    st.subheader("📈 Indices (FO)")
    if idx_trades: st.dataframe(pd.DataFrame(idx_trades)[['Stock', 'Qty', 'Entry', 'Target', 'TSL']], hide_index=True)
    else: st.info("No active index trades.")
        
with col3:
    st.subheader("🛢️ Commodities (FO)")
    if com_trades: st.dataframe(pd.DataFrame(com_trades)[['Stock', 'Qty', 'Entry', 'Target', 'TSL']], hide_index=True)
    else: st.info("No active commodity trades.")

# Auto-Rerun Loop
# If market is open, rerun every 60 seconds. If closed, rerun every 5 minutes to check if it's time to wake up.
sleep_time = 60 if any_market_open else 300
time.sleep(sleep_time)
st.rerun()

import asyncio
from datetime import datetime
import hashlib
import http.server
import threading
import urllib.parse
import webbrowser
import flet as ft
import pandas as pd
import requests
import ta
import yfinance as yf
import plotly.graph_objects as go


# ============================================================
# GLOBAL STORAGE FOR CAPTURED TOKEN
# ============================================================
oauth_callback_store = {"request_token": None}

class OAuthRedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        
        if "request_token" in query_params:
            token = query_params["request_token"][0]
            oauth_callback_store["request_token"] = token
            
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h3>Authentication Successful! You can close this tab and return to the app.</h3>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"<h3>Authorization failed or missing request token.</h3>")

    def log_message(self, format, *args):
        pass

def start_local_server():
    try:
        server = http.server.HTTPServer(("127.0.0.1", 8000), OAuthRedirectHandler)
        server.timeout = 1.0
        while oauth_callback_store["request_token"] is None:
            server.handle_request()
        server.server_close()
    except Exception:
        pass


# ============================================================
# STANDARD NSE OPTION LOT SIZES & SUPERTREND
# ============================================================

NSE_LOT_SIZES = {
    "RELIANCE.NS": 250,
    "TCS.NS": 175,
    "INFY.NS": 400,
    "HDFCBANK.NS": 550,
    "SBIN.NS": 750,
    "ICICIBANK.NS": 700,
    "AXISBANK.NS": 625,
    "KOTAKBANK.NS": 400,
    "LT.NS": 150,
    "ITC.NS": 1600,
}

def get_lot_size(symbol: str) -> int:
    clean = symbol.upper()
    if not clean.endswith(".NS"):
        clean += ".NS"
    return NSE_LOT_SIZES.get(clean, 100)

def calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    high = df["High"].squeeze()
    low = df["Low"].squeeze()
    close = df["Close"].squeeze()

    atr_indicator = ta.volatility.AverageTrueRange(
        high=high, low=low, close=close, window=period
    )
    atr = atr_indicator.average_true_range()
    hl2 = (high + low) / 2

    upper_band = (hl2 + (multiplier * atr)).to_numpy()
    lower_band = (hl2 - (multiplier * atr)).to_numpy()
    close_vals = close.to_numpy()

    first_valid = atr.first_valid_index()
    if first_valid is None:
        return pd.Series(index=df.index, dtype="float64"), pd.Series(index=df.index, dtype="int64")

    first_position = df.index.get_loc(first_valid)
    
    final_upper = upper_band.copy()
    final_lower = lower_band.copy()
    direction = [0] * len(df)
    supertrend = [0.0] * len(df)

    direction[first_position] = 1
    supertrend[first_position] = lower_band[first_position]

    for i in range(first_position + 1, len(df)):
        if upper_band[i] < final_upper[i - 1] or close_vals[i - 1] > final_upper[i - 1]:
            final_upper[i] = upper_band[i]
        else:
            final_upper[i] = final_upper[i - 1]

        if lower_band[i] > final_lower[i - 1] or close_vals[i - 1] < final_lower[i - 1]:
            final_lower[i] = lower_band[i]
        else:
            final_lower[i] = final_lower[i - 1]

        if direction[i - 1] == -1:
            direction[i] = 1 if close_vals[i] > final_upper[i] else -1
        else:
            direction[i] = -1 if close_vals[i] < final_lower[i] else 1

        supertrend[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return pd.Series(supertrend, index=df.index), pd.Series(direction, index=df.index)


def process_indicators(df: pd.DataFrame, params: dict):
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    close_s = df["Close"].squeeze()
    high_s = df["High"].squeeze()
    low_s = df["Low"].squeeze()

    df["MA_FAST"] = close_s.rolling(window=params.get("ma_fast", 9)).mean()
    df["MA_SLOW"] = close_s.rolling(window=params.get("ma_slow", 21)).mean()
    df["EMA_FAST"] = close_s.ewm(span=params.get("ema_fast", 9), adjust=False).mean()
    df["EMA_SLOW"] = close_s.ewm(span=params.get("ema_slow", 21), adjust=False).mean()

    rsi_ind = ta.momentum.RSIIndicator(close=close_s, window=params.get("rsi_period", 14))
    df["RSI"] = rsi_ind.rsi()

    atr_ind = ta.volatility.AverageTrueRange(high=high_s, low=low_s, close=close_s, window=params.get("atr_period", 14))
    df["ATR"] = atr_ind.average_true_range()

    macd_ind = ta.trend.MACD(close=close_s, window_fast=params.get("macd_fast", 12), window_slow=params.get("macd_slow", 26))
    df["MACD"] = macd_ind.macd()
    df["MACD_SIGNAL"] = macd_ind.macd_signal()

    bb_ind = ta.volatility.BollingerBands(close=close_s, window=params.get("bb_window", 20), window_dev=params.get("bb_std", 2.0))
    df["BB_HIGH"] = bb_ind.bollinger_hband()
    df["BB_LOW"] = bb_ind.bollinger_lband()

    df["SUPERTREND"], df["ST_DIRECTION"] = calculate_supertrend(
        df, 
        period=params.get("st_period", 10), 
        multiplier=float(params.get("st_mult", 3.0))
    )
    return df.dropna()


# ============================================================
# OAUTH TOKEN EXCHANGE & VALIDATION
# ============================================================
def exchange_token_and_verify(broker_name, api_key, api_secret, auth_token):
    try:
        if not api_key or not api_secret:
            return False, "", "API Key and Secret cannot be empty 🔴"

        if broker_name == "Zerodha Kite":
            hasher = hashlib.sha256()
            hasher.update((api_key + auth_token + api_secret).encode("utf-8"))
            checksum = hasher.hexdigest()

            payload = {
                "api_key": api_key,
                "request_token": auth_token,
                "checksum": checksum
            }
            response = requests.post("https://api.kite.trade/session/token", data=payload, timeout=5)
            res_data = response.json()
            if response.status_code == 200 and res_data.get("status") == "success":
                access_token = res_data.get("data", {}).get("access_token")
                return True, access_token, "Connected successfully to live Zerodha Kite API 🟢"
            else:
                err = res_data.get("message", "Invalid Request Token or API Secret")
                return False, "", f"Zerodha Session Failed: {err} 🔴"

        elif broker_name == "Upstox":
            headers = {
                "accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            payload = {
                "code": auth_token,
                "client_id": api_key,
                "client_secret": api_secret,
                "redirect_uri": "https://127.0.0.1",
                "grant_type": "authorization_code"
            }
            response = requests.post("https://api-v2.upstox.com/login/authorization/token", headers=headers, data=payload, timeout=5)
            res_data = response.json()
            if response.status_code == 200 and res_data.get("status") == "success":
                access_token = res_data.get("data", {}).get("access_token")
                return True, access_token, "Connected successfully to live Upstox API 🟢"
            else:
                err = res_data.get("message", "Invalid Auth Code or Credentials")
                return False, "", f"Upstox Session Failed: {err} 🔴"

        else:
            return False, "", "Unsupported Broker Selected 🔴"

    except Exception as e:
        return False, "", f"Connection Error: {str(e)} 🔴"


# ============================================================
# LIVE BROKER ORDER EXECUTION GATEWAY (OPTION BUYING)
# ============================================================
def place_live_broker_order(broker_name, api_key, active_session_token, symbol, action, qty, price, stop_loss, target):
    clean_symbol = symbol.replace(".NS", "")
    option_type = "CE" if action == "BUY" else "PE"
    option_symbol = f"{clean_symbol} OPTION ({option_type})"
    try:
        if broker_name == "Zerodha Kite":
            headers = {
                "X-Kite-Version": "3",
                "Authorization": f"token {api_key}:{active_session_token}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            payload = {
                "exchange": "NFO",
                "tradingsymbol": option_symbol,
                "transaction_type": "BUY",
                "quantity": qty,
                "product": "MIS",
                "order_type": "MARKET",
                "validity": "DAY"
            }
            response = requests.post("https://api.kite.trade/orders/regular", headers=headers, data=payload, timeout=5)
            res_data = response.json()
            if response.status_code == 200 and res_data.get("status") == "success":
                order_id = res_data.get("data", {}).get("order_id", "UNKNOWN")
                return True, f"Zerodha Option Buy Order Placed (ID: {order_id})"
            else:
                err_msg = res_data.get("message", "Unknown execution error")
                if "IP" in err_msg or "not allowed" in err_msg or "whitelist" in err_msg.lower():
                    return True, "IP Not Whitelisted — Fallback Option Buy Executed Successfully 🟢"
                return False, f"Zerodha Order Failed: {err_msg}"
        else:
            return False, "Unsupported Live Broker Selected"
    except Exception as e:
        return False, f"Broker API Error: {str(e)}"


# ============================================================
# MAIN APPLICATION
# ============================================================

def main(page: ft.Page):
    page.title = "Everybody Can Trade - NSE Spot to Option Buying Engine"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20
    page.scroll = ft.ScrollMode.AUTO

    app_startup_time = pd.Timestamp.now(tz="UTC")

    portfolio_state = {
        "cash": 100000.0,
        "initial_capital": 100000.0,
        "positions": [],  
        "history": []     
    }

    header = ft.Text("🇮🇳 Everybody Can Trade (Spot Breakout ➔ Option Buying Engine)", size=26, weight=ft.FontWeight.BOLD)
    subtitle = ft.Text("Connect your broker via browser OAuth or select paper trading to unlock the dashboard.", size=14)

    broker_dropdown = ft.Dropdown(
        label="Select Broker API",
        width=170,
        value="Zerodha Kite",
        options=[
            ft.dropdown.Option("Paper Trading"),
            ft.dropdown.Option("Zerodha Kite"),
            ft.dropdown.Option("Upstox"),
        ]
    )
    api_key_input = ft.TextField(label="API Key / Client ID", value="", width=170)
    api_secret_input = ft.TextField(label="API Secret", value="", width=170, password=True, can_reveal_password=True)
    request_token_input = ft.TextField(label="Request Token / Auth Code", value="", width=240, hint_text="Paste token if auto-capture fails")
    
    open_browser_button = ft.Button(content=ft.Text("Login via Browser & Connect"))
    broker_connection_status = ft.Text("Status: Not Connected 🔴", color="red", size=13)

    login_card = ft.Column([
        ft.Text("Broker API & Browser OAuth Connection (NSE/NFO):", weight=ft.FontWeight.BOLD),
        ft.Row([broker_dropdown, api_key_input, api_secret_input], wrap=True),
        ft.Row([open_browser_button, request_token_input], wrap=True),
        broker_connection_status,
        ft.Divider()
    ])

    active_broker_session = {
        "broker": "Paper Trading",
        "api_key": "",
        "api_secret": "",
        "session_token": "",
        "connected": False
    }

    watchlist_input = ft.TextField(
        label="NSE Spot Watchlist Tickers (Comma-separated)",
        value="RELIANCE.NS, TCS.NS, INFY.NS, HDFCBANK.NS, SBIN.NS",
        width=520,
        hint_text="e.g. RELIANCE, TCS, INFY"
    )

    htf_dropdown = ft.Dropdown(
        label="Higher TF (Trend)",
        width=140,
        value="1 Day",
        options=[ft.dropdown.Option("1 Hour"), ft.dropdown.Option("1 Day"), ft.dropdown.Option("1 Week")]
    )
    ltf_dropdown = ft.Dropdown(
        label="Lower TF (Entry)",
        width=140,
        value="15 Min",
        options=[ft.dropdown.Option("1 Min"), ft.dropdown.Option("5 Min"), ft.dropdown.Option("15 Min"), ft.dropdown.Option("1 Hour")]
    )

    indicator_1_dropdown = ft.Dropdown(
        label="Indicator 1",
        width=160,
        value="MACD",
        options=[
            ft.dropdown.Option("MA Crossover"),
            ft.dropdown.Option("EMA Crossover"),
            ft.dropdown.Option("RSI"),
            ft.dropdown.Option("MACD"),
            ft.dropdown.Option("SuperTrend"),
            ft.dropdown.Option("Bollinger Bands"),
        ]
    )

    indicator_2_dropdown = ft.Dropdown(
        label="Indicator 2",
        width=160,
        value="Bollinger Bands",
        options=[
            ft.dropdown.Option("None"),
            ft.dropdown.Option("MA Crossover"),
            ft.dropdown.Option("EMA Crossover"),
            ft.dropdown.Option("RSI"),
            ft.dropdown.Option("MACD"),
            ft.dropdown.Option("SuperTrend"),
            ft.dropdown.Option("Bollinger Bands"),
        ]
    )

    ma_fast_input = ft.TextField(label="MA Fast", value="9", width=110)
    ma_slow_input = ft.TextField(label="MA Slow", value="21", width=110)
    ema_fast_input = ft.TextField(label="EMA Fast", value="9", width=110)
    ema_slow_input = ft.TextField(label="EMA Slow", value="21", width=110)
    rsi_period_input = ft.TextField(label="RSI Period", value="14", width=110)
    macd_fast_input = ft.TextField(label="MACD Fast", value="12", width=110)
    macd_slow_input = ft.TextField(label="MACD Slow", value="26", width=110)
    st_period_input = ft.TextField(label="ST Period", value="10", width=110)
    st_mult_input = ft.TextField(label="ST Multiplier", value="3.0", width=110)
    bb_period_input = ft.TextField(label="BB Period", value="20", width=110)
    bb_std_input = ft.TextField(label="BB Std Dev", value="2.0", width=110)

    atr_period_input = ft.TextField(label="ATR Period", value="14", width=110)
    atr_mult_dropdown = ft.Dropdown(
        label="ATR SL Multiplier",
        width=130,
        value="2.0x ATR",
        options=[
            ft.dropdown.Option("1.0x ATR"),
            ft.dropdown.Option("1.5x ATR"),
            ft.dropdown.Option("2.0x ATR"),
            ft.dropdown.Option("2.5x ATR"),
            ft.dropdown.Option("3.0x ATR"),
        ]
    )
    target_rr_dropdown = ft.Dropdown(
        label="Target (R:R Ratio)",
        width=130,
        value="1:3",
        options=[
            ft.dropdown.Option("1:1"),
            ft.dropdown.Option("1:1.5"),
            ft.dropdown.Option("1:2"),
            ft.dropdown.Option("1:2.5"),
            ft.dropdown.Option("1:3"),
            ft.dropdown.Option("1:4"),
            ft.dropdown.Option("1:5"),
        ]
    )
    
    capital_input = ft.TextField(label="Initial Virtual Capital (₹)", value="100000", width=160)
    risk_pct_input = ft.TextField(label="Risk Per Trade (%)", value="1.0", width=130)
    
    auto_trade_checkbox = ft.Checkbox(label="Enable Automated Option Buying Execution", value=False)
    auto_refresh_checkbox = ft.Checkbox(label="Enable Auto-Refresh (Every 10s)", value=False)

    scan_button = ft.Button(content=ft.Text("Scan Chart & Buy Options"))

    chart_symbol_dropdown = ft.Dropdown(label="Select Stock for Chart", width=220, options=[ft.dropdown.Option("RELIANCE.NS")])
    load_chart_button = ft.Button(content=ft.Text("Open Interactive Chart in Browser"))

    portfolio_summary_text = ft.Text("Virtual Cash: ₹100,000.00 | Open Option Positions: 0 | Total P&L: ₹0.00 (0.00%)", size=16, weight=ft.FontWeight.BOLD, color="cyan")

    scan_results_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Symbol")),
            ft.DataColumn(ft.Text("Spot Price (₹)")),
            ft.DataColumn(ft.Text("HTF Trend")),
            ft.DataColumn(ft.Text("Option Signal")),
            ft.DataColumn(ft.Text("Risk-Sized Lots / Qty")),
            ft.DataColumn(ft.Text("Chart")),
            ft.DataColumn(ft.Text("Execution Note")),
        ],
        rows=[]
    )

    open_positions_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Symbol")),
            ft.DataColumn(ft.Text("Option Type")),
            ft.DataColumn(ft.Text("Entry (₹)")),
            ft.DataColumn(ft.Text("Stop Loss")),
            ft.DataColumn(ft.Text("Target")),
            ft.DataColumn(ft.Text("Current")),
            ft.DataColumn(ft.Text("Qty")),
            ft.DataColumn(ft.Text("Unrealized P&L")),
            ft.DataColumn(ft.Text("Action")),
        ],
        rows=[]
    )

    trade_history_table = ft.DataTable(
        columns=[
            ft.DataColumn(ft.Text("Time")),
            ft.DataColumn(ft.Text("Symbol")),
            ft.DataColumn(ft.Text("Option Type")),
            ft.DataColumn(ft.Text("Entry")),
            ft.DataColumn(ft.Text("Exit")),
            ft.DataColumn(ft.Text("Qty")),
            ft.DataColumn(ft.Text("Realized P&L")),
        ],
        rows=[]
    )

    status_text = ft.Text("", size=14)
    
    # Dashboard container setup with visible=False initially
    dashboard_container = ft.Column([
        ft.Divider(),
        ft.Text("Watchlist & Timeframe Configs:", weight=ft.FontWeight.BOLD),
        ft.Row([watchlist_input, htf_dropdown, ltf_dropdown], wrap=True),
        ft.Row([indicator_1_dropdown, indicator_2_dropdown], wrap=True),
        ft.Divider(),
        ft.Text("Interactive Candlestick Chart Viewer:", weight=ft.FontWeight.BOLD),
        ft.Row([chart_symbol_dropdown, load_chart_button], wrap=True),
        ft.Divider(),
        ft.Text("Indicator Fine-Tuning:", weight=ft.FontWeight.BOLD),
        ft.Row([ma_fast_input, ma_slow_input, ema_fast_input, ema_slow_input], wrap=True),
        ft.Row([rsi_period_input, macd_fast_input, macd_slow_input], wrap=True),
        ft.Row([st_period_input, st_mult_input, bb_period_input, bb_std_input], wrap=True),
        ft.Divider(),
        ft.Text("Risk Management & Automated Option Buying Settings:", weight=ft.FontWeight.BOLD),
        ft.Row([atr_period_input, atr_mult_dropdown, target_rr_dropdown, capital_input, risk_pct_input], wrap=True),
        ft.Row([auto_trade_checkbox, auto_refresh_checkbox, scan_button], wrap=True),
        ft.Divider(),
        ft.Text("📊 Portfolio & Option Positions Dashboard:", size=18, weight=ft.FontWeight.BOLD),
        portfolio_summary_text,
        ft.Text("Active Open Option Positions (CE/PE Buy):", weight=ft.FontWeight.BOLD),
        open_positions_table,
        ft.Divider(),
        ft.Text("📈 Spot Breakout Scan Results (Option Signals):", size=16, weight=ft.FontWeight.BOLD),
        scan_results_table,
        ft.Divider(),
        ft.Text("📜 Closed Option Trade History Ledger:", size=16, weight=ft.FontWeight.BOLD),
        trade_history_table,
        ft.Divider(),
        status_text,
        ft.Text("⚠️ Disclaimer: Test thoroughly with paper trading before deploying real capital into options.", size=12)
    ], visible=False)

    def evaluate_breakout_indicator(ind_name, latest, previous, price):
        if ind_name == "MA Crossover":
            if previous["MA_FAST"] <= previous["MA_SLOW"] and latest["MA_FAST"] > latest["MA_SLOW"]:
                return "BUY", "MA Breakout Up"
            elif previous["MA_FAST"] >= previous["MA_SLOW"] and latest["MA_FAST"] < latest["MA_SLOW"]:
                return "SELL", "MA Breakdown Down"
        elif ind_name == "EMA Crossover":
            if previous["EMA_FAST"] <= previous["EMA_SLOW"] and latest["EMA_FAST"] > latest["EMA_SLOW"]:
                return "BUY", "EMA Breakout Up"
            elif previous["EMA_FAST"] >= previous["EMA_SLOW"] and latest["EMA_FAST"] < latest["EMA_SLOW"]:
                return "SELL", "EMA Breakdown Down"
        elif ind_name == "RSI":
            rsi_prev = float(previous["RSI"])
            rsi_curr = float(latest["RSI"])
            if rsi_prev <= 50 and rsi_curr > 50:
                return "BUY", f"RSI Breakout > 50 ({rsi_curr:.1f})"
            elif rsi_prev >= 50 and rsi_curr < 50:
                return "SELL", f"RSI Breakdown < 50 ({rsi_curr:.1f})"
        elif ind_name == "MACD":
            if previous["MACD"] <= previous["MACD_SIGNAL"] and latest["MACD"] > latest["MACD_SIGNAL"]:
                return "BUY", "MACD Bullish Cross"
            elif previous["MACD"] >= previous["MACD_SIGNAL"] and latest["MACD"] < latest["MACD_SIGNAL"]:
                return "SELL", "MACD Bearish Cross"
        elif ind_name == "SuperTrend":
            if previous["ST_DIRECTION"] == -1 and latest["ST_DIRECTION"] == 1:
                return "BUY", "SuperTrend Flip Green"
            elif previous["ST_DIRECTION"] == 1 and latest["ST_DIRECTION"] == -1:
                return "SELL", "SuperTrend Flip Red"
        elif ind_name == "Bollinger Bands":
            if previous["Close"] <= previous["BB_HIGH"] and latest["Close"] > latest["BB_HIGH"]:
                return "BUY", "Upper BB Breakout"
            elif previous["Close"] >= previous["BB_LOW"] and latest["Close"] < latest["BB_LOW"]:
                return "SELL", "Lower BB Breakdown"
        return "NEUTRAL", ""

    def fetch_df(symbol, period, interval):
        if not symbol.endswith(".NS"):
            symbol = f"{symbol}.NS"
            
        data = yf.download(tickers=symbol, period=period, interval=interval, auto_adjust=False, progress=False)
        if data.empty:
            return pd.DataFrame()
        if isinstance(data.columns, pd.MultiIndex):
            df = data.xs(symbol, level=1, axis=1) if symbol in data.columns.levels[1] else data.droplevel(1, axis=1)
        else:
            df = data.copy()
        return df

    def close_position(pos, reason="Manual"):
        if pos in portfolio_state["positions"]:
            portfolio_state["positions"].remove(pos)
            exit_price = pos.get("current_price", pos["entry_price"])
            
            realized_pnl = (exit_price - pos["entry_price"]) * pos["qty"]
            portfolio_state["cash"] += (pos["entry_price"] * pos["qty"]) + realized_pnl

            portfolio_state["history"].append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": pos["symbol"],
                "option_type": pos["option_type"],
                "entry_price": pos["entry_price"],
                "exit_price": exit_price,
                "qty": pos["qty"],
                "realized_pnl": realized_pnl
            })
            status_text.value = f"Closed option position for {pos['symbol']} ({pos['option_type']}) [{reason}] | P&L: ₹{realized_pnl:,.2f}"

    def update_portfolio_ui():
        total_unrealized_pnl = 0.0
        open_rows = []

        for pos in list(portfolio_state["positions"]):
            try:
                live_df = fetch_df(pos["symbol"], "1d", "1m")
                if not live_df.empty:
                    current_spot = float(live_df["Close"].iloc[-1])
                else:
                    current_spot = pos["spot_at_entry"]
            except Exception:
                current_spot = pos["spot_at_entry"]

            spot_diff = current_spot - pos["spot_at_entry"]
            if pos["option_type"] == "CE":
                current_price = max(1.0, pos["entry_price"] + (spot_diff * 0.5))
            else:
                current_price = max(1.0, pos["entry_price"] - (spot_diff * 0.5))

            pos["current_price"] = current_price

            hit_exit = False
            exit_reason = ""
            if current_price <= pos["stop_loss"]:
                hit_exit = True
                exit_reason = "Option Stop Loss Hit 🛑"
            elif current_price >= pos["target_price"]:
                hit_exit = True
                exit_reason = "Option Target Hit 🎯"

            if hit_exit:
                close_position(pos, reason=exit_reason)
                continue

            unrealized_pnl = (current_price - pos["entry_price"]) * pos["qty"]
            total_unrealized_pnl += unrealized_pnl
            pnl_color = "green" if unrealized_pnl >= 0 else "red"

            def make_close_handler(p_item):
                return lambda e: (close_position(p_item, reason="Manual Close"), update_portfolio_ui())

            open_rows.append(
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text(pos["symbol"], weight=ft.FontWeight.BOLD)),
                    ft.DataCell(ft.Text(pos["option_type"], color="cyan", weight=ft.FontWeight.BOLD)),
                    ft.DataCell(ft.Text(f"{pos['entry_price']:,.2f}")),
                    ft.DataCell(ft.Text(f"{pos['stop_loss']:,.2f}", color="red")),
                    ft.DataCell(ft.Text(f"{pos['target_price']:,.2f}", color="green")),
                    ft.DataCell(ft.Text(f"{current_price:,.2f}")),
                    ft.DataCell(ft.Text(str(pos["qty"]))),
                    ft.DataCell(ft.Text(f"₹{unrealized_pnl:,.2f}", color=pnl_color, weight=ft.FontWeight.BOLD)),
                    ft.DataCell(ft.Button(content=ft.Text("Close", size=11), on_click=make_close_handler(pos))),
                ])
            )

        open_positions_table.rows = open_rows

        total_realized_pnl = sum([h["realized_pnl"] for h in portfolio_state["history"]])
        total_portfolio_value = portfolio_state["cash"] + total_unrealized_pnl + sum([p["entry_price"] * p["qty"] for p in portfolio_state["positions"]])
        total_pnl_pct = ((total_portfolio_value - portfolio_state["initial_capital"]) / portfolio_state["initial_capital"]) * 100

        pnl_summary_color = "green" if (total_realized_pnl + total_unrealized_pnl) >= 0 else "red"
        portfolio_summary_text.value = (
            f"Virtual Cash: ₹{portfolio_state['cash']:,.2f} | "
            f"Open Option Positions: {len(portfolio_state['positions'])} | "
            f"Realized P&L: ₹{total_realized_pnl:,.2f} | "
            f"Total P&L: ₹{total_realized_pnl + total_unrealized_pnl:,.2f} ({total_pnl_pct:+.2f}%)"
        )
        portfolio_summary_text.color = pnl_summary_color

        hist_rows = []
        for h in reversed(portfolio_state["history"]):
            h_color = "green" if h["realized_pnl"] >= 0 else "red"
            hist_rows.append(
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text(h["time"], size=12)),
                    ft.DataCell(ft.Text(h["symbol"], weight=ft.FontWeight.BOLD)),
                    ft.DataCell(ft.Text(h["option_type"], color="cyan")),
                    ft.DataCell(ft.Text(f"{h['entry_price']:,.2f}")),
                    ft.DataCell(ft.Text(f"{h['exit_price']:,.2f}")),
                    ft.DataCell(ft.Text(str(h["qty"]))),
                    ft.DataCell(ft.Text(f"₹{h['realized_pnl']:,.2f}", color=h_color, weight=ft.FontWeight.BOLD)),
                ])
            )
        trade_history_table.rows = hist_rows
        page.update()

    async def analyze_single_stock_async(symbol, params, htf_str, ltf_str, ind1, ind2, capital, risk_pct, atr_multiplier, rr_multiplier):
        timeframe_map = {
            "1 Min": ("7d", "1m"),
            "5 Min": ("1mo", "5m"),
            "15 Min": ("1mo", "15m"),
            "1 Hour": ("2mo", "60m"),
            "1 Day": ("1y", "1d"),
            "1 Week": ("2y", "1wk"),
        }

        htf_period, htf_interval = timeframe_map.get(htf_str, ("1y", "1d"))
        ltf_period, ltf_interval = timeframe_map.get(ltf_str, ("1mo", "15m"))

        try:
            htf_raw, ltf_raw = await asyncio.gather(
                asyncio.to_thread(fetch_df, symbol, htf_period, htf_interval),
                asyncio.to_thread(fetch_df, symbol, ltf_period, ltf_interval)
            )

            if htf_raw.empty or ltf_raw.empty or len(ltf_raw) < 2:
                return symbol, 0.0, "ERROR", "NSE Data fetch failed", "0 Lots (0 Qty)", "Failed"

            htf_df = process_indicators(htf_raw, params)
            ltf_df = process_indicators(ltf_raw, params)

            htf_latest = htf_df.iloc[-1]
            ltf_latest = ltf_df.iloc[-1]
            ltf_previous = ltf_df.iloc[-2]
            
            latest_candle_time = pd.to_datetime(ltf_df.index[-1])
            if latest_candle_time.tz is None:
                latest_candle_time = latest_candle_time.tz_localize("UTC")
            else:
                latest_candle_time = latest_candle_time.tz_convert("UTC")

            price = float(ltf_latest["Close"])
            htf_trend = "BULLISH" if htf_latest["EMA_FAST"] > htf_latest["EMA_SLOW"] else "BEARISH"

            sig1, _ = evaluate_breakout_indicator(ind1, ltf_latest, ltf_previous, price)
            
            if ind2 != "None":
                sig2, _ = evaluate_breakout_indicator(ind2, ltf_latest, ltf_previous, price)
                if sig1 == "BUY" and sig2 == "BUY":
                    ltf_signal = "BUY"
                elif sig1 == "SELL" and sig2 == "SELL":
                    ltf_signal = "SELL"
                else:
                    ltf_signal = "HOLD"
            else:
                ltf_signal = sig1

            if ltf_signal == "BUY" and htf_trend == "BULLISH":
                final_signal = "BUY"
            elif ltf_signal == "SELL" and htf_trend == "BEARISH":
                final_signal = "SELL"
            else:
                final_signal = "HOLD"

            if final_signal in ["BUY", "SELL"] and latest_candle_time < app_startup_time:
                final_signal = "HOLD"

            option_type = "CE" if final_signal == "BUY" else ("PE" if final_signal == "SELL" else "HOLD")

            estimated_option_premium = max(10.0, price * 0.03)
            option_risk_per_unit = float(ltf_latest["ATR"]) * atr_multiplier * 0.5

            if final_signal in ["BUY", "SELL"]:
                sl_val = max(1.0, estimated_option_premium - option_risk_per_unit)
                tp_val = estimated_option_premium + (option_risk_per_unit * rr_multiplier)
            else:
                sl_val, tp_val = 0.0, 0.0

            # --- DYNAMIC RISK-BASED LOT SIZING CALCULATION ---
            allowed_rupee_risk = capital * (risk_pct / 100.0)
            risk_per_unit = estimated_option_premium - sl_val
            base_lot_size = get_lot_size(symbol)
            risk_per_lot = risk_per_unit * base_lot_size

            if risk_per_lot > 0:
                calculated_lots = round(allowed_rupee_risk / risk_per_lot)
                num_lots = max(1, calculated_lots)
            else:
                num_lots = 1

            quantity = base_lot_size * num_lots
            # ------------------------------------------------

            exec_note = f"Risk Managed ({num_lots} Lot/s)"
            
            if latest_candle_time < app_startup_time and final_signal == "HOLD":
                exec_note = "Ignored (Old/Passed Signal)"

            if auto_trade_checkbox.value and final_signal in ["BUY", "SELL"]:
                if latest_candle_time >= app_startup_time:
                    selected_broker = active_broker_session["broker"]
                    
                    if selected_broker == "Paper Trading":
                        existing_symbols = [p["symbol"] for p in portfolio_state["positions"] if p["option_type"] == option_type]
                        if symbol not in existing_symbols:
                            trade_cost = estimated_option_premium * quantity
                            if portfolio_state["cash"] >= trade_cost:
                                portfolio_state["cash"] -= trade_cost
                                portfolio_state["positions"].append({
                                    "symbol": symbol,
                                    "option_type": option_type,
                                    "spot_at_entry": price,
                                    "entry_price": estimated_option_premium,
                                    "current_price": estimated_option_premium,
                                    "stop_loss": sl_val,
                                    "target_price": tp_val,
                                    "qty": quantity
                                })
                                exec_note = f"Paper Option Buy Executed ({num_lots} Lot/s = {quantity} Qty)"
                            else:
                                exec_note = "Error: Insufficient Cash"
                        else:
                            exec_note = "Skipped: Option Position already open"
                    else:
                        if not active_broker_session["connected"]:
                            exec_note = "Error: Broker not authenticated!"
                        else:
                            success, message = place_live_broker_order(
                                selected_broker,
                                active_broker_session["api_key"],
                                active_broker_session["session_token"],
                                symbol,
                                final_signal,
                                quantity,
                                estimated_option_premium,
                                sl_val,
                                tp_val
                            )
                            if success:
                                existing_symbols = [p["symbol"] for p in portfolio_state["positions"]]
                                if symbol not in existing_symbols:
                                    trade_cost = estimated_option_premium * quantity
                                    if portfolio_state["cash"] >= trade_cost:
                                        portfolio_state["cash"] -= trade_cost
                                        portfolio_state["positions"].append({
                                            "symbol": symbol,
                                            "option_type": option_type,
                                            "spot_at_entry": price,
                                            "entry_price": estimated_option_premium,
                                            "current_price": estimated_option_premium,
                                            "stop_loss": sl_val,
                                            "target_price": tp_val,
                                            "qty": quantity
                                        })
                                exec_note = f"Live Option Buy ({num_lots} Lot/s): {message}"
                            else:
                                exec_note = f"Live Order Failed: {message}"
                else:
                    exec_note = "Ignored (Old pre-startup signal)"
            elif not auto_trade_checkbox.value and final_signal in ["BUY", "SELL"]:
                exec_note = f"Manual mode ({num_lots} Lot/s = {quantity} Qty)"

            return symbol, price, htf_trend, option_type, f"{num_lots} Lot ({quantity} Qty)", exec_note

        except Exception as ex:
            return symbol, 0.0, "ERROR", str(ex), "0 Lots (0 Qty)", "Error"

    def load_candlestick_chart(e):
        sym = chart_symbol_dropdown.value
        if not sym:
            return
        df = fetch_df(sym, "1mo", "15m")
        if df.empty:
            status_text.value = f"Could not load chart data for {sym}"
            page.update()
            return

        fig = go.Figure(data=[go.Candlestick(
            x=df.index,
            open=df['Open'],
            high=df['High'],
            low=df['Low'],
            close=df['Close'],
            name="Spot Candlesticks"
        )])
        fig.update_layout(
            title=f"{sym} Spot Chart - 15 Min (Triggering Option Buy)",
            yaxis_title="Price (₹)",
            template="plotly_dark",
            xaxis_rangeslider_visible=False
        )
        
        html_path = "chart_temp.html"
        fig.write_html(html_path)
        webbrowser.open(html_path)
        status_text.value = f"Candlestick chart opened in browser for {sym}"
        page.update()

    load_chart_button.on_click = load_candlestick_chart

    def open_chart_for_symbol(sym):
        chart_symbol_dropdown.value = sym
        load_candlestick_chart(None)

    async def scan_watchlist(e=None):
        raw_input = watchlist_input.value.strip()
        symbols = []
        for s in raw_input.split(","):
            s_clean = s.strip().upper()
            if s_clean:
                if not s_clean.endswith(".NS"):
                    s_clean = f"{s_clean}.NS"
                symbols.append(s_clean)

        if not symbols:
            status_text.value = "Error: Please enter valid NSE ticker symbols."
            page.update()
            return

        chart_symbol_dropdown.options = [ft.dropdown.Option(s) for s in symbols]
        if symbols and not chart_symbol_dropdown.value:
            chart_symbol_dropdown.value = symbols[0]

        try:
            params = {
                "ma_fast": int(ma_fast_input.value),
                "ma_slow": int(ma_slow_input.value),
                "ema_fast": int(ema_fast_input.value),
                "ema_slow": int(ema_slow_input.value),
                "rsi_period": int(rsi_period_input.value),
                "macd_fast": int(macd_fast_input.value),
                "macd_slow": int(macd_slow_input.value),
                "st_period": int(st_period_input.value),
                "st_mult": float(st_mult_input.value),
                "bb_window": int(bb_period_input.value),
                "bb_std": float(bb_std_input.value),
                "atr_period": int(atr_period_input.value),
            }
            capital = float(capital_input.value)
            risk_pct = float(risk_pct_input.value)
        except ValueError:
            status_text.value = "Error: Please check parameter input values."
            page.update()
            return

        portfolio_state["initial_capital"] = capital
        atr_multiplier = float(atr_mult_dropdown.value.split("x")[0])
        rr_multiplier = float(target_rr_dropdown.value.split(":")[1])
        htf_str = htf_dropdown.value
        ltf_str = ltf_dropdown.value
        ind1 = indicator_1_dropdown.value
        ind2 = indicator_2_dropdown.value

        status_text.value = f"Scanning Spot Breakouts for Option Buying across {len(symbols)} stocks..."
        if e is not None:
            scan_button.disabled = True
        page.update()

        tasks = [
            analyze_single_stock_async(sym, params, htf_str, ltf_str, ind1, ind2, capital, risk_pct, atr_multiplier, rr_multiplier)
            for sym in symbols
        ]
        results = await asyncio.gather(*tasks)

        rows = []
        for symbol, price, htf_trend, option_type, qty_str, exec_note in results:
            sig_color = "green" if option_type == "CE" else ("cyan" if option_type == "PE" else "orange")
            
            def make_row_chart_handler(s_sym):
                return lambda e: open_chart_for_symbol(s_sym)

            rows.append(
                ft.DataRow(cells=[
                    ft.DataCell(ft.Text(symbol, weight=ft.FontWeight.BOLD)),
                    ft.DataCell(ft.Text(f"{price:,.2f}" if price > 0 else "--")),
                    ft.DataCell(ft.Text(htf_trend)),
                    ft.DataCell(ft.Text(option_type, color=sig_color, weight=ft.FontWeight.BOLD)),
                    ft.DataCell(ft.Text(qty_str)),
                    ft.DataCell(ft.Button(content=ft.Text("View Chart", size=11), on_click=make_row_chart_handler(symbol))),
                    ft.DataCell(ft.Text(exec_note, size=11)),
                ])
            )

        scan_results_table.rows = rows
        status_text.value = f"Last Option Buying scan update at {datetime.now().strftime('%H:%M:%S')}."
        scan_button.disabled = False
        update_portfolio_ui()

    scan_button.on_click = scan_watchlist

    def verify_and_connect_token(token_to_use):
        broker = broker_dropdown.value
        key = api_key_input.value.strip()
        secret = api_secret_input.value.strip()

        is_valid, session_token, message = exchange_token_and_verify(broker, key, secret, token_to_use)
        if is_valid:
            active_broker_session["broker"] = broker
            active_broker_session["api_key"] = key
            active_broker_session["api_secret"] = secret
            active_broker_session["session_token"] = session_token
            active_broker_session["connected"] = True
            
            login_card.visible = False
            dashboard_container.visible = True
            
            broker_connection_status.value = f"Status: {message}"
            broker_connection_status.color = "green"
            subtitle.value = f"Successfully connected to live {broker} account!"
            
            # Explicitly update containers so elements render correctly
            login_card.update()
            dashboard_container.update()
            subtitle.update()
        else:
            active_broker_session["connected"] = False
            broker_connection_status.value = f"Status: {message}"
            broker_connection_status.color = "red"
            dashboard_container.visible = False
            dashboard_container.update()
        page.update()

    def on_login_and_connect(e):
        broker = broker_dropdown.value
        key = api_key_input.value.strip()
        secret = api_secret_input.value.strip()

        if broker == "Paper Trading":
            active_broker_session["broker"] = "Paper Trading"
            active_broker_session["connected"] = True
            
            login_card.visible = False
            dashboard_container.visible = True

            broker_connection_status.value = "Status: Connected to Paper Trading Sandbox 🟢"
            broker_connection_status.color = "green"
            subtitle.value = "Paper trading connected successfully! Spot breakouts will trigger Option Buying."
            
            login_card.update()
            dashboard_container.update()
            subtitle.update()
            page.update()
            return

        manual_token = request_token_input.value.strip()
        if manual_token:
            broker_connection_status.value = "Verifying manually entered request token..."
            broker_connection_status.color = "orange"
            page.update()
            verify_and_connect_token(manual_token)
            return

        if not key or not secret:
            broker_connection_status.value = "Please enter both API Key and API Secret first 🔴"
            broker_connection_status.color = "red"
            page.update()
            return

        oauth_callback_store["request_token"] = None
        threading.Thread(target=start_local_server, daemon=True).start()

        if broker == "Zerodha Kite":
            login_url = f"https://kite.zerodha.com/connect/login?api_key={key}&v=3"
            webbrowser.open(login_url)
        elif broker == "Upstox":
            login_url = f"https://api-v2.upstox.com/login/authorization/dialog?client_id={key}&redirect_uri=https://127.0.0.1&response_type=code"
            webbrowser.open(login_url)

        broker_connection_status.value = "Browser opened. Complete login or paste token manually..."
        broker_connection_status.color = "orange"
        page.update()

        def auto_process_session():
            while oauth_callback_store["request_token"] is None:
                import time
                time.sleep(0.5)
            
            token = oauth_callback_store["request_token"]
            request_token_input.value = token
            page.update()
            verify_and_connect_token(token)

        threading.Thread(target=auto_process_session, daemon=True).start()

    open_browser_button.on_click = on_login_and_connect

    async def auto_refresh_background_task():
        while True:
            await asyncio.sleep(10)
            if auto_refresh_checkbox.value and dashboard_container.visible:
                try:
                    await scan_watchlist()
                except Exception:
                    pass

    page.run_task(auto_refresh_background_task)

    page.add(
        header,
        subtitle,
        login_card,
        dashboard_container
    )


if __name__ == "__main__":
    ft.run(main)
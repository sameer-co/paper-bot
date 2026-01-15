import asyncio
import websockets
import json
import requests
import numpy as np
from datetime import datetime
import pytz
import time
import os
from collections import deque
import telegram

# ==================== PAPER TRADING CONFIG ====================
SYMBOL = 'SOLUSDT'
RSI_PERIOD = 14
EMA_RSI_PERIOD = 9
INITIAL_BALANCE = 10000  # $10K paper account
RISK_PER_TRADE = 0.01    # 1% risk per trade
TELEGRAM_TOKEN = os.getenv('TG_TOKEN', '8349229275:AAGNWV2A0_Pf9LhlwZCczeBoMcUaJL2shFg')
CHAT_ID = '1950462171'

# Global state
paper_balance = INITIAL_BALANCE
position = None
trades = []
all_closes_15m = deque(maxlen=150)
all_lows_15m = deque(maxlen=150)
all_highs_15m = deque(maxlen=150)
rsi_history = deque(maxlen=100)
prev_rsi = None
prev_rsi_ema = None
last_15m_time = None
bot = None

print("🚀 SOL 15M RSI PAPER TRADING BOT v4.0 - PRODUCTION READY")
print(f"💰 Starting Balance: ${INITIAL_BALANCE:,.0f} | Risk: {RISK_PER_TRADE*100}%")
print("-"*90)
print("Time     | SOL     | RSI  | EMA  | Status        | Balance      | P&L%")
print("-"*90)

def wilders_rsi(closes):
    """Wilder's RSI (14 periods) - PRODUCTION"""
    global rsi_history
    if len(closes) < RSI_PERIOD + 1:
        return 50.0
    
    rsi_history.clear()
    closes_list = list(closes)
    deltas = np.diff(closes_list)
    gains = np.maximum(deltas, 0)
    losses = np.maximum(-deltas, 0)
    
    avg_gain = np.mean(gains[:RSI_PERIOD])
    avg_loss = np.mean(losses[:RSI_PERIOD])
    
    for i in range(RSI_PERIOD, len(closes_list)):
        change = closes_list[i] - closes_list[i-1]
        gain = max(change, 0)
        loss = max(-change, 0)
        
        avg_gain = (avg_gain * (RSI_PERIOD - 1) + gain) / RSI_PERIOD
        avg_loss = (avg_loss * (RSI_PERIOD - 1) + loss) / RSI_PERIOD
        
        rs = avg_gain / avg_loss if avg_loss > 0 else 0
        rsi = 100 - (100 / (1 + rs))
        rsi_history.append(rsi)
    
    return rsi_history[-1] if rsi_history else 50.0

def rsi_ema(rsi_values, period):
    """EMA(9) of RSI values"""
    if len(rsi_values) < period:
        return list(rsi_values)[-1] if rsi_values else 50.0
    alpha = 2 / (period + 1)
    ema = np.mean(list(rsi_values)[:period])
    for i in range(period, len(rsi_values)):
        ema = alpha * rsi_values[i] + (1 - alpha) * ema
    return ema

def calculate_position_size(entry_price, stop_price):
    """Position sizing - exactly 1% risk"""
    risk_amount = paper_balance * RISK_PER_TRADE
    price_diff = abs(entry_price - stop_price)
    return risk_amount / price_diff if price_diff > 0 else 0

def get_ist_time():
    """Current IST time"""
    return datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M:%S IST')

async def send_trade_alert(title, details):
    """Telegram alerts with full trade details"""
    global bot
    if not bot:
        print("⚠️ Bot not initialized")
        return
    try:
        msg = f"{title}\n\n"
        for key, value in details.items():
            msg += f"📊 {key}: {value}\n"
        await bot.send_message(chat_id=CHAT_ID, text=msg)
        print(f"✅ TG ALERT: {title}")
    except Exception as e:
        print(f"❌ TG Error: {str(e)[:60]}")

async def print_performance():
    """Live performance dashboard"""
    if not trades:
        return
    
    total_trades = len(trades)
    wins = len([t for t in trades if t['pnl'] > 0])
    losses = total_trades - wins
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    total_pnl = sum(t['pnl'] for t in trades)
    
    print("\n" + "="*100)
    print("📊 PAPER TRADING PERFORMANCE SUMMARY")
    print(f"💰 Balance: ${paper_balance:,.2f} | Total PnL: ${total_pnl:,.2f} ({total_pnl/INITIAL_BALANCE*100:+.2f}%)")
    print(f"🎯 Win Rate: {win_rate:.1f}% | Wins: {wins} | Losses: {losses} | Trades: {total_trades}")
    print("="*100 + "\n")

async def safe_fetch_klines(symbol, interval, limit=100):
    """Bulletproof Binance API - 15m OHLCV"""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            resp = requests.get('https://api.binance.com/api/v3/klines',
                              params={'symbol': symbol, 'interval': interval, 'limit': limit},
                              timeout=10)
            resp.raise_for_status()
            data = resp.json()
            
            closes, lows, highs = [], [], []
            for c in data:
                if len(c) >= 6:
                    closes.append(float(c[4]))  # Close
                    lows.append(float(c[3]))     # Low
                    highs.append(float(c[2]))    # High
            
            if len(closes) >= 50:
                print(f"📡 15m DATA: {len(closes)} candles | SOL: ${closes[-1]:.4f}")
                return closes, lows, highs
            
            await asyncio.sleep(1)
        except Exception as e:
            print(f"⚠️ API Retry {attempt+1}/5: {str(e)[:40]}")
            await asyncio.sleep(1)
    print("💥 15m data fetch FAILED")
    return [], [], []

async def main():
    global paper_balance, position, prev_rsi, prev_rsi_ema, last_15m_time
    global all_closes_15m, all_lows_15m, all_highs_15m, bot
    
    # Initialize Telegram
    try:
        bot = telegram.Bot(token=TELEGRAM_TOKEN)
        await send_trade_alert("🚀", {
            "Status": "SOL 15M RSI Paper Trading v4.0 STARTED",
            "Balance": f"${INITIAL_BALANCE:,.0f}",
            "Strategy": "RSI(14)+EMA(9) 0.3 Hysteresis | Alert Candle SL"
        })
        print("✅ Telegram connected")
    except Exception as e:
        print(f"❌ Telegram failed: {e}")
    
    # Load initial 15m data
    closes, lows, highs = await safe_fetch_klines(SYMBOL, '15m', 100)
    if closes:
        all_closes_15m.extend(closes)
        all_lows_15m.extend(lows)
        all_highs_15m.extend(highs)
    else:
        print("💥 FATAL: No initial data")
        return
    
    # WebSocket connection
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_1m"
    reconnect_count = 0
    
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                reconnect_count = 0
                print("🔥 LIVE TRADING SIMULATION - 0.3 Hysteresis + Alert Candle SL")
                
                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        msg = json.loads(msg)
                    except asyncio.TimeoutError:
                        continue
                    
                    # Process closed 1m candle
                    if 'k' in msg and msg['k']['x']:
                        price = float(msg['k']['c'])
                        now = datetime.now(pytz.timezone('Asia/Kolkata'))
                        timestamp = now.strftime('%H:%M')
                        
                        # 🔥 15m DATA REFRESH (00,15,30,45)
                        current_15m = now.strftime('%H%M')
                        period_minute = int(current_15m[-2:]) // 15 * 15
                        
                        if last_15m_time != period_minute:
                            closes, lows, highs = await safe_fetch_klines(SYMBOL, '15m', 101)
                            if closes:
                                all_closes_15m.clear()
                                all_closes_15m.extend(closes[-100:])
                                all_lows_15m.clear()
                                all_lows_15m.extend(lows[-100:])
                                all_highs_15m.clear()
                                all_highs_15m.extend(highs[-100:])
                                last_15m_time = period_minute
                                print(f"🔄 15M REFRESH: SOL=${price:.3f} | Low=${lows[-1]:.3f}")
                        
                        # Update current price
                        if all_closes_15m:
                            all_closes_15m[-1] = price
                        
                        # 🔥 CALCULATE INDICATORS
                        rsi = wilders_rsi(all_closes_15m)
                        rsi_ema_val = rsi_ema(rsi_history, EMA_RSI_PERIOD)
                        
                        # 🔥 0.3 HYSTERESIS BULLISH CROSSOVER
                        BULL_CROSS = 0.3
                        bullish_cross = (prev_rsi is not None and prev_rsi_ema is not None and 
                                       prev_rsi <= prev_rsi_ema - BULL_CROSS and 
                                       rsi > rsi_ema_val + BULL_CROSS)
                        
                        prev_rsi, prev_rsi_ema = rsi, rsi_ema_val
                        
                        # 🔥 ENTRY LOGIC: RSI CROSS + NO POSITION
                        if bullish_cross and position is None:
                            alert_candle_low = all_lows_15m[-1]  # SL = ALERT CANDLE LOW
                            entry_price = price
                            
                            if alert_candle_low and alert_candle_low < entry_price * 0.999:
                                risk_distance = entry_price - alert_candle_low
                                tp_price = entry_price + (2 * risk_distance)  # 2x SL
                                size = calculate_position_size(entry_price, alert_candle_low)
                                
                                position = {
                                    'size': size,
                                    'entry_price': entry_price,
                                    'sl_price': alert_candle_low,  # EXACT 15m LOW
                                    'tp_price': tp_price,
                                    'entry_time': now,
                                    'risk_distance': risk_distance
                                }
                                
                                # Telegram entry alert
                                details = {
                                    "Entry": f"${entry_price:.4f}",
                                    "SL": f"${alert_candle_low:.4f} <b>(15m Alert Candle Low)</b>",
                                    "TP": f"${tp_price:.4f} <b>(2x SL Distance)</b>",
                                    "Size": f"{size:.1f} SOL",
                                    "Risk": f"${size*risk_distance:.1f} (1%)",
                                    "RSI": f"{rsi:.1f}",
                                    "Time": get_ist_time()
                                }
                                await send_trade_alert("🟢 LONG ENTRY v4.0", details)
                                
                                print(f"[{timestamp}] ${price:>6.2f} | {rsi:>4.1f} | "
                                      f"{rsi_ema_val:>4.1f} | 🟢 ENTRY | SL:{alert_candle_low:.4f} | "
                                      f"${paper_balance:>9,.0f}")
                        
                        # 🔥 POSITION MANAGEMENT
                        elif position:
                            pnl = position['size'] * (price - position['entry_price'])
                            pnl_pct = (pnl / paper_balance) * 100 if paper_balance > 0 else 0
                            
                            if price <= position['sl_price']:
                                # STOP LOSS HIT
                                paper_balance += pnl
                                trade = {
                                    'type': 'LONG',
                                    'entry': position['entry_price'],
                                    'exit': price,
                                    'sl': position['sl_price'],
                                    'tp': position['tp_price'],
                                    'pnl': pnl,
                                    'pnl_pct': pnl_pct,
                                    'exit_type': 'SL (Alert Candle Low)',
                                    'time': now.isoformat()
                                }
                                trades.append(trade)
                                position = None  # 🔓 READY FOR NEXT TRADE
                                
                                details = {
                                    "Status": "STOP LOSS HIT ❌",
                                    "Entry": f"${trade['entry']:.4f}",
                                    "Exit": f"${price:.4f}",
                                    "SL": f"${trade['sl']:.4f}",
                                    "PnL": f"${pnl:,.1f} ({pnl_pct:+.2f}%)",
                                    "Time": get_ist_time()
                                }
                                await send_trade_alert("🔴 SL HIT", details)
                                print(f"[{timestamp}] ${price:>6.2f} | {rsi:>4.1f} | "
                                      f"{rsi_ema_val:>4.1f} | 🔴 SL HIT | "
                                      f"${paper_balance:>9,.0f} | {pnl_pct:+5.1f}%")
                            
                            elif price >= position['tp_price']:
                                # TAKE PROFIT HIT
                                paper_balance += pnl
                                trade = {
                                    'type': 'LONG',
                                    'entry': position['entry_price'],
                                    'exit': price,
                                    'sl': position['sl_price'],
                                    'tp': position['tp_price'],
                                    'pnl': pnl,
                                    'pnl_pct': pnl_pct,
                                    'exit_type': 'TP (2x Alert Candle SL)',
                                    'time': now.isoformat()
                                }
                                trades.append(trade)
                                position = None  # 🔓 READY FOR NEXT TRADE
                                
                                details = {
                                    "Status": "TAKE PROFIT HIT ✅",
                                    "Entry": f"${trade['entry']:.4f}",
                                    "Exit": f"${price:.4f}",
                                    "TP": f"${trade['tp']:.4f}",
                                    "PnL": f"${pnl:,.1f} ({pnl_pct:+.2f}%)",
                                    "Time": get_ist_time()
                                }
                                await send_trade_alert("🟢 TP HIT", details)
                                print(f"[{timestamp}] ${price:>6.2f} | {rsi:>4.1f} | "
                                      f"{rsi_ema_val:>4.1f} | 🟢 TP HIT | "
                                      f"${paper_balance:>9,.0f} | {pnl_pct:+5.1f}%")
                            
                            else:
                                # HOLD POSITION
                                print(f"[{timestamp}] ${price:>6.2f} | {rsi:>4.1f} | "
                                      f"{rsi_ema_val:>4.1f} | HOLD      | "
                                      f"${paper_balance:>9,.0f} | {pnl_pct:+5.1f}%")
                        
                        else:
                            # NO POSITION - MONITORING
                            signal = "🟢 BULL" if rsi > rsi_ema_val else "🔴 BEAR"
                            print(f"[{timestamp}] ${price:>6.2f} | {rsi:>4.1f} | "
                                  f"{rsi_ema_val:>4.1f} | READY     | "
                                  f"${paper_balance:>9,.0f} | {signal}")
                        
                        # Performance report
                        if len(trades) % 5 == 0 and trades:
                            await print_performance()
                    
                    await asyncio.sleep(0.1)
                    
        except Exception as e:
            reconnect_count += 1
            print(f"🔄 RECONNECT #{reconnect_count}: {str(e)[:60]}")
            await asyncio.sleep(min(5 * reconnect_count, 30))

if __name__ == "__main__":
    try:
        print(f"🤖 STARTING SOL 15M RSI BOT | {get_ist_time()}")
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n📊 FINAL PERFORMANCE:")
        asyncio.run(print_performance())
        print("👋 Paper trading stopped by user")
    except Exception as e:
        print(f"💥 FATAL ERROR: {e}")
        if 'bot' in globals() and bot:
            asyncio.create_task(send_trade_alert("💥", {"Error": str(e)[:100]}))

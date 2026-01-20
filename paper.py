import asyncio, websockets, json, telegram, requests, logging, sys
import pandas as pd
import pandas_ta as ta

# ==================== CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
SIGNAL_TIMEFRAME = '1h'  # The trend we follow
CHECK_INTERVAL = '5m'    # How often we "repaint" the indicator (5 mins)
TELEGRAM_TOKEN = '8050135427:AAFNQYFpU8lMQ-reJlvLnPYFKc8pyPrHblE'
CHAT_ID = '1950462171'

stats = {"balance": 1000.0, "risk_percent": 0.02, "wins": 0, "losses": 0, "total_trades": 0}
active_trade = None
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ==================== DATA ENGINE ====================

async def fetch_indicators():
    """Fetches 1H candles and calculates indicators to sync with Binance."""
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': SIGNAL_TIMEFRAME, 'limit': 100}
        resp = requests.get(url, params=params, timeout=10)
        df = pd.DataFrame(resp.json(), columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df['close'] = df['c'].astype(float)
        
        # Calculate RSI 14 and EMA 9 of RSI
        rsi = ta.rsi(df['close'], length=14)
        rsi_ema = ta.ema(rsi, length=9)
        
        return rsi.iloc[-1], rsi_ema.iloc[-1], rsi.iloc[-2], rsi_ema.iloc[-2]
    except Exception as e:
        print(f"Sync Error: {e}")
        return None, None, None, None

# ==================== TRADE MANAGEMENT ====================

async def monitor_trade(price):
    global active_trade
    if not active_trade: return

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    # Current Real-Time RR
    rr_ratio = (price - active_trade['entry']) / risk_dist if risk_dist > 0 else 0

    # STAGE 1: Hit 1.5R -> SL to +0.5R
    if rr_ratio >= 1.5 and active_trade['trail_level'] < 1:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.5)
        active_trade['trail_level'] = 1
        await bot.send_message(CHAT_ID, f"🛡️ STAGE 1: SL Trailed to +0.5R (${active_trade['sl']:.2f})")

    # STAGE 2: Hit 2.2R -> SL to +1.4R
    elif rr_ratio >= 2.2 and active_trade['trail_level'] < 2:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 1.4)
        active_trade['trail_level'] = 2
        await bot.send_message(CHAT_ID, f"🛡️ STAGE 2: SL Trailed to +1.4R (${active_trade['sl']:.2f})")

    # STAGE 3: EXIT AT 3.0R
    if rr_ratio >= 3.0:
        await close_trade(price, "🎯 FINAL TARGET REACHED (3.0R)")
    elif price <= active_trade['sl']:
        reason = "🛡️ TRAILING STOP" if active_trade['trail_level'] > 0 else "🛑 STOP LOSS"
        await close_trade(price, reason)

async def close_trade(exit_price, reason):
    global active_trade, stats
    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    pnl_rr = (exit_price - active_trade['entry']) / risk_dist
    pnl_cash = pnl_rr * active_trade['risk_usd']
    
    stats['balance'] += pnl_cash
    stats['total_trades'] += 1
    if pnl_cash > 0: stats['wins'] += 1
    else: stats['losses'] += 1
    
    msg = (f"🏁 *TRADE CLOSED: {reason}*\n"
           f"💰 Exit Price: `${exit_price:.2f}`\n"
           f"💵 PnL: `{pnl_cash:+.2f} USDT`\n"
           f"🏦 Balance: `${stats['balance']:.2f}`")
    await bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
    active_trade = None

# ==================== MAIN EXECUTION ====================

async def main():
    global active_trade
    # Subscribing to 5m stream for "repainting" every 5 mins
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_{CHECK_INTERVAL}"
    
    print(f"Bot Started: Monitoring {SYMBOL} on 1H trend with 5M sync.")
    
    async with websockets.connect(uri) as ws:
        while True:
            try:
                data = json.loads(await ws.recv())
                if 'k' in data:
                    price = float(data['k']['c'])
                    
                    # 1. Live Price Monitoring for active trades
                    if active_trade:
                        await monitor_trade(price)
                    
                    # 2. Every 5 Minutes (Candle Close), Repaint/Sync Indicators
                    if data['k']['x']:
                        rsi, rsi_ema, prsi, pema = await fetch_indicators()
                        
                        # Check for entry if no trade is open
                        if rsi and not active_trade:
                            # 1H Crossover Check (Repainted every 5 mins)
                            if prsi <= pema and rsi > rsi_ema:
                                resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={SIGNAL_TIMEFRAME}&limit=1").json()
                                low_price = float(resp[0][3]) * 0.9995
                                
                                active_trade = {
                                    'entry': price, 'initial_sl': low_price, 'sl': low_price,
                                    'risk_usd': stats['balance'] * stats['risk_percent'],
                                    'trail_level': 0
                                }
                                
                                entry_msg = (f"🚀 *LONG SIGNAL (1H Trend)*\n"
                                             f"📊 RSI ({rsi:.2f}) crossed EMA ({rsi_ema:.2f})\n"
                                             f"💰 Entry: `${price:.2f}` | SL: `${low_price:.2f}`")
                                await bot.send_message(CHAT_ID, entry_msg, parse_mode='Markdown')

            except Exception as e:
                print(f"Error in main loop: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())

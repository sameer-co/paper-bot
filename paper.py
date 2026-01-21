import asyncio
import websockets
import json
import telegram
import requests
import pandas as pd
import pandas_ta as ta

# ==================== CONFIGURATION ====================
SYMBOL = 'SOLUSDT'
SIGNAL_TIMEFRAME = '1h'
CHECK_INTERVAL = '5m'
TELEGRAM_TOKEN = '8349229275:AAGNWV2A0_Pf9LhlwZCczeBoMcUaJL2shFg'
CHAT_ID = '1950462171'

stats = {
    "balance": 1000.0, 
    "risk_percent": 0.02, 
    "wins": 0, 
    "losses": 0, 
    "total_trades": 0,
    "trailed_trades": 0 
}

active_trade = None
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# ==================== DATA ENGINE ====================

async def fetch_indicators():
    try:
        url = "https://api.binance.com/api/v3/klines"
        params = {'symbol': SYMBOL, 'interval': SIGNAL_TIMEFRAME, 'limit': 100}
        resp = requests.get(url, params=params, timeout=10)
        df = pd.DataFrame(resp.json(), columns=['ts', 'o', 'h', 'l', 'c', 'v', 'ts_e', 'q', 'n', 'tb', 'tq', 'i'])
        df['close'] = df['c'].astype(float)
        rsi = ta.rsi(df['close'], length=14)
        rsi_ema = ta.ema(rsi, length=9)
        return rsi.iloc[-1], rsi_ema.iloc[-1], rsi.iloc[-2], rsi_ema.iloc[-2]
    except Exception as e:
        print(f"Sync Error: {e}")
        return None, None, None, None

# ==================== TRADE MANAGEMENT ====================

async def monitor_trade(price):
    global active_trade, stats
    if not active_trade: return

    risk_dist = active_trade['entry'] - active_trade['initial_sl']
    rr_ratio = (price - active_trade['entry']) / risk_dist if risk_dist > 0 else 0

    # NEW 6-STAGE LADDER LOGIC
    # Stage 1: Hit 1.5R -> Trail to 0.8R
    if rr_ratio >= 1.5 and active_trade['trail_level'] < 1:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 0.8)
        active_trade['trail_level'] = 1
        stats['trailed_trades'] += 1
        await bot.send_message(CHAT_ID, f"🛡️ STAGE 1: Hit 1.5R | SL Trailed to +0.8R (${active_trade['sl']:.2f})")

    # Stage 2: Hit 2.2R -> Trail to 1.4R
    elif rr_ratio >= 2.2 and active_trade['trail_level'] < 2:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 1.4)
        active_trade['trail_level'] = 2
        await bot.send_message(CHAT_ID, f"🛡️ STAGE 2: Hit 2.2R | SL Trailed to +1.4R (${active_trade['sl']:.2f})")

    # Stage 3: Hit 3.0R -> Trail to 2.2R
    elif rr_ratio >= 3.0 and active_trade['trail_level'] < 3:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 2.2)
        active_trade['trail_level'] = 3
        await bot.send_message(CHAT_ID, f"🛡️ STAGE 3: Hit 3.0R | SL Trailed to +2.2R (${active_trade['sl']:.2f})")

    # Stage 4: Hit 3.8R -> Trail to 3.0R
    elif rr_ratio >= 3.8 and active_trade['trail_level'] < 4:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 3.0)
        active_trade['trail_level'] = 4
        await bot.send_message(CHAT_ID, f"🛡️ STAGE 4: Hit 3.8R | SL Trailed to +3.0R (${active_trade['sl']:.2f})")

    # Stage 5: Hit 4.5R -> Trail to 3.8R
    elif rr_ratio >= 4.5 and active_trade['trail_level'] < 5:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 3.8)
        active_trade['trail_level'] = 5
        await bot.send_message(CHAT_ID, f"🛡️ STAGE 5: Hit 4.5R | SL Trailed to +3.8R (${active_trade['sl']:.2f})")

    # Stage 6: Hit 5.2R -> Trail to 4.4R
    elif rr_ratio >= 5.2 and active_trade['trail_level'] < 6:
        active_trade['sl'] = active_trade['entry'] + (risk_dist * 4.4)
        active_trade['trail_level'] = 6
        await bot.send_message(CHAT_ID, f"🛡️ STAGE 6: Hit 5.2R | SL Trailed to +4.4R (${active_trade['sl']:.2f})")

    # EXIT LOGIC: Hit 6.0R or Trail Hit
    if rr_ratio >= 6.0:
        await close_trade(price, "🎯 FINAL TARGET REACHED (6.0R)")
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
    
    win_rate = (stats['wins'] / stats['total_trades']) * 100
    msg = (f"🏁 *TRADE CLOSED: {reason}*\n"
           f"💰 Exit Price: `${exit_price:.2f}`\n"
           f"💵 PnL: `{pnl_cash:+.2f} USDT`\n"
           f"🏦 Balance: `${stats['balance']:.2f}`\n"
           f"📈 Win Rate: `{win_rate:.1f}%` | 🔄 Trailed: `{stats['trailed_trades']}`")
    await bot.send_message(CHAT_ID, msg, parse_mode='Markdown')
    active_trade = None

# ==================== MAIN EXECUTION ====================

async def main():
    global active_trade
    uri = f"wss://stream.binance.com:9443/ws/{SYMBOL.lower()}@kline_{CHECK_INTERVAL}"
    print(f"Bot Active: {SYMBOL}")
    
    async with websockets.connect(uri) as ws:
        while True:
            try:
                data = json.loads(await ws.recv())
                if 'k' in data:
                    price = float(data['k']['c'])
                    if active_trade: await monitor_trade(price)
                    
                    if data['k']['x'] and not active_trade:
                        rsi, rsi_ema, prsi, pema = await fetch_indicators()
                        if rsi and prsi <= pema and rsi > rsi_ema:
                            resp = requests.get(f"https://api.binance.com/api/v3/klines?symbol={SYMBOL}&interval={SIGNAL_TIMEFRAME}&limit=1").json()
                            low_price = float(resp[0][3]) * 0.9995
                            active_trade = {
                                'entry': price, 'initial_sl': low_price, 'sl': low_price,
                                'risk_usd': stats['balance'] * stats['risk_percent'],
                                'trail_level': 0
                            }
                            await bot.send_message(CHAT_ID, f"🚀 *LONG SIGNAL*\n💰 Entry: `${price:.2f}`\n🛑 SL: `${low_price:.2f}`", parse_mode='Markdown')
            except Exception as e:
                print(f"Loop Error: {e}")
                await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())

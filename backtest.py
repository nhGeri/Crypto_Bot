import asyncio
import httpx
import time
import pandas as pd
from decimal import Decimal
import logging

# Importáljuk a bot.py-ból a logikát és a struktúrákat
import bot
from bot import (
    SimAccount, DCAPosition, calculate_indicators, 
    TP_PCT, SO_PCT, MAX_SAFETY_LEVELS, get_so_amount,
    RSI_PERIOD, PRODUCT_SYMBOL, BB_CANDLE_INTERVAL
)

DAYS_TO_BACKTEST = 30

# Új fiókok csak a backteszthez
BT_ACCOUNTS = [
    SimAccount(name="BT $50 Fiók", capital=Decimal("50"), virtual_balance=Decimal("50"), csv_file="bt_50.csv"),
    SimAccount(name="BT $100 Fiók", capital=Decimal("100"), virtual_balance=Decimal("100"), csv_file="bt_100.csv"),
    SimAccount(name="BT $200 Fiók", capital=Decimal("200"), virtual_balance=Decimal("200"), csv_file="bt_200.csv"),
]

class BacktestStats:
    def __init__(self):
        self.sl_hits = 0
        self.trades_closed_at_level = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        self.wins_at_level = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        self.sum_levels_for_tp = 0
        self.total_tp_trades = 0

BT_STATS = {acc.name: BacktestStats() for acc in BT_ACCOUNTS}

async def fetch_historical_candles(days: int) -> pd.DataFrame:
    print(f"Adatok letöltése az elmúlt {days} napra...")
    candles_per_day = 24 * (60 // int(BB_CANDLE_INTERVAL.replace("m", "")))
    limit = (days * candles_per_day) + RSI_PERIOD + 10
    
    resolution = BB_CANDLE_INTERVAL.replace("m", "")
    interval_seconds = int(resolution) * 60
    to_ts = int(time.time())
    from_ts = to_ts - (limit * interval_seconds)
    symbol = f"{PRODUCT_SYMBOL}-Perp"
    
    # Mivel max 1000 gyertyát adhat vissza az API, lehet, hogy paginálni kell!
    # A TradingView API countback paramétere limitált lehet.
    # Megpróbáljuk egyszerre lekérni, ha nem ad eleget, daraboljuk.
    all_candles = []
    current_to = to_ts
    remaining = limit
    
    async with httpx.AsyncClient() as client:
        while remaining > 0:
            fetch_count = min(remaining, 1000)
            from_t = current_to - (fetch_count * interval_seconds)
            r = await client.get(
                "https://tradingview.ethereal.trade/v1/oracle-price/history",
                params={"symbol": symbol, "resolution": resolution, "from": from_t, "to": current_to, "countback": fetch_count},
            )
            data = r.json()
            if data.get("s") != "ok":
                break
            
            chunk = pd.DataFrame({
                "timestamp": data["t"], 
                "open": [float(o) for o in data["o"]],
                "close": [float(c) for c in data["c"]]
            })
            all_candles.append(chunk)
            current_to = data["t"][0] - interval_seconds
            remaining -= fetch_count
            
    if not all_candles:
        return pd.DataFrame()
        
    df = pd.concat(all_candles).drop_duplicates(subset=['timestamp']).sort_values('timestamp').reset_index(drop=True)
    df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
    print(f"Letöltve {len(df)} db gyertya.")
    return df

def check_take_profit(acc: SimAccount, price: Decimal) -> bool:
    if not acc.position: return False
    tp_pct = TP_PCT.get(acc.position.safety_level, Decimal("0.015"))
    target = acc.position.average_price * (Decimal("1") + tp_pct)
    return price >= target

def check_reset_logic(acc: SimAccount, price: Decimal) -> bool:
    if not acc.position or acc.position.safety_level < 3: return False
    target = acc.position.extreme_price * Decimal("1.003")
    return price >= target

def check_stop_loss(acc: SimAccount, price: Decimal) -> bool:
    if not acc.position: return False
    sl_drop = Decimal("0.13")
    target = acc.position.average_price * (Decimal("1") - sl_drop)
    return price <= target

def check_safety_orders(acc: SimAccount, price: Decimal) -> bool:
    if not acc.position: return False
    if acc.position.safety_level >= MAX_SAFETY_LEVELS: return False
    next_level = acc.position.safety_level + 1
    so_drop = SO_PCT.get(next_level, Decimal("0.015"))
    target = acc.position.average_price * (Decimal("1") - so_drop)
    return price <= target

def close_position(acc: SimAccount, price: Decimal, reason: str):
    pos = acc.position
    pnl = (price - pos.average_price) * pos.total_quantity
    
    acc.virtual_balance += pos.total_invested + pnl
    
    stats = BT_STATS[acc.name]
    level = pos.safety_level
    stats.trades_closed_at_level[level] = stats.trades_closed_at_level.get(level, 0) + 1
    
    if pnl > 0:
        acc.win_count += 1
        stats.wins_at_level[level] = stats.wins_at_level.get(level, 0) + 1
        if reason == "Take Profit":
            stats.sum_levels_for_tp += level
            stats.total_tp_trades += 1
    else:
        acc.loss_count += 1
        if reason == "Stop Loss (-13%)":
            stats.sl_hits += 1
            
    acc.position = None

def open_or_add_position(acc: SimAccount, price: Decimal, level: int):
    amount_usd = get_so_amount(level)
    if acc.virtual_balance < amount_usd:
        return
    acc.virtual_balance -= amount_usd
    quantity = (amount_usd / price)
    
    if acc.position is None:
        acc.position = DCAPosition(
            side="LONG", safety_level=level, total_invested=amount_usd,
            total_quantity=quantity, average_price=price,
            entry_price=price, extreme_price=price
        )
    else:
        pos = acc.position
        pos.safety_level = level
        pos.total_invested += amount_usd
        pos.total_quantity += quantity
        pos.average_price = pos.total_invested / pos.total_quantity
        pos.extreme_price = price

def update_extreme_price(acc: SimAccount, price: Decimal):
    if acc.position and price < acc.position.extreme_price:
        acc.position.extreme_price = price

async def run_backtest():
    df = await fetch_historical_candles(DAYS_TO_BACKTEST)
    if df.empty: return

    print("\nSzimuláció futtatása (FIBONACCI DCA, 30 NAP)...")
    start_idx = RSI_PERIOD + 1
    
    logging.getLogger("ethereal-dca-bot").setLevel(logging.WARNING)
    
    for i in range(start_idx, len(df)):
        current_slice = df.iloc[:i+1]
        indicator = calculate_indicators(current_slice.copy())
        current_price = Decimal(str(df.iloc[i]['close']))
        
        for acc in BT_ACCOUNTS:
            update_extreme_price(acc, current_price)
            
            if acc.position:
                if check_take_profit(acc, current_price):
                    close_position(acc, current_price, "Take Profit")
                    continue
                if check_reset_logic(acc, current_price):
                    close_position(acc, current_price, "Reset Bounce")
                    continue
                if check_stop_loss(acc, current_price):
                    close_position(acc, current_price, "Stop Loss (-13%)")
                    continue
                
                if check_safety_orders(acc, current_price):
                    next_level = acc.position.safety_level + 1
                    open_or_add_position(acc, current_price, next_level)
            else:
                if indicator.signal == "LONG":
                    open_or_add_position(acc, current_price, 0)
                    
    print("\n==== BACKTEST EREDMÉNYEK (30 NAP) ====")
    for acc in BT_ACCOUNTS:
        stats = BT_STATS[acc.name]
        total_trades = acc.win_count + acc.loss_count
        win_rate = (acc.win_count / total_trades * 100) if total_trades > 0 else 0
        pnl_total = acc.virtual_balance - acc.capital
        roi = (pnl_total / acc.capital) * 100
        
        unrealized = Decimal("0")
        if acc.position:
            unrealized = (current_price - acc.position.average_price) * acc.position.total_quantity
            
        print(f"{acc.name}:")
        print(f"  - Induló tőke: ${acc.capital:.2f}")
        print(f"  - Jelenlegi tőke: ${acc.virtual_balance:.2f} (Realizált ROI: {roi:+.2f}%)")
        if acc.position:
            print(f"  - Nyitott pozíció: Lvl {acc.position.safety_level}, U-PnL: {unrealized:+.4f}$")
            print(f"  - Teljes Tőke: ${(acc.virtual_balance + acc.position.total_invested + unrealized):.2f}")
        else:
            print(f"  - Nyitott pozíció: Nincs")
            
        print(f"  - Tradek száma: {total_trades} (Win: {acc.win_count}, Loss: {acc.loss_count})")
        print(f"  - Win Rate: {win_rate:.1f}%")
        
        # A 3 kérdés megválaszolása:
        print("  [STATISZTIKÁK]")
        print(f"  1. Hányszor ért el Stop Loss-t (-13%): {stats.sl_hits}")
        
        avg_tp_level = (stats.sum_levels_for_tp / stats.total_tp_trades) if stats.total_tp_trades > 0 else 0
        print(f"  2. Átlagos elért szint TP előtt: {avg_tp_level:.2f}")
        
        print(f"  3. Win Rate szintenként:")
        for lvl in range(MAX_SAFETY_LEVELS + 1):
            closed = stats.trades_closed_at_level.get(lvl, 0)
            if closed > 0:
                lvl_wins = stats.wins_at_level.get(lvl, 0)
                lvl_wr = (lvl_wins / closed) * 100
                print(f"     - Szint {lvl}: {lvl_wr:.1f}% ({lvl_wins}/{closed} trade)")
            else:
                print(f"     - Szint {lvl}: Nincs lezárt trade")
        print("-" * 40)

if __name__ == "__main__":
    asyncio.run(run_backtest())

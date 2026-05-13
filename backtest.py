import asyncio
import httpx
import time
import pandas as pd
from decimal import Decimal

# Importáljuk a bot.py-ból a logikát és a struktúrákat
from bot import (
    SimAccount, DCAPosition, calculate_indicators, 
    TP_PCT, SO_PCT, MAX_SAFETY_LEVELS, get_so_amount,
    BB_PERIOD, RSI_PERIOD, PRODUCT_SYMBOL, API_BASE, BB_CANDLE_INTERVAL
)

# ---------------------------------------------
# BACKTESTER KONFIG
# ---------------------------------------------
DAYS_TO_BACKTEST = 3

# Új fiókok csak a backteszthez (hogy ne a live csv-ket írja felül)
BT_ACCOUNTS = [
    SimAccount(name="BT $50 Fiók", capital=Decimal("50"), virtual_balance=Decimal("50"), csv_file="bt_50.csv"),
    SimAccount(name="BT $100 Fiók", capital=Decimal("100"), virtual_balance=Decimal("100"), csv_file="bt_100.csv"),
    SimAccount(name="BT $200 Fiók", capital=Decimal("200"), virtual_balance=Decimal("200"), csv_file="bt_200.csv"),
]

async def fetch_historical_candles(days: int) -> pd.DataFrame:
    print(f"Adatok letöltése az elmúlt {days} napra...")
    # 1 nap = 24 óra = 96 db 15 perces gyertya
    # Plusz rászámolunk egy kis puffert (max(BB_PERIOD, RSI_PERIOD)) az indikátorok bemelegedéséhez
    candles_per_day = 24 * (60 // int(BB_CANDLE_INTERVAL.replace("m", "")))
    limit = (days * candles_per_day) + max(BB_PERIOD, RSI_PERIOD) + 10
    
    resolution = BB_CANDLE_INTERVAL.replace("m", "")
    interval_seconds = int(resolution) * 60
    to_ts = int(time.time())
    from_ts = to_ts - (limit * interval_seconds)
    symbol = f"{PRODUCT_SYMBOL}-Perp"
    
    async with httpx.AsyncClient() as client:
        r = await client.get(
            "https://tradingview.ethereal.trade/v1/oracle-price/history",
            params={"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts, "countback": limit},
        )
        data = r.json()
        if data.get("s") != "ok":
            print("Hiba az adatok letöltésekor!")
            return pd.DataFrame()
            
        df = pd.DataFrame({"timestamp": data["t"], "close": [float(c) for c in data["c"]]})
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
        print(f"Letöltve {len(df)} db gyertya.")
        return df

def check_take_profit(acc: SimAccount, price: Decimal) -> bool:
    if not acc.position: return False
    tp_pct = TP_PCT.get(acc.position.safety_level, Decimal("0.015"))
    if acc.position.side == "LONG":
        target = acc.position.average_price * (Decimal("1") + tp_pct)
        return price >= target
    else:
        target = acc.position.average_price * (Decimal("1") - tp_pct)
        return price <= target

def check_reset_logic(acc: SimAccount, price: Decimal) -> bool:
    if not acc.position or acc.position.safety_level < 3: return False
    if acc.position.side == "LONG":
        target = acc.position.extreme_price * Decimal("1.003")
        return price >= target
    else:
        target = acc.position.extreme_price * Decimal("0.997")
        return price <= target

def check_stop_loss(acc: SimAccount, price: Decimal) -> bool:
    if not acc.position or acc.position.safety_level < MAX_SAFETY_LEVELS: return False
    sl_drop = Decimal("0.05")
    if acc.position.side == "LONG":
        target = acc.position.average_price * (Decimal("1") - sl_drop)
        return price <= target
    else:
        target = acc.position.average_price * (Decimal("1") + sl_drop)
        return price >= target

def check_safety_orders(acc: SimAccount, price: Decimal) -> bool:
    if not acc.position: return False
    if acc.position.safety_level >= MAX_SAFETY_LEVELS: return False
    next_level = acc.position.safety_level + 1
    so_drop = SO_PCT.get(next_level, Decimal("0.025"))
    if acc.position.side == "LONG":
        target = acc.position.average_price * (Decimal("1") - so_drop)
        return price <= target
    else:
        target = acc.position.average_price * (Decimal("1") + so_drop)
        return price >= target

def close_position(acc: SimAccount, price: Decimal, reason: str, timestamp: pd.Timestamp):
    pos = acc.position
    if pos.side == "LONG":
        pnl = (price - pos.average_price) * pos.total_quantity
    else:
        pnl = (pos.average_price - price) * pos.total_quantity
        
    acc.virtual_balance += pos.total_invested + pnl
    if pnl > 0:
        acc.win_count += 1
    else:
        acc.loss_count += 1
        
    acc.position = None

def open_or_add_position(acc: SimAccount, side: str, price: Decimal, level: int):
    amount_usd = get_so_amount(level)
    if acc.virtual_balance < amount_usd:
        return
    acc.virtual_balance -= amount_usd
    quantity = (amount_usd / price)
    
    if acc.position is None:
        acc.position = DCAPosition(
            side=side, safety_level=level, total_invested=amount_usd,
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
    if not acc.position: return
    if acc.position.side == "LONG":
        if price < acc.position.extreme_price:
            acc.position.extreme_price = price
    else:
        if price > acc.position.extreme_price:
            acc.position.extreme_price = price

async def run_backtest():
    df = await fetch_historical_candles(DAYS_TO_BACKTEST)
    if df.empty:
        return

    print("\nSzimuláció futtatása...")
    start_idx = max(BB_PERIOD, RSI_PERIOD) + 1
    
    for i in range(start_idx, len(df)):
        # Az adott pillanatig elérhető gyertyák (az indikátorok számára)
        # Az indikátort mindig az elozo gyertyák + az aktuális gyertya alapján számoljuk
        current_slice = df.iloc[:i+1]
        
        # A pandas SettingWithCopyWarning elkerülésére nem csinálunk deep copy-t,
        # csak átadjuk a calculate_indicators-nek.
        # Viszont a bot.py logger-e sokat írna, azt kikapcsoljuk.
        import logging
        logging.getLogger("ethereal-dca-bot").setLevel(logging.WARNING)
        
        indicator = calculate_indicators(current_slice.copy())
        current_price = Decimal(str(df.iloc[i]['close']))
        current_time = df.iloc[i]['datetime']
        
        for acc in BT_ACCOUNTS:
            update_extreme_price(acc, current_price)
            
            if acc.position:
                if check_take_profit(acc, current_price):
                    close_position(acc, current_price, "TP", current_time)
                    continue
                if check_reset_logic(acc, current_price):
                    close_position(acc, current_price, "Reset", current_time)
                    continue
                if check_stop_loss(acc, current_price):
                    close_position(acc, current_price, "SL", current_time)
                    continue
                
                if check_safety_orders(acc, current_price):
                    next_level = acc.position.safety_level + 1
                    open_or_add_position(acc, acc.position.side, current_price, next_level)
            else:
                if indicator.signal in ("LONG", "SHORT"):
                    open_or_add_position(acc, indicator.signal, current_price, 0)
                    
    print("\n==== BACKTEST EREDMÉNYEK ====")
    for acc in BT_ACCOUNTS:
        total_trades = acc.win_count + acc.loss_count
        win_rate = (acc.win_count / total_trades * 100) if total_trades > 0 else 0
        pnl_total = acc.virtual_balance - acc.capital
        roi = (pnl_total / acc.capital) * 100
        
        # Ha maradt nyitott pozíció a végén, számoljuk bele a nem realizált PnL-t
        unrealized = Decimal("0")
        if acc.position:
            p = acc.position
            if p.side == "LONG":
                unrealized = (current_price - p.average_price) * p.total_quantity
            else:
                unrealized = (p.average_price - current_price) * p.total_quantity
        
        print(f"{acc.name}:")
        print(f"  - Induló tőke: ${acc.capital:.2f}")
        print(f"  - Jelenlegi tőke: ${acc.virtual_balance:.2f} (Realizált ROI: {roi:+.2f}%)")
        if acc.position:
            print(f"  - Nyitott pozíció: {acc.position.side} (Szint: {acc.position.safety_level}), U-PnL: {unrealized:+.4f}$")
            print(f"  - Becsült Teljes Tőke (U-PnL-el): ${(acc.virtual_balance + acc.position.total_invested + unrealized):.2f}")
        else:
            print(f"  - Nyitott pozíció: Nincs")
        print(f"  - Tradek száma: {total_trades} (Win: {acc.win_count}, Loss: {acc.loss_count})")
        print(f"  - Win Rate: {win_rate:.1f}%")
        print("-" * 30)

if __name__ == "__main__":
    asyncio.run(run_backtest())

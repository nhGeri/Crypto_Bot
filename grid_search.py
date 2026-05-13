import asyncio
import httpx
import time
import pandas as pd
from decimal import Decimal
import logging

from bot import (
    SimAccount, DCAPosition, 
    TP_PCT as BOT_TP_PCT, SO_PCT, get_so_amount,
    RSI_PERIOD, PRODUCT_SYMBOL, BB_CANDLE_INTERVAL
)

# Overrides for grid search
def get_grid_so_amount(level: int, base_order: Decimal) -> Decimal:
    if level == 0: return base_order
    return base_order * Decimal(str(2 ** level))

CONFIGS = [
    {"name": "1. RSI < 33 | Base $20", "max_l": 2, "tp": Decimal("0.010"), "rsi": 33, "base": Decimal("20")},
    {"name": "2. RSI < 35 | Base $20", "max_l": 2, "tp": Decimal("0.010"), "rsi": 35, "base": Decimal("20")},
    {"name": "3. RSI < 37 | Base $20", "max_l": 2, "tp": Decimal("0.010"), "rsi": 37, "base": Decimal("20")},
]

async def fetch_historical_candles(days: int) -> pd.DataFrame:
    print(f"Adatok letöltése az elmúlt {days} napra...")
    candles_per_day = 24 * (60 // int(BB_CANDLE_INTERVAL.replace("m", "")))
    limit = (days * candles_per_day) + RSI_PERIOD + 10
    
    resolution = BB_CANDLE_INTERVAL.replace("m", "")
    interval_seconds = int(resolution) * 60
    to_ts = int(time.time())
    from_ts = to_ts - (limit * interval_seconds)
    symbol = f"{PRODUCT_SYMBOL}-Perp"
    
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

class ConfigStats:
    def __init__(self):
        self.sl_hits = 0
        self.trades_closed_at_level = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        self.max_capital_used = Decimal("0")
        self.win_count = 0
        self.loss_count = 0
        self.pnl = Decimal("0")

def run_simulation(df, config, timeframe_days):
    acc = SimAccount(name="Test", capital=Decimal("10000"), virtual_balance=Decimal("10000"), csv_file="")
    stats = ConfigStats()
    
    start_idx = RSI_PERIOD + 1
    closes = df['close'].tolist()
    opens = df['open'].tolist()
    rsis = df['rsi'].tolist()
    
    for i in range(start_idx, len(df)):
        current_price = Decimal(str(closes[i]))
        open_price = Decimal(str(opens[i]))
        rsi_val = rsis[i]
        signal = "LONG" if (rsi_val < config["rsi"] and current_price > open_price) else "HOLD"
        
        if acc.position and current_price < acc.position.extreme_price:
            acc.position.extreme_price = current_price
            
        if acc.position:
            target_tp = acc.position.average_price * (Decimal("1") + config["tp"])
            if current_price >= target_tp:
                pnl = (current_price - acc.position.average_price) * acc.position.total_quantity
                acc.virtual_balance += acc.position.total_invested + pnl
                stats.pnl += pnl
                stats.win_count += 1
                stats.trades_closed_at_level[acc.position.safety_level] += 1
                acc.position = None
                continue
                
            if acc.position.safety_level >= 3:
                target_reset = acc.position.extreme_price * Decimal("1.003")
                if current_price >= target_reset:
                    pnl = (current_price - acc.position.average_price) * acc.position.total_quantity
                    acc.virtual_balance += acc.position.total_invested + pnl
                    stats.pnl += pnl
                    if pnl > 0: stats.win_count += 1
                    else: stats.loss_count += 1
                    stats.trades_closed_at_level[acc.position.safety_level] += 1
                    acc.position = None
                    continue
            
            target_sl = acc.position.average_price * (Decimal("1") - Decimal("0.13"))
            if current_price <= target_sl:
                pnl = (current_price - acc.position.average_price) * acc.position.total_quantity
                acc.virtual_balance += acc.position.total_invested + pnl
                stats.pnl += pnl
                stats.loss_count += 1
                stats.sl_hits += 1
                stats.trades_closed_at_level[acc.position.safety_level] += 1
                acc.position = None
                continue
                
            if acc.position.safety_level < config["max_l"]:
                next_l = acc.position.safety_level + 1
                so_drop = SO_PCT.get(next_l, Decimal("0.015"))
                target_so = acc.position.average_price * (Decimal("1") - so_drop)
                if current_price <= target_so:
                    amt = get_grid_so_amount(next_l, config["base"])
                    acc.virtual_balance -= amt
                    qty = amt / current_price
                    acc.position.safety_level = next_l
                    acc.position.total_invested += amt
                    acc.position.total_quantity += qty
                    acc.position.average_price = acc.position.total_invested / acc.position.total_quantity
                    acc.position.extreme_price = current_price
                    
                    if acc.position.total_invested > stats.max_capital_used:
                        stats.max_capital_used = acc.position.total_invested
        else:
            if signal == "LONG":
                amt = get_grid_so_amount(0, config["base"])
                acc.virtual_balance -= amt
                qty = amt / current_price
                acc.position = DCAPosition(
                    side="LONG", safety_level=0, total_invested=amt,
                    total_quantity=qty, average_price=current_price,
                    entry_price=current_price, extreme_price=current_price
                )
                if acc.position.total_invested > stats.max_capital_used:
                    stats.max_capital_used = acc.position.total_invested
                    
    total_trades = stats.win_count + stats.loss_count
    wr = (stats.win_count / total_trades * 100) if total_trades > 0 else 0
    roi = (stats.pnl / stats.max_capital_used * 100) if stats.max_capital_used > 0 else 0
    if timeframe_days > 0:
        annual_roi = roi * Decimal(str(365 / timeframe_days))
    else:
        annual_roi = Decimal("0")
        
    return {
        "trades": total_trades,
        "wr": wr,
        "L0": stats.trades_closed_at_level[0],
        "L1": stats.trades_closed_at_level[1],
        "L2": stats.trades_closed_at_level[2],
        "L3": stats.trades_closed_at_level[3],
        "L4": stats.trades_closed_at_level[4],
        "sl": stats.sl_hits,
        "profit": stats.pnl,
        "max_cap": stats.max_capital_used,
        "annual_roi": annual_roi
    }

async def main():
    logging.getLogger("ethereal-dca-bot").setLevel(logging.WARNING)
    df_all = await fetch_historical_candles(730)
    if df_all.empty: return
    
    actual_days = len(df_all) / (24 * 4)
    print(f"\n=========================================")
    print(f" TÉNYLEGES LETÖLTÖTT IDŐTÁV: {actual_days:.1f} NAP")
    print(f"=========================================")
    
    timeframes = [int(actual_days)]
    
    for tf in timeframes:
        print(f"\n### IDŐTÁV: {tf} NAP")
        print(f"| Kombináció | Tradek | Win Rate | Szintek (L0/L1/L2/L3) | SL | Profit | Max Tőke | Éves ROI |")
        print(f"|---|---|---|---|---|---|---|---|")
        
        limit = int(tf * 96) + RSI_PERIOD + 10
        if limit > len(df_all): limit = len(df_all)
        df_tf = df_all.tail(limit).reset_index(drop=True)
        
        delta = df_tf['close'].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/RSI_PERIOD, adjust=False).mean()
        rs = gain / loss
        df_tf['rsi'] = 100 - (100 / (1 + rs))
        
        for cfg in CONFIGS:
            res = run_simulation(df_tf, cfg, tf)
            levels_str = f"{res['L0']}/{res['L1']}/{res['L2']}/{res['L3']}"
            print(f"| {cfg['name']} | {res['trades']} | {res['wr']:.1f}% | {levels_str} | {res['sl']} | ${res['profit']:.2f} | ${res['max_cap']:.2f} | {res['annual_roi']:.1f}% |")

if __name__ == "__main__":
    asyncio.run(main())

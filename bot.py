import os
import csv
import time
import asyncio
import logging
import datetime
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass
from typing import Optional

import httpx
import pandas as pd
from dotenv import load_dotenv
load_dotenv()

# ==========================================
# KONFIGURÁCIÓ
# ==========================================
PRODUCT_SYMBOL = os.getenv("PRODUCT", "BTCUSD")
VIRTUAL_CAPITAL = Decimal(os.getenv("VIRTUAL_CAPITAL", "200"))
BASE_ORDER_USD = VIRTUAL_CAPITAL * Decimal("0.33") # Tőke x 33%

BB_CANDLE_INTERVAL = os.getenv("BB_CANDLE_INTERVAL", "15m")
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "60"))
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# DCA Paraméterek
TP_PCT = Decimal("0.010")   # 1.0% Take Profit
SO_PCT = Decimal("0.015")   # -1.5% L1 drop
SL_PCT = Decimal("0.130")   # -13.0% Stop Loss

# ==========================================
# LOGGING & ADATSTRUKTÚRÁK
# ==========================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")],
)
log = logging.getLogger("dca-sniper")

@dataclass
class Position:
    safety_level: int
    total_invested: Decimal
    total_quantity: Decimal
    average_price: Decimal
    entry_price: Decimal
    extreme_price: Decimal

class BotState:
    def __init__(self, capital: Decimal):
        self.capital = capital
        self.virtual_balance = capital
        self.pos: Optional[Position] = None
        self.win_count = 0
        self.loss_count = 0
        self.csv_file = "live_trades.csv"
        
        try:
            with open(self.csv_file, "x", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["timestamp", "level", "entry", "avg", "exit", "pnl", "result", "invested", "balance"])
        except FileExistsError:
            pass
            
    def log_trade(self, pos: Position, exit_price: Decimal, pnl: Decimal):
        result = "WIN" if pnl > 0 else "LOSS"
        if pnl > 0: self.win_count += 1
        else: self.loss_count += 1
        try:
            with open(self.csv_file, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    pos.safety_level, float(pos.entry_price), float(pos.average_price),
                    float(exit_price), float(pnl.quantize(Decimal("0.0001"))),
                    result, float(pos.total_invested), float(self.virtual_balance.quantize(Decimal("0.0001")))
                ])
        except Exception as e:
            log.error(f"CSV írási hiba: {e}")

# ==========================================
# TELEGRAM KLIENS
# ==========================================
async def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            )
    except Exception as e:
        log.warning(f"Telegram hiba: {e}")

# ==========================================
# FŐBOT LOGIKA
# ==========================================
class SniperBot:
    def __init__(self):
        self.state = BotState(VIRTUAL_CAPITAL)
        self.client = httpx.AsyncClient(timeout=15.0)

    async def get_market_data(self):
        resolution = BB_CANDLE_INTERVAL.replace("m", "")
        interval_seconds = int(resolution) * 60
        to_ts = int(time.time())
        from_ts = to_ts - ((RSI_PERIOD + 10) * interval_seconds * 2)
        
        r = await self.client.get(
            "https://tradingview.ethereal.trade/v1/oracle-price/history",
            params={"symbol": f"{PRODUCT_SYMBOL}-Perp", "resolution": resolution, "from": from_ts, "to": to_ts, "countback": RSI_PERIOD + 10},
        )
        if r.status_code != 200 or r.json().get("s") != "ok": return None, None
        
        data = r.json()
        df = pd.DataFrame({"close": [float(c) for c in data["c"]], "open": [float(o) for o in data["o"]]})
        
        # RSI számolás
        close_delta = df["close"].diff()
        up = close_delta.clip(lower=0).ewm(com=RSI_PERIOD - 1, adjust=True, min_periods=RSI_PERIOD).mean()
        down = (-1 * close_delta.clip(upper=0)).ewm(com=RSI_PERIOD - 1, adjust=True, min_periods=RSI_PERIOD).mean()
        rsi = 100 - (100 / (1 + (up / down)))
        
        current_price = Decimal(str(df.iloc[-1]["close"]))
        open_price = Decimal(str(df.iloc[-1]["open"]))
        current_rsi = float(rsi.iloc[-1])
        
        return current_price, open_price, current_rsi

    async def execute_order(self, amount_usd: Decimal, price: Decimal, level: int):
        if self.state.virtual_balance < amount_usd:
            log.warning(f"Nincs elég tőke az L{level} pozícióhoz! Szükséges: ${amount_usd}, Elérhető: ${self.state.virtual_balance}")
            await send_telegram(f"⚠️ <b>L{level} Blokkolva!</b> Nincs elég szabad tőke.")
            return False
            
        self.state.virtual_balance -= amount_usd
        qty = (amount_usd / price).quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
        
        if self.state.pos is None:
            self.state.pos = Position(0, amount_usd, qty, price, price, price)
            log.info(f"🟢 LONG Nyitva | Ár: ${price:.2f} | Bázis: ${amount_usd}")
            await send_telegram(f"📈 <b>ÚJ LONG (L0)</b>\nÁr: ${price:.2f}\nBefektetve: ${amount_usd:.2f}")
        else:
            p = self.state.pos
            p.safety_level = level
            p.total_invested += amount_usd
            p.total_quantity += qty
            p.average_price = p.total_invested / p.total_quantity
            p.extreme_price = price
            log.info(f"🛡️ Safety Order (L{level}) | Vettünk: ${amount_usd} @ ${price:.2f} | Új átlag: ${p.average_price:.2f}")
            await send_telegram(f"🛡️ <b>SAFETY ORDER (L{level})</b>\nÁtlagár: ${p.average_price:.2f}\nBefektetve: ${p.total_invested:.2f}")
        return True

    async def close_position(self, price: Decimal, reason: str):
        p = self.state.pos
        pnl = (price - p.average_price) * p.total_quantity
        self.state.virtual_balance += p.total_invested + pnl
        self.state.log_trade(p, price, pnl)
        
        log.info(f"Zárás ({reason}) | PnL: ${pnl:.2f} | Új egyenleg: ${self.state.virtual_balance:.2f}")
        await send_telegram(
            f"{'✅' if pnl > 0 else '❌'} <b>ZÁRVA ({reason})</b>\n"
            f"Szint: L{p.safety_level}\nÁtlag: ${p.average_price:.2f} → Kilépés: ${price:.2f}\n"
            f"<b>PnL: {'+' if pnl >= 0 else ''}{pnl:.4f} USD</b>\nSzabad tőke: ${self.state.virtual_balance:.2f}"
        )
        self.state.pos = None

    async def run_once(self):
        try:
            res = await self.get_market_data()
            if not res[0]: return
            price, open_price, rsi = res
            
            p = self.state.pos
            if p:
                if price < p.extreme_price: p.extreme_price = price
                
                # Take Profit (1.0%)
                if price >= p.average_price * (Decimal("1") + TP_PCT):
                    await self.close_position(price, "Take Profit")
                    return
                    
                # Stop Loss (-13.0%)
                if price <= p.average_price * (Decimal("1") - SL_PCT):
                    await self.close_position(price, "Stop Loss")
                    return
                    
                # Safety Order (L1, -1.5%)
                if p.safety_level < 1 and price <= p.average_price * (Decimal("1") - SO_PCT):
                    await self.execute_order(BASE_ORDER_USD * 2, price, 1)
            else:
                # Belépés: RSI < 35 ÉS a gyertya zöld (current > open)
                if rsi < 35 and price > open_price:
                    await self.execute_order(BASE_ORDER_USD, price, 0)
        except Exception as e:
            log.error(f"Hiba futás közben: {e}")

    async def run(self):
        log.info("Bot elindult (Egyszerűsített L1 Sniper)")
        await send_telegram(f"🚀 <b>L1 Sniper Bot Indul</b>\nTőke: ${self.state.capital} | Bázis: ${BASE_ORDER_USD}")
        
        iteration = 0
        while True:
            iteration += 1
            await self.run_once()
            
            if iteration % max(1, 3600 // POLL_INTERVAL_SEC) == 0:
                price = (await self.get_market_data())[0] if (await self.get_market_data()) else Decimal("0")
                if self.state.pos and price > 0:
                    u_pnl = (price - self.state.pos.average_price) * self.state.pos.total_quantity
                    status = f"L{self.state.pos.safety_level} | U-PnL: {u_pnl:+.2f}$"
                else:
                    status = "Üres"
                await send_telegram(f"💓 <b>Életjel</b> | BTC: ${price:.2f}\nSzabad: ${self.state.virtual_balance:.2f}\nStátusz: {status}")
                
            await asyncio.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    bot = SniperBot()
    asyncio.run(bot.run())

"""
Ethereal DCA-Martingale Trading Bot - Multi-Account Szimuláció
Fibonacci Stratégia (LONG ONLY)
"""

import os
import csv
import time
import asyncio
import logging
import secrets
import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional
from dataclasses import dataclass

import httpx
import pandas as pd
from eth_account import Account
from web3 import Web3

from dotenv import load_dotenv
load_dotenv()

# ---------------------------------------------
# KONFIGURÁCIÓ
# ---------------------------------------------
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
SUBACCOUNT: str = os.getenv("SUBACCOUNT", "primary")
PRODUCT_SYMBOL: str = os.getenv("PRODUCT", "BTCUSD")

BASE_ORDER_USD: Decimal = Decimal(os.getenv("BASE_ORDER_USD", "2"))
MAX_SAFETY_LEVELS: int = 4  # 0, 1, 2, 3, 4 (összesen 5 szint)

BB_CANDLE_INTERVAL: str = os.getenv("BB_CANDLE_INTERVAL", "15m")
RSI_PERIOD: int = int(os.getenv("RSI_PERIOD", "14"))

POLL_INTERVAL_SEC: int = int(os.getenv("POLL_INTERVAL_SEC", "60"))
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

API_BASE = "https://api.ethereal.trade/v1"

# ---------------------------------------------
# LOGGING
# ---------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
log = logging.getLogger("ethereal-dca-bot")

# ---------------------------------------------
# DCA PARAMÉTEREK (FIBONACCI)
# ---------------------------------------------
TP_PCT = {
    0: Decimal("0.010"),
    1: Decimal("0.010"),
    2: Decimal("0.010"), # Fib 23.6%
    3: Decimal("0.007"), # Fib 38.2%
    4: Decimal("0.003"), # Minimalizálás
}

SO_PCT = {
    1: Decimal("0.015"), # -1.5%
    2: Decimal("0.030"), # -3.0%
    3: Decimal("0.050"), # -5.0%
    4: Decimal("0.080"), # -8.0%
}

def get_so_amount(level: int) -> Decimal:
    if level == 0:
        return BASE_ORDER_USD
    return BASE_ORDER_USD * Decimal(str(2 ** level))

# ---------------------------------------------
# ADATSTRUKTÚRÁK
# ---------------------------------------------
@dataclass
class DCAPosition:
    side: str
    safety_level: int
    total_invested: Decimal
    total_quantity: Decimal
    average_price: Decimal
    entry_price: Decimal
    extreme_price: Decimal

@dataclass
class SimAccount:
    name: str
    capital: Decimal
    virtual_balance: Decimal
    csv_file: str
    position: Optional[DCAPosition] = None
    win_count: int = 0
    loss_count: int = 0

SIM_ACCOUNTS = [
    SimAccount(name="$50 Fiók", capital=Decimal("50"), virtual_balance=Decimal("50"), csv_file="dca_trades_50.csv"),
    SimAccount(name="$100 Fiók", capital=Decimal("100"), virtual_balance=Decimal("100"), csv_file="dca_trades_100.csv"),
    SimAccount(name="$200 Fiók", capital=Decimal("200"), virtual_balance=Decimal("200"), csv_file="dca_trades_200.csv"),
]

@dataclass
class IndicatorResult:
    price: Decimal
    rsi: float
    signal: str

# ---------------------------------------------
# TELEGRAM
# ---------------------------------------------
_tg_offset: int = 0

async def send_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            )
    except Exception as e:
        log.warning(f"Telegram hiba: {e}")

async def get_telegram_updates() -> list:
    global _tg_offset
    if not TELEGRAM_TOKEN:
        return []
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                params={"offset": _tg_offset, "timeout": 1, "limit": 10},
            )
            if r.status_code == 200:
                updates = r.json().get("result", [])
                if updates:
                    _tg_offset = updates[-1]["update_id"] + 1
                return updates
    except Exception as e:
        pass
    return []

# ---------------------------------------------
# EIP-712 ALÁÍRÁS
# ---------------------------------------------
class EthereumSigner:
    def __init__(self, private_key: str):
        self.account = Account.from_key(private_key)
        self.address = self.account.address

    @staticmethod
    def encode_subaccount(name: str) -> bytes:
        return name.encode("utf-8").ljust(32, b"\x00")

    @staticmethod
    def get_nonce() -> int:
        return int(time.time() * 1e9) + secrets.randbelow(1_000)

    @staticmethod
    def get_signed_at() -> int:
        return int(time.time())

    def sign_trade_order(self, domain, product_id, side, quantity, price, order_type="LIMIT", reduce_only=False):
        nonce = self.get_nonce()
        signed_at = self.get_signed_at()
        subaccount_bytes = self.encode_subaccount(SUBACCOUNT)
        subaccount_hex = "0x" + subaccount_bytes.hex()
        return {}, "mock_signature" # Nem használt valós beküldésre jelenleg

# ---------------------------------------------
# ETHEREAL API KLIENS
# ---------------------------------------------
class EtherealClient:
    def __init__(self, signer: EthereumSigner):
        self.signer = signer
        self.client = httpx.AsyncClient(timeout=15.0)
        self._product_onchain_id = 1

    async def get_candles(self, limit: int = 100) -> pd.DataFrame:
        resolution = BB_CANDLE_INTERVAL.replace("m", "")
        interval_seconds = int(resolution) * 60
        to_ts = int(time.time())
        from_ts = to_ts - (limit * interval_seconds * 2)
        symbol = f"{PRODUCT_SYMBOL}-Perp"
        r = await self.client.get(
            "https://tradingview.ethereal.trade/v1/oracle-price/history",
            params={"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts, "countback": limit},
        )
        if r.status_code != 200 or r.json().get("s") != "ok":
            return pd.DataFrame()
        data = r.json()
        return pd.DataFrame({
            "timestamp": data["t"], 
            "open": [float(o) for o in data["o"]],
            "close": [float(c) for c in data["c"]]
        })

    async def get_current_price(self) -> Decimal:
        r = await self.client.get(
            "https://tradingview.ethereal.trade/v1/oracle-price/history",
            params={"symbol": f"{PRODUCT_SYMBOL}-Perp", "resolution": "1", "countback": 1},
        )
        data = r.json()
        return Decimal(str(data["c"][-1]))

    async def place_order(self, side: str, quantity: Decimal, price: Decimal, reduce_only: bool = False):
        if DRY_RUN: return

    async def close(self):
        await self.client.aclose()

# ---------------------------------------------
# INDIKÁTOROK
# ---------------------------------------------
def calculate_indicators(df: pd.DataFrame) -> IndicatorResult:
    closes = df["close"].astype(float)
    
    # RSI
    close_delta = closes.diff()
    up = close_delta.clip(lower=0)
    down = -1 * close_delta.clip(upper=0)
    ma_up = up.ewm(com=RSI_PERIOD - 1, adjust=True, min_periods=RSI_PERIOD).mean()
    ma_down = down.ewm(com=RSI_PERIOD - 1, adjust=True, min_periods=RSI_PERIOD).mean()
    rsi_s = ma_up / ma_down
    rsi = 100 - (100 / (1 + rsi_s))
    
    price_val = Decimal(str(closes.iloc[-1]))
    open_val = Decimal(str(df.iloc[-1]["open"]))
    rsi_val = float(rsi.iloc[-1])
    
    signal = "HOLD"
    if rsi_val < 35 and price_val > open_val:
        signal = "LONG"
        
    log.info(f"Indikátorok | Ár: {price_val:.2f} | Open: {open_val:.2f} | RSI: {rsi_val:.1f} | Jel: {signal}")
    return IndicatorResult(price=price_val, rsi=rsi_val, signal=signal)

# ---------------------------------------------
# FŐBOT LOGIKA
# ---------------------------------------------
class EtherealDCABot:
    def __init__(self):
        if not PRIVATE_KEY:
            raise ValueError("PRIVATE_KEY hiányzik!")
        self.signer = EthereumSigner(PRIVATE_KEY)
        self.api = EtherealClient(self.signer)
        
        self.trading_active = True
        self._stop_event = asyncio.Event()

        for acc in SIM_ACCOUNTS:
            self._init_csv(acc.csv_file)

    def _init_csv(self, file_path: str):
        try:
            with open(file_path, "x", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([
                    "timestamp", "side", "safety_level", "entry_price", "avg_price", 
                    "exit_price", "pnl_usd", "win_loss", "total_invested", "running_pnl"
                ])
        except FileExistsError:
            pass

    def _append_csv(self, file_path: str, row: list):
        try:
            with open(file_path, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)
        except Exception:
            pass

    async def _close_position(self, acc: SimAccount, exit_price: Decimal, reason: str):
        if not acc.position: return
            
        pos = acc.position
        pnl = (exit_price - pos.average_price) * pos.total_quantity
            
        acc.virtual_balance += pos.total_invested + pnl
        win_loss = "WIN" if pnl > 0 else "LOSS"
        if pnl > 0: acc.win_count += 1
        else: acc.loss_count += 1
            
        self._append_csv(acc.csv_file, [
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), pos.side, pos.safety_level,
            float(pos.entry_price), float(pos.average_price), float(exit_price),
            float(pnl.quantize(Decimal("0.0001"))), win_loss, float(pos.total_invested),
            float(acc.virtual_balance.quantize(Decimal("0.0001")))
        ])
        
        log.info(f"[{acc.name}] Pozíció zárva ({reason}) | {win_loss} | PnL: {pnl:.4f} USD")
        await send_telegram(
            f"{'✅' if pnl > 0 else '❌'} <b>{acc.name} - Zárva ({reason})</b>\n"
            f"Max Szint: {pos.safety_level}\n"
            f"Átlagár: ${pos.average_price:.2f} → Kilépő ár: ${exit_price:.2f}\n"
            f"<b>PnL: {'+' if pnl >= 0 else ''}{pnl:.4f} USD</b>\n"
            f"Új egyenleg: ${acc.virtual_balance:.2f}"
        )
        
        await self.api.place_order(side="SHORT", quantity=pos.total_quantity, price=exit_price, reduce_only=True)
        acc.position = None

    async def _open_or_add_position(self, acc: SimAccount, price: Decimal, level: int):
        amount_usd = get_so_amount(level)
        
        if acc.virtual_balance < amount_usd:
            log.warning(f"[{acc.name}] Elégtelen egyenleg a(z) {level}. szinthez. Kereskedés blokkolva.")
            if level > 0:
                await send_telegram(f"⚠️ <b>{acc.name} - SO Blokkolva!</b>\nElégtelen tőke a(z) {level}. szinthez.")
            return

        acc.virtual_balance -= amount_usd
        quantity = (amount_usd / price).quantize(Decimal("0.00001"), rounding=ROUND_DOWN)

        if acc.position is None:
            acc.position = DCAPosition("LONG", level, amount_usd, quantity, price, price, price)
            log.info(f"[{acc.name}] Új LONG pozíció nyitva | Ár: ${price:.2f} | Összeg: ${amount_usd}")
            await send_telegram(
                f"📈 <b>{acc.name} - ÚJ POZÍCIÓ: LONG</b>\n"
                f"Szint: Alap (0)\nÁr: ${price:.2f}\nBefektetve: ${amount_usd:.2f}"
            )
        else:
            pos = acc.position
            pos.safety_level = level
            pos.total_invested += amount_usd
            pos.total_quantity += quantity
            pos.average_price = pos.total_invested / pos.total_quantity
            pos.extreme_price = price
            
            log.info(f"[{acc.name}] Safety Order #{level} | Vettünk: ${amount_usd} @ ${price:.2f}")
            await send_telegram(
                f"🛡 <b>{acc.name} - SAFETY ORDER #{level}</b>\n"
                f"Új átlagár: ${pos.average_price:.2f}\nÖsszesen befektetve: ${pos.total_invested:.2f}"
            )
            
        await self.api.place_order(side="LONG", quantity=quantity, price=price)

    def _update_extreme_price(self, acc: SimAccount, price: Decimal):
        if acc.position and price < acc.position.extreme_price:
            acc.position.extreme_price = price

    async def _check_take_profit(self, acc: SimAccount, price: Decimal) -> bool:
        if not acc.position: return False
        tp_pct = TP_PCT.get(acc.position.safety_level, Decimal("0.015"))
        target = acc.position.average_price * (Decimal("1") + tp_pct)
        if price >= target:
            await self._close_position(acc, price, "Take Profit")
            return True
        return False

    async def _check_safety_orders(self, acc: SimAccount, price: Decimal) -> bool:
        if not acc.position or acc.position.safety_level >= MAX_SAFETY_LEVELS: return False
        next_level = acc.position.safety_level + 1
        so_drop = SO_PCT.get(next_level, Decimal("0.015"))
        target = acc.position.average_price * (Decimal("1") - so_drop)
        if price <= target:
            await self._open_or_add_position(acc, price, next_level)
            return True
        return False

    async def _check_reset_logic(self, acc: SimAccount, price: Decimal) -> bool:
        if not acc.position or acc.position.safety_level < 3: return False
        target = acc.position.extreme_price * Decimal("1.003")
        if price >= target:
            await self._close_position(acc, price, "Reset Bounce")
            return True
        return False

    async def _check_stop_loss(self, acc: SimAccount, price: Decimal) -> bool:
        if not acc.position: return False
        sl_drop = Decimal("0.13") # -13% az átlagártól
        target = acc.position.average_price * (Decimal("1") - sl_drop)
        if price <= target:
            await self._close_position(acc, price, "Stop Loss (-13%)")
            return True
        return False

    async def run_once(self):
        try:
            df = await self.api.get_candles(limit=RSI_PERIOD + 10)
            if len(df) < RSI_PERIOD: return

            price = await self.api.get_current_price()
            indicator = calculate_indicators(df)
            
            for acc in SIM_ACCOUNTS:
                self._update_extreme_price(acc, price)
                
                if acc.position:
                    if await self._check_take_profit(acc, price): continue
                    if await self._check_reset_logic(acc, price): continue
                    if await self._check_stop_loss(acc, price): continue
                    await self._check_safety_orders(acc, price)
                else:
                    if indicator.signal == "LONG":
                        await self._open_or_add_position(acc, price, 0)

        except Exception as e:
            log.exception(f"Váratlan hiba: {e}")

    async def run(self):
        log.info("Bot elindult")
        await send_telegram("🚀 <b>Fibonacci DCA Bot Indul (LONG ONLY)</b>\nParancsok: /status, /balance, /pnl")

        heartbeat_every = max(1, 3600 // POLL_INTERVAL_SEC)
        iteration = 0
        while not self._stop_event.is_set():
            iteration += 1
            if self.trading_active:
                await self.run_once()

            if iteration % heartbeat_every == 0:
                try:
                    price = await self.api.get_current_price()
                    lines = []
                    for acc in SIM_ACCOUNTS:
                        unrealized = Decimal("0")
                        if acc.position:
                            unrealized = (price - acc.position.average_price) * acc.position.total_quantity
                            pos_txt = f"Lvl {acc.position.safety_level}"
                        else:
                            pos_txt = "Üres"
                        lines.append(f"<b>{acc.name}</b>: ${acc.virtual_balance:.2f} | {pos_txt} | U-PnL: {unrealized:+.4f}$")
                        
                    await send_telegram(f"💓 <b>Életjel</b> | BTC: ${price:.2f}\n\n" + "\n".join(lines))
                except Exception:
                    pass

            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def handle_telegram_commands(self):
        while not self._stop_event.is_set():
            updates = await get_telegram_updates()
            for upd in updates:
                msg = upd.get("message") or upd.get("edited_message", {})
                text = (msg.get("text") or "").strip().lower()
                
                if text == "/status":
                    lines = []
                    for acc in SIM_ACCOUNTS:
                        if acc.position:
                            p = acc.position
                            lines.append(f"<b>{acc.name}</b>\nSzint: {p.safety_level}/{MAX_SAFETY_LEVELS} | Átlag: ${p.average_price:.2f}")
                        else:
                            lines.append(f"<b>{acc.name}</b>: Nincs pozíció.")
                    await send_telegram(f"📊 <b>Státusz</b>\n\n" + "\n\n".join(lines))
                    
                elif text == "/balance":
                    try:
                        price = await self.api.get_current_price()
                        lines = []
                        for acc in SIM_ACCOUNTS:
                            unrealized = Decimal("0")
                            total_equity = acc.virtual_balance
                            if acc.position:
                                unrealized = (price - acc.position.average_price) * acc.position.total_quantity
                                total_equity += acc.position.total_invested + unrealized
                            
                            lines.append(
                                f"<b>{acc.name}</b>\n"
                                f"Szabad: ${acc.virtual_balance:.2f} | Tőke: ${total_equity:.2f}"
                            )
                        await send_telegram(f"💰 <b>Egyenlegek</b>\n\n" + "\n\n".join(lines))
                    except Exception:
                        pass
            await asyncio.sleep(3)

    async def cleanup(self):
        self._stop_event.set()
        await self.api.close()

async def main():
    bot = EtherealDCABot()
    try:
        await asyncio.gather(bot.run(), bot.handle_telegram_commands())
    except KeyboardInterrupt:
        pass
    finally:
        await bot.cleanup()

if __name__ == "__main__":
    asyncio.run(main())

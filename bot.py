"""
Ethereal DCA-Martingale Trading Bot - Multi-Account Szimuláció
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
from dataclasses import dataclass, field

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
MAX_SAFETY_LEVELS: int = int(os.getenv("MAX_SAFETY_LEVELS", "5"))

BB_PERIOD: int = int(os.getenv("BB_PERIOD", "20"))
BB_STD: float = float(os.getenv("BB_STD", "2.0"))
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
# DCA PARAMÉTEREK
# ---------------------------------------------
TP_PCT = {
    0: Decimal("0.015"),
    1: Decimal("0.015"),
    2: Decimal("0.015"),
    3: Decimal("0.010"),
    4: Decimal("0.007"),
    5: Decimal("0.003"),
}

SO_PCT = {
    1: Decimal("0.025"),
    2: Decimal("0.050"),
    3: Decimal("0.100"),
    4: Decimal("0.150"),
    5: Decimal("0.200"),
}

def get_so_amount(level: int) -> Decimal:
    """Visszaadja a vásárolandó USD mennyiséget az adott szintre."""
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
    bb_upper: Decimal
    bb_lower: Decimal
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
            if r.status_code != 200:
                log.warning(f"Telegram API hiba {r.status_code}: {r.text}")
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
        log.debug(f"Telegram polling hiba: {e}")
    return []

# ---------------------------------------------
# EIP-712 ALÁÍRÁS (Csak megőrzés végett)
# ---------------------------------------------
class EthereumSigner:
    def __init__(self, private_key: str):
        if not private_key:
            raise ValueError("PRIVATE_KEY nincs beállítva!")
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.w3 = Web3()

    @staticmethod
    def encode_subaccount(name: str) -> bytes:
        b = name.encode("utf-8")
        if len(b) > 32:
            raise ValueError("Subaccount név max 32 byte lehet!")
        return b.ljust(32, b"\x00")

    @staticmethod
    def get_nonce() -> int:
        return int(time.time() * 1e9) + secrets.randbelow(1_000)

    @staticmethod
    def get_signed_at() -> int:
        return int(time.time())

    @staticmethod
    def to_gwei9(value: str) -> int:
        d = Decimal(value).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)
        return int(d * Decimal("1000000000"))

    def sign_trade_order(self, domain, product_id, side, quantity, price,
                         order_type="LIMIT", reduce_only=False):
        nonce = self.get_nonce()
        signed_at = self.get_signed_at()
        subaccount_bytes = self.encode_subaccount(SUBACCOUNT)
        subaccount_hex = "0x" + subaccount_bytes.hex()
        is_market = (order_type == "MARKET")
        price_bigint = 0 if is_market else self.to_gwei9(price)
        qty_bigint = self.to_gwei9(quantity)

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "TradeOrder": [
                    {"name": "sender", "type": "address"},
                    {"name": "subaccount", "type": "bytes32"},
                    {"name": "quantity", "type": "uint256"},
                    {"name": "price", "type": "uint256"},
                    {"name": "reduceOnly", "type": "bool"},
                    {"name": "side", "type": "uint8"},
                    {"name": "engineType", "type": "uint8"},
                    {"name": "productId", "type": "uint32"},
                    {"name": "nonce", "type": "uint64"},
                    {"name": "signedAt", "type": "uint64"},
                ],
            },
            "primaryType": "TradeOrder",
            "domain": domain,
            "message": {
                "sender": self.address,
                "subaccount": subaccount_bytes,
                "quantity": qty_bigint,
                "price": price_bigint,
                "reduceOnly": reduce_only,
                "side": side,
                "engineType": 0,
                "productId": product_id,
                "nonce": nonce,
                "signedAt": signed_at,
            },
        }
        signed = self.account.sign_typed_data(
            domain_data=domain,
            message_types={"TradeOrder": typed_data["types"]["TradeOrder"]},
            message_data=typed_data["message"],
        )
        signature = signed.signature.hex()
        if not signature.startswith("0x"):
            signature = "0x" + signature

        body_data = {
            "sender": self.address,
            "subaccount": subaccount_hex,
            "quantity": quantity,
            "reduceOnly": reduce_only,
            "side": side,
            "engineType": 0,
            "onchainId": product_id,
            "type": order_type,
            "timeInForce": "GTD" if not is_market else "IOC",
            "postOnly": False,
            "nonce": str(nonce),
            "signedAt": signed_at,
        }
        if not is_market:
            body_data["price"] = price
        return body_data, signature

# ---------------------------------------------
# ETHEREAL API KLIENS
# ---------------------------------------------
class EtherealClient:
    def __init__(self, signer: EthereumSigner):
        self.signer = signer
        self.client = httpx.AsyncClient(timeout=15.0)
        self._domain: Optional[dict] = None
        self._product_onchain_id: Optional[int] = None

    async def get_domain(self) -> dict:
        if not self._domain:
            r = await self.client.get(f"{API_BASE}/rpc/config")
            r.raise_for_status()
            self._domain = r.json()["domain"]
        return self._domain

    async def get_product_info(self) -> dict:
        r = await self.client.get(f"{API_BASE}/product?order=asc&orderBy=createdAt")
        r.raise_for_status()
        products = r.json().get("data", [])
        for p in products:
            if p.get("ticker") == PRODUCT_SYMBOL:
                self._product_onchain_id = p["onchainId"]
                log.info(f"Termék megtalálva: {PRODUCT_SYMBOL} onchainId={self._product_onchain_id}")
                return p
        raise ValueError(f"Termék nem található: {PRODUCT_SYMBOL}")

    async def get_candles(self, limit: int = 100) -> pd.DataFrame:
        resolution_map = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240"}
        resolution = resolution_map.get(BB_CANDLE_INTERVAL, "15")
        interval_seconds = int(resolution) * 60
        to_ts = int(time.time())
        from_ts = to_ts - (limit * interval_seconds * 2)
        symbol = f"{PRODUCT_SYMBOL}-Perp"
        r = await self.client.get(
            "https://tradingview.ethereal.trade/v1/oracle-price/history",
            params={"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts, "countback": limit},
        )
        if r.status_code != 200 or r.json().get("s") != "ok":
            log.warning(f"Candle API hiba: {r.status_code}, fallback")
            return await self._get_price_fallback(limit)
        data = r.json()
        return pd.DataFrame({"timestamp": data["t"], "close": [float(c) for c in data["c"]]})

    async def _get_price_fallback(self, limit: int) -> pd.DataFrame:
        times = pd.date_range(end=pd.Timestamp.now(), periods=limit, freq="15min")
        return pd.DataFrame({"timestamp": times, "close": [80000.0] * limit})

    async def get_current_price(self) -> Decimal:
        to_ts = int(time.time())
        from_ts = to_ts - 300
        symbol = f"{PRODUCT_SYMBOL}-Perp"
        r = await self.client.get(
            "https://tradingview.ethereal.trade/v1/oracle-price/history",
            params={"symbol": symbol, "resolution": "1", "from": from_ts, "to": to_ts, "countback": 1},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("s") == "ok" and data.get("c"):
            return Decimal(str(data["c"][-1]))
        raise ValueError("Nem sikerült az árat lekérni")

    async def place_order(self, side: str, quantity: Decimal, price: Decimal, reduce_only: bool = False):
        if DRY_RUN:
            return
        
        domain = await self.get_domain()
        side_int = 1 if side == "LONG" else 2
        body_data, signature = self.signer.sign_trade_order(
            domain=domain,
            product_id=self._product_onchain_id,
            side=side_int,
            quantity=str(quantity),
            price=str(price),
            order_type="MARKET",
            reduce_only=reduce_only
        )
        # Itt kellene POSTolni a backend felé

    async def close(self):
        await self.client.aclose()


# ---------------------------------------------
# INDIKÁTOROK
# ---------------------------------------------
def calculate_indicators(df: pd.DataFrame) -> IndicatorResult:
    closes = df["close"].astype(float)
    
    # Bollinger Bands
    sma = closes.rolling(BB_PERIOD).mean()
    std = closes.rolling(BB_PERIOD).std()
    upper = sma + (BB_STD * std)
    lower = sma - (BB_STD * std)
    
    # RSI
    close_delta = closes.diff()
    up = close_delta.clip(lower=0)
    down = -1 * close_delta.clip(upper=0)
    ma_up = up.ewm(com=RSI_PERIOD - 1, adjust=True, min_periods=RSI_PERIOD).mean()
    ma_down = down.ewm(com=RSI_PERIOD - 1, adjust=True, min_periods=RSI_PERIOD).mean()
    rsi_s = ma_up / ma_down
    rsi = 100 - (100 / (1 + rsi_s))
    
    price_val = Decimal(str(closes.iloc[-1]))
    upper_val = Decimal(str(upper.iloc[-1]))
    lower_val = Decimal(str(lower.iloc[-1]))
    rsi_val = float(rsi.iloc[-1])
    
    signal = "HOLD"
    if price_val < lower_val and rsi_val < 40:
        signal = "LONG"
    elif price_val > upper_val and rsi_val > 60:
        signal = "SHORT"
        
    log.info(
        f"Indikátorok | Ár: {price_val:.2f} | BB_L: {lower_val:.2f} | "
        f"BB_U: {upper_val:.2f} | RSI: {rsi_val:.1f} | Jel: {signal}"
    )
    return IndicatorResult(price=price_val, bb_upper=upper_val, bb_lower=lower_val, rsi=rsi_val, signal=signal)

# ---------------------------------------------
# FŐBOT LOGIKA
# ---------------------------------------------
class EtherealDCABot:
    def __init__(self):
        if not PRIVATE_KEY:
            raise ValueError("Állítsd be a PRIVATE_KEY értékét!")
        self.signer = EthereumSigner(PRIVATE_KEY)
        self.api = EtherealClient(self.signer)
        
        self.trading_active: bool = True
        self._stop_event = asyncio.Event()

        for acc in SIM_ACCOUNTS:
            self._init_csv(acc.csv_file)

        log.info(f"Bot inicializálva | Cím: {self.signer.address} | Piac: {PRODUCT_SYMBOL}")
        if DRY_RUN:
            log.info("🧪 DRY RUN MÓD AKTÍV - Csak szimuláció, nincs valós order")

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
        except Exception as e:
            log.warning(f"CSV mentési hiba ({file_path}): {e}")

    async def _close_position(self, acc: SimAccount, exit_price: Decimal, reason: str):
        if not acc.position:
            return
            
        pos = acc.position
        if pos.side == "LONG":
            pnl = (exit_price - pos.average_price) * pos.total_quantity
        else:
            pnl = (pos.average_price - exit_price) * pos.total_quantity
            
        acc.virtual_balance += pnl
        win_loss = "WIN" if pnl > 0 else "LOSS"
        
        if pnl > 0:
            acc.win_count += 1
        else:
            acc.loss_count += 1
            
        self._append_csv(acc.csv_file, [
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            pos.side,
            pos.safety_level,
            float(pos.entry_price),
            float(pos.average_price),
            float(exit_price),
            float(pnl.quantize(Decimal("0.0001"))),
            win_loss,
            float(pos.total_invested),
            float(acc.virtual_balance.quantize(Decimal("0.0001")))
        ])
        
        log.info(f"[{acc.name}] Pozíció zárva ({reason}) | {win_loss} | PnL: {pnl:.4f} USD")
        await send_telegram(
            f"{'✅' if pnl > 0 else '❌'} <b>{acc.name} - Zárva ({reason})</b>\n"
            f"Irány: {pos.side} | Max Szint: {pos.safety_level}\n"
            f"Átlagár: ${pos.average_price:.2f} → Kilépő ár: ${exit_price:.2f}\n"
            f"<b>PnL: {'+' if pnl >= 0 else ''}{pnl:.4f} USD</b>\n"
            f"Új egyenleg: ${acc.virtual_balance:.2f}"
        )
        
        await self.api.place_order(
            side="SHORT" if pos.side == "LONG" else "LONG",
            quantity=pos.total_quantity,
            price=exit_price,
            reduce_only=True
        )
        
        acc.position = None

    async def _open_or_add_position(self, acc: SimAccount, side: str, price: Decimal, level: int):
        amount_usd = get_so_amount(level)
        
        if acc.virtual_balance < amount_usd:
            log.warning(f"[{acc.name}] Nincs elég egyenleg a(z) {level}. szinthez. Egyenleg: ${acc.virtual_balance:.2f}, Kellene: ${amount_usd:.2f}. Kereskedés kihagyva a szinten.")
            if level > 0:
                await send_telegram(f"⚠️ <b>{acc.name} - SO Blokkolva!</b>\nElégtelen tőke a(z) {level}. szinthez (${amount_usd:.2f} hiányzik). Pozíció várakozik.")
            return

        acc.virtual_balance -= amount_usd
        
        quantity = (amount_usd / price).quantize(Decimal("0.00001"), rounding=ROUND_DOWN)
        
        if quantity <= Decimal("0"):
            log.warning(f"[{acc.name}] Túl kicsi mennyiség, kihagyás.")
            acc.virtual_balance += amount_usd
            return

        if acc.position is None:
            # Új pozíció
            acc.position = DCAPosition(
                side=side,
                safety_level=level,
                total_invested=amount_usd,
                total_quantity=quantity,
                average_price=price,
                entry_price=price,
                extreme_price=price
            )
            log.info(f"[{acc.name}] Új {side} pozíció nyitva | Ár: ${price:.2f} | Összeg: ${amount_usd}")
            await send_telegram(
                f"{'📈' if side == 'LONG' else '📉'} <b>{acc.name} - ÚJ POZÍCIÓ: {side}</b>\n"
                f"Szint: Alap (0)\n"
                f"Ár: ${price:.2f}\n"
                f"Befektetve: ${amount_usd:.2f}"
            )
        else:
            # Safety Order
            pos = acc.position
            pos.safety_level = level
            pos.total_invested += amount_usd
            pos.total_quantity += quantity
            pos.average_price = pos.total_invested / pos.total_quantity
            pos.extreme_price = price
            
            log.info(f"[{acc.name}] Safety Order #{level} | {side} | Vettünk: ${amount_usd} @ ${price:.2f} | Új átlag: ${pos.average_price:.2f}")
            await send_telegram(
                f"🛡 <b>{acc.name} - SAFETY ORDER #{level} ({side})</b>\n"
                f"Hozzáadva: ${amount_usd:.2f} @ ${price:.2f}\n"
                f"Új átlagár: ${pos.average_price:.2f}\n"
                f"Összesen befektetve: ${pos.total_invested:.2f}"
            )
            
        await self.api.place_order(side=side, quantity=quantity, price=price)

    def _update_extreme_price(self, acc: SimAccount, price: Decimal):
        if not acc.position:
            return
        if acc.position.side == "LONG":
            if price < acc.position.extreme_price:
                acc.position.extreme_price = price
        else:
            if price > acc.position.extreme_price:
                acc.position.extreme_price = price

    async def _check_take_profit(self, acc: SimAccount, price: Decimal) -> bool:
        if not acc.position: return False
        
        tp_pct = TP_PCT.get(acc.position.safety_level, Decimal("0.015"))
        if acc.position.side == "LONG":
            target = acc.position.average_price * (Decimal("1") + tp_pct)
            if price >= target:
                await self._close_position(acc, price, "Take Profit")
                return True
        else:
            target = acc.position.average_price * (Decimal("1") - tp_pct)
            if price <= target:
                await self._close_position(acc, price, "Take Profit")
                return True
        return False

    async def _check_safety_orders(self, acc: SimAccount, price: Decimal) -> bool:
        if not acc.position: return False
        if acc.position.safety_level >= MAX_SAFETY_LEVELS: return False
        
        next_level = acc.position.safety_level + 1
        so_drop = SO_PCT.get(next_level, Decimal("0.025"))
        
        if acc.position.side == "LONG":
            target = acc.position.average_price * (Decimal("1") - so_drop)
            if price <= target:
                await self._open_or_add_position(acc, "LONG", price, next_level)
                return True
        else:
            target = acc.position.average_price * (Decimal("1") + so_drop)
            if price >= target:
                await self._open_or_add_position(acc, "SHORT", price, next_level)
                return True
        return False

    async def _check_reset_logic(self, acc: SimAccount, price: Decimal) -> bool:
        if not acc.position or acc.position.safety_level < 3: return False
        
        if acc.position.side == "LONG":
            target = acc.position.extreme_price * Decimal("1.003")
            if price >= target:
                await self._close_position(acc, price, "Reset Bounce")
                return True
        else:
            target = acc.position.extreme_price * Decimal("0.997")
            if price <= target:
                await self._close_position(acc, price, "Reset Bounce")
                return True
        return False

    async def _check_stop_loss(self, acc: SimAccount, price: Decimal) -> bool:
        if not acc.position or acc.position.safety_level < MAX_SAFETY_LEVELS: return False
        
        sl_drop = Decimal("0.05")
        if acc.position.side == "LONG":
            target = acc.position.average_price * (Decimal("1") - sl_drop)
            if price <= target:
                await self._close_position(acc, price, "Stop Loss")
                return True
        else:
            target = acc.position.average_price * (Decimal("1") + sl_drop)
            if price >= target:
                await self._close_position(acc, price, "Stop Loss")
                return True
        return False

    async def run_once(self):
        try:
            df = await self.api.get_candles(limit=max(BB_PERIOD, RSI_PERIOD) + 10)
            if len(df) < max(BB_PERIOD, RSI_PERIOD):
                log.warning(f"Nem elég adat az indikátorokhoz.")
                return

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
                    if indicator.signal in ("LONG", "SHORT"):
                        await self._open_or_add_position(acc, indicator.signal, price, 0)

        except httpx.HTTPError as e:
            log.error(f"HTTP hiba: {e}")
        except Exception as e:
            log.exception(f"Váratlan hiba: {e}")

    async def run(self):
        log.info(f"Bot elindult | Polling: {POLL_INTERVAL_SEC}s")
        try:
            await self.api.get_product_info()
        except ValueError as e:
            log.error(str(e))
            return

        acc_lines = "\n".join([f"• {a.name} (Tőke: ${a.capital:.0f})" for a in SIM_ACCOUNTS])
        await send_telegram(
            f"🚀 <b>Multi-Account DCA Bot Indul</b>\n"
            f"Piac: {PRODUCT_SYMBOL}\n"
            f"Mód: {'🧪 DRY RUN' if DRY_RUN else '🔥 ÉLES'}\n\n"
            f"Fiókok:\n{acc_lines}\n\n"
            f"Parancsok: /pnl /status /stop /start /reset"
        )

        heartbeat_every = max(1, 3600 // POLL_INTERVAL_SEC)
        iteration = 0
        while not self._stop_event.is_set():
            iteration += 1
            if self.trading_active:
                await self.run_once()
            else:
                log.info(f"Szünet (/stop) | #{iteration}")

            if iteration % heartbeat_every == 0:
                try:
                    price = await self.api.get_current_price()
                    lines = []
                    for acc in SIM_ACCOUNTS:
                        unrealized = Decimal("0")
                        if acc.position:
                            p = acc.position
                            if p.side == "LONG":
                                unrealized = (price - p.average_price) * p.total_quantity
                            else:
                                unrealized = (p.average_price - price) * p.total_quantity
                            pos_txt = f"{p.side} (Lvl {p.safety_level})"
                        else:
                            pos_txt = "Nincs pozíció"
                        
                        lines.append(f"<b>{acc.name}</b>: ${acc.virtual_balance:.2f} | {pos_txt} | U-PnL: {'+' if unrealized >= 0 else ''}{unrealized:.4f}$")
                        
                    await send_telegram(
                        f"💓 <b>Életjel</b> | {PRODUCT_SYMBOL}: ${price:.2f}\n\n"
                        + "\n".join(lines)
                    )
                except Exception as e:
                    log.warning(f"Heartbeat hiba: {e}")

            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def handle_telegram_commands(self):
        log.info("Telegram parancs-figyelő elindult")
        while not self._stop_event.is_set():
            updates = await get_telegram_updates()
            for upd in updates:
                msg = upd.get("message") or upd.get("edited_message", {})
                text = (msg.get("text") or "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
                    continue

                if text == "/stop":
                    self.trading_active = False
                    await send_telegram("⏸ <b>Kereskedés szüneteltetve</b>\nFolytatáshoz: /start")

                elif text == "/start":
                    self.trading_active = True
                    await send_telegram(f"▶️ <b>Kereskedés folytatva</b> | {PRODUCT_SYMBOL}")
                    
                elif text == "/reset":
                    try:
                        price = await self.api.get_current_price()
                        reset_count = 0
                        for acc in SIM_ACCOUNTS:
                            if acc.position:
                                await self._close_position(acc, price, "Manuális Reset")
                                reset_count += 1
                        if reset_count > 0:
                            await send_telegram(f"🔄 {reset_count} fiók pozíciója manuálisan lezárva.")
                        else:
                            await send_telegram("Nincs nyitott pozíció amit resetelni kéne.")
                    except Exception as e:
                        await send_telegram(f"❌ Hiba a reset során: {e}")

                elif text == "/status":
                    state = "▶️ AKTÍV" if self.trading_active else "⏸ SZÜNET"
                    lines = []
                    for acc in SIM_ACCOUNTS:
                        if acc.position:
                            p = acc.position
                            lines.append(
                                f"<b>{acc.name}</b>\n"
                                f"  Irány: {p.side} | Szint: {p.safety_level}/{MAX_SAFETY_LEVELS}\n"
                                f"  Átlag: ${p.average_price:.2f} | Befektetve: ${p.total_invested:.2f}"
                            )
                        else:
                            lines.append(f"<b>{acc.name}</b>: Jelenleg nincs nyitott pozíció.")
                    await send_telegram(f"📊 <b>Státusz – {state}</b>\n\n" + "\n\n".join(lines))

                elif text == "/balance":
                    try:
                        price = await self.api.get_current_price()
                        lines = []
                        for acc in SIM_ACCOUNTS:
                            unrealized = Decimal("0")
                            if acc.position:
                                p = acc.position
                                if p.side == "LONG":
                                    unrealized = (price - p.average_price) * p.total_quantity
                                else:
                                    unrealized = (p.average_price - price) * p.total_quantity
                            
                            lines.append(
                                f"<b>{acc.name}</b>\n"
                                f"  Egyenleg: ${acc.virtual_balance:.2f}\n"
                                f"  Nem realizált: {'+' if unrealized >= 0 else ''}{unrealized:.4f} USD"
                            )
                        await send_telegram(
                            f"💰 <b>Egyenlegek | {PRODUCT_SYMBOL}: ${price:.2f}</b>\n\n"
                            + "\n\n".join(lines)
                        )
                    except Exception as e:
                        await send_telegram(f"❌ Egyenleg hiba: {e}")

                elif text == "/pnl":
                    lines = []
                    for acc in SIM_ACCOUNTS:
                        try:
                            with open(acc.csv_file, newline="", encoding="utf-8") as f:
                                trades = list(csv.DictReader(f))
                            total = len(trades)
                            if total == 0:
                                lines.append(f"<b>{acc.name}</b>: Nincs lezárt trade.")
                                continue
                                
                            wins = sum(1 for t in trades if t["win_loss"] == "WIN")
                            pnls = [float(t["pnl_usd"]) for t in trades]
                            total_pnl = sum(pnls)
                            win_rate = wins / total * 100
                            sign = "+" if total_pnl >= 0 else ""
                            
                            lines.append(
                                f"<b>{acc.name}</b>\n"
                                f"  Tradek: {total} | Win: {win_rate:.1f}%\n"
                                f"  Total PnL: {sign}{total_pnl:.4f} USD"
                            )
                        except FileNotFoundError:
                            lines.append(f"<b>{acc.name}</b>: Nincs adat.")
                    
                    await send_telegram("📊 <b>Összesített Statisztika</b>\n\n" + "\n\n".join(lines))

            await asyncio.sleep(3)

    async def cleanup(self):
        self._stop_event.set()
        await self.api.close()

# ---------------------------------------------
# BELÉPÉSI PONT
# ---------------------------------------------
async def main():
    bot = EtherealDCABot()
    try:
        await asyncio.gather(
            bot.run(),
            bot.handle_telegram_commands(),
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Bot leállítva (Ctrl+C)")
    except Exception as e:
        log.exception(f"Fatális hiba: {e}")
    finally:
        await bot.cleanup()
        await send_telegram("🛑 <b>Bot leállítva</b>")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Program kilépett.")

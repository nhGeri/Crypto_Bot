"""
Ethereal Trading Bot - Bollinger Bands Strategy
Automatikus long/short kereskedés + airdrop farmolás
Telegram vezérlés: /start /stop /status /balance
"""

import os
import time
import math
import asyncio
import logging
import secrets
from decimal import Decimal, ROUND_DOWN
from typing import Optional
from dataclasses import dataclass

import httpx
import pandas as pd
import numpy as np
from eth_account import Account
from eth_account.messages import encode_defunct
from web3 import Web3

# ---------------------------------------------
# KONFIGURÁCIÓ - töltsd ki a .env fájlban!
# ---------------------------------------------
from dotenv import load_dotenv
load_dotenv()

PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")          # főtárca private key (0x-szel)
SIGNER_KEY: str = os.getenv("SIGNER_KEY", "")            # linked signer key (bot generálja, ha üres)
SUBACCOUNT: str = os.getenv("SUBACCOUNT", "primary")      # subaccount neve
PRODUCT_SYMBOL: str = os.getenv("PRODUCT", "BTCUSD")      # melyik piacon kereskedj

# Bollinger Bands paraméterek
BB_PERIOD: int = int(os.getenv("BB_PERIOD", "20"))
BB_STD: float = float(os.getenv("BB_STD", "2.0"))
BB_CANDLE_INTERVAL: str = os.getenv("BB_CANDLE_INTERVAL", "1m")  # 1m, 5m, 15m, 1h

# Kockázatkezelés
POSITION_SIZE_USD: str = os.getenv("POSITION_SIZE_USD", "10")   # pozíció méret USD-ben
LEVERAGE: int = int(os.getenv("LEVERAGE", "3"))                  # tőkeáttétel
STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "0.5")) # % stop-loss
TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "1.0"))  # % take-profit
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "1"))

# Futási paraméterek
POLL_INTERVAL_SEC: int = int(os.getenv("POLL_INTERVAL_SEC", "60"))  # mennyit várjon loop között
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"      # true = nem küld valódi ordereket

# Telegram értesítők (opcionális)
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
log = logging.getLogger("ethereal-bot")

# ---------------------------------------------
# TELEGRAM ÉRTESÍTŐK ÉS PARANCSKEZELŐ
# ---------------------------------------------
_tg_offset: int = 0  # utolsó feldolgozott update_id

async def send_telegram(message: str):
    """Telegram üzenet küldése."""
    if not TELEGRAM_TOKEN:
        log.warning("Telegram hiba: TELEGRAM_TOKEN hiányzik!")
        return
    if not TELEGRAM_CHAT_ID:
        log.warning("Telegram hiba: TELEGRAM_CHAT_ID hiányzik!")
        return
    try:
        log.info(f"Telegram küldés → chat_id={TELEGRAM_CHAT_ID}")
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            )
            if r.status_code == 200:
                log.info("Telegram: üzenet sikeresen elküldve")
            else:
                log.warning(f"Telegram API hiba {r.status_code}: {r.text}")
    except Exception as e:
        log.warning(f"Telegram küldési hiba: {e}")


async def get_telegram_updates() -> list:
    """Lekéri az új Telegram üzeneteket (long-poll timeout=1s)."""
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
# ADATSTRUKTÚRÁK
# ---------------------------------------------
@dataclass
class BollingerBands:
    upper: Decimal
    middle: Decimal
    lower: Decimal
    price: Decimal
    signal: str  # "LONG", "SHORT", "HOLD"


@dataclass
class Position:
    side: str       # "BUY" or "SELL"
    entry_price: Decimal
    quantity: Decimal
    stop_loss: Decimal
    take_profit: Decimal


# ---------------------------------------------
# EIP-712 ALÁÍRÁS (web3.py alapú)
# ---------------------------------------------
class EthereumSigner:
    """EIP-712 üzenetek aláírása az Ethereal API-hoz."""

    def __init__(self, private_key: str):
        if not private_key:
            raise ValueError("PRIVATE_KEY nincs beállítva!")
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.w3 = Web3()

    @staticmethod
    def encode_subaccount(name: str) -> bytes:
        """Subaccount nevet bytes32-re konvertál (UTF-8, jobbra nullával kitöltve)."""
        b = name.encode("utf-8")
        if len(b) > 32:
            raise ValueError("Subaccount név max 32 byte lehet!")
        return b.ljust(32, b"\x00")

    @staticmethod
    def get_nonce() -> int:
        """Nanosecond timestamp + random padding = replay-safe nonce."""
        ns = int(time.time() * 1e9)
        rand = secrets.randbelow(1_000)
        return ns + rand

    @staticmethod
    def get_signed_at() -> int:
        """Unix timestamp másodpercben."""
        return int(time.time())

    @staticmethod
    def to_gwei9(value: str) -> int:
        """Decimal string → 9 tizedesjegyű bigint (Ethereal precision)."""
        d = Decimal(value).quantize(Decimal("0.000000001"), rounding=ROUND_DOWN)
        return int(d * Decimal("1000000000"))

    def sign_trade_order(
        self,
        domain: dict,
        product_id: int,
        side: int,       # 0=BUY, 1=SELL
        quantity: str,
        price: str,      # "0" for market orders
        order_type: str = "LIMIT",
        reduce_only: bool = False,
    ) -> tuple[dict, str]:
        """Aláír egy TradeOrder EIP-712 üzenetet.
        
        Returns: (body_data, signature_hex)
        """
        nonce = self.get_nonce()
        signed_at = self.get_signed_at()
        subaccount_bytes = self.encode_subaccount(SUBACCOUNT)
        subaccount_hex = "0x" + subaccount_bytes.hex()

        is_market = (order_type == "MARKET")
        price_bigint = 0 if is_market else self.to_gwei9(price)
        qty_bigint = self.to_gwei9(quantity)

        # EIP-712 typed data
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
                "engineType": 0,  # 0 = Perp
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
        self._product_id: Optional[int] = None
        self._product_onchain_id: Optional[int] = None

    async def get_domain(self) -> dict:
        if not self._domain:
            r = await self.client.get(f"{API_BASE}/rpc/config")
            r.raise_for_status()
            self._domain = r.json()["domain"]
            log.info(f"EIP-712 domain betöltve: {self._domain}")
        return self._domain

    async def get_product_info(self) -> dict:
        """PRODUCT_SYMBOL alapján megkeresi a productId-t."""
        r = await self.client.get(f"{API_BASE}/product?order=asc&orderBy=createdAt")
        r.raise_for_status()
        products = r.json().get("data", [])
        for p in products:
            if p.get("ticker") == PRODUCT_SYMBOL:
                self._product_onchain_id = p["onchainId"]
                log.info(f"Termek megtalálva: {PRODUCT_SYMBOL} onchainId={self._product_onchain_id}")
                return p
        raise ValueError(f"Termék nem található: {PRODUCT_SYMBOL}. Elérhető: {[p['symbol'] for p in products]}")

    async def get_candles(self, limit: int = 100) -> pd.DataFrame:
        """TradingView API-bol OHLCV adatok lekérése."""
        import time as time_module
        resolution_map = {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "4h": "240"}
        resolution = resolution_map.get(BB_CANDLE_INTERVAL, "5")
        interval_seconds = int(resolution) * 60
        to_ts = int(time_module.time())
        from_ts = to_ts - (limit * interval_seconds * 2)
        symbol = f"{PRODUCT_SYMBOL}-Perp"
        r = await self.client.get(
            "https://tradingview.ethereal.trade/v1/oracle-price/history",
            params={"symbol": symbol, "resolution": resolution, "from": from_ts, "to": to_ts, "countback": limit}
        )
        if r.status_code != 200 or r.json().get("s") != "ok":
            log.warning(f"Candle API hiba: {r.status_code}, fallback")
            return await self._get_price_fallback(limit)
        data = r.json()
        closes = data["c"]
        timestamps = data["t"]
        df = pd.DataFrame({"timestamp": timestamps, "close": [float(c) for c in closes]})
        return df

    async def _get_price_fallback(self, limit: int) -> pd.DataFrame:
        """Fallback ha a fo candle API nem elerheto."""
        log.warning("Fallback: dummy adatok, BB nem pontos!")
        times = pd.date_range(end=pd.Timestamp.now(), periods=limit, freq="1min")
        closes = [80000.0] * limit
        return pd.DataFrame({"timestamp": times, "close": closes})

    async def get_current_price(self) -> Decimal:
        import time as time_module
        to_ts = int(time_module.time())
        from_ts = to_ts - 300
        symbol = f"{PRODUCT_SYMBOL}-Perp"
        r = await self.client.get(
            "https://tradingview.ethereal.trade/v1/oracle-price/history",
            params={"symbol": symbol, "resolution": "1", "from": from_ts, "to": to_ts, "countback": 1}
        )
        r.raise_for_status()
        data = r.json()
        if data.get("s") == "ok" and data.get("c"):
            return Decimal(str(data["c"][-1]))
        raise ValueError("Nem sikerult az arat lekerni")

    async def get_positions(self) -> list:
        r = await self.client.get(
            f"{API_BASE}/position",
            params={"address": self.signer.address, "subaccount": SUBACCOUNT}
        )
        if r.status_code == 200:
            return r.json().get("positions", [])
        return []

    async def place_order(
        self,
        side: int,        # 0=BUY, 1=SELL
        quantity: str,
        price: str = "0",
        order_type: str = "MARKET",
        reduce_only: bool = False,
    ) -> dict:
        if DRY_RUN:
            side_str = "LONG" if side == 0 else "SHORT"
            log.info(f"[DRY RUN] {order_type} {side_str} {quantity} @ {price if price != '0' else 'MARKET'}")
            return {"status": "dry_run", "side": side_str, "quantity": quantity}

        domain = await self.get_domain()
        if not self._product_onchain_id:
            await self.get_product_info()

        body_data, signature = self.signer.sign_trade_order(
            domain=domain,
            product_id=self._product_onchain_id,
            side=side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            reduce_only=reduce_only,
        )

        r = await self.client.post(
            f"{API_BASE}/order",
            json={"data": body_data, "signature": signature},
        )
        
        if r.status_code not in (200, 201):
            log.error(f"Order hiba {r.status_code}: {r.text}")
            return {"error": r.text}
        
        result = r.json()
        log.info(f"Order elküldve: {result}")
        return result

    async def close_all_positions(self):
        """Minden nyitott pozíció lezarasa (market close)."""
        positions = await self.get_positions()
        for pos in positions:
            if float(pos.get("quantity", 0)) != 0:
                side = 1 if pos["side"] == "BUY" else 0  # ellentétes side
                qty = str(abs(float(pos["quantity"])))
                log.info(f"Pozicio zarasa: {pos['side']} {qty}")
                await self.place_order(side=side, quantity=qty, order_type="MARKET", reduce_only=True)

    async def close(self):
        await self.client.aclose()


# ---------------------------------------------
# BOLLINGER BANDS STRATÉGIA
# ---------------------------------------------
def calculate_bollinger_bands(df: pd.DataFrame) -> BollingerBands:
    """Bollinger Bands számítás és jel generálás."""
    closes = df["close"].astype(float)
    
    sma = closes.rolling(BB_PERIOD).mean()
    std = closes.rolling(BB_PERIOD).std()
    
    upper = sma + (BB_STD * std)
    lower = sma - (BB_STD * std)
    
    current_price = Decimal(str(closes.iloc[-1]))
    upper_val = Decimal(str(upper.iloc[-1]))
    lower_val = Decimal(str(lower.iloc[-1]))
    middle_val = Decimal(str(sma.iloc[-1]))

    # Jel logika:
    # - Ha az ár az ALSÓ sáv alatt van → LONG (túladott)
    # - Ha az ár a FELSŐ sáv felett van → SHORT (túlvett)
    # - Különben → HOLD
    if current_price < lower_val:
        signal = "LONG"
    elif current_price > upper_val:
        signal = "SHORT"
    else:
        # Középvonal visszatérés: ha a középső sáv felé tart
        prev_close = Decimal(str(closes.iloc[-2]))
        if prev_close < lower_val and current_price > lower_val:
            signal = "LONG"  # lower sáv felett visszatért → erős long
        elif prev_close > upper_val and current_price < upper_val:
            signal = "SHORT"  # upper sáv alá visszaesett → erős short
        else:
            signal = "HOLD"

    log.info(
        f"BB | Ár: {current_price:.2f} | "
        f"Felső: {upper_val:.2f} | Közép: {middle_val:.2f} | Alsó: {lower_val:.2f} | "
        f"Jel: {signal}"
    )

    return BollingerBands(
        upper=upper_val,
        middle=middle_val,
        lower=lower_val,
        price=current_price,
        signal=signal,
    )


def calculate_stop_take(
    side: str,
    entry_price: Decimal,
) -> tuple[Decimal, Decimal]:
    """Stop-loss és take-profit árszintek kiszámítása."""
    sl_pct = Decimal(str(STOP_LOSS_PCT / 100))
    tp_pct = Decimal(str(TAKE_PROFIT_PCT / 100))

    if side == "LONG":
        stop_loss = entry_price * (1 - sl_pct)
        take_profit = entry_price * (1 + tp_pct)
    else:  # SHORT
        stop_loss = entry_price * (1 + sl_pct)
        take_profit = entry_price * (1 - tp_pct)

    return stop_loss, take_profit


# ---------------------------------------------
# FŐBOT LOGIKA
# ---------------------------------------------
class EtherealBot:
    def __init__(self):
        if not PRIVATE_KEY:
            raise ValueError("Állítsd be a PRIVATE_KEY értékét a .env fájlban!")
        
        self.signer = EthereumSigner(PRIVATE_KEY)
        self.api = EtherealClient(self.signer)
        self.current_position: Optional[Position] = None
        self.trading_active: bool = True   # /stop → False, /start → True
        self._stop_event = asyncio.Event()  # teljes leállításhoz
        log.info(f"Bot inicializálva | Cím: {self.signer.address} | Piac: {PRODUCT_SYMBOL}")
        if DRY_RUN:
            log.warning("DRY RUN módban fut – nem küld valódi ordereket!")

    async def check_stop_take(self, price: Decimal) -> bool:
        """Ellenőrzi a stop-loss és take-profit szinteket."""
        if not self.current_position:
            return False

        pos = self.current_position
        hit = False

        if pos.side == "LONG":
            if price <= pos.stop_loss:
                log.warning(f"STOP-LOSS: {price:.2f} <= {pos.stop_loss:.2f}")
                hit = True
            elif price >= pos.take_profit:
                log.info(f"TAKE-PROFIT: {price:.2f} >= {pos.take_profit:.2f}")
                hit = True
        else:  # SHORT
            if price >= pos.stop_loss:
                log.warning(f"STOP-LOSS: {price:.2f} >= {pos.stop_loss:.2f}")
                hit = True
            elif price <= pos.take_profit:
                log.info(f"TAKE-PROFIT: {price:.2f} <= {pos.take_profit:.2f}")
                hit = True

        if hit:
            await send_telegram(
                f"<b>Pozicio zarva</b>\n"
                f"Piac: {PRODUCT_SYMBOL}\n"
                f"Ar: ${price:.2f}\n"
                f"Oldal: {pos.side}"
            )
            await self.close_position()
            return True
        return False

    async def close_position(self):
        """Aktuális pozíció lezarasa."""
        if not self.current_position:
            return
        
        pos = self.current_position
        close_side = 1 if pos.side == "LONG" else 0
        qty = str(pos.quantity.quantize(Decimal("0.00001"), rounding=ROUND_DOWN))
        
        log.info(f"Pozicio zarasa: {pos.side} {qty}")
        await self.api.place_order(
            side=close_side,
            quantity=qty,
            order_type="MARKET",
            reduce_only=True,
        )
        self.current_position = None

    async def open_position(self, signal: str, price: Decimal):
        """Új pozíció nyitása BB jel alapján."""
        if self.current_position:
            # Ha van nyitott pozíció, ellenőrizzük irányát
            if (signal == "LONG" and self.current_position.side == "LONG") or \
               (signal == "SHORT" and self.current_position.side == "SHORT"):
                return  # Ugyanolyan irányú → nem csinálunk semmit
            # Ellentétes irány → zárjuk az aktuálisát
            log.info(f"Iranyvaltas: {self.current_position.side} → {signal}")
            await self.close_position()

        # Pozíció méret kiszámítása
        position_usd = Decimal(POSITION_SIZE_USD)
        quantity = (position_usd * LEVERAGE / price).quantize(
            Decimal("0.00001"), rounding=ROUND_DOWN
        )
        log.info(f"Számított pozíció: {quantity} BTC (${position_usd} * {LEVERAGE}x / ${price:.2f})")

        if quantity <= Decimal("0.00001"):
            log.warning(f"Tul kis pozicio méret: {quantity} BTC, kihagyás")
            return

        side = 0 if signal == "LONG" else 1
        stop_loss, take_profit = calculate_stop_take(signal, price)

        log.info(
            f"{'LONG' if signal == 'LONG' else 'SHORT'} megnyitasa | "
            f"Qty: {quantity} | Ár: ~{price:.2f} | "
            f"SL: {stop_loss:.2f} | TP: {take_profit:.2f}"
        )

        result = await self.api.place_order(
            side=side,
            quantity=str(quantity),
            order_type="MARKET",
        )

        if "error" not in result:
            self.current_position = Position(
                side=signal,
                entry_price=price,
                quantity=quantity,
                stop_loss=stop_loss,
                take_profit=take_profit,
            )

    async def run_once(self, iteration: int = 0):
        """Egy iteracio futtatasa."""
        try:
            # Gyertyadatok lekérése
            df = await self.api.get_candles(limit=BB_PERIOD + 10)
            if len(df) < BB_PERIOD:
                log.warning(f"Nem elég adat: {len(df)} < {BB_PERIOD}")
                return

            # Aktuális ár
            price = await self.api.get_current_price()

            # Stop-loss / Take-profit ellenőrzés
            if await self.check_stop_take(price):
                return

            # Bollinger Bands számítás
            bb = calculate_bollinger_bands(df)

            # Trade döntés
            if bb.signal in ("LONG", "SHORT"):
                await self.open_position(bb.signal, price)
            else:
                log.info(f"HOLD | Ár: {price:.2f}")

        except httpx.HTTPError as e:
            log.error(f"HTTP hiba: {e}")
        except Exception as e:
            log.exception(f"Váratlan hiba: {e}")

    async def handle_telegram_commands(self):
        """Háttérben futó Telegram parancs-figyelő loop."""
        log.info("Telegram parancs-figyelő elindult")
        while not self._stop_event.is_set():
            updates = await get_telegram_updates()
            for upd in updates:
                msg = upd.get("message") or upd.get("edited_message", {})
                text = (msg.get("text") or "").strip().lower()
                chat_id = str(msg.get("chat", {}).get("id", ""))

                # Csak az engedélyezett chat_id-ről fogad
                if TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
                    continue

                if text == "/stop":
                    self.trading_active = False
                    log.info("Telegram: kereskedés szüneteltetve (/stop)")
                    await send_telegram(
                        "⏸ <b>Kereskedés szüneteltetve</b>\n"
                        "Nyitott pozíció megmarad.\n"
                        "Folytatáshoz: /start"
                    )

                elif text == "/start":
                    self.trading_active = True
                    log.info("Telegram: kereskedés folytatva (/start)")
                    await send_telegram(
                        "▶️ <b>Kereskedés folytatva</b>\n"
                        f"Piac: {PRODUCT_SYMBOL} | Leverage: {LEVERAGE}x"
                    )

                elif text == "/status":
                    pos_text = "Nincs nyitott pozíció"
                    if self.current_position:
                        p = self.current_position
                        pos_text = (
                            f"{p.side} | Qty: {p.quantity}\n"
                            f"Belépés: ${p.entry_price:.2f}\n"
                            f"SL: ${p.stop_loss:.2f} | TP: ${p.take_profit:.2f}"
                        )
                    state = "▶️ AKTÍV" if self.trading_active else "⏸ SZÜNET"
                    await send_telegram(
                        f"📊 <b>Bot státusz</b>\n"
                        f"Állapot: {state}\n"
                        f"Mód: {'DRY RUN' if DRY_RUN else '🔴 ÉLES'}\n"
                        f"Piac: {PRODUCT_SYMBOL}\n"
                        f"Pozíció: {pos_text}"
                    )

                elif text == "/balance":
                    try:
                        price = await self.api.get_current_price()
                        pnl_text = "—"
                        if self.current_position:
                            p = self.current_position
                            if p.side == "LONG":
                                pnl = (price - p.entry_price) * p.quantity * LEVERAGE
                            else:
                                pnl = (p.entry_price - price) * p.quantity * LEVERAGE
                            pnl_text = f"{'+' if pnl >= 0 else ''}{pnl:.2f} USD"
                        await send_telegram(
                            f"💰 <b>Egyenleg / PnL</b>\n"
                            f"Aktuális ár: ${price:.2f}\n"
                            f"Nem realizált PnL: {pnl_text}\n"
                            f"Pozíció méret: ${POSITION_SIZE_USD} | {LEVERAGE}x"
                        )
                    except Exception as e:
                        await send_telegram(f"❌ Hiba az egyenleg lekérésekor: {e}")

            await asyncio.sleep(3)  # 3 másodpercenként polloz

    async def run(self):
        """Főhurok - 0-24-ben fut."""
        log.info(f"Bot elindult | Polling: {POLL_INTERVAL_SEC}s")

        # Termék adatok betöltése
        try:
            await self.api.get_product_info()
        except ValueError as e:
            log.error(str(e))
            return

        await send_telegram(
            f"🚀 <b>Bot elindult!</b>\n"
            f"Piac: {PRODUCT_SYMBOL}\n"
            f"Mód: {'DRY RUN 🧪' if DRY_RUN else '🔴 ÉLES'}\n"
            f"Cím: {self.signer.address[:10]}...\n"
            f"BB: {BB_PERIOD} gyertya / {BB_CANDLE_INTERVAL}\n"
            f"Pozíció: ${POSITION_SIZE_USD} | {LEVERAGE}x\n"
            f"Parancsok: /stop /start /status /balance"
        )

        iteration = 0
        while not self._stop_event.is_set():
            iteration += 1
            if self.trading_active:
                log.info(f"--- Iteráció #{iteration} ---")
                await self.run_once(iteration)
            else:
                log.info(f"--- Szünet (Telegram /stop) | #{iteration} ---")
            await asyncio.sleep(POLL_INTERVAL_SEC)

    async def cleanup(self):
        self._stop_event.set()
        await self.api.close()


# ---------------------------------------------
# BELÉPÉSI PONT
# ---------------------------------------------
async def main():
    bot = EtherealBot()
    try:
        # Két taszkot futtatunk párhuzamosan:
        # 1) Fő trading loop
        # 2) Telegram parancs-figyelő
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
        await send_telegram("🛑 <b>Bot leállítva!</b>")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot leállítva.")
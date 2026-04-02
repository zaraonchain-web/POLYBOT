"""
Trading Bot Module - Main Trading Interface

A production-ready trading bot for Polymarket with:
- Gasless transactions via Builder Program
- Encrypted private key storage
- Modular strategy support
- Comprehensive order management

Example:
    from src.bot import TradingBot

    bot = TradingBot(config_path="config.yaml")

    result = await bot.place_order(
        token_id="123...",
        price=0.65,
        size=10,
        side="BUY"
    )
"""

import os
import asyncio
import logging
from typing import Optional, Dict, Any, List, Callable, TypeVar
from dataclasses import dataclass, field
from enum import Enum

from eth_utils import to_checksum_address

from .config import Config, BuilderConfig
from .signer import OrderSigner, Order
from .client import ClobClient, RelayerClient, ApiCredentials
from .crypto import KeyManager, CryptoError, InvalidPasswordError


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

T = TypeVar("T")

class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    status: Optional[str] = None
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, response: Dict[str, Any]) -> "OrderResult":
        success = response.get("success", False)
        error_msg = response.get("errorMsg", "")

        return cls(
            success=success,
            order_id=response.get("orderId"),
            status=response.get("status"),
            message=error_msg if not success else "Order placed successfully",
            data=response
        )


class TradingBotError(Exception):
    pass


class NotInitializedError(TradingBotError):
    pass


class TradingBot:
    """Main trading bot class for Polymarket."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        config: Optional[Config] = None,
        safe_address: Optional[str] = None,
        builder_creds: Optional[BuilderConfig] = None,
        private_key: Optional[str] = None,
        encrypted_key_path: Optional[str] = None,
        password: Optional[str] = None,
        api_creds_path: Optional[str] = None,
        log_level: int = logging.INFO
    ):
        logger.setLevel(log_level)

        if config_path:
            self.config = Config.load(config_path)
        elif config:
            self.config = config
        else:
            self.config = Config()

        if safe_address:
            self.config.safe_address = safe_address
        if builder_creds:
            self.config.builder = builder_creds
            self.config.use_gasless = True

        self.signer: Optional[OrderSigner] = None
        self.clob_client: Optional[ClobClient] = None
        self.relayer_client: Optional[RelayerClient] = None
        self._api_creds: Optional[ApiCredentials] = None

        if private_key:
            self.signer = OrderSigner(private_key)
        elif encrypted_key_path and password:
            self._load_encrypted_key(encrypted_key_path, password)

        if api_creds_path:
            self._load_api_creds(api_creds_path)

        self._init_clients()

        if self.signer and not self._api_creds:
            self._derive_api_creds()

        logger.info(f"TradingBot initialized (gasless: {self.config.use_gasless})")

    def _load_encrypted_key(self, filepath: str, password: str) -> None:
        try:
            manager = KeyManager()
            private_key = manager.load_and_decrypt(password, filepath)
            self.signer = OrderSigner(private_key)
            logger.info(f"Loaded encrypted key from {filepath}")
        except FileNotFoundError:
            raise TradingBotError(f"Encrypted key file not found: {filepath}")
        except InvalidPasswordError:
            raise TradingBotError("Invalid password for encrypted key")
        except CryptoError as e:
            raise TradingBotError(f"Failed to load encrypted key: {e}")

    def _load_api_creds(self, filepath: str) -> None:
        if os.path.exists(filepath):
            try:
                self._api_creds = ApiCredentials.load(filepath)
                logger.info(f"Loaded API credentials from {filepath}")
            except Exception as e:
                logger.warning(f"Failed to load API credentials: {e}")

    def _derive_api_creds(self) -> None:
        """Derive L2 API credentials from signer."""
        if not self.signer or not self.clob_client:
            return

        try:
            logger.info("Deriving L2 API credentials...")
            self._api_creds = self.clob_client.create_or_derive_api_key(self.signer)
            # FIX: Pass signer.address so ClobClient uses the EOA address
            # in POLY_ADDRESS header, not the Safe/proxy funder address.
            self.clob_client.set_api_creds(self._api_creds, signer_address=self.signer.address)
            logger.info("L2 API credentials derived successfully")
        except Exception as e:
            logger.warning(f"Failed to derive API credentials: {e}")
            logger.warning("Some API endpoints may not be accessible")

    def _init_clients(self) -> None:
        """Initialize API clients."""
        signer_address = self.signer.address if self.signer else None

        self.clob_client = ClobClient(
            host=self.config.clob.host,
            chain_id=self.config.clob.chain_id,
            signature_type=self.config.clob.signature_type,
            funder=self.config.safe_address,
            api_creds=self._api_creds,
            builder_creds=self.config.builder if self.config.use_gasless else None,
            signer_address=signer_address,
        )

        if self.config.use_gasless:
            self.relayer_client = RelayerClient(
                host=self.config.relayer.host,
                chain_id=self.config.clob.chain_id,
                builder_creds=self.config.builder,
                tx_type=self.config.relayer.tx_type,
            )
            logger.info("Relayer client initialized (gasless enabled)")

    async def _run_in_thread(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return await asyncio.to_thread(func, *args, **kwargs)

    def is_initialized(self) -> bool:
        return (
            self.signer is not None and
            self.config.safe_address and
            self.clob_client is not None
        )

    def require_signer(self) -> OrderSigner:
        if not self.signer:
            raise NotInitializedError(
                "Signer not initialized. Provide private_key or encrypted_key."
            )
        return self.signer

    async def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        order_type: str = "GTC",
        fee_rate_bps: int = 0
    ) -> OrderResult:
        signer = self.require_signer()

        try:
            order = Order(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
                maker=to_checksum_address(self.config.safe_address),  # FIX: checksum, not lowercase
                fee_rate_bps=fee_rate_bps,
            )

            signed = signer.sign_order(order)

            response = await self._run_in_thread(
                self.clob_client.post_order,
                signed,
                order_type,
            )

            logger.info(
                f"Order placed: {side} {size}@{price} "
                f"(token: {token_id[:16]}...)"
            )

            return OrderResult.from_response(response)

        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return OrderResult(success=False, message=str(e))

    async def place_orders(
        self,
        orders: List[Dict[str, Any]],
        order_type: str = "GTC"
    ) -> List[OrderResult]:
        results = []
        for order_data in orders:
            result = await self.place_order(
                token_id=order_data["token_id"],
                price=order_data["price"],
                size=order_data["size"],
                side=order_data["side"],
                order_type=order_type,
            )
            results.append(result)
            await asyncio.sleep(0.1)

        return results

    async def cancel_order(self, order_id: str) -> OrderResult:
        try:
            response = await self._run_in_thread(self.clob_client.cancel_order, order_id)
            logger.info(f"Order cancelled: {order_id}")
            return OrderResult(
                success=True,
                order_id=order_id,
                message="Order cancelled",
                data=response
            )
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return OrderResult(success=False, order_id=order_id, message=str(e))

    async def cancel_all_orders(self) -> OrderResult:
        try:
            response = await self._run_in_thread(self.clob_client.cancel_all_orders)
            logger.info("All orders cancelled")
            return OrderResult(success=True, message="All orders cancelled", data=response)
        except Exception as e:
            logger.error(f"Failed to cancel orders: {e}")
            return OrderResult(success=False, message=str(e))

    async def cancel_market_orders(
        self,
        market: Optional[str] = None,
        asset_id: Optional[str] = None
    ) -> OrderResult:
        try:
            response = await self._run_in_thread(
                self.clob_client.cancel_market_orders,
                market,
                asset_id,
            )
            logger.info(f"Market orders cancelled (market: {market or 'all'}, asset: {asset_id or 'all'})")
            return OrderResult(
                success=True,
                message=f"Orders cancelled for market {market or 'all'}",
                data=response
            )
        except Exception as e:
            logger.error(f"Failed to cancel market orders: {e}")
            return OrderResult(success=False, message=str(e))

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        try:
            orders = await self._run_in_thread(self.clob_client.get_open_orders)
            logger.debug(f"Retrieved {len(orders)} open orders")
            return orders
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    async def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await self._run_in_thread(self.clob_client.get_order, order_id)
        except Exception as e:
            logger.error(f"Failed to get order {order_id}: {e}")
            return None

    async def get_trades(
        self,
        token_id: Optional[str] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        try:
            trades = await self._run_in_thread(self.clob_client.get_trades, token_id, limit)
            logger.debug(f"Retrieved {len(trades)} trades")
            return trades
        except Exception as e:
            logger.error(f"Failed to get trades: {e}")
            return []

    async def get_order_book(self, token_id: str) -> Dict[str, Any]:
        try:
            return await self._run_in_thread(self.clob_client.get_order_book, token_id)
        except Exception as e:
            logger.error(f"Failed to get order book: {e}")
            return {}

    async def get_market_price(self, token_id: str) -> Dict[str, Any]:
        try:
            return await self._run_in_thread(self.clob_client.get_market_price, token_id)
        except Exception as e:
            logger.error(f"Failed to get market price: {e}")
            return {}

    async def deploy_safe_if_needed(self) -> bool:
        if not self.config.use_gasless or not self.relayer_client:
            logger.debug("Gasless not enabled, skipping Safe deployment")
            return False

        try:
            response = await self._run_in_thread(
                self.relayer_client.deploy_safe,
                self.config.safe_address,
            )
            logger.info(f"Safe deployment initiated: {response}")
            return True
        except Exception as e:
            logger.warning(f"Safe deployment failed (may already be deployed): {e}")
            return False

    def create_order_dict(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str
    ) -> Dict[str, Any]:
        return {
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side.upper(),
        }


def create_bot(
    config_path: str = "config.yaml",
    private_key: Optional[str] = None,
    encrypted_key_path: Optional[str] = None,
    password: Optional[str] = None,
    **kwargs
) -> TradingBot:
    return TradingBot(
        config_path=config_path,
        private_key=private_key,
        encrypted_key_path=encrypted_key_path,
        password=password,
        **kwargs
    )

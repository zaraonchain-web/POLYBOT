"""
Market Manager - Market Discovery and WebSocket Management
"""

import asyncio
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Dict, Callable, List, Union, Awaitable

from src.gamma_client import GammaClient
from src.websocket_client import MarketWebSocket, OrderbookSnapshot


@dataclass
class MarketInfo:
    """Current market information."""

    slug: str
    question: str
    end_date: str
    token_ids: Dict[str, str]
    prices: Dict[str, float]
    accepting_orders: bool

    @property
    def up_token(self) -> str:
        return self.token_ids.get("up", "")

    @property
    def down_token(self) -> str:
        return self.token_ids.get("down", "")

    def get_countdown(self) -> tuple[int, int]:
        if not self.end_date:
            return (-1, -1)
        try:
            end_date_str = self.end_date.replace("Z", "+00:00")
            end_time = datetime.fromisoformat(end_date_str)
            now = datetime.now(timezone.utc)
            remaining = end_time - now
            if remaining.total_seconds() <= 0:
                return (0, 0)
            total_secs = int(remaining.total_seconds())
            return (total_secs // 60, total_secs % 60)
        except Exception:
            return (-1, -1)

    def get_countdown_str(self) -> str:
        mins, secs = self.get_countdown()
        if mins < 0:
            return "--:--"
        if mins == 0 and secs == 0:
            return "ENDED"
        return f"{mins:02d}:{secs:02d}"

    def slug_timestamp(self) -> Optional[int]:
        if not self.slug:
            return None
        ts = self.slug.split("-")[-1]
        if not ts.isdigit():
            return None
        try:
            return int(ts)
        except ValueError:
            return None

    def end_timestamp(self) -> Optional[int]:
        if not self.end_date:
            return None
        try:
            end_date_str = self.end_date.replace("Z", "+00:00")
            return int(datetime.fromisoformat(end_date_str).timestamp())
        except Exception:
            return None

    def is_ending_soon(self, threshold_seconds: int = 60) -> bool:
        mins, secs = self.get_countdown()
        if mins < 0:
            return False
        return (mins * 60 + secs) <= threshold_seconds

    def has_ended(self) -> bool:
        mins, secs = self.get_countdown()
        return mins == 0 and secs == 0


BookCallback = Callable[[OrderbookSnapshot], Union[None, Awaitable[None]]]
MarketChangeCallback = Callable[[str, str], None]
ConnectionCallback = Callable[[], None]


class MarketManager:
    """Manages market discovery and WebSocket connections."""

    def __init__(
        self,
        coin: str = "BTC",
        market_check_interval: float = 10.0,
        auto_switch_market: bool = True,
    ):
        self.coin = coin.upper()
        self.market_check_interval = market_check_interval
        self.auto_switch_market = auto_switch_market

        self.gamma = GammaClient()
        self.ws: Optional[MarketWebSocket] = None

        self.current_market: Optional[MarketInfo] = None
        self._previous_slug: Optional[str] = None
        self._running = False
        self._ws_connected = False
        self._ws_task: Optional[asyncio.Task] = None
        self._market_check_task: Optional[asyncio.Task] = None

        self._on_book_callbacks: List[BookCallback] = []
        self._on_market_change_callbacks: List[MarketChangeCallback] = []
        self._on_connect_callbacks: List[ConnectionCallback] = []
        self._on_disconnect_callbacks: List[ConnectionCallback] = []

    @property
    def is_connected(self) -> bool:
        return self._ws_connected

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def token_ids(self) -> Dict[str, str]:
        if self.current_market:
            return self.current_market.token_ids
        return {}

    def get_orderbook(self, side: str) -> Optional[OrderbookSnapshot]:
        if not self.ws or not self.current_market:
            return None
        token_id = self.current_market.token_ids.get(side)
        if token_id:
            return self.ws.get_orderbook(token_id)
        return None

    def get_mid_price(self, side: str) -> float:
        ob = self.get_orderbook(side)
        return ob.mid_price if ob else 0.0

    def get_best_bid(self, side: str) -> float:
        ob = self.get_orderbook(side)
        return ob.best_bid if ob else 0.0

    def get_best_ask(self, side: str) -> float:
        ob = self.get_orderbook(side)
        return ob.best_ask if ob else 1.0

    def get_spread(self, side: str) -> float:
        ob = self.get_orderbook(side)
        if ob and ob.best_bid > 0:
            return ob.best_ask - ob.best_bid
        return 0.0

    def on_book_update(self, callback: BookCallback) -> BookCallback:
        self._on_book_callbacks.append(callback)
        return callback

    def on_market_change(self, callback: MarketChangeCallback) -> MarketChangeCallback:
        self._on_market_change_callbacks.append(callback)
        return callback

    def on_connect(self, callback: ConnectionCallback) -> ConnectionCallback:
        self._on_connect_callbacks.append(callback)
        return callback

    def on_disconnect(self, callback: ConnectionCallback) -> ConnectionCallback:
        self._on_disconnect_callbacks.append(callback)
        return callback

    def _update_current_market(self, market: MarketInfo) -> None:
        self._previous_slug = market.slug
        self.current_market = market

    def _market_sort_key(self, market: MarketInfo) -> Optional[int]:
        # FIX: Prefer slug_timestamp exclusively; only fall back to
        # end_timestamp when slug_timestamp is unavailable. Mixing both
        # semantics in a single comparison across two markets produces
        # incorrect ordering when one has a slug-embedded timestamp and
        # the other only has an end date.
        return market.slug_timestamp() or market.end_timestamp()

    def _should_switch_market(
        self,
        old_market: Optional[MarketInfo],
        new_market: MarketInfo
    ) -> bool:
        if not old_market:
            return True
        old_tokens = set(old_market.token_ids.values())
        new_tokens = set(new_market.token_ids.values())
        if new_tokens == old_tokens:
            return False
        old_key = self._market_sort_key(old_market)
        new_key = self._market_sort_key(new_market)
        # Only suppress the switch when both keys are present and comparable.
        # If either is missing, allow the switch rather than silently blocking.
        if old_key is not None and new_key is not None and new_key <= old_key:
            return False
        return True

    def discover_market(self, update_state: bool = True) -> Optional[MarketInfo]:
        market_data = self.gamma.get_market_info(self.coin)
        if not market_data:
            return None
        if not market_data.get("accepting_orders", False):
            return None

        market = MarketInfo(
            slug=market_data.get("slug", ""),
            question=market_data.get("question", ""),
            end_date=market_data.get("end_date", ""),
            token_ids=market_data.get("token_ids", {}),
            prices=market_data.get("prices", {}),
            accepting_orders=market_data.get("accepting_orders", False),
        )

        if update_state:
            self._update_current_market(market)
        return market

    async def _setup_websocket(self) -> bool:
        if not self.current_market:
            return False

        self.ws = MarketWebSocket()

        @self.ws.on_book
        async def handle_book(snapshot: OrderbookSnapshot):  # pyright: ignore[reportUnusedFunction]
            for callback in self._on_book_callbacks:
                try:
                    result = callback(snapshot)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass

        @self.ws.on_connect
        def handle_connect():  # pyright: ignore[reportUnusedFunction]
            self._ws_connected = True
            for callback in self._on_connect_callbacks:
                try:
                    callback()
                except Exception:
                    pass

        @self.ws.on_disconnect
        def handle_disconnect():  # pyright: ignore[reportUnusedFunction]
            self._ws_connected = False
            for callback in self._on_disconnect_callbacks:
                try:
                    callback()
                except Exception:
                    pass

        token_list = list(self.current_market.token_ids.values())
        if token_list:
            await self.ws.subscribe(token_list, replace=True)

        return True

    async def _run_websocket(self) -> None:
        if self.ws:
            await self.ws.run(auto_reconnect=True)

    async def _market_check_loop(self) -> None:
        """Periodically check for market changes."""
        while self._running:
            await asyncio.sleep(self.market_check_interval)

            if not self._running:
                break

            old_market = self.current_market
            old_tokens = set(old_market.token_ids.values()) if old_market else set()
            old_slug = old_market.slug if old_market else None

            market = await asyncio.to_thread(self.discover_market, update_state=False)

            if not market:
                continue

            new_tokens = set(market.token_ids.values())
            if new_tokens == old_tokens:
                self._update_current_market(market)
                continue

            if not (self.auto_switch_market and self.ws):
                self._update_current_market(market)
                continue

            if not self._should_switch_market(old_market, market):
                continue

            # FIX: subscribe(replace=True) now atomically sends an unsubscribe
            # for old tokens, clears the local cache, then subscribes to new
            # tokens — all in one call. Eliminates the previous race window
            # where stale book events could re-populate the cache between a
            # separate clear_orderbooks() call and the subscribe.
            await self.ws.subscribe(list(new_tokens), replace=True)
            self._update_current_market(market)

            # Wait briefly to confirm book data arrived on the new market.
            # If nothing comes back (e.g. market not live yet), skip firing
            # callbacks so the UI doesn't render a broken 0.0000 state and
            # the next check cycle can retry cleanly instead of re-triggering
            # the same switch over and over.
            await asyncio.sleep(2.0)
            if not self.get_orderbook("up") and not self.get_orderbook("down"):
                continue

            if old_slug and old_slug != market.slug:
                for callback in self._on_market_change_callbacks:
                    try:
                        callback(old_slug, market.slug)
                    except Exception:
                        pass

    async def start(self) -> bool:
        self._running = True

        # FIX: Run the blocking discover_market() call off the event loop
        # thread so it doesn't block the event loop during startup.
        market = await asyncio.to_thread(self.discover_market, update_state=True)
        if not market:
            self._running = False
            return False

        if not await self._setup_websocket():
            self._running = False
            return False

        self._ws_task = asyncio.create_task(self._run_websocket())

        if self.auto_switch_market:
            self._market_check_task = asyncio.create_task(self._market_check_loop())

        return True

    async def stop(self) -> None:
        self._running = False

        if self._market_check_task:
            self._market_check_task.cancel()
            try:
                await self._market_check_task
            except asyncio.CancelledError:
                pass
            self._market_check_task = None

        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None

        if self.ws:
            await self.ws.disconnect()
            self.ws = None

        self._ws_connected = False

    async def wait_for_data(self, timeout: float = 5.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            if self._ws_connected:
                if self.get_orderbook("up") or self.get_orderbook("down"):
                    return True
            await asyncio.sleep(0.1)
        return False

    async def refresh_market(self) -> Optional[MarketInfo]:
        old_market = self.current_market
        old_tokens = set(old_market.token_ids.values()) if old_market else set()

        market = await asyncio.to_thread(self.discover_market, update_state=False)

        if not market:
            return None

        new_tokens = set(market.token_ids.values())
        if new_tokens == old_tokens:
            self._update_current_market(market)
            return self.current_market

        if not self._should_switch_market(old_market, market):
            return old_market

        # FIX: Same as _market_check_loop — rely on subscribe(replace=True)
        # to handle unsubscribe + clear + subscribe atomically.
        if self.ws:
            await self.ws.subscribe(list(new_tokens), replace=True)

        self._update_current_market(market)
        return market

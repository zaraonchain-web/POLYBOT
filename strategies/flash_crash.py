"""
Flash Crash Strategy - Improved for Higher Win Rate

Changes from original:
1. Time filter - no trades in first 60s or last 2 minutes of market
2. Confirmation delay - waits 2s after crash before buying (avoid catching falling knife)
3. Tighter stop loss - $0.03 instead of $0.05 (protect small bankroll)
4. Spread filter - skips trades when market is illiquid (wide spread)
5. Trend check - won't buy a side that was already trending down before the crash
6. Skip cooldown - 15s cooldown after any skip to prevent rapid re-triggering
7. Trend check logging - exceptions are now logged instead of silently swallowed
8. Bid depth filter - skips trades when order book bids are too thin underneath

Usage:
    python apps/run_flash_crash.py --coin BTC
"""

import asyncio
from dataclasses import dataclass
from typing import Dict, Optional

from lib.console import Colors, format_countdown
from strategies.base import BaseStrategy, StrategyConfig
from src.bot import TradingBot
from src.websocket_client import OrderbookSnapshot


@dataclass
class FlashCrashConfig(StrategyConfig):
    """Flash crash strategy configuration."""

    drop_threshold: float = 0.30        # Absolute probability drop to trigger entry
    max_spread: float = 0.05            # Skip trade if spread is wider than this
    min_market_age_seconds: int = 60    # Don't trade in first 60s (price too chaotic)
    min_remaining_seconds: int = 120    # Don't trade with less than 2 mins left
    confirmation_delay: float = 2.0     # Wait this many seconds after crash before buying
    trend_lookback_seconds: int = 30    # How far back to check for pre-crash trend
    stop_loss: float = 0.03             # Tighter stop loss ($0.03 instead of $0.05)
    take_profit: float = 0.10           # Keep the same take profit
    skip_cooldown_seconds: float = 15.0  # Don't re-trigger for this long after a skip
    min_bid_depth: float = 0.0           # Disabled until tuned (set >0 to enable)


class FlashCrashStrategy(BaseStrategy):
    """
    Improved Flash Crash Trading Strategy.

    Monitors 15-minute markets for sudden price drops and trades
    the volatility — but only when conditions are favorable.
    """

    def __init__(self, bot: TradingBot, config: FlashCrashConfig):
        """Initialize flash crash strategy."""
        super().__init__(bot, config)
        self.flash_config = config

        # Update price tracker with our threshold
        self.prices.drop_threshold = config.drop_threshold

        # Pending entry: (side, price, trigger_time)
        self._pending_entry: Optional[tuple] = None

        # Track why we skipped trades (for display)
        self._last_skip_reason: str = ""
        self._skipped_count: int = 0
        self._confirmed_count: int = 0
        self._last_skip_time: float = 0.0  # Cooldown: time of last skip

    # ------------------------------------------------------------------ #
    #  Core Strategy Logic                                                 #
    # ------------------------------------------------------------------ #

    async def on_book_update(self, snapshot: OrderbookSnapshot) -> None:
        """Handle orderbook update - price recording done in base class."""
        pass

    async def on_tick(self, prices: Dict[str, float]) -> None:
        """Main strategy tick - check for crash and validate conditions."""

        # --- Handle pending confirmation delay ---
        if self._pending_entry is not None:
            await self._check_pending_entry(prices)
            return  # Don't look for new crashes while waiting to confirm

        # --- Already in a position, nothing to do ---
        if not self.positions.can_open_position:
            return

        # --- Detect flash crash ---
        event = self.prices.detect_flash_crash()
        if not event:
            return

        # --- Run all filters ---
        skip_reason = self._get_skip_reason(event.side, prices)
        if skip_reason:
            self._last_skip_time = asyncio.get_event_loop().time()
            self._last_skip_reason = f"SKIPPED ({skip_reason}): {event.side.upper()} drop {event.drop:.2f}"
            self._skipped_count += 1
            self.log(self._last_skip_reason, "warning")
            return

        # --- All filters passed — start confirmation delay ---
        current_price = prices.get(event.side, 0)
        if current_price <= 0:
            return

        self.log(
            f"CRASH DETECTED: {event.side.upper()} "
            f"drop {event.drop:.2f} ({event.old_price:.2f} -> {event.new_price:.2f}) "
            f"— waiting {self.flash_config.confirmation_delay:.0f}s to confirm...",
            "trade"
        )
        self._pending_entry = (event.side, current_price, asyncio.get_event_loop().time())

    async def _check_pending_entry(self, prices: Dict[str, float]) -> None:
        """
        After the confirmation delay, check the price is still low
        (hasn't crashed further) before actually buying.
        """
        side, entry_price, trigger_time = self._pending_entry
        elapsed = asyncio.get_event_loop().time() - trigger_time

        if elapsed < self.flash_config.confirmation_delay:
            return  # Still waiting

        # Delay passed — check price hasn't kept falling (still a crash, not a trend)
        current_price = prices.get(side, 0)
        self._pending_entry = None

        if current_price <= 0:
            self.log("Confirmation failed: no price data", "warning")
            return

        # If price dropped another 5%+ during our wait, it's momentum not a crash
        if current_price < entry_price * 0.95:
            self._last_skip_reason = f"SKIPPED (still falling after delay): {side.upper()}"
            self._skipped_count += 1
            self.log(self._last_skip_reason, "warning")
            return

        # Good to go
        self._confirmed_count += 1
        self.log(
            f"CONFIRMED: {side.upper()} price stable at {current_price:.4f} — entering",
            "success"
        )
        await self.execute_buy(side, current_price)

    def _get_skip_reason(self, side: str, prices: Dict[str, float]) -> Optional[str]:
        """
        Run all filters. Returns a reason string if we should skip,
        or None if all filters pass.
        """
        market = self.current_market
        if not market:
            return "no market data"

        # 0. Cooldown — don't re-trigger too soon after a skip
        now = asyncio.get_event_loop().time()
        if now - self._last_skip_time < self.flash_config.skip_cooldown_seconds:
            remaining_cooldown = self.flash_config.skip_cooldown_seconds - (now - self._last_skip_time)
            return f"cooldown active ({remaining_cooldown:.0f}s remaining)"

        mins, secs = market.get_countdown()
        total_remaining = mins * 60 + secs

        # 1. Time filter — too late in the market
        if total_remaining < self.flash_config.min_remaining_seconds:
            return f"only {total_remaining}s remaining (min {self.flash_config.min_remaining_seconds}s)"

        # 2. Time filter — too early in the market
        market_duration = 15 * 60  # 15 minutes in seconds
        market_age = market_duration - total_remaining
        if market_age < self.flash_config.min_market_age_seconds:
            return f"market too new ({market_age:.0f}s old, min {self.flash_config.min_market_age_seconds}s)"

        # 3. Spread filter — market too illiquid
        spread = self.market.get_spread(side)
        if spread > self.flash_config.max_spread:
            return f"spread too wide ({spread:.4f} > {self.flash_config.max_spread:.4f})"

        # 4. Trend check — was this side already falling before the crash?
        if self._was_already_trending_down(side):
            return f"{side} was already trending down before crash"

        # 5. Bid depth filter — is there enough support under the current price?
        ob = self.market.get_orderbook(side)
        if ob and ob.bids:
            top_bid_depth = sum(b.size for b in ob.bids[:3])
            if top_bid_depth < self.flash_config.min_bid_depth:
                return f"thin bid depth ({top_bid_depth:.1f} < {self.flash_config.min_bid_depth:.1f})"

        return None  # All filters passed

    def _was_already_trending_down(self, side: str) -> bool:
        """
        Check if the price was already in a downtrend before the crash.
        We look at the price trend_lookback_seconds before the crash window.

        Returns True if we should skip (already trending down).
        """
        try:
            lookback = self.flash_config.trend_lookback_seconds
            history = self.prices.get_recent_prices(side, lookback)

            if len(history) < 5:
                return False  # Not enough data to judge, allow trade

            # Compare first third vs last third of the history window
            third = max(1, len(history) // 3)
            early_avg = sum(history[:third]) / third
            recent_avg = sum(history[-third:]) / third

            # If recent prices are already 3%+ below early prices, it's a trend
            return recent_avg < early_avg * 0.97

        except Exception as e:
            self.log(f"Trend check error ({side}): {e}", "warning")
            return False  # On any error, don't block the trade

    # ------------------------------------------------------------------ #
    #  Display                                                             #
    # ------------------------------------------------------------------ #

    def render_status(self, prices: Dict[str, float]) -> None:
        """Render TUI status display."""
        lines = []

        ws_status = f"{Colors.GREEN}WS{Colors.RESET}" if self.is_connected else f"{Colors.RED}REST{Colors.RESET}"
        countdown = self._get_countdown_str()
        stats = self.positions.get_stats()

        # Header
        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")
        lines.append(
            f"{Colors.CYAN}[{self.config.coin}]{Colors.RESET} [{ws_status}] "
            f"Ends: {countdown} | Trades: {stats['trades_closed']} | "
            f"PnL: ${stats['total_pnl']:+.2f} | "
            f"Win Rate: {stats['win_rate']:.0f}%"
        )
        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")

        # Trade filter status
        market = self.current_market
        filter_status = self._get_filter_status(market)
        lines.append(filter_status)
        lines.append("")

        # Orderbook display
        up_ob = self.market.get_orderbook("up")
        down_ob = self.market.get_orderbook("down")

        lines.append(f"{Colors.GREEN}{'UP':^39}{Colors.RESET}|{Colors.RED}{'DOWN':^39}{Colors.RESET}")
        lines.append(f"{'Bid':>9} {'Size':>9} | {'Ask':>9} {'Size':>9}|{'Bid':>9} {'Size':>9} | {'Ask':>9} {'Size':>9}")
        lines.append("-" * 80)

        up_bids = up_ob.bids[:5] if up_ob else []
        up_asks = up_ob.asks[:5] if up_ob else []
        down_bids = down_ob.bids[:5] if down_ob else []
        down_asks = down_ob.asks[:5] if down_ob else []

        for i in range(5):
            up_bid = f"{up_bids[i].price:>9.4f} {up_bids[i].size:>9.1f}" if i < len(up_bids) else f"{'--':>9} {'--':>9}"
            up_ask = f"{up_asks[i].price:>9.4f} {up_asks[i].size:>9.1f}" if i < len(up_asks) else f"{'--':>9} {'--':>9}"
            down_bid = f"{down_bids[i].price:>9.4f} {down_bids[i].size:>9.1f}" if i < len(down_bids) else f"{'--':>9} {'--':>9}"
            down_ask = f"{down_asks[i].price:>9.4f} {down_asks[i].size:>9.1f}" if i < len(down_asks) else f"{'--':>9} {'--':>9}"
            lines.append(f"{up_bid} | {up_ask}|{down_bid} | {down_ask}")

        lines.append("-" * 80)

        # Mid / spread summary
        up_mid = up_ob.mid_price if up_ob else prices.get("up", 0)
        down_mid = down_ob.mid_price if down_ob else prices.get("down", 0)
        up_spread = self.market.get_spread("up")
        down_spread = self.market.get_spread("down")

        spread_up_color = Colors.RED if up_spread > self.flash_config.max_spread else Colors.GREEN
        spread_down_color = Colors.RED if down_spread > self.flash_config.max_spread else Colors.GREEN

        lines.append(
            f"Mid: {Colors.GREEN}{up_mid:.4f}{Colors.RESET}  "
            f"Spread: {spread_up_color}{up_spread:.4f}{Colors.RESET}          |"
            f"Mid: {Colors.RED}{down_mid:.4f}{Colors.RESET}  "
            f"Spread: {spread_down_color}{down_spread:.4f}{Colors.RESET}"
        )

        # Price history + thresholds
        up_history = self.prices.get_history_count("up")
        down_history = self.prices.get_history_count("down")
        lines.append(
            f"History: UP={up_history}/100 DOWN={down_history}/100 | "
            f"Drop threshold: {self.flash_config.drop_threshold:.2f} in {self.config.price_lookback_seconds}s | "
            f"Confirmed: {self._confirmed_count} | Skipped: {self._skipped_count}"
        )

        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")

        # Pending entry
        if self._pending_entry is not None:
            side, price, trigger_time = self._pending_entry
            elapsed = asyncio.get_event_loop().time() - trigger_time
            remaining = max(0, self.flash_config.confirmation_delay - elapsed)
            lines.append(
                f"{Colors.CYAN}⏳ CONFIRMING: {side.upper()} @ {price:.4f} "
                f"— entering in {remaining:.1f}s...{Colors.RESET}"
            )
        elif self._last_skip_reason:
            lines.append(f"{Colors.DIM}Last skip: {self._last_skip_reason}{Colors.RESET}")

        # Open orders
        lines.append(f"{Colors.BOLD}Open Orders:{Colors.RESET}")
        if self.open_orders:
            for order in self.open_orders[:5]:
                side = order.get("side", "?")
                price = float(order.get("price", 0))
                size = float(order.get("original_size", order.get("size", 0)))
                filled = float(order.get("size_matched", 0))
                order_id = order.get("id", "")[:8]
                token = order.get("asset_id", "")
                token_side = "UP" if token == self.token_ids.get("up") else "DOWN" if token == self.token_ids.get("down") else "?"
                color = Colors.GREEN if side == "BUY" else Colors.RED
                lines.append(
                    f"  {color}{side:4}{Colors.RESET} {token_side:4} @ {price:.4f} "
                    f"Size: {size:.1f} Filled: {filled:.1f} ID: {order_id}..."
                )
        else:
            lines.append(f"  {Colors.CYAN}(no open orders){Colors.RESET}")

        # Positions
        lines.append(f"{Colors.BOLD}Positions:{Colors.RESET}")
        all_positions = self.positions.get_all_positions()
        if all_positions:
            for pos in all_positions:
                current = prices.get(pos.side, 0)
                pnl = pos.get_pnl(current)
                pnl_pct = pos.get_pnl_percent(current)
                hold_time = pos.get_hold_time()
                color = Colors.GREEN if pnl >= 0 else Colors.RED
                lines.append(
                    f"  {Colors.BOLD}{pos.side.upper():4}{Colors.RESET} "
                    f"Entry: {pos.entry_price:.4f} | Current: {current:.4f} | "
                    f"Size: ${pos.size:.2f} | PnL: {color}${pnl:+.2f} ({pnl_pct:+.1f}%){Colors.RESET} | "
                    f"Hold: {hold_time:.0f}s"
                )
                lines.append(
                    f"       TP: {pos.take_profit_price:.4f} (+${self.config.take_profit:.2f}) | "
                    f"SL: {pos.stop_loss_price:.4f} (-${self.config.stop_loss:.2f})"
                )
        else:
            lines.append(f"  {Colors.CYAN}(no open positions){Colors.RESET}")

        # Recent logs
        if self._log_buffer.messages:
            lines.append("-" * 80)
            lines.append(f"{Colors.BOLD}Recent Events:{Colors.RESET}")
            for msg in self._log_buffer.get_messages():
                lines.append(f"  {msg}")

        output = "\033[H\033[J" + "\n".join(lines)
        print(output, flush=True)

    def _get_filter_status(self, market) -> str:
        """Show which filters are currently active/blocking."""
        if not market:
            return f"{Colors.RED}No market data{Colors.RESET}"

        mins, secs = market.get_countdown()
        total_remaining = mins * 60 + secs
        market_age = (15 * 60) - total_remaining

        parts = []

        # Time remaining check
        if total_remaining < self.flash_config.min_remaining_seconds:
            parts.append(f"{Colors.RED}⛔ Too late ({total_remaining}s left){Colors.RESET}")
        elif market_age < self.flash_config.min_market_age_seconds:
            parts.append(f"{Colors.YELLOW}⏳ Too early ({market_age:.0f}s old){Colors.RESET}")
        else:
            parts.append(f"{Colors.GREEN}✓ Time OK{Colors.RESET}")

        # Spread checks
        up_spread = self.market.get_spread("up")
        down_spread = self.market.get_spread("down")
        if up_spread > self.flash_config.max_spread or down_spread > self.flash_config.max_spread:
            parts.append(f"{Colors.RED}⛔ Spread wide{Colors.RESET}")
        else:
            parts.append(f"{Colors.GREEN}✓ Spread OK{Colors.RESET}")

        return "Filters: " + " | ".join(parts)

    def _get_countdown_str(self) -> str:
        """Get formatted countdown string."""
        market = self.current_market
        if not market:
            return "--:--"
        mins, secs = market.get_countdown()
        return format_countdown(mins, secs)

    def on_market_change(self, old_slug: str, new_slug: str) -> None:
        """Handle market change — clear price history and pending entry."""
        self.prices.clear()
        self._pending_entry = None
        self.log(f"Market switched: {old_slug} → {new_slug}", "warning")

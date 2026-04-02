"""
Snapback Strategy - Buy cheap side, take profit on snapback

Strategy Logic:
1. Monitor UP and DOWN prices
2. When either side drops to 0.22 or below, buy it
3. Take profit at +0.10, stop loss at -0.04
4. Block entries in last 3 minutes
"""

import os
from dataclasses import dataclass
from typing import Dict

from lib.console import Colors, format_countdown
from strategies.base import BaseStrategy, StrategyConfig
from src.bot import TradingBot
from src.websocket_client import OrderbookSnapshot


@dataclass
class FlashCrashConfig(StrategyConfig):
    """Snapback strategy configuration."""

    entry_threshold: float = 0.37   # Buy when price drops to this or below
    take_profit: float = 0.10       # Take profit at +0.10
    stop_loss: float = 0.03         # Stop loss at -0.04
    min_countdown_mins: int = 3     # Block entries in last 3 minutes


class FlashCrashStrategy(BaseStrategy):
    """
    Snapback Trading Strategy.

    Buys the cheap side when it drops to 0.22 or below,
    expecting a snapback to ~0.30+.
    """

    def __init__(self, bot: TradingBot, config: FlashCrashConfig):
        """Initialize snapback strategy."""
        super().__init__(bot, config)
        self.flash_config = config

    async def on_book_update(self, snapshot: OrderbookSnapshot) -> None:
        """Handle orderbook update."""
        pass  # Price recording is done in base class

    async def on_tick(self, prices: Dict[str, float]) -> None:
        """Check for snapback entry on each tick."""
        if not self.positions.can_open_position:
            return

        # Block entries in last 3 minutes
        market = self.current_market
        if market:
            mins, _ = market.get_countdown()
            if mins < self.flash_config.min_countdown_mins:
                return

        # Check both sides for entry
        for side in ["up", "down"]:
            price = prices.get(side, 0)
            if price > 0 and price <= self.flash_config.entry_threshold:
                self.log(
                    f"SNAPBACK ENTRY: {side.upper()} @ {price:.2f} "
                    f"(threshold: {self.flash_config.entry_threshold:.2f})",
                    "trade"
                )
                await self.execute_buy(side, price)
                break  # Only one position at a time

    def render_status(self, prices: Dict[str, float]) -> None:
        """Render TUI status display."""
        lines = []

        # Header
        ws_status = f"{Colors.GREEN}WS{Colors.RESET}" if self.is_connected else f"{Colors.RED}REST{Colors.RESET}"
        countdown = self._get_countdown_str()
        stats = self.positions.get_stats()

        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")
        lines.append(
            f"{Colors.CYAN}[{self.config.coin}]{Colors.RESET} [{ws_status}] "
            f"Ends: {countdown} | Trades: {stats['trades_closed']} | PnL: ${stats['total_pnl']:+.2f}"
        )
        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")

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

        # Summary
        up_mid = up_ob.mid_price if up_ob else prices.get("up", 0)
        down_mid = down_ob.mid_price if down_ob else prices.get("down", 0)
        up_spread = self.market.get_spread("up")
        down_spread = self.market.get_spread("down")

        # Highlight prices near threshold
        up_color = Colors.YELLOW if up_mid <= self.flash_config.entry_threshold else Colors.GREEN
        down_color = Colors.YELLOW if down_mid <= self.flash_config.entry_threshold else Colors.RED

        lines.append(
            f"Mid: {up_color}{up_mid:.4f}{Colors.RESET}  Spread: {up_spread:.4f}           |"
            f"Mid: {down_color}{down_mid:.4f}{Colors.RESET}  Spread: {down_spread:.4f}"
        )

        lines.append(
            f"Entry: ≤{self.flash_config.entry_threshold:.2f} | "
            f"TP: +{self.flash_config.take_profit:.2f} | "
            f"SL: -{self.flash_config.stop_loss:.2f} | "
            f"Block last: {self.flash_config.min_countdown_mins}mins"
        )

        lines.append(f"{Colors.BOLD}{'='*80}{Colors.RESET}")

        # Open Orders
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
                lines.append(f"  {color}{side:4}{Colors.RESET} {token_side:4} @ {price:.4f} Size: {size:.1f} Filled: {filled:.1f} ID: {order_id}...")
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

        # Render — Railway safe
        output = ("\033[H\033[J" if os.isatty(1) else "") + "\n".join(lines)
        print(output, flush=True)

    def _get_countdown_str(self) -> str:
        """Get formatted countdown string."""
        market = self.current_market
        if not market:
            return "--:--"

        mins, secs = market.get_countdown()
        return format_countdown(mins, secs)

    def on_market_change(self, old_slug: str, new_slug: str) -> None:
        """Handle market change - clear price history."""
        self.prices.clear()

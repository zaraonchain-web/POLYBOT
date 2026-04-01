#!/usr/bin/env python3
"""
Snapback Strategy Runner

Entry point for running the snapback strategy.

Usage:
    python apps/run_flash_crash.py --coin ETH
    python apps/run_flash_crash.py --coin BTC --size 10
    python apps/run_flash_crash.py --coin BTC --entry 0.22
"""

import os
import sys
import asyncio
import argparse
import logging
from pathlib import Path

# Suppress noisy logs
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)
logging.getLogger("src.bot").setLevel(logging.WARNING)

# Auto-load .env file
from dotenv import load_dotenv
load_dotenv()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.console import Colors
from src.bot import TradingBot
from src.config import Config
from strategies.flash_crash import FlashCrashStrategy, FlashCrashConfig


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Snapback Strategy for Polymarket 15-minute markets"
    )
    parser.add_argument(
        "--coin",
        type=str,
        default="BTC",
        choices=["BTC", "ETH", "SOL", "XRP"],
        help="Coin to trade (default: BTC)"
    )
    parser.add_argument(
        "--size",
        type=float,
        default=5.0,
        help="Trade size in USDC (default: 5.0)"
    )
    parser.add_argument(
        "--entry",
        type=float,
        default=0.22,
        help="Entry threshold - buy when price drops to this or below (default: 0.22)"
    )
    parser.add_argument(
        "--take-profit",
        type=float,
        default=0.10,
        help="Take profit in dollars (default: 0.10)"
    )
    parser.add_argument(
        "--stop-loss",
        type=float,
        default=0.04,
        help="Stop loss in dollars (default: 0.04)"
    )
    parser.add_argument(
        "--min-countdown",
        type=int,
        default=3,
        help="Block entries when minutes remaining is below this (default: 3)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    # Enable debug logging if requested
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("src.websocket_client").setLevel(logging.DEBUG)

    # Check environment
    private_key = os.environ.get("POLY_PRIVATE_KEY")
    safe_address = os.environ.get("POLY_SAFE_ADDRESS")

    if not private_key or not safe_address:
        print(f"{Colors.RED}Error: POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS must be set{Colors.RESET}")
        print("Set them in .env file or export as environment variables")
        sys.exit(1)

    # Create bot
    config = Config.from_env()
    bot = TradingBot(config=config, private_key=private_key)

    if not bot.is_initialized():
        print(f"{Colors.RED}Error: Failed to initialize bot{Colors.RESET}")
        sys.exit(1)

    # Create strategy config
    strategy_config = FlashCrashConfig(
        coin=args.coin.upper(),
        size=args.size,
        entry_threshold=args.entry,
        take_profit=args.take_profit,
        stop_loss=args.stop_loss,
        min_countdown_mins=args.min_countdown,
    )

    # Print configuration
    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  Snapback Strategy - {strategy_config.coin} 15-Minute Markets{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n")

    print(f"Configuration:")
    print(f"  Coin:           {strategy_config.coin}")
    print(f"  Size:           ${strategy_config.size:.2f}")
    print(f"  Entry at:       ≤{strategy_config.entry_threshold:.2f}")
    print(f"  Take profit:    +${strategy_config.take_profit:.2f}")
    print(f"  Stop loss:      -${strategy_config.stop_loss:.2f}")
    print(f"  Block last:     {strategy_config.min_countdown_mins} mins")
    print()

    # Create and run strategy
    strategy = FlashCrashStrategy(bot=bot, config=strategy_config)

    try:
        asyncio.run(strategy.run())
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

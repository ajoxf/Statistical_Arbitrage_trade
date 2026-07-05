"""Entry point for the basis trading system.

Usage:
    python main.py                          # interactive, config.json if present
    python main.py --config config.json    # explicit config
    python main.py --mode live              # skip mode prompt (still confirms)
"""

import argparse
import logging
import os
import sys
import time

from statarb.config import AlgoTradingConfig
from statarb.system import AlgorithmicTradingSystem

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('algo_trading_system.log', encoding='utf-8'),
        logging.StreamHandler(),
    ],
)


def load_config(path):
    if path and os.path.exists(path):
        return AlgoTradingConfig.from_file(path)
    if path:
        print(f"Config file not found: {path}")
        sys.exit(1)
    logging.info("No config file — using built-in defaults "
                 "(copy config.example.json to config.json to customize)")
    return AlgoTradingConfig()


def prompt_swap_charges(config):
    """Ask only for swap charges the config file didn't provide."""
    missing = [k for k, a in config.ASSETS.items()
               if a['enabled'] and a['swap_charge'] <= 0]
    if not missing:
        return

    print("\n" + "=" * 84)
    print("SWAP CONFIGURATION")
    print("Enter daily swap charges for long positions (USD per lot per day)")
    print("-" * 84)
    for asset_key in missing:
        asset = config.ASSETS[asset_key]
        while True:
            try:
                value = float(input(
                    f"{asset['name']} (lot size: {asset['lot_size']:,}): $"
                ).strip())
                if value <= 0:
                    print("  Enter a positive swap charge")
                    continue
                asset['swap_charge'] = value
                break
            except ValueError:
                print("  Enter a valid number")
    print("=" * 84)


def prompt_trading_mode(default="PAPER"):
    print("\n1. PAPER TRADING (simulated)\n2. LIVE TRADING (real money)")
    choice = input("Select mode (1/2, default 1): ").strip()
    if choice == "2":
        confirm = input("WARNING: REAL trades with REAL money. "
                        "Sure? (yes/no): ").strip().lower()
        if confirm == "yes":
            return "LIVE"
    return default


def main():
    parser = argparse.ArgumentParser(description="MT5 basis trading system")
    parser.add_argument('--config', default='config.json' if
                        os.path.exists('config.json') else None,
                        help='Path to config JSON')
    parser.add_argument('--mode', choices=['paper', 'live'],
                        help='Trading mode (skips the prompt)')
    args = parser.parse_args()

    config = load_config(args.config)

    mode = args.mode.upper() if args.mode else prompt_trading_mode()
    prompt_swap_charges(config)

    system = AlgorithmicTradingSystem(config, trading_mode=mode)

    if not system.initialize_broker():
        print("Failed to initialize MT5 — check terminal/credentials/config")
        sys.exit(1)
    if not system.active_assets:
        print("No assets available for trading — check symbol names in config")
        sys.exit(1)

    print("\nACTIVE ASSETS")
    for asset_key, data in system.active_assets.items():
        swap = config.ASSETS[asset_key]['swap_charge']
        print(f"  {asset_key}: {data['spot_symbol']} + "
              f"{data['futures_symbol']} | Swap: ${swap:.2f}/day")

    if mode == "LIVE":
        print("\nWARNING: LIVE TRADING MODE - REAL MONEY AT RISK")
        if input("Type 'START' to begin live trading: ").strip().upper() != "START":
            print("Trading cancelled")
            sys.exit(0)

    time.sleep(2)
    system.performance_tracker.reset_daily_metrics()
    system.is_running = True

    try:
        system.trading_loop()
    except KeyboardInterrupt:
        print("\nShutdown requested...")
    finally:
        system.stop()


if __name__ == "__main__":
    main()

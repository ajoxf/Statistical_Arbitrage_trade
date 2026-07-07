"""Watchdog: relaunch the coordinator if it exits or crashes.

    python run_watchdog.py --config config.json --mode paper

Backs off exponentially on rapid crashes (2s -> 4s -> ... -> 60s cap)
and resets the backoff after 10 minutes of healthy running. The
coordinator's own restart recovery + reconcile-before-trading make
relaunches safe: it reloads open positions from the DB and checks
them against the broker before acting.
"""

import argparse
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description="Coordinator watchdog")
    parser.add_argument('--config', required=True)
    parser.add_argument('--mode', choices=['paper', 'live'], default='paper')
    args = parser.parse_args()

    backoff = 2
    while True:
        started = time.time()
        print(f"[watchdog] launching coordinator ({args.mode})...")
        result = subprocess.run(
            [sys.executable, 'run_coordinator.py',
             '--config', args.config, '--mode', args.mode])

        healthy_for = time.time() - started
        if result.returncode == 0:
            print("[watchdog] coordinator exited cleanly — stopping")
            return
        if healthy_for > 600:
            backoff = 2
        print(f"[watchdog] coordinator died (code {result.returncode}) "
              f"after {healthy_for:.0f}s — relaunch in {backoff}s")
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


if __name__ == '__main__':
    main()

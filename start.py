"""ONE program to run everything.

    python start.py          (or double-click start.bat on Windows)

What it does:
1. Creates config.json and .env from templates on first run.
2. Reads the broker/leg topology from config.json and starts a leg
   runner for every account that needs one (two different accounts =
   two runners; single account = none, the coordinator connects
   directly).
3. Starts the coordinator (paper or live per config.json
   'trading_mode'; live skips the console prompt — the UI toggle and
   saved config are the consent).
4. Serves the web control panel at http://127.0.0.1:8080 and opens
   the browser. ALL settings — brokers, passwords, thresholds, mode —
   are edited there, never by hand.
5. Relaunches any child that crashes (watchdog with backoff) and
   shuts everything down cleanly on Ctrl+C / window close.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

DASHBOARD_PORT = 8080


BANNER = """
============================================================
  NEXUS — first run
------------------------------------------------------------
  Open your MT5 terminal(s) and LOG IN before trading.
  With one terminal open, the engine attaches to it directly.
  For two brokers at once, set each account's terminal path,
  login/server/password and a leg-runner endpoint in the web
  UI (Brokers page), then restart this launcher.
============================================================
"""


def ensure_files():
    fresh = not os.path.exists('config.json')
    if fresh:
        shutil.copyfile('config.example.json', 'config.json')
        print(BANNER)
    if not os.path.exists('.env'):
        with open('.env', 'w', encoding='utf-8') as f:
            f.write("# Managed by the web UI (Settings page) — "
                    "you never need to edit this file.\n")
    return fresh


def unconfigured_accounts(raw_config):
    """Accounts a leg points at that can neither launch a terminal nor
    log in — they can only attach to an already-open terminal."""
    accounts = raw_config.get('accounts', {})
    legs = raw_config.get('leg_accounts', {})
    used = {legs.get('spot'), legs.get('futures')} - {None}
    return sorted(name for name in used
                  if not (accounts.get(name) or {}).get('terminal_path')
                  and not (accounts.get(name) or {}).get('login'))


def plan_leg_runners(raw_config):
    """Which accounts need their own leg-runner process?
    Rule: every account referenced by a leg that has an endpoint.
    Same account for both legs without an endpoint -> none (the
    coordinator connects to the terminal in-process)."""
    accounts = raw_config.get('accounts', {})
    legs = raw_config.get('leg_accounts', {})
    needed = {legs.get('spot'), legs.get('futures')} - {None}
    return sorted(name for name in needed
                  if (accounts.get(name) or {}).get('endpoint'))


class Child:
    def __init__(self, name, cmd):
        self.name = name
        self.cmd = cmd
        self.proc = None
        self.backoff = 2
        self.restarts = 0

    def spawn(self):
        print(f"[launcher] starting {self.name}: {' '.join(self.cmd)}")
        self.proc = subprocess.Popen(self.cmd)

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


MAX_RESTARTS = 5


def monitor(children, stop_event):
    """Relaunch dead children with backoff — the coordinator recovers
    its positions from the DB and reconciles before trading again.

    A child that keeps dying is almost always a configuration problem
    (wrong terminal path, bad login). After MAX_RESTARTS we stop
    retrying and say so, instead of flooding the console forever —
    the web UI stays up so the operator can fix the settings."""
    for child in children:
        child.restarts = 0
    while not stop_event.is_set():
        for child in children:
            if child.proc is None or child.alive():
                continue
            if child.restarts >= MAX_RESTARTS:
                if child.restarts == MAX_RESTARTS:
                    print(f"[launcher] {child.name} keeps failing — giving "
                          f"up after {MAX_RESTARTS} tries. Fix the settings "
                          f"in the web UI, then restart the launcher.")
                    child.restarts += 1     # log once
                continue
            print(f"[launcher] {child.name} exited "
                  f"(code {child.proc.returncode}) — restart in "
                  f"{child.backoff}s")
            stop_event.wait(child.backoff)
            if stop_event.is_set():
                return
            child.backoff = min(child.backoff * 2, 60)
            child.restarts += 1
            child.spawn()
        stop_event.wait(2)


def main():
    ensure_files()
    with open('config.json', 'r', encoding='utf-8') as f:
        raw = json.load(f)
    mode = raw.get('trading_mode', 'paper')

    pending = unconfigured_accounts(raw)
    if pending:
        print(f"[launcher] note: account(s) {', '.join(pending)} have no "
              f"terminal path or login — the engine will attach to the "
              f"MT5 terminal you already have open. Configure them in the "
              f"web UI to target specific terminals.")

    children = []
    for account in plan_leg_runners(raw):
        children.append(Child(
            f"leg runner [{account}]",
            [sys.executable, 'run_leg.py', '--config', 'config.json',
             '--account', account]))

    coordinator_cmd = [sys.executable, 'run_coordinator.py',
                       '--config', 'config.json', '--mode', mode]
    if mode == 'live':
        coordinator_cmd.append('--yes')
        print("[launcher] LIVE MODE — set via the web UI settings")
    children.append(Child("coordinator", coordinator_cmd))

    stop_event = threading.Event()
    try:
        for child in children[:-1]:
            child.spawn()
        if children[:-1]:
            time.sleep(3)          # give terminals a moment to log in
        children[-1].spawn()       # coordinator last

        threading.Thread(target=monitor, args=(children, stop_event),
                         daemon=True).start()

        url = f"http://127.0.0.1:{DASHBOARD_PORT}"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        print(f"[launcher] control panel: {url}  (Ctrl+C stops everything)")

        from statarb.webapp import create_app, run_app
        run_app(create_app(config_path='config.json'),
                host='127.0.0.1', port=DASHBOARD_PORT)
    except KeyboardInterrupt:
        pass
    finally:
        print("[launcher] shutting down...")
        stop_event.set()
        for child in reversed(children):   # coordinator first
            child.stop()


if __name__ == '__main__':
    main()

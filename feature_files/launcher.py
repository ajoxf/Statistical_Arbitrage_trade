"""
Application Launcher - Simple Version
Opens browser automatically after server starts
"""

import sys
import os
import threading
import time
import subprocess
import webbrowser
import logging
from pathlib import Path

# Suppress Flask/Werkzeug logging
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# Add the application directory to path
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).parent
    sys.path.insert(0, str(APP_DIR))
else:
    APP_DIR = Path(__file__).parent.parent
    sys.path.insert(0, str(APP_DIR))

# Load environment variables from .env file
try:
    from dotenv import load_dotenv
    env_file = APP_DIR / '.env'
    if env_file.exists():
        load_dotenv(env_file)
        print(f"Loaded environment from {env_file}", flush=True)
except ImportError:
    pass  # python-dotenv not installed, use system env vars


def start_server():
    """Start the Flask server"""
    from web.app import init_app, socketio, app
    init_app()
    # Suppress Flask startup messages
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    socketio.run(app, host='127.0.0.1', port=5000, debug=False, use_reloader=False, log_output=False)


def start_engine():
    """Start the trading engine via API"""
    import urllib.request
    import json

    try:
        req = urllib.request.Request(
            'http://127.0.0.1:5000/api/engine/start',
            method='POST',
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read().decode())
            if result.get('success'):
                print("Trading engine started successfully!", flush=True)
            else:
                print(f"Engine start warning: {result.get('error', 'Unknown')}", flush=True)
    except Exception as e:
        print(f"Could not auto-start engine: {e}", flush=True)


def main():
    """Main entry point"""
    print("=" * 50, flush=True)
    print("  StatArb Pro", flush=True)
    print("=" * 50, flush=True)
    print("", flush=True)

    # Start server in background
    print("Starting server...", flush=True)
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Wait for server to start
    time.sleep(3)

    # Auto-start the trading engine
    print("Starting trading engine...", flush=True)
    start_engine()

    # Open browser
    url = 'http://127.0.0.1:5000'
    print(f"Opening browser at {url}...", flush=True)

    if sys.platform == 'win32':
        subprocess.Popen(['cmd', '/c', 'start', url], shell=False)
    else:
        webbrowser.open(url)

    print("", flush=True)
    print("=" * 50, flush=True)
    print(f"  Server running at {url}", flush=True)
    print("  Press 'q' to stop the server", flush=True)
    print("=" * 50, flush=True)

    # Windows-specific key detection
    if sys.platform == 'win32':
        import msvcrt
        while True:
            if msvcrt.kbhit():
                key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                if key == 'q':
                    break
            time.sleep(0.1)
    else:
        try:
            input()
        except:
            pass

    print("Shutting down...", flush=True)
    os._exit(0)


if __name__ == '__main__':
    main()

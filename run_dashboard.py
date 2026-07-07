"""Start the read-only web dashboard (own process, safe for LIVE).

    python run_dashboard.py --db algo_trading.db --port 8080
"""

from statarb.webapp import main

if __name__ == '__main__':
    main()

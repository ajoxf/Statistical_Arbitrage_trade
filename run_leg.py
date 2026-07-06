"""Start a leg runner (one per MT5 account).

    python run_leg.py --config config.json --account account_a
"""

from statarb.leg_runner import main

if __name__ == '__main__':
    main()

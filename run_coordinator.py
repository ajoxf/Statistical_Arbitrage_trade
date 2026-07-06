"""Start the coordinator (fuses both legs' prices, routes orders).

    python run_coordinator.py --config config.json --mode paper
"""

from statarb.coordinator import main

if __name__ == '__main__':
    main()

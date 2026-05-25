"""Pytest bootstrap for the statistical-arbitrage test suite.

The application modules live under ``feature_files/`` and several of them
lazily ``import MetaTrader5`` (a Windows-only package). This makes those
modules importable on Linux/CI by putting ``feature_files`` on the path and
stubbing MetaTrader5 when it is not installed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

FEATURE_DIR = Path(__file__).parent / "feature_files"
if str(FEATURE_DIR) not in sys.path:
    sys.path.insert(0, str(FEATURE_DIR))

# MetaTrader5 only ships on Windows with a running terminal. Stub it so the
# trading modules can be imported and their pure logic tested anywhere.
if "MetaTrader5" not in sys.modules:
    sys.modules["MetaTrader5"] = MagicMock()

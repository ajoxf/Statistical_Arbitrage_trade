"""
Web app module - re-exports Flask app components from the main app.py

This module exists to provide a clean import path for the launcher:
    from web.app import init_app, socketio, app
"""

# Import from the parent directory's app.py
import sys
from pathlib import Path

# Ensure the parent directory is in the path
parent_dir = Path(__file__).parent.parent
if str(parent_dir) not in sys.path:
    sys.path.insert(0, str(parent_dir))

# Import and re-export the Flask components
from app import app, socketio, init_app

__all__ = ['app', 'socketio', 'init_app']

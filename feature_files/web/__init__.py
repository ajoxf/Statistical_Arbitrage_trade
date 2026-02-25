# Web module - exports Flask app components
from .app import app, socketio, init_app

__all__ = ['app', 'socketio', 'init_app']

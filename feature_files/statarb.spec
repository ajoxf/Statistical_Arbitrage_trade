# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller Spec File for StatArb Pro

This file defines how to bundle the application into a standalone executable.
Run with: pyinstaller statarb.spec
"""

import os
import sys
from pathlib import Path

# Get the project root directory
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))
PROJECT_ROOT = os.path.dirname(SPEC_DIR)

# Analysis - collect all Python files and dependencies
a = Analysis(
    # Main entry point
    [os.path.join(SPEC_DIR, 'launcher.py')],

    # Additional paths to search for imports
    pathex=[PROJECT_ROOT],

    # Binary dependencies (DLLs, etc.)
    binaries=[],

    # Data files to include
    datas=[
        # Web templates
        (os.path.join(PROJECT_ROOT, 'web', 'templates'), 'web/templates'),
        # Static files (CSS, JS)
        (os.path.join(PROJECT_ROOT, 'web', 'static'), 'web/static'),
        # Configuration
        (os.path.join(PROJECT_ROOT, 'config'), 'config'),
    ],

    # Hidden imports that PyInstaller might miss
    hiddenimports=[
        # Flask and extensions
        'flask',
        'flask_socketio',
        'engineio.async_drivers.threading',
        'socketio',

        # Web server
        'eventlet',
        'eventlet.hubs.epolls',
        'eventlet.hubs.kqueue',
        'eventlet.hubs.selects',
        'dns',
        'dns.rdatatype',
        'dns.rdataclass',

        # Database
        'sqlite3',

        # Numerical
        'numpy',
        'numpy.core._methods',
        'numpy.lib.format',

        # Our modules
        'adapters',
        'adapters.base',
        'adapters.mt5_adapter',
        'adapters.fix_adapter',
        'core',
        'core.signals',
        'core.orchestrator',
        'core.trading_engine',
        'core.ipc',
        'database',
        'database.models',
        'database.manager',
        'workers',
        'workers.broker_worker',
        'web',
        'web.app',

        # Redis (optional)
        'redis',
        'redis.asyncio',

        # YAML
        'yaml',

        # FIX protocol
        'simplefix',
    ],

    # Packages to include entirely
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],

    # Modules to exclude
    excludes=[
        'matplotlib',
        'scipy',
        'pandas',
        'PIL',
        'cv2',
        'torch',
        'tensorflow',
        'pytest',
        'mypy',
    ],

    # Don't treat warnings as errors
    noarchive=False,
)

# Create the PYZ archive (compressed Python modules)
pyz = PYZ(
    a.pure,
    a.zipped_data,
)

# Create the executable
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='StatArbPro',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,  # Show console window for server output
    disable_windowed_traceback=False,

    # Application metadata
    version='version_info.txt',

    # Icon
    icon=os.path.join(SPEC_DIR, 'assets', 'icon.ico'),
)

# Collect all files into distribution folder
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='StatArbPro',
)

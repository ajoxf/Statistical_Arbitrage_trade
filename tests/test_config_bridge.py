"""Web-UI bridge: config hot-reload rules and the control file."""

import json
import os
import time

import pytest

from statarb.config import AlgoTradingConfig


def test_hot_apply_updates_safe_sections(config):
    fresh = AlgoTradingConfig()
    fresh.SIGNALS['ENTRY_Z'] = 2.5
    fresh.EXITS['HARD_MAX_HOLD_MIN'] = 60.0
    fresh.COSTS['MIN_EDGE_MULTIPLE'] = 2.0
    fresh.TRADING['CLIP_LOTS'] = 25.0

    applied, blocked = config.hot_apply(fresh, positions_open=True)
    assert config.SIGNALS['ENTRY_Z'] == 2.5
    assert config.EXITS['HARD_MAX_HOLD_MIN'] == 60.0
    assert config.COSTS['MIN_EDGE_MULTIPLE'] == 2.0
    assert config.TRADING['CLIP_LOTS'] == 25.0
    assert 'SIGNALS' in applied and 'TRADING.CLIP_LOTS' in applied
    assert blocked == []


def test_hot_apply_blocks_structural_changes(config):
    fresh = AlgoTradingConfig()
    fresh.TRADING['HEDGE_RATIO'] = 2.0
    fresh.leg_accounts = {'spot': 'default', 'futures': 'other'}

    applied, blocked = config.hot_apply(fresh, positions_open=True)
    # beta NEVER hot-applies; with a position open it is REJECTED
    assert config.TRADING['HEDGE_RATIO'] == 1.0
    assert any('HEDGE_RATIO' in b and 'position open' in b for b in blocked)
    assert any('leg_accounts' in b for b in blocked)

    applied, blocked = config.hot_apply(fresh, positions_open=False)
    assert config.TRADING['HEDGE_RATIO'] == 1.0     # still restart-only
    assert any('restart' in b for b in blocked)


def test_coordinator_control_and_reload(tmp_path, monkeypatch, config):
    monkeypatch.chdir(tmp_path)     # keep DB/status files out of the repo
    from statarb.coordinator import Coordinator

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({'signals': {'ENTRY_Z': 3.0}}))
    coordinator = Coordinator(config, trading_mode='PAPER',
                              config_path=str(config_path))
    assert coordinator.algo_enabled

    # Control file: web UI stops the algo (entries only)
    control = tmp_path / "control.json"
    control.write_text(json.dumps({'algo_enabled': False}))
    coordinator.control_path = str(control)
    coordinator._read_control()
    assert not coordinator.algo_enabled

    # Config file: web UI changes ENTRY_Z -> hot-applied within a poll
    config_path.write_text(json.dumps({'signals': {'ENTRY_Z': 2.2}}))
    os.utime(config_path, (time.time() + 10, time.time() + 10))
    coordinator._maybe_reload_config()
    assert coordinator.config.SIGNALS['ENTRY_Z'] == 2.2

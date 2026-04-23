"""Shared fixtures for Home Lab Monitor tests."""
import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

# Ensure server/ is on the import path
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

from db import Database


@pytest.fixture
def tmp_db(tmp_path):
    """In-memory-equivalent: a fresh SQLite DB in a temp file."""
    db = Database(str(tmp_path / "test.db"))
    db.init()
    return db


@pytest.fixture
def minimal_config(tmp_path):
    """Write a minimal config.yml and return its path."""
    cfg = {
        "server": {"port": 8888},
        "hosts": [
            {"name": "host-a", "address": "10.0.0.1", "port": 9100,
             "docker": True, "max_slots": 4},
            {"name": "host-b", "address": "10.0.0.2", "port": 9100,
             "docker": True, "max_slots": 2},
            {"name": "host-c", "address": "10.0.0.3", "port": 9100,
             "docker": False},
        ],
        "slot_policy": {
            "max_slots_per_host": 4,
            "default_ttl_seconds": 3600,
            "port_base": 20000,
            "port_stride": 10,
            "max_port_offsets": 50,
        },
    }
    path = tmp_path / "config.yml"
    path.write_text(yaml.dump(cfg))
    return str(path)

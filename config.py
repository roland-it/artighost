"""
Config persistence.
Rules and instructions are stored in agent_config.json next to this file.
Simple JSON on disk for now — easy to swap for Azure Blob or SharePoint later.
"""

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "agent_config.json"
_cache: dict | None = None


def load_config(force: bool = False) -> dict:
    global _cache
    if _cache is not None and not force:
        return _cache

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                _cache = json.load(f)
                log.info(f"Config loaded from {CONFIG_PATH}")
                return _cache
        except Exception as e:
            log.error(f"Failed to load config: {e}")

    _cache = {"rules": [], "instructions": []}
    return _cache


def save_config(config: dict) -> None:
    global _cache
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        _cache = config
        log.info("Config saved.")
    except Exception as e:
        log.error(f"Failed to save config: {e}")

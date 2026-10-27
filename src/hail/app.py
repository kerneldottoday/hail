"""Wire config, journal, Telegram and the hub together for the CLI and MCP server."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass

from .config import Config, load_config
from .hub import Hub
from .store import Store, default_store
from .telegram import Telegram, TelegramError, parse_config


@dataclass(frozen=True)
class App:
    config: Config
    store: Store
    hub: Hub
    telegram: Telegram | None


def make_app(environ: Mapping[str, str] | None = None, trust_user: bool = True) -> App:
    env = os.environ if environ is None else environ
    config = load_config()
    store = default_store()
    _prune(store, config.retention_days)
    tg = _telegram(config, store, env)
    hub = Hub(config, store, env, notify=tg.notify if tg else None, trust_user=trust_user)
    return App(config, store, hub, tg)


def _prune(store: Store, retention_days: int | None) -> None:
    try:
        store.auto_prune(retention_days)
    except Exception as exc:  # housekeeping must never block a call
        print(f"hail: journal prune failed: {exc}", file=sys.stderr)


def _telegram(config: Config, store: Store, environ: Mapping[str, str]) -> Telegram | None:
    try:
        cfg = parse_config(config.telegram, environ)
    except TelegramError as exc:
        print(f"hail: telegram disabled: {exc}", file=sys.stderr)
        return None
    if cfg is None:
        return None
    return Telegram(cfg, store, agents=frozenset(config.agents))

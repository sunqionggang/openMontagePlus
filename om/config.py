"""Configuration helpers for the OpenMontage online layer.

Reads config.yaml (the project's single source of truth) and exposes the
pieces the online layer needs: LLM config, budget defaults, and checkpoint
policy. BYOK keys override server-level env keys at job time.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from .llm import LLMConfig, LLMError

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config.yaml"


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Load config.yaml (cached, read-only)."""
    if not CONFIG_PATH.exists():
        return {}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_llm_section() -> dict[str, Any]:
    cfg = load_config()
    return cfg.get("llm") or {}


def get_budget_section() -> dict[str, Any]:
    cfg = load_config()
    return cfg.get("budget") or {}


def get_checkpoint_policy() -> str:
    cfg = load_config()
    cp = cfg.get("checkpoint") or {}
    return str(cp.get("policy") or "guided")


def build_llm_config(byok_key: Optional[str] = None) -> LLMConfig:
    """Build an LLMConfig from config.yaml + optional BYOK key.

    Raises LLMError only for an *unsupported provider name* — a missing key
    is allowed here and surfaces later as a per-job blocker (fail kindly).
    """
    section = get_llm_section()
    if not section:
        # No llm section at all: fall back to env-driven OpenAI default.
        return LLMConfig(provider="openai", api_key=byok_key)
    return LLMConfig.from_config(section, byok_key=byok_key)


def server_env_has_llm_key() -> bool:
    """Whether the server process env carries a usable LLM key for the
    configured provider (used by /healthz to report preflight status)."""
    try:
        cfg = build_llm_config()
    except LLMError:
        return False
    info = _provider_info(cfg.provider)
    if info is None:
        return False
    env_var = info.get("env")
    if env_var is None:  # ollama — no key needed
        return True
    return bool(os.environ.get(env_var))


def _provider_info(provider: str) -> Optional[dict[str, Any]]:
    from .llm import _PROVIDERS
    return _PROVIDERS.get(provider)

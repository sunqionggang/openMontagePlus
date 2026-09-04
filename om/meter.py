"""Task-level cost meter for the online layer (om/).

Why: OpenMontage lets users run real paid generations (video clips, images,
TTS narration). We want an honest per-task ledger so the account page can
show "what did this job actually consume".

Model:
- jobs.py calls meter_begin(byok_envs) right before a job executes and
  meter_finish() after it ends. Only the online job runner does this; direct
  backlot usage never starts a meter, and meter_* calls are safe no-ops then.
- renderer.py (and other asset producers) call meter_add(...) on SUCCESSFUL
  paid generations only. Failed/fallback/placeholder attempts are not billed.
- Each line is marked hosted=True when the tool ran on a SERVER-provisioned
  key (i.e. the key env var is NOT one the user provided for this job) —
  that spend is deducted from the user's hosted balance. Lines the user paid
  for with their own BYOK keys are recorded for transparency but not deducted.

Rates are platform guidance prices (USD), calibrated to provider list prices.
They can be overridden per-env: OM_RATE_<TOOL> (e.g. OM_RATE_KLING_VIDEO).
LLM/creative-stage cost is NOT metered yet (server MiniMax quota) — the
account UI labels totals as "asset-stage estimate".
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

_log_lock = threading.Lock()

# ---- platform guidance rates (USD) ---------------------------------------
# video: per generated clip (typically ~5s). image: per image. tts: per char.
RATES: dict[str, float] = {
    # video clips
    "apiz_video": 0.0800,
    "kling_video": 0.3500,
    "jimeng_video": 0.1500,
    "minimax_video": 0.0800,
    # images
    "apiz_image": 0.0400,
    "dashscope_image": 0.0200,
    "pexels_image": 0.0,
    "pixabay_image": 0.0,
    # TTS (per character)
    "tts_dashscope": 0.0000004,
    "tts_doubao": 0.0000004,
    "tts_elevenlabs": 0.0000300,
    "tts_google": 0.0,
    "tts_openai": 0.0000150,
    "tts_minimax": 0.0000008,
}

# capability/tool -> which env var carries the key used to run it.
# Used to decide hosted (server key) vs byok (user's own key).
KEY_ENV: dict[str, str] = {
    "apiz_video": "APIZ_API_KEY",
    "kling_video": "KLING_API_KEY",
    "jimeng_video": "VOLC_ACCESSKEY",
    "minimax_video": "MINIMAX_API_KEY",
    "apiz_image": "APIZ_API_KEY",
    "dashscope_image": "DASHSCOPE_API_KEY",
    "pexels_image": "PEXELS_API_KEY",
    "pixabay_image": "PIXABAY_API_KEY",
    "tts_dashscope": "DASHSCOPE_API_KEY",
    "tts_doubao": "DOUBAO_API_KEY",
    "tts_elevenlabs": "ELEVENLABS_API_KEY",
    "tts_google": "GOOGLE_TTS_API_KEY",
    "tts_openai": "OPENAI_API_KEY",
    "tts_minimax": "MINIMAX_API_KEY",
}


def _rate(tool: str) -> float:
    """Effective rate for a tool id (env override wins)."""
    env_key = "OM_RATE_" + tool.upper().replace("-", "_")
    raw = os.environ.get(env_key)
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return RATES.get(tool, 0.0)


class _Meter:
    def __init__(self, byok_envs: set[str]) -> None:
        self.byok_envs = set(byok_envs or ())
        self.lines: list[dict[str, Any]] = []
        self.total_usd = 0.0
        self.hosted_usd = 0.0
        self.byok_usd = 0.0

    def add(self, tool: str, qty: float, *, label: str = "") -> None:
        rate = _rate(tool)
        usd = round(qty * rate, 6)
        if usd <= 0 and rate == 0:
            # free tool: still record for transparency (hosted=False, cost 0)
            pass
        key_env = KEY_ENV.get(tool, "")
        hosted = bool(key_env) and (key_env not in self.byok_envs) and bool(os.environ.get(key_env))
        self.lines.append({
            "tool": tool,
            "qty": round(qty, 4),
            "unit_usd": rate,
            "usd": usd,
            "hosted": hosted,
            "label": label,
            "key_env": key_env,
        })
        self.total_usd = round(self.total_usd + usd, 6)
        if hosted:
            self.hosted_usd = round(self.hosted_usd + usd, 6)
        elif usd > 0:
            self.byok_usd = round(self.byok_usd + usd, 6)

    def summary(self) -> dict[str, Any]:
        # 注意：微额成本（如通义配音 $0.0000004/字，一段 ~8e-6 USD）必须保留到
        # 6 位小数，否则 4 位小数会把它舍成 0，导致“托管扣费恒为 0”的假象。
        return {
            "lines": self.lines,
            "total_usd": round(self.total_usd, 6),
            "hosted_usd": round(self.hosted_usd, 6),
            "byok_usd": round(self.byok_usd, 6),
        }


_active: Optional[_Meter] = None
# meter runs inside the global job serialization lock, but keep it thread-safe
# anyway for future parallel workers.
_active_lock = threading.Lock()


def meter_begin(byok_envs: Optional[set[str]] = None) -> None:
    """Start metering the current job (online runner only)."""
    global _active
    with _active_lock:
        _active = _Meter(byok_envs or set())


def meter_add(tool: str, qty: float = 1.0, *, label: str = "") -> None:
    """Record one successful paid generation. Safe no-op when not metering."""
    with _active_lock:
        m = _active
    if m is not None:
        m.add(tool, qty, label=label)


def meter_finish() -> Optional[dict[str, Any]]:
    """Stop metering and return the summary (None when no meter was active)."""
    global _active
    with _active_lock:
        m = _active
        _active = None
    return m.summary() if m is not None else None


def meter_active() -> bool:
    with _active_lock:
        return _active is not None

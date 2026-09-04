"""Pluggable LLM client for the OpenMontage online layer (BYOK friendly).

The repo itself ships no LLM client — it relies on the coding agent's own
LLM. For a self-hosted product we need one here. This module talks to any
OpenAI-compatible endpoint (OpenAI, OpenRouter, Ollama, Mistral, MiniMax,
Gemini via its OpenAI-compatible surface) plus Anthropic, with:

  * BYOK — each job may inject its own API key, overriding the server env.
  * Fail-closed, fail-kindly — missing key / missing SDK raises LLMError
    with an actionable message instead of crashing the worker.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Raised when the LLM cannot be called (missing key/SDK/network)."""


# provider -> (env var to look up, default base_url, whether SDK exists)
_PROVIDERS: dict[str, dict[str, Any]] = {
    "openai": {
        "env": "OPENAI_API_KEY",
        "base_url": None,
        "sdk": "openai",
    },
    "openrouter": {
        "env": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1",
        "sdk": "openai",
    },
    "ollama": {
        "env": None,  # local, no key required
        "base_url": "http://localhost:11434/v1",
        "sdk": "openai",
    },
    "mistral": {
        "env": "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1",
        "sdk": "openai",
    },
    "minimax": {
        "env": "MINIMAX_API_KEY",
        "base_url": "https://api.minimax.chat/v1",
        "sdk": "openai",
    },
    "gemini": {
        "env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "sdk": "openai",
    },
    "anthropic": {
        "env": "ANTHROPIC_API_KEY",
        "base_url": None,
        "sdk": "anthropic",
    },
    "dashscope": {
        "env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "sdk": "openai",
    },
}

# Sensible default model per provider when config.model is null.
_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "openrouter": "openrouter/auto",
    "ollama": "qwen2.5:7b",
    "mistral": "mistral-small-latest",
    "minimax": "abab6.5s-chat",
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-sonnet-4-20250514",
    "dashscope": "qwen-plus",
}


@dataclass
class LLMConfig:
    provider: str = "openai"
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: int = 4096
    api_key: Optional[str] = None  # BYOK — overrides server env
    base_url: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, cfg: Optional[dict[str, Any]], byok_key: Optional[str] = None) -> "LLMConfig":
        """Build from config.yaml's ``llm:`` section + optional BYOK key.

        ``byok_key`` wins over the environment; the server env is used as a
        fallback when no BYOK key is supplied.
        """
        cfg = cfg or {}
        provider = str(cfg.get("provider") or "openai").lower()
        if provider not in _PROVIDERS:
            raise LLMError(
                f"Unsupported llm.provider {provider!r}. Supported: "
                f"{sorted(_PROVIDERS)}. Fix config.yaml or pass a supported provider."
            )
        return cls(
            provider=provider,
            model=cfg.get("model") or None,
            temperature=float(cfg.get("temperature", 0.7)),
            max_tokens=int(cfg.get("max_tokens", 4096)),
            api_key=byok_key,
            base_url=cfg.get("base_url") or None,
            extra=cfg.get("extra") or {},
        )

    def resolved_model(self) -> str:
        return self.model or _DEFAULT_MODELS.get(self.provider, "gpt-4o-mini")


class LLMClient:
    """Thin chat client. ``complete_json`` returns a parsed dict or raises."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = None

    # -- lifecycle -------------------------------------------------------

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        info = _PROVIDERS.get(self.config.provider)
        if info is None:
            raise LLMError(f"Unsupported provider {self.config.provider!r}")

        key = self._resolve_key(info)
        if info["sdk"] == "openai":
            try:
                import openai  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise LLMError(
                    "The 'openai' SDK is required for provider "
                    f"{self.config.provider!r}. Install it with: pip install openai"
                ) from exc
            kwargs: dict[str, Any] = {}
            if key:
                kwargs["api_key"] = key
            base_url = self.config.base_url or info.get("base_url")
            if base_url:
                kwargs["base_url"] = base_url
            self._client = openai.OpenAI(
                max_retries=3,  # 网络抖动/DNS 瞬断自动重试
                timeout=120.0,
                **kwargs,
            )
            return self._client

        if info["sdk"] == "anthropic":
            try:
                import anthropic  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise LLMError(
                    "The 'anthropic' SDK is required for provider 'anthropic'. "
                    "Install it with: pip install anthropic"
                ) from exc
            self._client = anthropic.Anthropic(api_key=key or "missing")
            return self._client

        raise LLMError(f"No client path for provider {self.config.provider!r}")

    def _resolve_key(self, info: dict[str, Any]) -> Optional[str]:
        byok = self.config.api_key
        env_var = info.get("env")
        if byok:
            return byok
        if env_var:
            return os.environ.get(env_var) or None
        return None

    # -- calls -----------------------------------------------------------

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        """Plain text completion. json_mode 强制 OpenAI 兼容端点输出合法 JSON。"""
        cfg = self.config
        if cfg.provider == "anthropic":
            client = self._ensure_client()
            resp = client.messages.create(
                model=cfg.resolved_model(),
                max_tokens=cfg.max_tokens,
                temperature=cfg.temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
        client = self._ensure_client()
        kwargs: dict[str, Any] = {}
        # 仅官方 OpenAI 支持 response_format=json_object；minimax/dashscope 等兼容端点
        # 会 400（unknown type），靠 complete_json 的容错修复链兜底
        if json_mode and cfg.provider == "openai":
            kwargs["response_format"] = {"type": "json_object"}
        resp = client.chat.completions.create(
            model=cfg.resolved_model(),
            temperature=cfg.temperature,
            max_tokens=cfg.max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        return (resp.choices[0].message.content or "").strip()

    def complete_json(self, system: str, user: str) -> dict[str, Any]:
        """Request a JSON object. Extracts code-fenced or bare JSON."""
        raw = self.complete(system, user, json_mode=True)
        data = extract_json_object(raw)
        # 模型偶尔会把 JSON Schema 的元字段（$id/$schema/definitions 等）复制进输出，
        # 而 artifact schema 一律 additionalProperties=False —— 递归剥离避免校验失败。
        return _strip_schema_meta(data)


def _strip_schema_meta(node: Any) -> Any:
    """Recursively remove JSON-Schema meta keys from a parsed LLM response."""
    _META_KEYS = {"definitions", "patternProperties", "additionalProperties", "allOf", "anyOf", "oneOf"}
    if isinstance(node, dict):
        return {
            k: _strip_schema_meta(v)
            for k, v in node.items()
            if not (k.startswith("$") or k in _META_KEYS)
        }
    if isinstance(node, list):
        return [_strip_schema_meta(item) for item in node]
    return node


def _close_json(text: str) -> str:
    """补全被截断 JSON 的闭合括号（栈跟踪，后开先关）。"""
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    suffix = "".join(reversed(stack))
    if in_str:  # 字符串未闭合，先补引号
        suffix = '"' + suffix
    return text + suffix


def extract_json_object(raw: str) -> dict[str, Any]:
    """Parse a JSON object from a model response (strips fences, finds braces)."""
    text = raw.strip()
    # Strip ```json ... ``` fences
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().lstrip("`").strip().lower() == "json":
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 容错修复 0：非法转义（Invalid \escape）——LLM 常写 \d、\u、\x 或 Windows 路径
    # json.loads 只允许 \" \\ \/ \b \f \n \r \t \uXXXX；其余反斜杠一律修复。
    if "{" in text and "\\" in text:
        import re as _re
        # lambda 返回字面 "\\"（两个反斜杠字符），避免 re.sub 替换串二次转义
        fixed = _re.sub(r'\\(?!["\\/bfnrtu])', lambda _m: "\\\\", text)
        if fixed != text:
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
    # 容错修复 1：括号精确补全（LLM 常在 max_tokens 处截断）——用栈跟踪
    # 未闭合的 { 与 [，按"后开先关"顺序补 } 与 ]。
    if "{" in text:
        try:
            return json.loads(_close_json(text))
        except json.JSONDecodeError:
            pass
    # 容错修复 2：渐进截断——从尾部砍不完整片段，找最大合法 JSON 前缀
    if "{" in text:
        cut = len(text)
        while cut > 0:
            try:
                return json.loads(text[:cut])
            except json.JSONDecodeError:
                cut -= 64
    # Fall back to the first balanced {...} block
    start = text.find("{")
    if start < 0:
        raise LLMError(f"LLM did not return JSON. Raw output (first 200 chars): {text[:200]!r}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise LLMError(
                        f"LLM returned invalid JSON. Error: {exc}. "
                        f"Raw (first 200 chars): {text[:200]!r}"
                    ) from exc
    raise LLMError(f"No balanced JSON object found in model output: {text[:200]!r}")

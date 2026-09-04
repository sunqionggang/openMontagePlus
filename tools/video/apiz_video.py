"""apiz.ai video generation (model minimax/h3, etc.) via the official REST API.

Why curl, not urllib:
    api.apiz.ai sits behind Cloudflare. A plain ``urllib`` request is blocked
    with HTTP 403 "error code: 1010" (browser-signature / TLS-fingerprint
    challenge). ``curl`` with browser headers + ``Origin: https://apiz.ai``
    passes cleanly, so this tool shells out to curl via subprocess.

Verified constraints (apiz.ai v3):
    params.resolution  -> required, one of ["768P", "2K", "4K"]
    params.duration    -> required, integer 4..15
    params.ratio       -> required, not "adaptive" (e.g. "16:9", "9:16", "1:1")

Flow:
    POST {base}/tasks/create  -> data.task_id  (status pending, balance/price in data)
    POST {base}/tasks/query   -> poll data.output (video URL) until ready
    download the URL to output_path
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)

_BASE = "https://api.apiz.ai/api/v3"

# Cloudflare on api.apiz.ai rejects Python TLS fingerprints; mimic a browser.
_CURL_HEADERS = [
    "-H", "Content-Type: application/json",
    "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "-H", "Accept: application/json",
    "-H", "Origin: https://apiz.ai",
]


class ApizVideo(BaseTool):
    name = "apiz_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "apiz"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []  # shells out to curl (subprocess)
    install_instructions = (
        "Set APIZ_API_KEY to your apiz.ai API key (sk-...).\n"
        "  Get one at https://apiz.ai/\n"
        "Requires `curl` on PATH (Cloudflare blocks plain urllib)."
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video"]
    supports = {"text_to_video": True, "image_to_video": False}
    best_for = [
        "apiz.ai aggregated video quota (minimax/h3 and similar models)",
        "text-to-video when direct MiniMax/Kling/Google keys fail auth/quota",
    ]
    not_good_for = ["offline generation", "image-to-video (not yet wired)"]
    fallback_tools = ["kling_video", "veo_video", "wan_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {
                "type": "string",
                "description": "apiz.ai model id, e.g. minimax/h3",
                "default": "minimax/h3",
            },
            "resolution": {
                "type": "string",
                "enum": ["768P", "2K", "4K"],
                "default": "768P",
            },
            "duration": {
                "type": "integer",
                "description": "Clip length in seconds (4-15, model dependent)",
                "default": 6,
            },
            "ratio": {
                "type": "string",
                "description": "Aspect ratio, e.g. 16:9, 9:16, 1:1 (cannot be 'adaptive')",
                "default": "16:9",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=2000, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model", "resolution", "duration", "ratio"]
    side_effects = ["writes video file to output_path", "calls apiz.ai API"]
    user_visible_verification = [
        "Watch generated clip for motion coherence and prompt adherence"
    ]

    # ---- config helpers -------------------------------------------------
    def _get_key(self) -> str | None:
        return os.environ.get("APIZ_API_KEY")

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if self._get_key() else ToolStatus.UNAVAILABLE

    # ---- estimates ------------------------------------------------------
    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # apiz.ai returns an internal "price" per task (credits, not USD).
        # This is a rough placeholder; real billing is in apiz credits.
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        duration = int(inputs.get("duration", 6))
        return float(duration + 120)

    # ---- http (curl) ----------------------------------------------------
    def _http(self, method, path, body=None, timeout=60):
        url = _BASE + path
        cmd = ["curl", "-s", "-w", "\n__HTTP__%{http_code}", "-X", method, url] + list(_CURL_HEADERS)
        cmd += ["-H", f"Authorization: Bearer {self._get_key()}"]
        if body is not None:
            cmd += ["-d", json.dumps(body, ensure_ascii=False)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        except subprocess.TimeoutExpired:
            return None, {"_raw": "curl timeout"}
        raw = res.stdout
        if "\n__HTTP__" in raw:
            body_str, code_str = raw.rsplit("\n__HTTP__", 1)
            try:
                code = int(code_str.strip())
            except Exception:
                code = res.returncode
        else:
            body_str, code = raw, res.returncode
        try:
            j = json.loads(body_str) if body_str.strip() else {}
        except Exception:
            j = {"_raw": body_str}
        return code, j

    # ---- core -----------------------------------------------------------
    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_key()
        if not api_key:
            return ToolResult(success=False, error="APIZ_API_KEY not set. " + self.install_instructions)

        start = time.time()
        prompt = (inputs.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(success=False, error="prompt is required")

        model = inputs.get("model", "minimax/h3")
        resolution = inputs.get("resolution", "768P")
        duration = int(inputs.get("duration", 6))
        ratio = inputs.get("ratio", "16:9")

        try:
            # 1) create
            st, j = self._http("POST", "/tasks/create", {
                "model": model,
                "params": {
                    "content": [{"type": "text", "text": prompt}],
                    "resolution": resolution,
                    "duration": duration,
                    "ratio": ratio,
                },
                "channel": None,
            })
            if st is None or st >= 400:
                msg = j.get("detail") or j.get("message") or j.get("_raw") or j
                return ToolResult(
                    success=False,
                    error=f"apiz create failed (HTTP {st}): {msg}",
                )
            data = j.get("data") or {}
            task_id = data.get("task_id")
            if not task_id:
                return ToolResult(
                    success=False,
                    error=f"apiz create returned no task_id: {j}",
                )

            # 2) poll
            url = self._poll_until_done(task_id, timeout=600)

            # 3) download (retry transient timeouts; apiz CDN can be slow/overloaded)
            output_path = Path(inputs.get("output_path", "apiz_output.mp4"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            dl_ok = False
            last_rc = None
            for attempt in range(3):
                dl = subprocess.run(
                    ["curl", "-s", "-L", "--retry", "3", "--retry-delay", "4",
                     "--connect-timeout", "30", "-m", "540", url,
                     "-o", str(output_path)],
                    capture_output=True, text=True, timeout=600,
                )
                last_rc = dl.returncode
                if last_rc == 0 and output_path.exists() and output_path.stat().st_size > 0:
                    dl_ok = True
                    break
                time.sleep(4)
            if not dl_ok:
                return ToolResult(
                    success=False,
                    error=f"apiz video download failed after retries (curl rc={last_rc})",
                )
        except Exception as e:  # noqa: BLE001
            import traceback as _tb
            return ToolResult(
                success=False,
                error=f"apiz video failed: {e}\n{_tb.format_exc()}",
            )

        return ToolResult(
            success=True,
            data={
                "provider": "apiz",
                "model": model,
                "prompt": prompt,
                "task_id": task_id,
                "output": str(output_path),
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=model,
        )

    @staticmethod
    def _extract_url(obj):
        """Pull a video URL out of apiz's flexible output shape.

        ``data.output`` may be a bare URL string or a nested object carrying the
        URL under any of several common keys; recurse to be safe.
        """
        if isinstance(obj, str):
            return obj if obj.startswith("http") else None
        if isinstance(obj, dict):
            for k in ("url", "video_url", "download_url", "mp4", "file_url", "play_url", "src"):
                v = obj.get(k)
                if isinstance(v, str) and v.startswith("http"):
                    return v
            for v in obj.values():
                r = ApizVideo._extract_url(v)
                if r:
                    return r
        if isinstance(obj, list):
            for item in obj:
                r = ApizVideo._extract_url(item)
                if r:
                    return r
        return None

    def _poll_until_done(self, task_id: str, timeout: int = 600) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, qj = self._http("POST", "/tasks/query", {"task_id": task_id})
            if st is not None and 200 <= st < 300:
                data = (qj or {}).get("data") or {}
                output = data.get("output")
                status = (data.get("status") or "").lower()
                if output:
                    url = self._extract_url(output)
                    if url:
                        return url
                if status in ("failed", "fail", "error", "cancelled", "canceled"):
                    raise RuntimeError(f"apiz task {task_id} {status}: {data}")
            time.sleep(8)
        raise TimeoutError(f"apiz task {task_id} timed out after {timeout}s")

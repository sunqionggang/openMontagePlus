"""MiniMax (Hailuo AI) video generation via the official MiniMax API.

Unlike ``minimax_video`` (which routes through fal.ai via FAL_KEY), this tool
calls MiniMax's own API using the user's official key (MINIMAX_API_KEY). It
supports text-to-video and image-to-video (first-frame), polls asynchronously
for completion, and downloads the finished clip.

Flow (per MiniMax docs):
  1. POST {base}/v1/video_generation  -> task_id
  2. Poll {base}/v2/query/video_generation/{task_id}  (or v1 fallback)
  3. Download the returned video URL to output_path

The poller tolerates both the v2 response shape (``task.content.url``) and the
v1 shape (``file_id`` -> ``/v1/files/retrieve`` -> ``file.download_url``).

Uses only the stdlib (urllib) so it works without third-party deps.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
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


class MiniMaxOfficialVideo(BaseTool):
    name = "minimax_official_video"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "video_generation"
    provider = "minimax"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set MINIMAX_API_KEY to your MiniMax official API key (sk-api-...).\n"
        "  Get one at https://platform.minimax.io/ (Account Management > API Keys)\n"
        "Optional: MINIMAX_API_BASE (default https://api.minimax.chat),\n"
        "         MINIMAX_GROUP_ID (usually not required for sk-api- keys)."
    )
    agent_skills = ["ai-video-gen"]

    capabilities = ["text_to_video", "image_to_video"]
    supports = {"text_to_video": True, "image_to_video": True, "camera_direction": True}
    best_for = [
        "using your own MiniMax/Hailuo official quota (not fal.ai)",
        "text-to-video and first-frame image-to-video",
        "camera-direction prompts via [Pan left] / [Push in] syntax on Hailuo-2.3/02",
    ]
    not_good_for = ["offline generation", "very long clips"]
    fallback_tools = ["kling_video", "veo_video", "wan_video"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "operation": {
                "type": "string",
                "enum": ["text_to_video", "image_to_video"],
                "default": "text_to_video",
            },
            "model_variant": {
                "type": "string",
                "enum": [
                    "MiniMax-Hailuo-2.3",
                    "MiniMax-Hailuo-02",
                    "T2V-01-Director",
                    "T2V-01",
                    "S2V-01",
                    "video-01",
                ],
                "default": "MiniMax-Hailuo-02",
            },
            "image_url": {
                "type": "string",
                "description": "First-frame image URL (required for image_to_video)",
            },
            "duration": {
                "type": "integer",
                "description": "Clip length in seconds (6 or 10, model/resolution dependent)",
                "default": 6,
            },
            "resolution": {
                "type": "string",
                "enum": ["720P", "768P", "1080P"],
                "default": "768P",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=500, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = ["prompt", "model_variant", "operation", "image_url"]
    side_effects = ["writes video file to output_path", "calls MiniMax official API"]
    user_visible_verification = [
        "Watch generated clip for motion coherence and prompt adherence"
    ]

    # ---- config helpers -------------------------------------------------
    def _get_key(self) -> str | None:
        return os.environ.get("MINIMAX_API_KEY")

    def _get_base(self) -> str:
        return os.environ.get("MINIMAX_API_BASE", "https://api.minimax.chat").rstrip("/")

    def _get_group(self) -> str:
        return os.environ.get("MINIMAX_GROUP_ID") or ""

    def get_status(self) -> ToolStatus:
        if self._get_key():
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    # ---- estimates ------------------------------------------------------
    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # ~$0.05/sec per PROVIDERS.md; conservative estimate.
        duration = int(inputs.get("duration", 6))
        return round(duration * 0.05, 3)

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        duration = int(inputs.get("duration", 6))
        return float(duration + 60)

    # ---- http helper ----------------------------------------------------
    def _http(self, method, url, headers, body=None, params=None, timeout=30):
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                j = json.loads(raw)
            except Exception:
                j = {"_raw": raw}
            return e.code, j

    # ---- core -----------------------------------------------------------
    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_key()
        if not api_key:
            return ToolResult(
                success=False,
                error="MINIMAX_API_KEY not set. " + self.install_instructions,
            )

        base = self._get_base()
        group = self._get_group()
        start = time.time()

        operation = inputs.get("operation", "text_to_video")
        variant = inputs.get("model_variant", "MiniMax-Hailuo-02")
        prompt = (inputs.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(success=False, error="prompt is required")

        duration = int(inputs.get("duration", 6))
        resolution = inputs.get("resolution", "768P")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": variant,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
        }
        if group:
            payload["group_id"] = group
        if operation == "image_to_video":
            img = inputs.get("image_url")
            if not img:
                return ToolResult(
                    success=False, error="image_to_video requires image_url"
                )
            payload["first_frame_image"] = img

        try:
            # 1) submit
            status, sj = self._http(
                "POST", f"{base}/v1/video_generation", headers, body=payload, timeout=30
            )
            if status >= 400:
                br = sj.get("base_resp", {})
                return ToolResult(
                    success=False,
                    error=f"MiniMax submit HTTP {status}: {br.get('status_msg')} | {sj}",
                )
            task_id = sj.get("task_id")
            if not task_id:
                br = sj.get("base_resp", {})
                return ToolResult(
                    success=False,
                    error=(
                        f"MiniMax submit rejected: status {br.get('status_code')} "
                        f"{br.get('status_msg')} | {sj}"
                    ),
                )

            # 2) poll + 3) download
            download_url = self._poll_until_done(base, task_id, headers, timeout=600)
            try:
                with urllib.request.urlopen(download_url, timeout=180) as vr:
                    video_bytes = vr.read()
            except urllib.error.HTTPError as e:
                return ToolResult(
                    success=False, error=f"Video download failed: HTTP {e.code}"
                )

            output_path = Path(
                inputs.get("output_path", "minimax_official_output.mp4")
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(video_bytes)

        except Exception as e:  # noqa: BLE001
            return ToolResult(success=False, error=f"MiniMax video failed: {e}")

        return ToolResult(
            success=True,
            data={
                "provider": "minimax_official",
                "model": variant,
                "prompt": prompt,
                "task_id": task_id,
                "output": str(output_path),
            },
            artifacts=[str(output_path)],
            cost_usd=self.estimate_cost(inputs),
            duration_seconds=round(time.time() - start, 2),
            model=variant,
        )

    def _poll_until_done(self, base: str, task_id: str, headers: dict, timeout: int = 600) -> str:
        deadline = time.time() + timeout
        interval = 10
        last_err: Exception | None = None

        while time.time() < deadline:
            # --- v2 path: {base}/v2/query/video_generation/{task_id} -> task.content.url
            try:
                st2, j2 = self._http(
                    "GET", f"{base}/v2/query/video_generation/{task_id}", headers, timeout=20
                )
                if 200 <= st2 < 300:
                    task = j2.get("task", j2)
                    st = (task.get("status") or "").lower()
                    if st in ("succeeded", "success", "successful"):
                        content = task.get("content", {}) or {}
                        url = content.get("url") or content.get("download_url")
                        if url:
                            return url
                    if st in ("failed", "fail", "cancelled", "canceled"):
                        raise RuntimeError(
                            f"MiniMax task {task_id} {st}: {task.get('error')}"
                        )
            except Exception as e:  # noqa: BLE001
                last_err = e

            # --- v1 path: {base}/v1/query/video_generation?task_id= -> file_id -> files/retrieve
            try:
                st1, j1 = self._http(
                    "GET",
                    f"{base}/v1/query/video_generation",
                    headers,
                    params={"task_id": task_id},
                    timeout=20,
                )
                if 200 <= st1 < 300:
                    st = (j1.get("status") or "").lower()
                    if st in ("success", "succeeded", "successful"):
                        file_id = j1.get("file_id")
                        if file_id:
                            rf, fj = self._http(
                                "GET",
                                f"{base}/v1/files/retrieve",
                                headers,
                                params={"file_id": file_id},
                                timeout=20,
                            )
                            if 200 <= rf < 300:
                                fobj = fj.get("file", {}) or {}
                                url = (
                                    fobj.get("download_url")
                                    or fobj.get("url")
                                    or fj.get("download_url")
                                    or fj.get("url")
                                )
                                if url:
                                    return url
                    if st in ("fail", "failed", "cancelled", "canceled"):
                        raise RuntimeError(
                            f"MiniMax task {task_id} {st}: {j1.get('error_message')}"
                        )
            except Exception as e:  # noqa: BLE001
                last_err = e

            time.sleep(interval)

        raise TimeoutError(
            f"MiniMax task {task_id} timed out after {timeout}s (last error: {last_err})"
        )

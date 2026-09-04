"""apiz.ai image generation (openai/gpt-image-2, openai/gpt-image-2/edit, etc.).

Uses the official REST API: create a task, poll until done, download the image.

``api.apiz.ai`` sits behind Cloudflare and rejects plain ``urllib`` requests
(403 JS challenge). We shell out to ``curl`` with browser headers + an
``Origin: https://apiz.ai`` header, exactly like ``tools/video/apiz_video.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from tools.base_tool import BaseTool, ExecutionMode, ToolResult, ToolStability, ToolTier, ToolRuntime, Determinism

_BASE = "https://api.apiz.ai/api/v3"

# Cloudflare on api.apiz.ai rejects Python TLS fingerprints; mimic a browser.
_CURL_HEADERS = [
    "-H", "Content-Type: application/json",
    "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "-H", "Accept: application/json",
    "-H", "Origin: https://apiz.ai",
]


class ApizImage(BaseTool):
    name = "apiz_image"
    version = "0.1.0"
    tier = ToolTier.GENERATE
    capability = "image_generation"
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
    agent_skills = ["ai-image-gen"]

    capabilities = ["text_to_image", "image_to_image"]
    supports = {"text_to_image": True, "image_to_image": True}
    best_for = [
        "apiz.ai aggregated image quota (openai/gpt-image-2 and friends)",
        "text-to-image / edit when direct OpenAI / DashScope keys fail auth/quota",
    ]
    not_good_for = ["offline generation", "local diffusion"]
    fallback_tools = ["dashscope_image", "pexels_image"]

    input_schema = {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {
                "type": "string",
                "description": "apiz.ai model id, e.g. openai/gpt-image-2 (text) or "
                               "openai/gpt-image-2/edit (edit, requires image_urls)",
                "default": "openai/gpt-image-2",
            },
            "image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source images for /edit models (optional for text models)",
            },
            "image_size": {
                "type": "string",
                "enum": ["1:1", "4:3", "3:4", "16:9", "9:16", "3:2", "2:3", "4:5", "5:4", "2:1", "1:2"],
                "description": "Aspect ratio of the generated image",
                "default": "16:9",
            },
            "quality": {
                "type": "string",
                "enum": ["low", "medium", "high"],
                "description": "Generation quality / speed tradeoff",
                "default": "medium",
            },
            "resolution": {
                "type": "string",
                "enum": ["1K", "2K", "4K"],
                "default": "1K",
            },
            "num_images": {"type": "integer", "default": 1},
            "output_path": {"type": "string", "description": "Where to save the image"},
        },
    }

    idempotency_key_fields = ["prompt", "model", "image_size", "resolution", "num_images", "quality"]

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # apiz.ai returns an internal "price" per task (credits, not USD).
        return 0.0

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        return 90.0

    # ---- http (curl) ----------------------------------------------------
    def _get_key(self) -> Optional[str]:
        return os.environ.get("APIZ_API_KEY")

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

    # ---- helpers --------------------------------------------------------
    @staticmethod
    def _extract_url(obj: Any) -> Optional[str]:
        """Pull an image URL out of apiz's flexible output shape."""
        if isinstance(obj, str):
            return obj if obj.startswith("http") else None
        if isinstance(obj, dict):
            for k in ("url", "image_url", "img_url", "download_url", "file_url", "src", "b64_json"):
                v = obj.get(k)
                if k == "b64_json" and isinstance(v, str) and v:
                    return "data:image/png;base64," + v
                if isinstance(v, str) and v.startswith("http"):
                    return v
            for v in obj.values():
                r = ApizImage._extract_url(v)
                if r:
                    return r
        if isinstance(obj, list):
            for item in obj:
                r = ApizImage._extract_url(item)
                if r:
                    return r
        return None

    def _poll_until_done(self, task_id: str, timeout: int = 300) -> str:
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

    def _download(self, url: str, output_path: Path) -> bool:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        last_rc = None
        for attempt in range(3):
            dl = subprocess.run(
                ["curl", "-s", "-L", "--retry", "3", "--retry-delay", "4",
                 "--connect-timeout", "30", "-m", "240", url,
                 "-o", str(output_path)],
                capture_output=True, text=True, timeout=300,
            )
            last_rc = dl.returncode
            if last_rc == 0 and output_path.exists() and output_path.stat().st_size > 0:
                return True
            time.sleep(4)
        return False

    # ---- core -----------------------------------------------------------
    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = self._get_key()
        if not api_key:
            return ToolResult(success=False, error="APIZ_API_KEY not set. " + self.install_instructions)

        start = time.time()
        prompt = (inputs.get("prompt") or "").strip()
        if not prompt:
            return ToolResult(success=False, error="prompt is required")

        model = inputs.get("model", "openai/gpt-image-2")
        image_urls = inputs.get("image_urls") or []
        image_size = inputs.get("image_size", "16:9")
        quality = inputs.get("quality", "medium")
        resolution = inputs.get("resolution", "1K")
        num_images = int(inputs.get("num_images", 1))

        params: dict[str, Any] = {
            "prompt": prompt,
            "quality": quality,
            "image_size": image_size,
            "resolution": resolution,
            "num_images": num_images,
        }
        if image_urls:
            params["image_urls"] = image_urls

        try:
            # 1) create
            st, j = self._http("POST", "/tasks/create", {
                "model": model,
                "params": params,
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

            # 2) poll for the image URL
            url = self._poll_until_done(task_id, timeout=300)

            # 3) download
            output_path = Path(inputs.get("output_path", "apiz_output.png"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if url.startswith("data:image"):
                # base64 data-uri: write directly
                import base64
                b64 = url.split(",", 1)[1]
                output_path.write_bytes(base64.b64decode(b64))
                dl_ok = output_path.stat().st_size > 0
            else:
                dl_ok = self._download(url, output_path)
            if not dl_ok:
                return ToolResult(
                    success=False,
                    error=f"apiz image download failed (task {task_id})",
                )
        except Exception as e:  # noqa: BLE001
            import traceback as _tb
            return ToolResult(
                success=False,
                error=f"apiz image failed: {e}\n{_tb.format_exc()}",
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
        )

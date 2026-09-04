"""Deterministic renderer: turn creative artifacts (script/scene_plan) into a real video.

This fills the last gap in the online product: the orchestrator's tool stages
(assets/edit/compose) used to block with "not wired". This module actually
executes them:

  * assets  — read script.sections → real TTS (dashscope_tts) narration per
              section; read scene_plan.scenes → real image generation
              (dashscope_image / pexels fallback) per scene; write
              asset_manifest.json.
  * edit    — build edit_decisions.json (cuts = scenes, subtitle tracks from
              the script sections).
  * compose — pure-ffmpeg slideshow: each scene image shown for its duration,
              narration + optional music mixed, subtitles burned, output to
              renders/final.mp4; write render_report.json.

Every sub-step degrades gracefully: a failed TTS call yields a video without
that narration chunk; a failed image gen falls back to Pexels then to a solid
placeholder frame. The pipeline still produces a watchable final.mp4 whenever
at least images or audio are available.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

from .meter import meter_add  # noqa: E402  (no-op unless a job meter is active)

# Canonical artifact files under a project dir
_CP = lambda d, s: d / f"checkpoint_{s}.json"


def _ffmpeg() -> str:
    """Locate the ffmpeg binary.

    服务器进程的 PATH 可能不含 ffmpeg（例如从 start.bat / 后台任务启动时），
    导致 subprocess 抛 FileNotFoundError (WinError 2)。这里先查 PATH，再查
    常见安装位置，最后回退到裸命令名。
    """
    import shutil as _sh
    found = _sh.which("ffmpeg")
    if found:
        return found
    candidates = [
        Path.home() / "bin" / "ffmpeg.exe",
        Path.home() / "bin" / "ffmpeg",
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
    ]
    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return "ffmpeg"


def _subtitle_font() -> str:
    """字幕字体（libass force_style FontName）：
    - Windows 本地有微软雅黑 → Microsoft YaHei
    - Linux 容器（Dockerfile 装 fonts-noto-cjk）→ Noto Sans CJK SC
    写死 Windows 字体名在 Linux 容器里会因找不到中文字体导致字幕乱码（方框）。
    """
    if sys.platform == "win32":
        return "Microsoft YaHei"
    return "Noto Sans CJK SC"


def _load_artifact(project_dir: Path, stage: str, name: str) -> Optional[dict[str, Any]]:
    cp = _CP(project_dir, stage)
    if not cp.is_file():
        return None
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
        return (data.get("artifacts") or {}).get(name)
    except (json.JSONDecodeError, OSError):
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------

def _model_prefs() -> dict[str, Any]:
    """读取任务注入的账户模型路由偏好（jobs 线程写入 os.environ["OM_MODEL_PREFS"]）。

    结构（与 om/db.py DEFAULT_MODEL_PREFS 一致）：
      {"video":  {"models": [...], "enabled": {...}, "apiz_keys": [...]},
       "voice":  {...},
       "image":  {...}}
    无偏好时返回 {}，调用方回退默认行为。
    """
    try:
        raw = os.environ.get("OM_MODEL_PREFS")
        if not raw:
            return {}
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — prefs 是增强项，损坏不应影响出片
        return {}


def run_assets(project_dir: Path, *, api_env: dict[str, str] | None = None) -> dict[str, Any]:
    """Generate narration + scene images from script/scene_plan. Returns notes."""
    api_env = api_env or {}
    notes: list[str] = []
    script = _load_artifact(project_dir, "script", "script") or {}
    scene_plan = _load_artifact(project_dir, "scene_plan", "scene_plan") or {}
    scenes = scene_plan.get("scenes", [])
    sections = script.get("sections", [])

    assets: list[dict[str, Any]] = []

    # --- narration: one TTS per script section ---
    audio_dir = project_dir / "assets" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    narration_total = 0.0
    for i, sec in enumerate(sections):
        text = (sec.get("text") or sec.get("narration") or "").strip()
        if not text:
            continue
        out = audio_dir / f"tts_{i+1}.mp3"
        ok, dur = _tts(text, out, api_env)
        if ok:
            assets.append({
                "id": f"tts_{i+1}", "type": "narration", "path": f"assets/audio/tts_{i+1}.mp3",
                "source_tool": "dashscope_tts", "scene_id": sec.get("id", f"sec{i+1}"),
                "duration_seconds": dur,
            })
            narration_total += dur
            notes.append(f"narration[{i+1}] {dur:.1f}s")
        else:
            notes.append(f"narration[{i+1}] skipped: {dur}")

    # --- scene assets: video-first（hainan-qingbuliang 标准）---
    # 默认每个场景生成真实 AI 视频（apiz_video）；失败自动回退静态图。
    # 关闭：api_env 传 VIDEO_FIRST=0，或 .env 无 APIZ_API_KEY 时自动降级。
    img_dir = project_dir / "assets" / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    video_first = (api_env.get("VIDEO_FIRST") or "1") != "0"
    if video_first:
        # 账户偏好：启用了哪些视频模型 + 是否有对应 key（APIZ / 可灵 / 即梦）
        vp = (_model_prefs().get("video") or {})
        v_enabled = [m for m in (vp.get("models") or []) if (vp.get("enabled") or {}).get(m)]
        has_video_key = bool(os.environ.get("APIZ_API_KEY")) \
            or bool(os.environ.get("KLING_API_KEY")) or bool(os.environ.get("VOLC_ACCESSKEY"))
        if not (v_enabled and has_video_key):
            video_first = False
            notes.append("VIDEO_FIRST disabled: no enabled video model with key")
    video_dir = project_dir / "assets" / "video"
    if video_first:
        video_dir.mkdir(parents=True, exist_ok=True)
    seen_hashes: set[str] = set()  # 本次 run_assets 内同图去重（避免 Pexels 同 query 返同图）
    for i, scene in enumerate(scenes):
        out_vid = video_dir / f"clip_{i+1}.mp4"
        out_img = img_dir / f"scene_{i+1}.png"
        if video_first:
            ok, meta = _scene_video(scene, out_vid, api_env)
            if ok:
                meter_add(meta.get("tool", "apiz_video"), 1, label="AI 视频片段")
                assets.append({
                    "id": f"scene_{i+1}", "type": "video", "path": f"assets/video/clip_{i+1}.mp4",
                    "source_tool": meta.get("tool", "apiz_video"), "scene_id": scene.get("id", f"scene{i+1}"),
                    "duration_seconds": meta.get("duration_seconds"),
                })
                notes.append(f"video[{i+1}] via apiz_video")
                continue
            notes.append(f"video[{i+1}] failed({(meta.get('error') or '')[:36]}); fallback image")
        ok, meta = _scene_image(scene, out_img, api_env, seen_hashes)
        if ok:
            meter_add(meta.get("tool", "dashscope_image"), 1, label="画面素材")
            assets.append({
                "id": f"scene_{i+1}", "type": "image", "path": f"assets/images/scene_{i+1}.png",
                "source_tool": meta.get("tool", "dashscope_image"), "scene_id": scene.get("id", f"scene{i+1}"),
            })
            notes.append(f"image[{i+1}] via {meta.get('tool')}")
        else:
            notes.append(f"image[{i+1}] fallback placeholder")

    manifest = {
        "version": "1.0",
        "assets": assets,
        # notes 仅供调用方展示，写盘前用 manifest_for_disk() 剥离
        "_notes": notes,
    }
    _write_json(project_dir / "artifacts" / "asset_manifest.json", manifest_for_disk(manifest))
    return manifest


def manifest_for_disk(manifest: dict[str, Any]) -> dict[str, Any]:
    """Strip non-schema fields before checkpoint write."""
    clean = {k: v for k, v in manifest.items() if not k.startswith("_")}
    return clean


def _tts(text: str, out: Path, env: dict[str, str]) -> tuple[bool, Any]:
    """Real TTS。按账户模型偏好（voice）顺序尝试启用的模型，失败自动降级下一个，兜底 dashscope。

    顺序与开关在设置页「语音合成」卡可调。模型 id：dashscope / doubao / elevenlabs / google / openai。
    """
    prefs = (_model_prefs().get("voice") or {})
    order = prefs.get("models") or []
    enabled = prefs.get("enabled") or {}
    candidates = [m for m in order if enabled.get(m)]
    if not candidates:
        candidates = ["dashscope"]
    if "dashscope" not in candidates:      # 最后兜底：服务器内置 qwen-tts
        candidates.append("dashscope")
    last_err = "no voice model"
    for mid in candidates:
        ok, info = _tts_one(mid, text, out, env)
        if ok:
            meter_add("tts_" + mid, len(text or ""), label="旁白配音")  # per-char billed
            return True, info
        last_err = str(info)
        if out.is_file():  # 失败产物清掉，避免混音时残留
            try:
                out.unlink()
            except Exception:  # noqa: BLE001
                pass
    return False, last_err


def _tts_one(mid: str, text: str, out: Path, env: dict[str, str]) -> tuple[bool, Any]:
    """按模型 id 调一个 TTS 工具；返回 (ok, duration | error)。"""
    try:
        if mid == "dashscope":
            from tools.audio.dashscope_tts import DashscopeTTS
            res = DashscopeTTS().execute({
                "text": text,
                "voice": env.get("TTS_VOICE", "Cherry"),
                "output_path": str(out),
                "model": env.get("TTS_MODEL", "qwen3-tts-flash"),
            })
        elif mid == "doubao":
            from tools.audio.doubao_tts import DoubaoTTS
            res = DoubaoTTS().execute({
                "text": text,
                "output_path": str(out),
                # 音色从环境变量取（DOUBAO_SPEECH_VOICE_TYPE）；无则工具报错→降级
                "voice_id": env.get("DOUBAO_SPEECH_VOICE_TYPE") or os.environ.get("DOUBAO_SPEECH_VOICE_TYPE", ""),
            })
        elif mid == "google":
            from tools.audio.google_tts import GoogleTTS
            res = GoogleTTS().execute({"text": text, "output_path": str(out)})
        elif mid == "elevenlabs":
            from tools.audio.elevenlabs_tts import ElevenLabsTTS
            res = ElevenLabsTTS().execute({"text": text, "output_path": str(out)})
        elif mid == "openai":
            from tools.audio.openai_tts import OpenAITTS
            res = OpenAITTS().execute({"text": text, "output_path": str(out)})
        else:
            return False, f"unknown voice model {mid}"
        if not res.success or not out.is_file():
            return False, (res.error or f"{mid} tts failed")
        dur = _media_duration(out)
        return True, dur
    except Exception as exc:  # noqa: BLE001 — degrade, don't crash the run
        logger.warning("TTS(%s) failed: %s", mid, exc)
        return False, str(exc)[:120]


def _scene_image(scene: dict[str, Any], out: Path, env: dict[str, str], seen_hashes: set[str] | None = None) -> tuple[bool, dict[str, Any]]:
    """Generate an image for a scene。按账户模型偏好（image）顺序尝试启用模型：
    apiz(gpt-image-2) → dashscope(qwen-image) → pexels → pixabay，失败自动降级；
    全部失败 → 纯色占位兜底。顺序/开关在设置页「图像生成」卡可调。
    """
    prompt = _scene_prompt(scene)
    prefs = (_model_prefs().get("image") or {})
    order = prefs.get("models") or []
    enabled = prefs.get("enabled") or {}
    chain = [m for m in order if enabled.get(m)]
    if not chain:
        chain = ["apiz", "pexels", "pixabay"]
    if "apiz" in chain and not os.environ.get("APIZ_API_KEY"):
        chain = [m for m in chain if m != "apiz"]          # 无 APIZ key 就不试 apiz
    for tool in chain:
        try:
            if tool == "apiz":
                from tools.graphics.apiz_image import ApizImage
                # 追加中国风后缀，避免 gpt-image-2 输出西方/英文元素
                cn_suffix = "（中国风格，中国文化元素，画面中无英文文字，无西方面孔，中国真实场景）"
                ok_k, meta_k = _image_apiz_one(ApizImage, prompt + " " + cn_suffix, out, env, seen_hashes)
                if ok_k:
                    return True, meta_k
                continue
            elif tool == "dashscope":
                from tools.graphics.dashscope_image import DashscopeImage
                res = DashscopeImage().execute({"prompt": prompt, "output_path": str(out), "size": "1280*720"})
            elif tool == "pexels":
                from tools.graphics.pexels_image import PexelsImage
                res = PexelsImage().execute({"query": prompt, "orientation": "landscape", "output_path": str(out)})
            else:  # pixabay
                from tools.graphics.pixabay_image import PixabayImage
                res = PixabayImage().execute({"query": prompt[:100], "orientation": "horizontal", "output_path": str(out)})
            if res.success and out.is_file():
                if seen_hashes is not None:
                    h = _file_hash(out)
                    if h in seen_hashes:
                        logger.info("scene_image: %s returned duplicate image, skip", tool)
                        continue
                    seen_hashes.add(h)
                return True, {"tool": tool + "_image"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s_image failed: %s", tool, exc)
    # Solid placeholder (guarantees a file exists → compose always works)
    try:
        _solid_frame(out, (80, 108, 160), text=prompt[:40])
        return True, {"tool": "placeholder"}
    except Exception as exc:  # noqa: BLE001
        return False, {"tool": "none", "error": str(exc)}


def _image_apiz_one(cls, prompt: str, out: Path, env: dict[str, str],
                    seen_hashes: set[str] | None) -> tuple[bool, dict[str, Any]]:
    """APIZ 生图（单 key，读进程 env 的 APIZ_API_KEY）。"""
    try:
        res = cls().execute({"prompt": prompt, "output_path": str(out),
                             "image_size": "16:9", "resolution": "1K", "quality": "medium"})
        if res.success and out.is_file():
            if seen_hashes is not None:
                h = _file_hash(out)
                if h in seen_hashes:
                    return False, {"tool": "apiz_image", "error": "duplicate"}
                seen_hashes.add(h)
            return True, {"tool": "apiz_image"}
        return False, {"tool": "apiz_image", "error": res.error or "apiz_image failed"}
    except Exception as exc:  # noqa: BLE001
        return False, {"tool": "apiz_image", "error": str(exc)}


def _scene_video(scene: dict[str, Any], out: Path, env: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    """Generate a real AI video clip for a scene。按账户模型偏好（video）顺序尝试启用模型：
    apiz → kling → jimeng → minimax，失败自动降级下一个。

    按 hainan-qingbuliang 标准：每个场景生成一段真实视频片段（而非静态图），
    后续 compose 直接拼接视频 → 画面有真实运动。顺序/开关在设置页「视频生成」卡可调。
    """
    prompt = _scene_prompt(scene)
    prefs = (_model_prefs().get("video") or {})
    order = prefs.get("models") or []
    enabled = prefs.get("enabled") or {}
    candidates = [m for m in order if enabled.get(m)]
    if not candidates:
        candidates = ["apiz"] if os.environ.get("APIZ_API_KEY") else []
    if not candidates:
        return False, {"tool": "none", "error": "no enabled video model"}
    errs: list[str] = []
    for mid in candidates:
        if mid == "apiz":
            ok, meta = _video_apiz_one(prompt, out, env)
            if ok:
                return True, meta
            errs.append(f"apiz:{(meta.get('error') or 'fail')[:70]}")
            continue
        ok, meta = _video_other_one(mid, prompt, out, env)
        if ok:
            return True, meta
        errs.append(f"{mid}:{(meta.get('error') or 'fail')[:70]}")
    # 全部视频模型失败 → 由调用方回退静态图（run_assets 已处理）；
    # 返回各模型失败原因，方便任务信息/看板定位（如余额不足、无 key）
    detail = "; ".join(errs)
    return False, {"tool": "none", "error": (detail[:400] or "all video models failed")}


def _video_apiz_one(prompt: str, out: Path, env: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    """APIZ 视频生成（单 key，读进程 env 的 APIZ_API_KEY）。"""
    try:
        from tools.video.apiz_video import ApizVideo
        res = ApizVideo().execute({
            "prompt": prompt,
            "model": env.get("VIDEO_MODEL", "minimax/h3"),
            "duration": int(env.get("VIDEO_DURATION", "5")),
            "ratio": env.get("VIDEO_RATIO", "16:9"),
            "resolution": env.get("VIDEO_RESOLUTION", "768P"),
            "output_path": str(out),
        })
        if res.success and out.is_file() and out.stat().st_size > 0:
            return True, {"tool": "apiz_video", "duration_seconds": _media_duration(out)}
        return False, {"tool": "apiz_video", "error": res.error or "apiz_video failed"}
    except Exception as exc:  # noqa: BLE001 — degrade, don't crash the run
        logger.warning("scene_video(apiz) failed: %s", exc)
        return False, {"tool": "apiz_video", "error": str(exc)[:120]}


def _video_other_one(mid: str, prompt: str, out: Path, env: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    """非 APIZ 视频模型（可灵/即梦/MiniMax直连）best-effort 尝试。失败由 _scene_video 降级。"""
    try:
        if mid == "kling":
            from tools.video.kling_official_video import KlingOfficialVideo
            res = KlingOfficialVideo().execute({
                "prompt": prompt, "output_path": str(out),
                "duration": int(env.get("VIDEO_DURATION", "5")),
            })
        elif mid == "jimeng":
            from tools.video.jimeng_video import JimengVideo
            res = JimengVideo().execute({
                "prompt": prompt, "output_path": str(out), "ratio": env.get("VIDEO_RATIO", "16:9"),
            })
        elif mid == "minimax":
            from tools.video.minimax_official_video import MiniMaxOfficialVideo
            res = MiniMaxOfficialVideo().execute({
                "prompt": prompt, "output_path": str(out),
                "duration": int(env.get("VIDEO_DURATION", "5")),
                "resolution": env.get("VIDEO_RESOLUTION", "768P"),
            })
        else:
            return False, {"tool": mid, "error": f"unknown video model {mid}"}
        if res.success and out.is_file() and out.stat().st_size > 0:
            return True, {"tool": mid + "_video", "duration_seconds": _media_duration(out)}
        return False, {"tool": mid, "error": res.error or f"{mid} video failed"}
    except Exception as exc:  # noqa: BLE001 — degrade, don't crash the run
        logger.warning("scene_video(%s) failed: %s", mid, exc)
        return False, {"tool": mid, "error": str(exc)[:120]}


def _file_hash(path: Path) -> str:
    """计算文件 SHA256（用于同图去重）。"""
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _has_cjk(text: str) -> bool:
    """是否含 CJK（中日韩）字符 → 免费英文素材库搜不了。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


# 抽象/概念/动画/科技/教学/特色类关键词 → 走 APIZ AI 生成
# 免费素材库对这些主题几乎必然返回"字面无关"图（machine=打字机、HTTP=键帽、data=键盘）
# 或完全搜不到（坡鹿/骑楼/清补凉/自贸港等中国特色）
_STYLIZED_KEYWORDS = (
    # --- 抽象/概念/动画 ---
    "abstract", "concept", "chart", "diagram", "graph", "infographic", "illustration",
    "cartoon", "animation", "robot", "futuristic", "cyber", "digital", "metaphor",
    "workflow", "timeline", "idea", "innovation", "artificial", "neural", "pixel",
    "flat design", "vector", "creative", "process", "symbol", "icon", "3d render",
    "sci-fi", "space", "galaxy", "circuit", "芯片", "动画", "插画", "概念", "图表", "未来", "机器人",
    # --- 科技/编程/网络（教学类高频主题）---
    "ai ", "machine learning", "data", "algorithm", "programming", "code", "coding",
    "server", "database", "sql", "nosql", "api", "rest", "graphql", "grpc", "microservice",
    "http", "https", "tcp", "udp", "ssl", "tls", "dns", "websocket", "tcp/ip", "tcpip",
    "encryption", "security", "csrf", "xss", "jwt", "oauth", "sso", "session", "cookie",
    "cloud", "kubernetes", "k8s", "docker", "container", "devops", "ci/cd", "ci cd", "cicd",
    "linux", "kernel", "shell", "bash", "command", "terminal", "vim", "git",
    "frontend", "backend", "fullstack", "spa", "ssr", "webpack", "vite", "react", "vue",
    "machine", "learning", "deep", "neural network", "transformer", "llm", "gpt", "bert",
    "blockchain", "bitcoin", "crypto", "web3", "nft", "smart contract", "ethereum", "solana",
    "iot", "5g", "edge", "embedded", "5G", "6g", "6G", "AIoT",
    "nginx", "proxy", "load balancer", "cache", "redis", "memcached", "kafka", "rabbitmq",
    "monitoring", "observability", "log", "metrics", "tracing", "prometheus", "grafana",
    "monolith", "architecture", "design pattern", "refactor", "agile", "scrum", "kanban",
    "node", "python", "java", "javascript", "typescript", "rust", "go ", "golang",
    "web", "internet", "intranet", "vpn", "cdn", "domain",
    "performance", "optimization", "benchmark", "load test", "stress test",
    # --- 教学动作/概念对比（How/Why/What/Difference/Principle）---
    "tutorial", "explainer", "difference", "principle", "mechanism", "theory", "formula",
    "theorem", "proof", "lesson", "lecture", "course", "teach", "student", "class",
    "how does", "how do", "how to", "why is", "why do", "what is", "what are",
    "vs", "versus", "compared to", "comparison", "pros and cons", "advantages",
    # --- 中国特色/地域/文化（免费库搜不到）---
    "hainan", "qilou", "arcade", "heritage", "landmark", "ancient", "temple", "ethnic",
    "traditional festival", "old street", "free trade port", "special economic zone",
    "海南", "骑楼", "老街", "古镇", "地标", "文化遗产", "民俗", "传统建筑",
    "坡鹿", "海南坡鹿", "eld's deer", "sambar",
    "清补凉", "椰奶", "椰子鸡", "自贸港", "自由贸易港", "骑楼老街", "海口",
    "海南自贸港", "中国（海南）自由贸易港", "数字经济", "智慧城市", "数字货币",
    "国产", "国产化", "信创", "龙芯", "鸿蒙",
)
# 写实/实物类关键词 → 免费素材优先
_REAL_KEYWORDS = (
    "food", "dish", "bowl", "fruit", "vegetable", "landscape", "beach", "ocean", "mountain",
    "city", "building", "house", "person", "people", "woman", "man", "portrait",
    "product", "car", "bike", "animal", "cat", "dog", "flower", "tree", "sunset", "sky",
    "咖啡", "美食", "食物", "水果", "风景", "大海", "城市", "建筑", "人物", "产品", "动物",
)


def _is_stylized_scene(prompt: str) -> bool:
    """判断场景描述偏向抽象/概念（需 AI 生成）还是写实（可用免费素材）。

    按命中关键词数量比较：stylized 命中数 > real 命中数 → APIZ；否则免费。
    """
    low = (prompt or "").lower()
    stylized_hits = sum(1 for k in _STYLIZED_KEYWORDS if k in low)
    real_hits = sum(1 for k in _REAL_KEYWORDS if k in low)
    if stylized_hits == 0:
        return False
    return stylized_hits > real_hits


def _scene_prompt(scene: dict[str, Any]) -> str:
    parts = []
    if scene.get("description"):
        parts.append(str(scene["description"]))
    for k in ("texture_keywords", "shot_intent", "framing"):
        v = scene.get(k)
        if isinstance(v, list) and v:
            parts.append(", ".join(str(x) for x in v))
        elif v:
            parts.append(str(v))
    return "; ".join(parts)[:300] or "abstract background, clean, minimal"


def _solid_frame(out: Path, rgb: tuple[int, int, int], text: str = "") -> None:
    """Create a colored PNG frame (placeholder), optionally with text via ffmpeg drawtext."""
    out.parent.mkdir(parents=True, exist_ok=True)
    color = f"0x{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
    cmd = [_ffmpeg(), "-y", "-f", "lavfi", "-i", f"color=c={color}:s=1280x720:d=1", "-frames:v", "1", str(out)]
    subprocess.run(cmd, capture_output=True, timeout=60)
    if text and out.is_file():
        safe = text.replace("'", "").replace(":", " ")
        cmd2 = [_ffmpeg(), "-y", "-i", str(out), "-vf",
                f"drawtext=text='{safe[:40]}':fontcolor=white:fontsize=32:x=(w-text_w)/2:y=(h-text_h)/2",
                "-frames:v", "1", str(out)]
        subprocess.run(cmd2, capture_output=True, timeout=60)


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

def run_edit(project_dir: Path) -> dict[str, Any]:
    """Build edit_decisions.json from scene_plan + script (cuts = scenes)."""
    scene_plan = _load_artifact(project_dir, "scene_plan", "scene_plan") or {}
    script = _load_artifact(project_dir, "script", "script") or {}
    scenes = scene_plan.get("scenes", [])
    sections = script.get("sections", [])
    sub_map = {s.get("id"): s for s in sections}

    cuts: list[dict[str, Any]] = []
    for i, scene in enumerate(scenes):
        sec = sub_map.get(scene.get("script_section_id"))
        start = float(scene.get("start_seconds", 0) or (sec.get("start_seconds", 0) if sec else i * 5))
        end = float(scene.get("end_seconds", 5) or (sec.get("end_seconds", 5) if sec else start + 5))
        cuts.append({
            "id": scene.get("id", f"cut{i+1}"),
            "source": f"assets/images/scene_{i+1}.png",
            "in_seconds": 0.0,
            "out_seconds": max(0.5, end - start),
            "reason": scene.get("description", ""),
        })

    # Subtitle tracks: one per script section, timed by scene span when possible
    subtitles = []
    for i, sec in enumerate(sections):
        start = float(sec.get("start_seconds", 0) or 0)
        end = float(sec.get("end_seconds", start + 4) or start + 4)
        text = (sec.get("text") or "").strip()
        if not text:
            continue
        subtitles.append({
            "index": i + 1,
            "start_seconds": start,
            "end_seconds": end,
            "text": text,
            "style": "default",
        })

    decisions = {
        "version": "1.0",
        "cuts": cuts,
        "subtitles": {
            "enabled": bool(subtitles),
            "style": "sentence",
            "source": "assets/subtitles.srt" if subtitles else None,
            "font": "Microsoft YaHei",
            "font_size": 20,
        },
        "audio": {
            "narration": {
                "segments": [
                    {"asset_id": f"tts_{i+1}", "start_seconds": s.get("start_seconds", 0),
                     "end_seconds": s.get("end_seconds", 4)}
                    for i, s in enumerate(sections) if (s.get("text") or "").strip()
                ]
            }
        },
        "render_runtime": "ffmpeg",
        "renderer_family": "explainer-data",
        "composition_mode": "templated",
    }
    _write_json(project_dir / "artifacts" / "edit_decisions.json", decisions)
    return decisions


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------

def run_compose(project_dir: Path) -> dict[str, Any]:
    """Pure-ffmpeg compose: scene images + narration + subtitles → final.mp4.

    clip-factory（素材输入）模式：从上传源视频按 scene start/end 真实切段。
    Returns render_report dict. Output: renders/final.mp4.
    """
    # ---- clip-factory：源视频真实切段 ----
    src_marker = project_dir / "source_video.txt"
    if src_marker.is_file():
        video_id = src_marker.read_text(encoding="utf-8").strip()
        src_video = Path("uploads") / video_id / "source.mp4"
        if src_video.is_file():
            return _compose_from_source(project_dir, src_video)

    # ---- video-first：assets/video/ 有 AI 生成的真实视频片段 → 直接拼接 ----
    video_dir = project_dir / "assets" / "video"
    if video_dir.is_dir():
        clips = sorted(video_dir.glob("clip_*.mp4"))
        if clips:
            return _compose_from_clips(project_dir, clips)

    edit = _load_artifact(project_dir, "edit", "edit_decisions") or run_edit(project_dir)
    cuts = edit.get("cuts", [])
    if not cuts:
        return {"version": "1.0", "success": False, "error": "no cuts in edit_decisions"}

    renders = project_dir / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    final = renders / "final.mp4"

    # 1. narration: concat tts chunks into one file if they exist
    narration_path = _concat_narration(project_dir)

    # 2. build the video: each image → still clip of its duration → concat
    # 若有配音：每段视频时长跟随对应 tts 音频时长（声画同步）；否则用 scene_plan 时间轴
    audio_dir = project_dir / "assets" / "audio"
    tts_durs: list[Optional[float]] = []
    if audio_dir.is_dir():
        for i in range(1, len(cuts) + 1):
            p = audio_dir / f"tts_{i}.mp3"
            if p.is_file():
                tts_durs.append(_media_duration(p) + 0.6)
            else:
                tts_durs.append(None)
    seg_dir = project_dir / "assets" / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    segs: list[Path] = []
    for i, cut in enumerate(cuts):
        src = project_dir / str(cut["source"])
        if not src.is_file():
            continue
        if i < len(tts_durs) and tts_durs[i]:
            dur = tts_durs[i]
        else:
            dur = float(cut.get("out_seconds", 5))
        seg = seg_dir / f"seg_{i+1}.mp4"
        cmd = [
            _ffmpeg(), "-y", "-loop", "1", "-i", str(src),
            "-t", f"{dur:.2f}", "-r", "30",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-an", str(seg),
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode == 0 and seg.is_file():
            segs.append(seg)

    if not segs:
        _solid_frame(project_dir / "assets" / "images" / "_fallback.png", (40, 60, 110), text="No visuals available")
        seg = seg_dir / "seg_0.mp4"
        subprocess.run([
            _ffmpeg(), "-y", "-loop", "1", "-i", str(project_dir / "assets" / "images" / "_fallback.png"),
            "-t", "5", "-r", "30", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", str(seg),
        ], capture_output=True, timeout=120)
        segs = [seg]

    concat_list = seg_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{s.resolve()}'\n" for s in segs), encoding="utf-8")
    video_only = seg_dir / "video_concat.mp4"
    subprocess.run([
        _ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(video_only),
    ], capture_output=True, timeout=180)

    # 3. mux narration (if any) + burn subtitles
    srt_path = _write_srt_from_script(project_dir)
    cmd_mux = [_ffmpeg(), "-y", "-i", str(video_only)]
    if narration_path and narration_path.is_file():
        cmd_mux += ["-i", str(narration_path)]
        audio_map = ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        audio_map = ["-map", "0:v:0"]
    if srt_path and srt_path.is_file():
        # Windows: ffmpeg subtitles 滤镜会解析坏反斜杠/冒号 → 用正斜杠并去掉盘符冒号前部分
        srt_arg = str(srt_path.resolve()).replace("\\", "/")
        srt_arg = srt_arg.split(":", 1)[-1] if ":" in srt_arg else srt_arg
        cmd_mux += ["-vf", f"subtitles='{srt_arg}':force_style='FontName={_subtitle_font()},FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=1'"]
    cmd_mux += audio_map + ["-c:v", "libx264", "-preset", "fast", "-crf", "23",
                            "-c:a", "aac", "-b:a", "128k", str(final)]
    subprocess.run(cmd_mux, capture_output=True, timeout=600)

    success = final.is_file() and final.stat().st_size > 0
    report = {
        "version": "1.0",
        "outputs": [
            {"path": "renders/final.mp4",
             "format": "mp4",
             "codec": "h264",
             "audio_codec": "aac",
             "resolution": "1280x720",
             "fps": 30,
             "duration_seconds": round(_media_duration(final), 2) if success else 0,
             "file_size_bytes": final.stat().st_size if success else 0,
             "platform_target": "web"}
        ],
        "render_time_seconds": 0,
        "verification_notes": [
            f"segments={len(segs)}",
            f"narration={bool(narration_path and narration_path.is_file())}",
            f"subtitles={bool(srt_path and srt_path.is_file())}",
        ],
        "metadata": {"success": success, "segments": len(segs)},
    }
    _write_json(project_dir / "artifacts" / "render_report.json", report)
    return report


def _compose_from_clips(project_dir: Path, clips: list[Path]) -> dict[str, Any]:
    """video-first 拼接（hainan-qingbuliang 标准）：每段 clip 截取对应时长 →
    concat → 混旁白 + 烧字幕 → final.mp4。"""
    sp = _load_artifact(project_dir, "scene_plan", "scene_plan") or {}
    scenes = sp.get("scenes", [])
    renders = project_dir / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    final = renders / "final.mp4"
    seg_dir = project_dir / "assets" / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    audio_dir = project_dir / "assets" / "audio"

    # 每段时长：scene_plan.duration_seconds > tts 时长 > 默认 5s
    tts_durs: list[Optional[float]] = []
    if audio_dir.is_dir():
        for i in range(1, len(clips) + 1):
            p = audio_dir / f"tts_{i}.mp3"
            tts_durs.append(_media_duration(p) + 0.6 if p.is_file() else None)

    segs: list[Path] = []
    for i, clip in enumerate(clips):
        dur = 5.0
        if i < len(scenes):
            sd = float(scenes[i].get("duration_seconds") or 0)
            if sd > 0:
                dur = sd
        elif i < len(tts_durs) and tts_durs[i]:
            dur = tts_durs[i]
        seg = seg_dir / f"seg_{i+1}.mp4"
        r = subprocess.run([
            _ffmpeg(), "-y", "-i", str(clip),
            "-t", f"{dur:.2f}", "-r", "30",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", str(seg),
        ], capture_output=True, timeout=300)
        if r.returncode == 0 and seg.is_file():
            segs.append(seg)
        else:
            logger.warning("clip cut failed: %s", clip)

    if not segs:
        report = {"version": "1.0",
                  "outputs": [{"path": "renders/final.mp4", "format": "mp4", "resolution": "1280x720", "duration_seconds": 0}],
                  "warnings": ["clip cut failed"],
                  "metadata": {"success": False, "segments": 0, "method": "video-first", "clips": len(clips)}}
        _write_json(project_dir / "artifacts" / "render_report.json", report)
        return report

    concat_list = seg_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{s.resolve()}'\n" for s in segs), encoding="utf-8")
    video_only = seg_dir / "video_concat.mp4"
    subprocess.run([
        _ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy", str(video_only),
    ], capture_output=True, timeout=180)

    narration_path = _concat_narration(project_dir)
    srt_path = _write_srt_from_script(project_dir)
    cmd_mux = [_ffmpeg(), "-y", "-i", str(video_only)]
    if narration_path and narration_path.is_file():
        cmd_mux += ["-i", str(narration_path)]
        cmd_mux += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        cmd_mux += ["-map", "0:v:0"]
    if srt_path and srt_path.is_file():
        # Windows: ffmpeg subtitles 滤镜会解析坏反斜杠/冒号 → 用正斜杠并去掉盘符冒号前部分
        srt_arg = str(srt_path.resolve()).replace("\\", "/")
        srt_arg = srt_arg.split(":", 1)[-1] if ":" in srt_arg else srt_arg
        cmd_mux += ["-vf", f"subtitles='{srt_arg}':force_style='FontName={_subtitle_font()},FontSize=20,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,Outline=1'"]
    cmd_mux += ["-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", str(final)]
    subprocess.run(cmd_mux, capture_output=True, timeout=600)

    success = final.is_file() and final.stat().st_size > 0
    report = {
        "version": "1.0",
        "outputs": [{
            "path": "renders/final.mp4", "format": "mp4", "codec": "h264", "audio_codec": "aac",
            "resolution": "1280x720", "fps": 30,
            "duration_seconds": round(_media_duration(final), 2) if success else 0,
            "file_size_bytes": final.stat().st_size if success else 0, "platform_target": "web"}],
        "render_time_seconds": 0,
        "verification_notes": [
            f"clips={len(clips)}", f"segments={len(segs)}",
            f"narration={bool(narration_path and narration_path.is_file())}",
            f"subtitles={bool(srt_path and srt_path.is_file())}",
        ],
        "metadata": {"success": success, "segments": len(segs), "method": "video-first", "clips": len(clips)},
    }
    _write_json(project_dir / "artifacts" / "render_report.json", report)
    return report


def _compose_from_source(project_dir: Path, src_video: Path) -> dict[str, Any]:
    """clip-factory：按 scene_plan 的 start/end 从源视频切段，拼接成 final.mp4。"""
    sp = _load_artifact(project_dir, "scene_plan", "scene_plan")
    scenes = (sp or {}).get("scenes", [])
    if not scenes:
        return {"version": "1.0",
                "outputs": [{"path": "renders/final.mp4", "format": "mp4", "resolution": "source", "duration_seconds": 0}],
                "warnings": ["no scenes in scene_plan"],
                "metadata": {"success": False, "segments": 0, "method": "clip-factory-source-cuts",
                             "clips": [], "source": str(src_video)}}
    renders = project_dir / "renders"
    renders.mkdir(parents=True, exist_ok=True)
    seg_dir = project_dir / "assets" / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    segs: list[Path] = []
    for i, sc in enumerate(scenes):
        start = float(sc.get("start_seconds") or 0)
        end = float(sc.get("end_seconds") or start + 5)
        dur = max(1.0, end - start)
        seg = seg_dir / f"clip_{i+1}.mp4"
        r = subprocess.run([
            _ffmpeg(), "-y", "-v", "error", "-ss", f"{start:.2f}", "-t", f"{dur:.2f}",
            "-i", str(src_video), "-c:v", "libx264", "-preset", "veryfast",
            "-c:a", "aac", "-shortest", str(seg),
        ], capture_output=True, timeout=300)
        if r.returncode == 0 and seg.is_file():
            segs.append(seg)
    if not segs:
        return {"version": "1.0",
                "outputs": [{"path": "renders/final.mp4", "format": "mp4", "resolution": "source", "duration_seconds": 0}],
                "warnings": ["clip extraction failed"],
                "metadata": {"success": False, "segments": 0, "method": "clip-factory-source-cuts",
                             "clips": [], "source": str(src_video)}}
    # 拼接
    concat_list = seg_dir / "concat_clips.txt"
    concat_list.write_text("".join(f"file '{s.resolve()}'\n" for s in segs), encoding="utf-8")
    final = renders / "final.mp4"
    r2 = subprocess.run([
        _ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", str(final),
    ], capture_output=True, timeout=300)
    ok = r2.returncode == 0 and final.is_file()
    dur_total = sum(max(1.0, (float(sc.get("end_seconds") or 0) - float(sc.get("start_seconds") or 0)))
                    for sc in scenes)
    report = {
        "version": "1.0",
        "outputs": [{
            "path": "renders/final.mp4",
            "format": "mp4",
            "codec": "h264",
            "audio_codec": "aac",
            "resolution": "source",
            "fps": 30,
            "duration_seconds": round(dur_total, 1),
            "file_size_bytes": final.stat().st_size if final.exists() else 0,
        }],
        "render_time_seconds": 0,
        "warnings": [],
        "metadata": {
            "success": ok,
            "segments": len(segs),
            "method": "clip-factory-source-cuts",
            "clips": [f"clip_{i+1}.mp4 ({sc.get('start_seconds')}-{sc.get('end_seconds')}s)" for i, sc in enumerate(scenes)],
            "source": str(src_video),
        },
    }
    _write_json(project_dir / "artifacts" / "render_report.json", report)
    return report


def _concat_narration(project_dir: Path) -> Optional[Path]:
    audio_dir = project_dir / "assets" / "audio"
    chunks = sorted(audio_dir.glob("tts_*.mp3"))
    if not chunks:
        return None
    # tts chunks 实际是 pcm_s16le（wav 数据），容器必须用 .wav，
    # 否则 ffmpeg 按 .mp3 输出格式编码报 "Exactly one MP3 audio stream is required"
    out = audio_dir / "narration.wav"
    if len(chunks) == 1:
        shutil.copyfile(chunks[0], out)
        return out
    lst = audio_dir / "concat.txt"
    # Windows 反斜杠会被 concat demuxer 转义错误 → 统一用正斜杠
    lst.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in chunks), encoding="utf-8")
    subprocess.run([_ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)],
                   capture_output=True, timeout=180)
    return out if out.is_file() and out.stat().st_size > 0 else None


def _write_srt(project_dir: Path, edit: dict[str, Any]) -> Optional[Path]:
    subs = (edit.get("subtitles") or {}).get("items") or []
    if not subs:
        return None
    out = project_dir / "assets" / "subtitles.srt"
    lines: list[str] = []
    for s in subs:
        idx = int(s.get("index", len(lines) + 1))
        lines.append(str(idx))
        lines.append(f"{_fmt_ts(float(s.get('start_seconds', 0)))} --> {_fmt_ts(float(s.get('end_seconds', 0)))}")
        lines.append(str(s.get("text", "")))
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _write_srt_from_script(project_dir: Path) -> Optional[Path]:
    """Build subtitles.srt directly from the script artifact sections."""
    script = _load_artifact(project_dir, "script", "script") or {}
    sections = script.get("sections", [])
    rows = [s for s in sections if (s.get("text") or "").strip()]
    if not rows:
        return None
    out = project_dir / "assets" / "subtitles.srt"
    lines: list[str] = []
    for i, s in enumerate(rows, start=1):
        lines.append(str(i))
        lines.append(f"{_fmt_ts(float(s.get('start_seconds', 0) or 0))} --> {_fmt_ts(float(s.get('end_seconds', 0) or 4))}")
        lines.append(str(s.get("text", "")))
        lines.append("")
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def _fmt_ts(seconds: float) -> str:
    ms = int(round((seconds % 1) * 1000))
    s = int(seconds) % 60
    m = int(seconds // 60) % 60
    h = int(seconds // 3600)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _media_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip())
    except (ValueError, OSError):
        return 0.0

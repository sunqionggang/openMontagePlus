"""FastAPI control plane for the OpenMontage online layer.

The control plane adds what Backlot intentionally lacks: the ability to
*trigger* production. It owns:

  * POST /api/jobs        — submit a prompt → create project + run pipeline
  * GET  /api/jobs        — list jobs (public fields only, secrets stripped)
  * GET  /api/jobs/{id}   — one job's status / stage results / blocker
  * GET  /api/pipelines   — available pipeline manifests
  * GET  /api/health      — preflight: LLM configured? server up?
  * GET  /board*          — the Backlot board (reused, read-only, live)

Run:  uvicorn om.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi import File, UploadFile
from pydantic import BaseModel, Field

# 启动时加载 .env（LLM/图片/音乐等 API key 都从这里进进程环境）
from lib.env_loader import load_env
load_env()

from lib.checkpoint import PROJECTS_DIR
from lib.pipeline_loader import list_pipelines

from .config import get_checkpoint_policy, server_env_has_llm_key
from .db import (
    AuthError, DuplicateUserError, WorksError,
    authenticate, create_work, delete_work, gallery, init_db,
    list_works, login, logout, register, update_work,
)
from .jobs import JobStore, create_job, get_job, run_job_background

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKLOT_UI_DIR = REPO_ROOT / "backlot" / "ui"
OM_UI_DIR = Path(__file__).resolve().parent / "ui"
STATE_DIR = REPO_ROOT / ".om" / "state"
# 持久化 JSON 放 projects/_data/ 子目录（proj_*/ 子目录内文件写一直放行；
# projects/ 根目录固定文件写多次后被环境锁死）。OM_DB_PATH 可覆盖。
DB_PATH = Path(os.environ.get("OM_DB_PATH") or REPO_ROOT / "projects" / "_data" / "om_data.json")

store: JobStore

# /api/tools 的缓存：provider_menu() 会跑全量依赖探测（最慢的工具如
# hyperframes 要十几秒），必须缓存 + 后台预热，否则每次请求都卡几十秒。
_tools_cache: dict[str, Any] = {}
_tools_cache_at: float = 0.0
_TOOLS_CACHE_TTL = 300.0
_tools_lock = threading.Lock()


def _warm_tools_cache() -> None:
    """Background task: compute the tool menu once so first page load is fast."""
    global _tools_cache, _tools_cache_at
    try:
        data = _build_tools_menu()
        with _tools_lock:
            _tools_cache = data
            _tools_cache_at = time.time()
        logger.info("Tool menu warmed (%d capabilities)", len(data.get("capabilities", {})))
    except Exception as exc:
        logger.warning("Tool menu warmup failed: %s", exc)


def _build_tools_menu() -> dict[str, Any]:
    try:
        from tools.tool_registry import registry
        registry.ensure_discovered()
        menu = registry.provider_menu()
    except Exception as exc:
        logger.exception("tool registry unavailable")
        return {"error": f"tool registry unavailable: {exc}", "capabilities": {}}

    out: dict[str, Any] = {"capabilities": {}, "llm": {}}
    for cap, bucket in menu.items():
        entry: dict[str, Any] = {
            "label": CAPABILITY_LABELS.get(cap, cap),
            "available": [_tool_entry(t) for t in bucket.get("available", [])],
            "unavailable": [_tool_entry(t) for t in bucket.get("unavailable", [])],
            "total": bucket.get("total", 0),
            "configured": bucket.get("configured", 0),
        }
        out["capabilities"][cap] = entry
    out["capability_order"] = [c for c in menu if c != "selector"]
    out["llm"] = {
        "providers": ["openai", "openrouter", "anthropic", "gemini", "ollama", "mistral", "minimax"],
        "server_configured": server_env_has_llm_key(),
    }
    return out

# 工具名 -> 它读取的 env key（源码扫描结果 + dependencies env: 前缀）。
# 配置页据此渲染"该填哪些 key"。
TOOL_ENV_KEYS: dict[str, list[str]] = {
    "dashscope_tts": ["DASHSCOPE_API_KEY"],
    "doubao_tts": ["DOUBAO_SPEECH_API_KEY"],
    "elevenlabs_tts": ["ELEVENLABS_API_KEY"],
    "google_tts": ["GOOGLE_API_KEY"],
    "openai_tts": ["OPENAI_API_KEY"],
    "kling_tts": ["KLING_API_KEY"],
    "music_gen": ["ELEVENLABS_API_KEY"],
    "suno_music": ["SUNO_API_KEY"],
    "freesound_music": ["FREESOUND_API_KEY"],
    "music_library": ["MUSIC_LIBRARY_DIR"],
    "dashscope_image": ["DASHSCOPE_API_KEY"],
    "apiz_image": ["APIZ_API_KEY"],
    "pexels_image": ["PEXELS_API_KEY"],
    "pixabay_image": ["PIXABAY_API_KEY"],
    "openai_image": ["OPENAI_API_KEY"],
    "google_imagen": ["GOOGLE_API_KEY"],
    "grok_image": ["XAI_API_KEY"],
    "flux_image": ["FAL_KEY", "FAL_AI_API_KEY"],
    "recraft_image": ["FAL_KEY", "FAL_AI_API_KEY"],
    "unsplash": ["UNSPLASH_ACCESS_KEY"],
    "apiz_video": ["APIZ_API_KEY"],
    "pexels_video": ["PEXELS_API_KEY"],
    "pixabay_video": ["PIXABAY_API_KEY"],
    "veo_video": ["FAL_KEY", "FAL_AI_API_KEY"],
    "gemini_omni_video": ["GOOGLE_API_KEY"],
    "kling_official_video": ["KLING_API_KEY"],
    "minimax_official_video": ["MINIMAX_API_KEY", "MINIMAX_GROUP_ID"],
    "runway_video": ["RUNWAY_API_KEY"],
    "heygen_video": ["HEYGEN_API_KEY"],
    "sora_video": ["OPENAI_API_KEY"],
    "grok_video": ["XAI_API_KEY"],
    "higgsfield_video": ["HIGGSFIELD_API_KEY", "HIGGSFIELD_API_SECRET"],
    "jimeng_video": ["VOLC_ACCESSKEY", "VOLC_SECRETKEY"],
    "kling_avatar": ["KLING_API_KEY"],
    "kling_lip_sync": ["KLING_API_KEY"],
    "dashscope_asr": ["DASHSCOPE_API_KEY"],
    "azure_stt": ["AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION"],
    "transcriber": ["HF_TOKEN"],
}

# 能力 -> 中文名（配置页分组标题）
CAPABILITY_LABELS: dict[str, str] = {
    "llm": "创意 LLM",
    "image_generation": "图像生成",
    "video_generation": "视频生成",
    "tts": "语音合成",
    "music": "音乐",
    "music_generation": "音乐生成",
    "music_library": "音乐素材库",
    "music_search": "音乐检索",
    "avatar": "数字人",
    "analysis": "分析 / ASR",
    "clip_retrieval": "素材检索",
    "subtitle": "字幕",
    "video_post": "视频后期",
    "screen_capture": "录屏",
    "publish": "发布",
}

# 配置页重点展示的能力（其余折叠不显示 key 明细）
KEY_CAPABILITIES = [
    "image_generation", "video_generation", "tts", "music_generation",
    "music_library", "music_search", "avatar", "analysis", "clip_retrieval",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    store = JobStore(state_dir=STATE_DIR)
    init_db(DB_PATH)  # SQLite: users/tokens/works
    # 后台预热工具清单（provider_menu 全量探测很慢，不能让首个请求扛）
    threading.Thread(target=_warm_tools_cache, name="om-tools-warm", daemon=True).start()
    yield


app = FastAPI(title="OpenMontage Online", version="0.1.0", lifespan=lifespan)


# --------------------------------------------------------------------------
# Schemas
# --------------------------------------------------------------------------

class JobCreate(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    pipeline_type: str = "animated-explainer"
    title: Optional[str] = None
    style_playbook: Optional[str] = None
    creative_only: bool = False
    auto_approve: bool = True
    llm_provider: Optional[str] = None  # 用户 BYOK 选择的 LLM provider，覆盖 config.yaml
    api_keys: dict[str, str] = Field(default_factory=dict, description="BYOK env vars, e.g. OPENAI_API_KEY")
    source_video: Optional[str] = None  # 素材输入类流水线（clip-factory 等）的上传视频 video_id


# --------------------------------------------------------------------------
# Auth / Users / Works (SQLite 多用户隔离)
# --------------------------------------------------------------------------

def _bearer(request: Request) -> dict[str, Any]:
    """FastAPI dependency: resolve the bearer token to a user dict."""
    token = request.headers.get("Authorization", "")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    try:
        return authenticate(token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


class RegisterBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=40)
    email: str = Field(..., min_length=3, max_length=120)
    password: str = Field(..., min_length=4, max_length=128)
    plan: Optional[str] = None


class LoginBody(BaseModel):
    email: str
    password: str


class WorkCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    dur: str = "60s"
    emoji: str = "🎬"
    status: str = "done"
    project_id: Optional[str] = None
    job_id: Optional[str] = None


class WorkPatch(BaseModel):
    title: Optional[str] = None
    published: Optional[bool] = None
    likes: Optional[int] = None
    ai_applied: Optional[bool] = None
    tags: Optional[list[str]] = None
    platform: Optional[str] = None
    status: Optional[str] = None


@app.post("/api/auth/register", status_code=201)
def api_register(body: RegisterBody) -> dict[str, Any]:
    try:
        return register(body.username, body.email, body.password, plan=body.plan or "免费版")
    except DuplicateUserError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/auth/login")
def api_login(body: LoginBody) -> dict[str, Any]:
    try:
        return login(body.email, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/auth/logout")
def api_logout(user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    token = ""
    # _bearer 已校验；这里从 store 端删 token 由 db.logout 处理
    return {"ok": True}


@app.get("/api/me")
def api_me(user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    return user


@app.get("/api/me/account")
def api_my_account(user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    """账户中心：托管余额（自动补开户体验金）+ 任务成本账本 + 资金流水。"""
    from .db import get_account
    return get_account(user["id"])


class RechargeBody(BaseModel):
    amount_usd: float = Field(..., gt=0, le=1000, description="充值金额（USD，测试通道）")
    note: str = Field(default="", max_length=80)


@app.post("/api/me/recharge")
def api_recharge(body: RechargeBody, user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    """托管余额充值（试水：测试通道，模拟入账，正式支付渠道对接中）。"""
    from .db import add_balance, get_account
    note = (body.note or "托管余额充值（测试通道）").strip()
    balance = add_balance(user["id"], round(body.amount_usd, 4), kind="recharge", note=note)
    return {"balance_usd": balance, "recharged_usd": round(body.amount_usd, 4),
            "note": "测试通道已入账 —— 正式支付（微信/支付宝）对接中，当前仅供体验扣费闭环。"}


@app.get("/api/me/keys")
def api_my_keys(user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    """返回账户已保存的 key 名称列表（绝不返回值）。"""
    from .db import get_api_keys
    keys = get_api_keys(user["id"])
    return {
        "keys": sorted(keys.keys()),
        "llm_configured": server_env_has_llm_key() or bool(keys),
    }


class KeysPut(BaseModel):
    api_keys: dict[str, str] = Field(default_factory=dict, description="key名→值；值传空字符串删除该key")


@app.put("/api/me/keys")
def api_put_keys(body: KeysPut, user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    """保存账户级 BYOK keys（Web 配置页调用）。只返回 key 名称。"""
    from .db import set_api_keys
    saved = set_api_keys(user["id"], body.api_keys)
    return {"keys": sorted(saved.keys())}


class KeysTestBody(BaseModel):
    env: str = Field(..., description="key 名称，如 OPENAI_API_KEY")
    value: str = Field(default="", description="要测试的 key 值；为空则用账户已存或服务器内置")


@app.post("/api/me/keys/test")
def api_test_key(body: KeysTestBody, user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    """测试一个 BYOK key 的连通性（真实最小请求，不消耗生成额度）。

    优先级：请求传入 value > 账户已存 key > 服务器 .env 内置 key。
    """
    from .db import get_api_keys
    value = body.value.strip()
    if not value:
        value = get_api_keys(user["id"]).get(body.env, "")
    if not value:
        value = os.environ.get(body.env, "")
    if not value:
        return {"ok": False, "env": body.env, "message": f"未提供 {body.env}，且账户/服务器也未配置"}

    try:
        return _probe_key(body.env, value)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "env": body.env, "message": f"测试异常: {exc}"}


def _probe_key(env: str, value: str) -> dict[str, Any]:
    """按 key 类型做一次最小连通性探测。"""
    env_u = env.upper()
    # ---- LLM 系（OpenAI 兼容 SDK）----
    if env_u in ("OPENAI_API_KEY", "MINIMAX_API_KEY", "DASHSCOPE_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        provider = {
            "OPENAI_API_KEY": "openai",
            "MINIMAX_API_KEY": "minimax",
            "DASHSCOPE_API_KEY": "dashscope",
            "ANTHROPIC_API_KEY": "anthropic",
            "GEMINI_API_KEY": "gemini",
        }[env_u]
        from om.llm import LLMClient, LLMConfig
        cfg = LLMConfig(provider=provider, api_key=value, max_tokens=8)
        out = LLMClient(cfg).complete("Reply with OK", "Say OK")
        if not (out or "").strip():
            return {"ok": False, "env": env, "message": f"{provider} 返回空响应"}
        return {"ok": True, "env": env, "message": f"{provider} 连通正常（{out.strip()[:20]}）"}
    # ---- Pexels 图片素材 ----
    if env_u == "PEXELS_API_KEY":
        import urllib.request
        # Pexels 拒绝 urllib 默认 UA（403）→ 带浏览器 UA
        req = urllib.request.Request(
            "https://api.pexels.com/v1/search?query=test&per_page=1",
            headers={"Authorization": value,
                     "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status == 200:
                return {"ok": True, "env": env, "message": "Pexels 连通正常（素材搜索 API）"}
            return {"ok": False, "env": env, "message": f"Pexels HTTP {r.status}"}
    # ---- Pixabay 音乐/图片 ----
    if env_u == "PIXABAY_API_KEY":
        import urllib.request
        # per_page 必须 ≥3（Pixabay 限制），否则 400
        with urllib.request.urlopen(f"https://pixabay.com/api/?key={value}&q=test&per_page=3", timeout=15) as r:
            import json as _json
            d = _json.loads(r.read().decode())
            if d.get("totalHits", -1) >= 0:
                return {"ok": True, "env": env, "message": "Pixabay 连通正常"}
            return {"ok": False, "env": env, "message": f"Pixabay: {d.get('error', '未知错误')}"}
    # ---- APIz 聚合（create 一次即验证鉴权，不轮询不下载，不触发实际生成）----
    if env_u == "APIZ_API_KEY":
        import subprocess as _sp, json as _json
        cmd = ["curl", "-s", "-w", "\n__HTTP__%{http_code}", "-X", "POST",
               "https://api.apiz.ai/api/v3/tasks/create",
               "-H", "Content-Type: application/json",
               "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
               "-H", "Accept: application/json",
               "-H", "Origin: https://apiz.ai",
               "-H", f"Authorization: Bearer {value}",
               "-d", _json.dumps({"model": "openai/gpt-image-2",
                                  "params": {"prompt": "connectivity test", "image_size": "1:1",
                                             "resolution": "1K", "num_images": 1, "quality": "low"},
                                  "channel": None})]
        raw = _sp.run(cmd, capture_output=True, text=True, timeout=40).stdout
        body_str, code_str = raw.rsplit("\n__HTTP__", 1) if "\n__HTTP__" in raw else (raw, "0")
        try:
            code = int(code_str.strip())
        except Exception:
            code = 0
        if 200 <= code < 300:
            return {"ok": True, "env": env, "message": "APIZ 连通正常（鉴权通过）"}
        if "余额不足" in body_str:
            return {"ok": True, "env": env, "message": "APIZ 鉴权通过，但余额不足：" + body_str[:60]}
        detail = body_str[:160].replace("\n", " ")
        return {"ok": False, "env": env, "message": f"APIZ HTTP {code}: {detail}"}

    return {"ok": False, "env": env, "message": f"暂不支持测试 {env}（支持 LLM/Pexels/Pixabay/APIZ）"}


@app.get("/api/me/prefs")
def api_my_prefs(user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    """返回账户的模型路由偏好（视频/语音/图片：启用模型、顺序、APIZ 多 key）。"""
    from .db import get_user_prefs
    return {"prefs": get_user_prefs(user["id"])}


class PrefsPut(BaseModel):
    prefs: Optional[dict[str, Any]] = Field(default=None, description="模型路由偏好 JSON")


@app.put("/api/me/prefs")
def api_put_prefs(body: PrefsPut, user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    """保存账户的模型路由偏好。"""
    from .db import set_user_prefs
    saved = set_user_prefs(user["id"], body.prefs)
    return {"prefs": saved}


@app.get("/api/me/works")
def api_my_works(user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    return {"works": [_attach_cover(w) for w in list_works(user["id"])]}


@app.post("/api/me/works", status_code=201)
def api_create_work(body: WorkCreate, user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    return create_work(
        user["id"], title=body.title, dur=body.dur,
        emoji=body.emoji, status=body.status,
        project_id=body.project_id, job_id=body.job_id,
    )


@app.patch("/api/me/works/{work_id}")
def api_patch_work(work_id: int, body: WorkPatch, user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    try:
        return update_work(
            user["id"], work_id,
            title=body.title, published=body.published, likes=body.likes,
            ai_applied=body.ai_applied, tags=body.tags, platform=body.platform,
            status=body.status,
        )
    except WorksError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.delete("/api/me/works/{work_id}", status_code=204)
def api_delete_work(work_id: int, user: dict[str, Any] = Depends(_bearer)) -> None:
    try:
        delete_work(user["id"], work_id)
    except WorksError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/gallery")
def api_gallery() -> dict[str, Any]:
    return {"works": [_attach_cover(w) for w in gallery()]}


# --------------------------------------------------------------------------
# AI 发布助手：标题 / 标签（真 LLM 生成，BYOK 可覆盖）
# --------------------------------------------------------------------------

class AiOptimizeBody(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=2000)   # 作品标题/创作意图
    api_keys: dict[str, str] = Field(default_factory=dict)    # 可选 BYOK
    llm_provider: Optional[str] = None


class ExportBody(BaseModel):
    work_id: int
    ratio: str = "16:9"     # 9:16 | 16:9 | 1:1
    resolution: str = "1080p"


@app.post("/api/export")
def api_export(body: ExportBody, user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    """把作品的成片按目标比例/分辨率用 ffmpeg 重新转码导出。"""
    import subprocess
    from .db import get_work, WorksError as _WE

    try:
        w = get_work(user["id"], body.work_id)
    except _WE as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    pid = w.get("project_id")
    if not pid:
        raise HTTPException(status_code=422, detail="作品没有关联项目，无法导出")
    project_dir = PROJECTS_DIR / pid
    src = project_dir / "renders" / "final.mp4"
    if not src.is_file():
        raise HTTPException(status_code=422, detail=f"未找到成片: {project_dir}/renders/final.mp4")

    # 目标尺寸（宽度×高度，16:9 1080p → 1920x1080）
    dims = {
        ("16:9","1080p"): (1920,1080), ("16:9","720p"): (1280,720), ("16:9","4K"): (3840,2160),
        ("9:16","1080p"): (1080,1920), ("9:16","720p"): (720,1280), ("9:16","4K"): (2160,3840),
        ("1:1","1080p"): (1080,1080), ("1:1","720p"): (720,720), ("1:1","4K"): (2160,2160),
    }
    target = dims.get((body.ratio, body.resolution))
    if not target:
        raise HTTPException(status_code=422, detail=f"不支持的组合: {body.ratio} {body.resolution}")

    out_dir = project_dir / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ratio_tag = body.ratio.replace(":", "x")
    out = out_dir / f"final_{ratio_tag}_{body.resolution}.mp4"
    w_, h_ = target
    # crop 保持内容居中：先 scale 到覆盖目标尺寸，再 center crop
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", f"scale={w_}:{h_}:force_original_aspect_ratio=increase,crop={w_}:{h_}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23", "-c:a", "aac", "-b:a", "128k",
        str(out),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not out.is_file():
            raise HTTPException(status_code=500, detail="ffmpeg 转码失败: " + (r.stderr or "")[-300:])
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="转码超时（>10 分钟）") from None
    return {
        "url": f"/media/{pid}/exports/{out.name}",
        "ratio": body.ratio,
        "resolution": body.resolution,
        "size_bytes": out.stat().st_size,
        "name": out.name,
    }


@app.post("/api/ai/optimize")
def api_ai_optimize(body: AiOptimizeBody) -> dict[str, Any]:
    """AI 生成 3 个标题候选 + 封面建议 + 话题标签。

    LLM 不可用（无 key / 无 SDK）时返回 502 + detail，前端应回退到 mock 池。
    """
    from .config import build_llm_config
    from .llm import LLMClient, LLMConfig, LLMError, extract_json_object

    key_env = None
    if body.llm_provider:
        key_env = {"openai":"OPENAI_API_KEY","openrouter":"OPENROUTER_API_KEY",
                   "anthropic":"ANTHROPIC_API_KEY","gemini":"GEMINI_API_KEY",
                   "mistral":"MISTRAL_API_KEY","minimax":"MINIMAX_API_KEY"}.get(body.llm_provider)
    byok = body.api_keys.get(key_env) if key_env else (body.api_keys.get("OPENAI_API_KEY") or body.api_keys.get("ANTHROPIC_API_KEY"))
    try:
        cfg = build_llm_config(byok_key=byok)
        if body.llm_provider:
            cfg.provider = body.llm_provider
            if byok: cfg.api_key = byok
        client = LLMClient(cfg)
        system = (
            "你是一个短视频运营专家。根据用户给出的视频主题，生成发布优化方案。\n"
            "只输出 JSON，格式如下：\n"
            '{"titles":[{"text":"标题1","style":"高互动/直接给价值/紧迫感 之一"},{"text":"标题2","style":"..."},{"text":"标题3","style":"..."}],'
            '"cover_suggestions":["封面建议1","封面建议2","封面建议3"],"tags":["#标签1","#标签2","#标签3","#标签4"]}'
        )
        user = f"视频主题：{body.prompt}\n生成 3 个差异化标题（每个标注风格类型）、3 条封面设计建议、4 个中文话题标签。"
        data = client.complete_json(system, user)
        titles = data.get("titles") or []
        if isinstance(titles, list) and titles and isinstance(titles[0], dict):
            title_pool = [[t.get("text",""), t.get("style","")] for t in titles[:3]]
        else:
            title_pool = [[str(t), "AI 生成"] for t in titles[:3]]
        covers = [str(c) for c in (data.get("cover_suggestions") or [])[:3]]
        tags = [str(t) for t in (data.get("tags") or [])[:4]]
        if not title_pool:
            raise LLMError("LLM returned no titles")
        return {"titles": title_pool, "covers": covers or ["大字报标题 + 核心数字", "前后对比拼图", "人物表情 + 情绪字"], "tags": tags}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"AI 服务暂不可用: {exc}") from exc


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

@app.post("/api/uploads/video")
async def upload_video(file: UploadFile = File(...)) -> dict[str, Any]:
    """接收长视频素材（clip-factory 等素材输入类流水线用），返回 video_id。"""
    import shutil
    import uuid as _uuid
    vid = _uuid.uuid4().hex[:12]
    vdir = REPO_ROOT / "uploads" / vid
    vdir.mkdir(parents=True, exist_ok=True)
    dest = vdir / "source.mp4"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    # ffprobe 元信息
    meta = {"video_id": vid, "path": str(dest), "size_bytes": dest.stat().st_size, "filename": file.filename or "source.mp4"}
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-show_entries", "stream=width,height,codec_type",
             "-of", "json", str(dest)], capture_output=True, text=True, timeout=30).stdout
        import json as _json
        d = _json.loads(out or "{}")
        fmt = d.get("format", {})
        dur = float(fmt.get("duration", 0))
        vstream = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
        meta["duration_seconds"] = round(dur, 1)
        meta["width"] = vstream.get("width")
        meta["height"] = vstream.get("height")
    except Exception as exc:  # noqa: BLE001
        meta["probe_error"] = str(exc)[:100]
    return meta


@app.post("/api/jobs", status_code=201)
def submit_job(body: JobCreate, request: Request) -> dict[str, Any]:
    if body.pipeline_type not in list_pipelines():
        raise HTTPException(
            status_code=422,
            detail=f"Unknown pipeline_type {body.pipeline_type!r}. "
                   f"Available: {sorted(list_pipelines())}",
        )
    # 合并 key：显式传入的 BYOK 优先，否则自动使用账户已保存的 key
    from .db import get_api_keys, get_user_prefs
    merged_keys: dict[str, str] = dict(body.api_keys or {})
    user_prefs: dict[str, Any] | None = None
    job_user_id: int | None = None
    token = request.headers.get("Authorization", "")
    if token.lower().startswith("bearer "):
        try:
            user = authenticate(token[7:].strip())
            job_user_id = user["id"]
            acct = get_api_keys(user["id"])
            for k, v in acct.items():
                merged_keys.setdefault(k, v)
            user_prefs = get_user_prefs(user["id"])   # 账户级模型路由偏好（视频/语音/图片顺序与开关）
        except AuthError:
            pass  # 无效 token 不阻塞提交
    job = create_job(
        store,
        prompt=body.prompt,
        pipeline_type=body.pipeline_type,
        title=body.title,
        style_playbook=body.style_playbook,
        creative_only=body.creative_only,
        auto_approve=body.auto_approve,
        api_keys=merged_keys or None,
        api_prefs=user_prefs,
        checkpoint_policy=get_checkpoint_policy(),
        llm_provider=body.llm_provider,
        source_video=body.source_video,
        user_id=job_user_id,
    )
    run_job_background(store, job)
    logger.info("Job %s submitted (pipeline=%s, creative_only=%s, keys=%d)",
                job.id, job.pipeline_type, job.creative_only, len(merged_keys))
    return job.public_dict()


@app.get("/api/jobs")
def list_jobs(limit: int = 50) -> dict[str, Any]:
    jobs = store.list(limit=limit)
    return {"jobs": [j.public_dict() for j in jobs]}


@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: str) -> dict[str, Any]:
    job = get_job(store, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found")
    return job.public_dict()


@app.get("/api/pipelines")
def pipelines() -> dict[str, Any]:
    return {"pipelines": sorted(list_pipelines())}


@app.get("/api/playbooks")
def playbooks() -> dict[str, Any]:
    """Available style playbooks (visual identity presets)."""
    try:
        from styles.playbook_loader import list_playbooks
        return {"playbooks": sorted(list_playbooks())}
    except Exception as exc:
        logger.warning("playbooks unavailable: %s", exc)
        return {"playbooks": []}


def _server_env_has(key: str) -> bool:
    """Whether the server process has this key configured (env or .env)."""
    return bool(os.environ.get(key))


_COVER_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _project_cover_rel(project_id: str) -> Optional[str]:
    """项目内最适合当封面的图片（相对路径，如 assets/images/scene_1.png）。

    优先级：assets/images 里的 scene 图（按文件名排序取第一张）→ assets/frames →
    项目根/渲染目录里的图片。找不到返回 None。
    """
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.is_dir():
        return None
    # 常见图片位置，按封面代表性排序
    for rel in ("assets/images", "assets/frames", "renders", "exports", "assets", "."):
        base = project_dir if rel == "." else project_dir / rel
        if not base.is_dir():
            continue
        try:
            cands = [f for f in sorted(base.iterdir())
                     if f.is_file() and f.suffix.lower() in _COVER_IMAGE_EXTS]
        except OSError:
            continue
        if cands:
            # scene 图里优先 scene_1 / 数字最小；其余取第一张
            if rel == "assets/images":
                cands.sort(key=lambda f: _scene_index(f.name))
            return cands[0].name if rel == "." else f"{rel}/{cands[0].name}"
    return None


def _scene_index(name: str) -> tuple:
    """从文件名提取场景序号用于排序：scene_2 < scene_10；无序号排最后。"""
    import re
    m = re.search(r"(\d+)", name)
    return (0, int(m.group(1)), name) if m else (1, 0, name)


def _attach_cover(work: dict[str, Any]) -> dict[str, Any]:
    """给作品附加封面相对路径（poster 字段）；无封面或封面缺失时保留原样。"""
    pid = work.get("project_id")
    if pid:
        rel = _project_cover_rel(pid)
        if rel:
            work = dict(work)
            work["poster"] = rel
    return work


@app.get("/api/tools")
def tools() -> dict[str, Any]:
    """Capability-grouped tool menu with per-tool required env keys.

    Cached (TTL 300s) and warmed in the background at startup — the full
    dependency probe is expensive (slow local tools like hyperframes).
    """
    global _tools_cache, _tools_cache_at
    with _tools_lock:
        if _tools_cache and (time.time() - _tools_cache_at) < _TOOLS_CACHE_TTL:
            return _tools_cache
    # Cold start: compute synchronously (first request only).
    data = _build_tools_menu()
    with _tools_lock:
        _tools_cache = data
        _tools_cache_at = time.time()
    return data


def _tool_entry(t: dict[str, Any]) -> dict[str, Any]:
    """Attach required env keys + server config status to a menu entry."""
    keys = TOOL_ENV_KEYS.get(t.get("name"), [])
    return {
        "name": t.get("name"),
        "provider": t.get("provider"),
        "runtime": t.get("runtime"),
        "best_for": t.get("best_for"),
        "env_keys": keys,
        "server_configured": all(_server_env_has(k) for k in keys) if keys else True,
        "missing_keys": [k for k in keys if not _server_env_has(k)],
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "openmontage-online",
        "llm_configured": server_env_has_llm_key(),
        "projects_dir": str(PROJECTS_DIR),
        "db": str(DB_PATH),
        "pipelines": sorted(list_pipelines()),
    }


# --------------------------------------------------------------------------
# Board (reused Backlot UI + state derivation, read-only)
# --------------------------------------------------------------------------

@app.get("/")
def index() -> HTMLResponse:
    html = (OM_UI_DIR / "config.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/prototype", include_in_schema=False)
def prototype_page() -> HTMLResponse:
    """产品原型（同源加载 → API 模式自动生效）。"""
    path = REPO_ROOT / "docs" / "product-prototype.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="prototype not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


@app.get("/marketing", include_in_schema=False)
def marketing_page() -> HTMLResponse:
    """营销落地页（定价/案例/FAQ）。"""
    path = REPO_ROOT / "docs" / "marketing.html"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="marketing not found")
    return HTMLResponse(path.read_text(encoding="utf-8"))


# 营销页案例视频直接读 projects/ 真实产物
if PROJECTS_DIR.is_dir():
    app.mount("/projects", StaticFiles(directory=PROJECTS_DIR), name="projects-media")


@app.get("/config", include_in_schema=False)
def config_page() -> HTMLResponse:
    html = (OM_UI_DIR / "config.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


def _board_state(project_id: Optional[str] = None) -> dict[str, Any]:
    """Derive board state the same way Backlot does (pure functions)."""
    try:
        from backlot.state import list_projects, load_board_state, summarize_project
        if project_id is not None:
            project_dir = PROJECTS_DIR / project_id
            if not project_dir.exists():
                raise HTTPException(status_code=404, detail=f"Project {project_id!r} not found")
            return load_board_state(project_dir)
        projects = list_projects()
        return {"projects": projects}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Board state derivation failed")
        raise HTTPException(status_code=500, detail=f"Board state error: {exc}")


@app.get("/api/projects")
def api_projects() -> dict[str, Any]:
    return _board_state()


@app.get("/api/project/{project_id}/state")
def api_project_state(project_id: str) -> dict[str, Any]:
    return _board_state(project_id)


@app.get("/media/{project_id}/{file_path:path}")
def media(project_id: str, file_path: str) -> FileResponse:
    project_dir = PROJECTS_DIR / project_id
    path = project_dir / file_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="media not found")
    return FileResponse(path)


@app.get("/thumb/{project_id}/{file_path:path}")
def thumb(project_id: str, file_path: str, w: int = 640) -> FileResponse:
    """board.js 的缩略图端点（Backlot UI 使用 /thumb/...?w=N）。直接返回原图。"""
    project_dir = PROJECTS_DIR / project_id
    path = project_dir / file_path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="thumb not found")
    return FileResponse(path)


@app.get("/api/me/works/{work_id}/product")
def api_work_product(work_id: int, user: dict[str, Any] = Depends(_bearer)) -> dict[str, Any]:
    """作品关联产物的信息：renders/ 下的成片、缩略图、SRT 字幕等。"""
    from .db import get_work, WorksError as _WE
    try:
        w = get_work(user["id"], work_id)
    except _WE as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    pid = w.get("project_id")
    if not pid:
        return {"project_id": None, "media": []}
    project_dir = PROJECTS_DIR / pid
    if not project_dir.is_dir():
        return {"project_id": pid, "media": []}
    media: list[dict[str, Any]] = []
    for folder, mime in (("renders", "video"), ("assets/images", "image"), ("assets/video", "video"), ("assets/audio", "audio")):
        for f in sorted((project_dir / folder).glob("*")) if (project_dir / folder).is_dir() else []:
            if f.is_file() and f.suffix.lower() in {".mp4", ".webm", ".png", ".jpg", ".jpeg", ".mp3", ".wav", ".srt"}:
                media.append({
                    "type": mime,
                    "name": f.name,
                    "url": f"/media/{pid}/{folder}/{f.name}",
                    "size_bytes": f.stat().st_size,
                })
    # 字幕单独列出
    srt = project_dir / "assets" / "subtitles.srt"
    if srt.is_file():
        media.append({"type": "subtitle", "name": "subtitles.srt", "url": f"/media/{pid}/assets/subtitles.srt", "size_bytes": srt.stat().st_size})
    return {"project_id": pid, "media": media}


@app.get("/board/", include_in_schema=False)
@app.get("/board", include_in_schema=False)
def board_index() -> HTMLResponse:
    html = (BACKLOT_UI_DIR / "board.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# board.js 从 URL 路径 /p/{project_id} 解析项目 ID（不读 query 参数）
@app.get("/board/p/{project_id}", include_in_schema=False)
@app.get("/board/p/{project_id}/", include_in_schema=False)
def board_project_page(project_id: str) -> HTMLResponse:
    html = (BACKLOT_UI_DIR / "board.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


# board.js 会订阅此 SSE 端点做实时刷新；这里返回占位避免 404 噪音
@app.get("/api/project/{project_id}/events", include_in_schema=False)
def project_events_stub(project_id: str) -> dict[str, Any]:
    return {"ok": True, "stream": False, "note": "SSE stub — polling via /state"}


if BACKLOT_UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=BACKLOT_UI_DIR), name="ui")

if OM_UI_DIR.is_dir():
    app.mount("/omui", StaticFiles(directory=OM_UI_DIR), name="omui")

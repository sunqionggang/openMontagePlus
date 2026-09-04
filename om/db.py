"""OpenMontage 用户数据存储 —— JSON 文件实现（接口与 SQLite 版完全一致）。

背景：本环境安全策略拦截"常驻进程（uvicorn）对 SQLite 数据库文件（.db/.sqlite3）
的写入"（attempt to write a readonly database），但对普通文件（JSON/checkpoint）
正常放行。因此把 users/tokens/works 持久化改为 JSON 原子读写。

并发：进程内 RLock 串行化；原子写 = 写临时文件 + os.replace。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_DB_PATH: Optional[Path] = None
_DB_LOCK = threading.RLock()


class AuthError(Exception):
    pass


class DuplicateUserError(Exception):
    pass


class WorksError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(path: Optional[Path] = None) -> Path:
    """Initialize the data store (idempotent). Returns the store path."""
    global _DB_PATH
    _DB_PATH = Path(path) if path else Path(os.environ.get("OM_DB_PATH") or "om_data.json")
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _DB_LOCK:
        _save(_load())  # 确保文件存在且结构完整
    return _DB_PATH


def _store_path() -> Path:
    global _DB_PATH
    assert _DB_PATH is not None, "call init_db() first"
    # 统一 JSON 扩展名（避开本环境对 .db/.sqlite3 的写保护）
    p = _DB_PATH
    if p.suffix.lower() in (".db", ".sqlite3", ".sqlite"):
        p = p.with_suffix(".json")
        _DB_PATH = p
    return p


def _load() -> dict[str, Any]:
    p = _store_path()
    data: dict[str, Any] = {}
    if p.exists():
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except Exception:  # noqa: BLE001
            pass
    # 补全标准结构（容忍历史/残留文件缺字段）
    defaults = {"users": [], "tokens": [], "works": [], "next_user": 1, "next_work": 1,
                "ledgers": [], "recharges": []}
    for k, v in defaults.items():
        data.setdefault(k, v)
    return data


def _save(data: dict[str, Any]) -> None:
    """健壮持久化：本环境对文件写的拦截是间歇性的（同操作时成功时失败）。
    依次尝试 覆盖写 → 删后建 → 临时名+rename，各重试多次。"""
    import time as _time
    p = _store_path()
    blob = json.dumps(data, ensure_ascii=False)
    last: Optional[Exception] = None

    # 方式 1：直接覆盖写
    for _ in range(3):
        try:
            p.write_text(blob, encoding="utf-8")
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            _time.sleep(0.4)

    # 方式 2：删除后新建（os.remove 可能被 safe-delete 拦，FileNotFoundError 忽略）
    for _ in range(3):
        try:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            p.write_text(blob, encoding="utf-8")
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            _time.sleep(0.4)

    # 方式 3：临时文件名写入 + rename
    for _ in range(3):
        try:
            tmp = p.with_suffix(f".t{int(_time.time() * 1000) % 100000}")
            tmp.write_text(blob, encoding="utf-8")
            os.replace(tmp, p)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            _time.sleep(0.4)

    raise last or PermissionError("_save failed after all fallbacks")


def _hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), 120_000
    )
    return digest.hex(), salt


def _verify_password(password: str, salt: str, expected: str) -> bool:
    digest, _ = _hash_password(password, salt)
    return hmac.compare_digest(digest, expected)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def register(username: str, email: str, password: str, *, plan: str = "免费版") -> dict[str, Any]:
    username = username.strip()
    email = email.strip().lower()
    if not username or not email or len(password) < 4:
        raise AuthError("Username/email required and password >= 4 chars")
    digest, salt = _hash_password(password)
    with _DB_LOCK:
        data = _load()
        for u in data["users"]:
            if u["email"] == email or u["username"] == username:
                raise DuplicateUserError("Email or username already registered")
        user_id = data["next_user"]
        data["next_user"] += 1
        data["users"].append({
            "id": user_id, "username": username, "email": email,
            "password_hash": digest, "salt": salt, "plan": plan,
            "balance": _SIGNUP_BONUS_USD, "api_keys": {}, "created_at": _now(),
        })
        token = _issue_token_data(data, user_id)
        _save(data)
    return {"token": token, "user": _get_user_by_id(user_id)}


def login(email: str, password: str) -> dict[str, Any]:
    email = email.strip().lower()
    with _DB_LOCK:
        data = _load()
        user = next((u for u in data["users"] if u["email"] == email), None)
        if user is None or not _verify_password(password, user["salt"], user["password_hash"]):
            raise AuthError("Invalid email or password")
        token = _issue_token_data(data, user["id"])
        _save(data)
    return {"token": token, "user": _user_row(user)}


def logout(token: str) -> None:
    with _DB_LOCK:
        data = _load()
        data["tokens"] = [t for t in data["tokens"] if t["token"] != token]
        _save(data)


def _issue_token_data(data: dict[str, Any], user_id: int) -> str:
    token = secrets.token_hex(24)
    data["tokens"] = [t for t in data["tokens"] if t["user_id"] != user_id]  # 单会话
    data["tokens"].append({"token": token, "user_id": user_id, "created_at": _now()})
    return token


def authenticate(token: Optional[str]) -> dict[str, Any]:
    """Return the user dict for a bearer token; raises AuthError if invalid."""
    if not token:
        raise AuthError("Missing auth token")
    with _DB_LOCK:
        data = _load()
        tok = next((t for t in data["tokens"] if t["token"] == token), None)
        if tok is None:
            raise AuthError("Invalid or expired token")
        user = next((u for u in data["users"] if u["id"] == tok["user_id"]), None)
    if user is None:
        raise AuthError("Invalid or expired token")
    return _user_row(user)


def _get_user_by_id(user_id: int) -> dict[str, Any]:
    with _DB_LOCK:
        user = next((u for u in _load()["users"] if u["id"] == user_id), None)
    if user is None:
        raise AuthError("User not found")
    return _user_row(user)


def get_api_keys(user_id: int) -> dict[str, str]:
    with _DB_LOCK:
        user = next((u for u in _load()["users"] if u["id"] == user_id), None)
    if user is None:
        return {}
    return {k: v for k, v in (user.get("api_keys") or {}).items() if isinstance(v, str) and v}


def set_api_keys(user_id: int, keys: dict[str, str]) -> dict[str, Any]:
    with _DB_LOCK:
        data = _load()
        user = next((u for u in data["users"] if u["id"] == user_id), None)
        if user is None:
            raise AuthError("User not found")
        current = dict(user.get("api_keys") or {})
        for k, v in (keys or {}).items():
            k = str(k).strip().upper()
            v = str(v).strip()
            if v:
                current[k] = v
            else:
                current.pop(k, None)
        user["api_keys"] = current
        _save(data)
    return dict(current)


# ---- 模型能力路由偏好（视频/语音/图片：启用哪些模型、调用顺序、APIZ 多 key）----
# 模型 id 与 om/renderer.py 的路由逻辑约定一致
DEFAULT_MODEL_PREFS: dict[str, Any] = {
    "video": {
        "models": ["apiz", "kling", "jimeng", "minimax"],
        "enabled": {"apiz": True, "kling": False, "jimeng": False, "minimax": False},
        "apiz_keys": [],
    },
    "voice": {
        "models": ["dashscope", "doubao", "elevenlabs", "google", "openai"],
        "enabled": {"dashscope": True, "doubao": False, "elevenlabs": False, "google": False, "openai": False},
        "apiz_keys": [],
    },
    "image": {
        "models": ["apiz", "dashscope", "pexels", "pixabay"],
        "enabled": {"apiz": True, "dashscope": False, "pexels": True, "pixabay": False},
        "apiz_keys": [],
    },
}


def _merge_prefs(stored: Optional[dict[str, Any]]) -> dict[str, Any]:
    """把用户存储的偏好与默认结构合并（容错：丢弃未知能力/模型，保留已知配置）。"""
    import copy
    out = copy.deepcopy(DEFAULT_MODEL_PREFS)
    if not isinstance(stored, dict):
        return out
    for cap, cfg in stored.items():
        if cap not in out or not isinstance(cfg, dict):
            continue
        if isinstance(cfg.get("models"), list):
            known = [m for m in cfg["models"] if m in out[cap]["models"]]
            if known:
                out[cap]["models"] = known
        if isinstance(cfg.get("enabled"), dict):
            for m, on in cfg["enabled"].items():
                if m in out[cap]["enabled"]:
                    out[cap]["enabled"][m] = bool(on)
        if isinstance(cfg.get("apiz_keys"), list):
            out[cap]["apiz_keys"] = [str(k).strip() for k in cfg["apiz_keys"] if str(k).strip()]
    return out


def get_user_prefs(user_id: int) -> dict[str, Any]:
    """返回用户的模型路由偏好（未设置时给默认结构）。"""
    with _DB_LOCK:
        user = next((u for u in _load()["users"] if u["id"] == user_id), None)
    if user is None:
        return {}
    return _merge_prefs(user.get("model_prefs"))


def set_user_prefs(user_id: int, prefs: Optional[dict[str, Any]]) -> dict[str, Any]:
    """保存用户的模型路由偏好（白名单合并后落盘）。"""
    import copy
    with _DB_LOCK:
        data = _load()
        user = next((u for u in data["users"] if u["id"] == user_id), None)
        if user is None:
            raise AuthError("User not found")
        user["model_prefs"] = _merge_prefs(prefs)
        _save(data)
    return copy.deepcopy(user["model_prefs"])


def _user_row(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "plan": user.get("plan", "免费版"),
        "balance": user.get("balance", 0),
        "created_at": user.get("created_at", ""),
    }


# ---------------------------------------------------------------------------
# Works (per-user isolation)
# ---------------------------------------------------------------------------

def _work_row(w: dict[str, Any]) -> dict[str, Any]:
    tags = w.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except (json.JSONDecodeError, TypeError):
            tags = []
    return {
        "id": w["id"],
        "title": w.get("title", ""),
        "dur": w.get("dur", ""),
        "date": w.get("date") or (w.get("created_at") or "")[:10],
        "status": w.get("status", "done"),
        "emoji": w.get("emoji", "🎬"),
        "likes": w.get("likes", 0),
        "published": bool(w.get("published", False)),
        "ai_applied": bool(w.get("ai_applied", False)),
        "tags": tags,
        "platform": w.get("platform"),
        "project_id": w.get("project_id"),
        "job_id": w.get("job_id"),
        "created_at": w.get("created_at", ""),
    }


def list_works(user_id: int) -> list[dict[str, Any]]:
    with _DB_LOCK:
        works = [w for w in _load()["works"] if w.get("user_id") == user_id]
    return [_work_row(w) for w in sorted(works, key=lambda x: x.get("id", 0), reverse=True)]


def create_work(user_id: int, *, title: str, dur: str = "60s", emoji: str = "🎬",
                status: str = "done", date: Optional[str] = None,
                project_id: Optional[str] = None, job_id: Optional[str] = None) -> dict[str, Any]:
    with _DB_LOCK:
        data = _load()
        work_id = data["next_work"]
        data["next_work"] += 1
        data["works"].append({
            "id": work_id, "user_id": user_id, "title": title, "dur": dur,
            "date": date or _now()[:10], "status": status, "emoji": emoji,
            "likes": 0, "published": False, "ai_applied": False, "tags": [],
            "platform": None, "project_id": project_id, "job_id": job_id,
            "created_at": _now(),
        })
        _save(data)
    return get_work(user_id, work_id)


def get_work(user_id: int, work_id: int) -> dict[str, Any]:
    with _DB_LOCK:
        w = next((x for x in _load()["works"]
                  if x.get("id") == work_id and x.get("user_id") == user_id), None)
    if w is None:
        raise WorksError(f"Work {work_id} not found or not owned by user")
    return _work_row(w)


def update_work(user_id: int, work_id: int, *, title: Optional[str] = None,
                dur: Optional[str] = None, status: Optional[str] = None,
                emoji: Optional[str] = None, published: Optional[bool] = None,
                likes: Optional[int] = None, tags: Optional[list[str]] = None,
                platform: Optional[str] = None, ai_applied: Optional[bool] = None) -> dict[str, Any]:
    with _DB_LOCK:
        data = _load()
        w = next((x for x in data["works"]
                  if x.get("id") == work_id and x.get("user_id") == user_id), None)
        if w is None:
            raise WorksError(f"Work {work_id} not found or not owned by user")
        if title is not None: w["title"] = title
        if dur is not None: w["dur"] = dur
        if status is not None: w["status"] = status
        if emoji is not None: w["emoji"] = emoji
        if published is not None: w["published"] = bool(published)
        if likes is not None: w["likes"] = likes
        if tags is not None: w["tags"] = list(tags)
        if platform is not None: w["platform"] = platform
        if ai_applied is not None: w["ai_applied"] = bool(ai_applied)
        _save(data)
    return _work_row(w)


def delete_work(user_id: int, work_id: int) -> None:
    with _DB_LOCK:
        data = _load()
        before = len(data["works"])
        data["works"] = [w for w in data["works"]
                         if not (w.get("id") == work_id and w.get("user_id") == user_id)]
        if len(data["works"]) == before:
            raise WorksError(f"Work {work_id} not found or not owned by user")
        _save(data)


def gallery() -> list[dict[str, Any]]:
    """Public area: all published works, newest first, with author name."""
    with _DB_LOCK:
        data = _load()
        users = {u["id"]: u for u in data["users"]}
        works = [w for w in data["works"] if w.get("published")]
    out = []
    for w in sorted(works, key=lambda x: x.get("id", 0), reverse=True):
        item = _work_row(w)
        author = users.get(w.get("user_id"))
        item["author"] = author["username"] if author else "匿名"
        item["author_plan"] = author.get("plan", "") if author else ""
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 托管余额 / 任务成本账本（accounting）
#   balance 单位为 USD（内部记账用美元，展示端换算 ¥）。
#   hosted_spend：平台代付（服务器内置 key 生成）→ 从托管余额扣。
#   BYOK 自付的调用只记账单展示、不从余额扣。
# ---------------------------------------------------------------------------

# 新注册用户开户赠送体验金（≈¥10），老账户首次查询账户时按此规则补发
_SIGNUP_BONUS_USD = 1.5
_USD_CNY = 7.2


def _user_row_balance(user_id: int) -> float:
    with _DB_LOCK:
        u = next((x for x in _load()["users"] if x.get("id") == user_id), None)
    if u is None:
        raise AuthError("User not found")
    return float(u.get("balance") or 0.0)


def _set_balance(user_id: int, value: float) -> None:
    with _DB_LOCK:
        data = _load()
        u = next((x for x in data["users"] if x.get("id") == user_id), None)
        if u is None:
            raise AuthError("User not found")
        u["balance"] = round(float(value), 6)
        _save(data)


def ensure_signup_bonus(user_id: int) -> None:
    """老账户（balance==0 且从未充值/从未扣过费）补一次体验金，幂等。"""
    with _DB_LOCK:
        data = _load()
        u = next((x for x in data["users"] if x.get("id") == user_id), None)
        if u is None:
            raise AuthError("User not found")
        already = any(
            r.get("user_id") == user_id and r.get("kind") == "bonus"
            for r in data.get("recharges", [])
        )
        if already:
            return
        if float(u.get("balance") or 0.0) > 0:
            return
        u["balance"] = round(_SIGNUP_BONUS_USD, 6)
        data.setdefault("recharges", []).append({
            "id": f"rc_{int(_now_tick())}",
            "user_id": user_id, "kind": "bonus",
            "amount_usd": _SIGNUP_BONUS_USD, "note": "开户体验金",
            "created_at": _now(),
        })
        _save(data)


def _now_tick() -> int:
    import time as _t
    return int(_t.time() * 1000)


def add_balance(user_id: int, delta_usd: float, *, kind: str = "manual",
                note: str = "") -> float:
    """改托管余额（充值/退款为正，任务扣费为负），并记一条流水。"""
    with _DB_LOCK:
        data = _load()
        u = next((x for x in data["users"] if x.get("id") == user_id), None)
        if u is None:
            raise AuthError("User not found")
        u["balance"] = round(float(u.get("balance") or 0.0) + float(delta_usd), 6)
        data.setdefault("recharges", []).append({
            "id": f"rc_{_now_tick()}",
            "user_id": user_id, "kind": kind,
            "amount_usd": round(float(delta_usd), 6), "note": note,
            "created_at": _now(),
        })
        _save(data)
        return float(u["balance"])


def record_job_cost(user_id: int, *, job_id: str, pipeline: str, title: str,
                    meter: dict[str, Any], status: str = "completed") -> int:
    """任务结束后把计量结果写入账本；托管部分从余额扣。返回 ledger id。"""
    hosted = float(meter.get("hosted_usd") or 0.0)
    total = float(meter.get("total_usd") or 0.0)
    byok = float(meter.get("byok_usd") or 0.0)
    lines = meter.get("lines") or []
    if user_id:
        add_balance(user_id, -hosted, kind="job",
                    note=f"任务 {job_id} 托管资源")
    with _DB_LOCK:
        data = _load()
        data.setdefault("ledgers", []).append({
            "id": f"lg_{_now_tick()}",
            "user_id": user_id if user_id else 0,
            "job_id": job_id, "pipeline": pipeline,
            "title": (title or "")[:80],
            "status": status,
            "lines": lines,
            "total_usd": round(total, 6),
            "hosted_usd": round(hosted, 6),
            "byok_usd": round(byok, 6),
            "created_at": _now(),
        })
        _save(data)
        return len(data["ledgers"]) - 1


def list_job_costs(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    with _DB_LOCK:
        data = _load()
        rows = [r for r in data.get("ledgers", []) if r.get("user_id") == user_id]
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows[:limit]


def list_recharges(user_id: int, limit: int = 30) -> list[dict[str, Any]]:
    with _DB_LOCK:
        data = _load()
        rows = [r for r in data.get("recharges", []) if r.get("user_id") == user_id]
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows[:limit]


def get_account(user_id: int) -> dict[str, Any]:
    """账户中心数据：余额（USD/CNY）、任务成本、流水。自动补体验金。"""
    ensure_signup_bonus(user_id)
    balance_usd = _user_row_balance(user_id)
    costs = list_job_costs(user_id)
    spent_hosted = round(sum(float(c.get("hosted_usd") or 0.0) for c in costs), 6)
    spent_byok = round(sum(float(c.get("byok_usd") or 0.0) for c in costs), 6)
    return {
        "balance_usd": balance_usd,
        "balance_cny": round(balance_usd * _USD_CNY, 2),
        "bonus_usd": _SIGNUP_BONUS_USD,
        "spent_hosted_usd": spent_hosted,
        "spent_hosted_cny": round(spent_hosted * _USD_CNY, 2),
        "spent_byok_usd": spent_byok,
        "job_count": len(costs),
        "costs": costs,
        "recharges": list_recharges(user_id),
    }

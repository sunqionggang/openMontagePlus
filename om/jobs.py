"""Job registry + background runner for the OpenMontage online layer.

A Job wraps a single pipeline run: the user prompt, chosen pipeline, optional
BYOK API keys, and the run result. Jobs run on a background thread so the
FastAPI control plane can return immediately.

BYOK keys are held ONLY in memory (never persisted) and are stripped before a
job is serialized for the API.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .orchestrator import Orchestrator, RunResult

logger = logging.getLogger(__name__)

JOB_STATUS = ("queued", "running", "completed", "blocked", "failed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


@dataclass
class Job:
    id: str
    project_id: str
    pipeline_type: str
    prompt: str
    status: str = "queued"
    user_id: Optional[int] = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    title: Optional[str] = None
    style_playbook: Optional[str] = None
    creative_only: bool = False
    auto_approve: bool = True
    checkpoint_policy: str = "guided"
    llm_provider: Optional[str] = None  # 用户选择，覆盖服务器 config.yaml
    source_video: Optional[str] = None  # 素材输入类流水线：上传视频 video_id（uploads/<id>/source.mp4）
    blocker: Optional[str] = None
    stages: list[dict[str, Any]] = field(default_factory=list)
    # 成本结算（USD）：真实资产生成计量，托管部分已从用户余额扣
    cost_total_usd: float = 0.0
    cost_hosted_usd: float = 0.0
    cost_byok_usd: float = 0.0
    # BYOK keys — memory only, never serialized.
    api_keys: dict[str, str] = field(default_factory=dict, repr=False, compare=False)
    # 账户级模型路由偏好（视频/语音/图片顺序+开关+APIZ多key）—— memory only。
    api_prefs: Optional[dict[str, Any]] = field(default=None, repr=False, compare=False)
    _run_result: Optional[RunResult] = field(default=None, repr=False, compare=False)

    def public_dict(self) -> dict[str, Any]:
        """API-safe representation (no secrets)."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "pipeline_type": self.pipeline_type,
            "prompt": self.prompt,
            "title": self.title,
            "status": self.status,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "style_playbook": self.style_playbook,
            "creative_only": self.creative_only,
            "auto_approve": self.auto_approve,
            "llm_provider": self.llm_provider,
            "blocker": self.blocker,
            "stages": self.stages,
            "cost_total_usd": round(self.cost_total_usd, 6),
            "cost_hosted_usd": round(self.cost_hosted_usd, 6),
            "cost_byok_usd": round(self.cost_byok_usd, 6),
        }


class JobStore:
    """In-process job store with optional JSON persistence (secrets stripped)."""

    def __init__(self, state_dir: Optional[Path] = None):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self.state_dir = Path(state_dir) if state_dir else None
        self._load()

    # -- persistence -----------------------------------------------------

    def _state_file(self) -> Path:
        assert self.state_dir is not None
        return self.state_dir / "jobs.json"

    def _load(self) -> None:
        if not self.state_dir:
            return
        try:
            path = self._state_file()
            if path.exists():
                raw = json.loads(path.read_text(encoding="utf-8"))
                for item in raw.get("jobs", []):
                    job = Job(**{k: v for k, v in item.items() if k in Job.__dataclass_fields__})
                    self._jobs[job.id] = job
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Could not load job state: %s", exc)

    def _persist(self) -> None:
        if not self.state_dir:
            return
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": "1.0",
                "jobs": [j.public_dict() for j in self._jobs.values()],
            }
            tmp = self._state_file().with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._state_file())
        except OSError as exc:
            logger.warning("Could not persist job state: %s", exc)

    # -- CRUD ------------------------------------------------------------

    def put(self, job: Job) -> Job:
        with self._lock:
            self._jobs[job.id] = job
            self._persist()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, limit: int = 50) -> list[Job]:
        with self._lock:
            jobs = sorted(
                self._jobs.values(), key=lambda j: j.created_at, reverse=True
            )
            return jobs[:limit]

    def update(self, job: Job) -> None:
        job.updated_at = _now()
        with self._lock:
            self._jobs[job.id] = job
            self._persist()


# --------------------------------------------------------------------------
# Job creation / execution
# --------------------------------------------------------------------------

def get_job(store: JobStore, job_id: str) -> Optional[Job]:
    """Fetch a job by id (None if not found)."""
    return store.get(job_id)


def create_job(
    store: JobStore,
    *,
    prompt: str,
    pipeline_type: str,
    title: Optional[str] = None,
    style_playbook: Optional[str] = None,
    creative_only: bool = False,
    auto_approve: bool = True,
    api_keys: Optional[dict[str, str]] = None,
    api_prefs: Optional[dict[str, Any]] = None,
    project_id: Optional[str] = None,
    checkpoint_policy: str = "guided",
    llm_provider: Optional[str] = None,
    source_video: Optional[str] = None,
    user_id: Optional[int] = None,
) -> Job:
    job = Job(
        id=f"job_{uuid.uuid4().hex[:12]}",
        project_id=project_id or f"proj_{_utc_stamp()}",
        pipeline_type=pipeline_type,
        prompt=prompt,
        title=title or (prompt[:60] if prompt else pipeline_type),
        style_playbook=style_playbook,
        creative_only=creative_only,
        auto_approve=auto_approve,
        checkpoint_policy=checkpoint_policy,
        llm_provider=llm_provider,
        source_video=source_video,
        user_id=user_id,
        api_keys=api_keys or {},
        api_prefs=api_prefs,
    )
    store.put(job)
    return job


def run_job_background(
    store: JobStore,
    job: Job,
    *,
    orchestrator: Optional[Orchestrator] = None,
) -> threading.Thread:
    """Execute a job on a daemon thread; updates the store as it goes."""

    def _work() -> None:
        job.status = "running"
        job.started_at = _now()
        store.update(job)
        # 全局串行锁：BYOK keys 注入进程级 os.environ，并发任务会互相污染
        # （A 的 key 残留被 B 读到）。自托管 MVP 单 worker 串行足够。
        with _RUN_LOCK:
            import os
            saved_env = _inject_env(job.api_keys)
            # 账户级模型路由偏好随任务注入：renderer 通过 os.environ["OM_MODEL_PREFS"] 读取
            if job.api_prefs:
                saved_env["OM_MODEL_PREFS"] = os.environ.get("OM_MODEL_PREFS")
                os.environ["OM_MODEL_PREFS"] = json.dumps(job.api_prefs, ensure_ascii=False)
            # 任务级真实成本计量：本次任务用户自带 key 的 env 集合 → 判定托管/BYOK
            from .meter import meter_begin, meter_finish
            meter_begin(set((job.api_keys or {}).keys()))
            try:
                _execute(job, orchestrator)
            finally:
                _restore_env(saved_env)
            summary = meter_finish()
        job.finished_at = _now()
        if summary:
            job.cost_total_usd = round(float(summary.get("total_usd") or 0.0), 6)
            job.cost_hosted_usd = round(float(summary.get("hosted_usd") or 0.0), 6)
            job.cost_byok_usd = round(float(summary.get("byok_usd") or 0.0), 6)
            _settle_ledger(job, summary)
        store.update(job)

    thread = threading.Thread(
        target=_work, name=f"om-job-{job.id}", daemon=True
    )
    thread.start()
    return thread


# 全局任务串行锁（见 _work 注释）
_RUN_LOCK = threading.Lock()


def _execute(job: Job, orchestrator: Optional[Orchestrator]) -> None:
    """Run the job body (assumes env already injected; lock held)."""
    try:
        orch = orchestrator or _default_orchestrator(job)
        result: RunResult = orch.run(
            job.project_id,
            job.pipeline_type,
            job.prompt,
            style_playbook=job.style_playbook,
            source_video=job.source_video,
        )
        job._run_result = result
        job.stages = [
            {
                "stage": s.stage,
                "status": s.status,
                "artifact_name": s.artifact_name,
                "message": s.message,
                "error": s.error,
                "checkpoint_path": s.checkpoint_path,
            }
            for s in result.stages
        ]
        if result.ok and result.stages:
            job.status = "completed"
        elif result.blocker:
            job.status = "blocked"
            job.blocker = result.blocker
        else:
            job.status = "failed"
            job.blocker = "Run ended without a blocker but not ok."
    except Exception as exc:  # never let the worker thread die silently
        logger.exception("Job %s failed with unexpected error", job.id)
        job.status = "failed"
        job.blocker = f"Unexpected error: {exc}"


def _settle_ledger(job: Job, summary: dict[str, Any]) -> None:
    """任务结束后写账本：托管消耗从用户余额扣（BYOK 只记账不扣费）。

    结算在锁内（_RUN_LOCK）执行，随后 store.update 落盘成本汇总。
    """
    if not job.user_id:
        return  # 匿名任务不记账
    if job.cost_hosted_usd <= 0 and not summary.get("lines"):
        return
    try:
        from .db import record_job_cost
        record_job_cost(
            job.user_id,
            job_id=job.id,
            pipeline=job.pipeline_type,
            title=job.title or "",
            meter=summary,
            status=job.status,
        )
    except Exception as exc:  # noqa: BLE001 — settle failure must not kill the worker
        logger.warning("Ledger settle failed for %s: %s", job.id, exc)


def _inject_env(api_keys: dict[str, str]) -> dict[str, Optional[str]]:
    """Inject BYOK keys into the process env; return prior values for restore.

    Keys are normalized to upper-case env var names. Values are stripped of
    whitespace; empty values are ignored. Best-effort only.
    """
    import os
    saved: dict[str, Optional[str]] = {}
    for raw_name, value in (api_keys or {}).items():
        name = raw_name.strip().upper()
        if not name or not value:
            continue
        saved[name] = os.environ.get(name)
        os.environ[name] = str(value).strip()
    return saved


def _restore_env(saved: dict[str, Optional[str]]) -> None:
    import os
    for name, prior in (saved or {}).items():
        if prior is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prior


def _default_orchestrator(job: Job) -> Orchestrator:
    """Build an orchestrator from job fields + server config + BYOK keys.

    BYOK keys are expected to already be injected into the process
    environment (see _inject_env) so downstream tools (OpenAI, FAL, etc.)
    pick them up naturally. The LLM client additionally receives the key
    explicitly so it works even for providers that read a custom env var.
    """
    from .config import build_llm_config

    # 用户显式选择 provider 时，覆盖 config.yaml 的 llm.provider，
    # 并从 BYOK keys 里挑对应该 provider 的 key。
    if job.llm_provider:
        provider = job.llm_provider.lower()
        key_env = _LLM_KEY_ENV.get(provider)
        byok_key = job.api_keys.get(key_env) if key_env else None
        base = build_llm_config(byok_key=byok_key)
        base.provider = provider
        if byok_key:
            base.api_key = byok_key
        llm_config = base
    else:
        llm_key = (
            job.api_keys.get("LLM_API_KEY")
            or job.api_keys.get("OPENAI_API_KEY")
            or job.api_keys.get("ANTHROPIC_API_KEY")
            or job.api_keys.get("GEMINI_API_KEY")
        )
        llm_config = build_llm_config(byok_key=llm_key)

    return Orchestrator(
        auto_approve=job.auto_approve,
        llm_config=llm_config,
        checkpoint_policy=job.checkpoint_policy,
        creative_only=job.creative_only,
    )


# provider -> 它读取的 BYOK key 名（用于 llm_provider 覆盖时自动配对）
_LLM_KEY_ENV: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "minimax": "MINIMAX_API_KEY",
}

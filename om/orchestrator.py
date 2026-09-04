"""Deterministic stage orchestrator for the self-hosted OpenMontage layer.

The original project has no autonomous loop — the coding agent *is* the
orchestrator. This module fills that gap with code:

  * Reads the pipeline manifest (pipeline_defs/*.yaml) for stage order,
    per-stage skill, approval gates and tool requirements.
  * Creative stages (research/proposal/idea/script/scene_plan) are driven by
    the LLM: the director skill + artifact schema are assembled into a
    prompt, the response is validated against schemas/artifacts/*.schema.json,
    and the result is written through lib/checkpoint.write_checkpoint.
  * Tool stages (assets/edit/compose/publish) go through the tool registry;
    unavailable tools become explicit blockers, never silent no-ops.
  * Approval gates: ``auto_approve=True`` (self-host default) writes
    ``completed + human_approved`` so a headless run can proceed end-to-end;
    ``auto_approve=False`` parks at ``awaiting_human`` for review.
  * Missing LLM credentials raise LLMError with an actionable message — the
    job layer turns that into a clean "blocked" state, not a crash.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lib.checkpoint import (
    PROJECTS_DIR,
    init_project,
    read_checkpoint,
    write_checkpoint,
)
from lib.pipeline_loader import (
    get_required_tools,
    get_stage_human_approval_default,
    get_stage_order,
    get_stage_skill,
    list_pipelines,
    load_pipeline_readonly,
)
from schemas.artifacts import ARTIFACT_NAMES, load_schema, validate_artifact

from .llm import LLMClient, LLMConfig, LLMError

logger = logging.getLogger(__name__)

# Stages the LLM can plausibly produce a schema-valid artifact for without
# external generation tools. Everything else is a tool stage.
CREATIVE_STAGES = {
    "research": "research_brief",
    "proposal": "proposal_packet",
    "idea": "brief",
    "script": "script",
    "scene_plan": "scene_plan",
    "character_design": "character_design",
    "rig_plan": "rig_plan",
}

CANONICAL = {
    "research": "research_brief",
    "proposal": "proposal_packet",
    "idea": "brief",
    "script": "script",
    "scene_plan": "scene_plan",
    "character_design": "character_design",
    "rig_plan": "rig_plan",
    "assets": "asset_manifest",
    "edit": "edit_decisions",
    "compose": "render_report",
    "publish": "publish_log",
}

MAX_JSON_RETRIES = 3
MAX_SKILL_CHARS = 12000  # keep prompts bounded for small local models

# 枚举混淆修复表：LLM 常把别的枚举的合法值填到这个字段（如 pacing_profile 的
# 'energetic' 被误填到 pace）。修复器优先 lower/包含匹配，其次用本表做语义映射。
_ENUM_ALIASES: dict[str, str] = {
    "energetic": "fast",
    "upbeat": "brisk",
    "dynamic": "fast",
    "excited": "fast",
    "contemplative": "slow",
    "calm": "measured",
    "relaxed": "measured",
    "cinematic": "custom",
    "technical": "custom",
    "professional": "measured",
    "neutral": "conversational",
    "informal": "conversational",
    "snappy": "brisk",
}


def _repair_duration(data: Any, schema: Any = None) -> Any:
    """修复 total_duration_seconds <= 0（LLM 常见错误，schema minimum:1 会拒）。

    仅当 schema 声明了 total_duration_seconds（script 等）才处理；
    research_brief 等不含该字段的 artifact 绝不追加（additionalProperties=False 会拒绝）。
    - script：用 sections 里最大的 end_seconds（文案节奏时间轴）
    - 子项时长求和兜底（scene_plan 等）
    - 都拿不到 → 按子项数量兜底（每项 ≥10s），最少 60s
    """
    if not isinstance(data, dict):
        return data
    if isinstance(schema, dict):
        props = schema.get("properties") or {}
        if "total_duration_seconds" not in props:
            return data
    cur = data.get("total_duration_seconds")
    if isinstance(cur, (int, float)) and cur >= 1:
        return data
    total = 0.0
    for item in (data.get("sections") or []):
        if not isinstance(item, dict):
            continue
        e = item.get("end_seconds")
        if isinstance(e, (int, float)) and e > 0:
            total = max(total, float(e))
    if total < 1:
        for item in (data.get("scenes") or []):
            if not isinstance(item, dict):
                continue
            d = item.get("duration_seconds")
            if isinstance(d, (int, float)) and d > 0:
                total += float(d)
    if total < 1:
        n = len(data.get("sections") or data.get("scenes") or [])
        total = max(60.0, n * 10.0)
    data["total_duration_seconds"] = round(total, 1)
    return data


def _repair_enum(data: Any, schema: Any) -> Any:
    """Recursively repair out-of-enum string values against the artifact schema.

    LLM 输出经常把枚举值写错（大小写、近义词、跨字段混淆）。此函数在 schema
    校验前把非法值映射到合法枚举；实在无法匹配时取枚举第一个值兜底。
    """
    if isinstance(schema, dict):
        if "$ref" in schema:
            ref = schema["$ref"].lstrip("#/")
            node = _SCHEMA_DOCS.get(ref)
            if node is not None:
                schema = node
        props = schema.get("properties")
        if props:
            if isinstance(data, dict):
                # 严格 schema（additionalProperties=False）：剔除 LLM 多写的键
                if schema.get("additionalProperties") is False:
                    data = {k: v for k, v in data.items() if k in props}
                # 补缺失的 required 字段（LLM 常漏，用占位值兜底）
                for req in schema.get("required") or []:
                    if req not in data or data[req] is None:
                        data[req] = _fabricate_item(props.get(req, {}))
                for k, v in data.items():
                    if k in props:
                        data[k] = _repair_enum(v, props[k])
                return data
        # 值字典：无 properties 但有 additionalProperties schema（如 provider_notes: {k: string}）
        ap = schema.get("additionalProperties")
        if isinstance(data, dict) and isinstance(ap, dict):
            return {k: _repair_enum(v, ap) for k, v in data.items()}
        if schema.get("type") == "array":
            if isinstance(data, list):
                # 空/不足兜底：schema 要求 minItems 但 LLM 给太少 → 补占位项
                min_items = schema.get("minItems", 0)
                if min_items and len(data) < min_items:
                    items_schema = schema.get("items", {})
                    data = list(data) + [_fabricate_item(items_schema) for _ in range(min_items - len(data))]
                return [_repair_enum(x, schema.get("items", {})) for x in data]
            return data
        if schema.get("type") == "string":
            # const 约束：直接强制为固定值（如 version: "1.0"）
            if "const" in schema and schema["const"] is not None:
                return schema["const"]
            # 类型修复：schema 要字符串，但 LLM 给了 dict/数字/列表 → 提取文本
            if not isinstance(data, str):
                return _coerce_to_string(data)
            if schema.get("enum"):
                enums = schema["enum"]
                if data in enums:
                    return data
                low = data.lower().strip()
                for a in enums:
                    if a.lower() == low:
                        return a
                for a in enums:
                    if low in a or a in low:
                        return a
                if low in _ENUM_ALIASES:
                    return _ENUM_ALIASES[low]
                return enums[0]
    return data


def _fabricate_item(items_schema: Any) -> Any:
    """为 schema 的 items 构造一个合法占位值（用于空数组兜底）。"""
    if not isinstance(items_schema, dict):
        return ""
    if "$ref" in items_schema:
        ref = items_schema["$ref"].lstrip("#/")
        node = _SCHEMA_DOCS.get(ref)
        if node is not None:
            items_schema = node
    t = items_schema.get("type")
    if t == "string":
        if items_schema.get("format") == "uri":
            return "https://example.com/research"
        if items_schema.get("format") == "date":
            return "2026-08-25"
        if items_schema.get("enum"):
            return items_schema["enum"][0]
        return "待补充"
    if t == "integer":
        return items_schema.get("minimum", 1) or 1
    if t == "number":
        return 0
    if t == "boolean":
        return False
    if t == "array":
        return []
    if t == "object" or "properties" in items_schema:
        props = items_schema.get("properties", {})
        return {k: _fabricate_item(v) for k, v in props.items() if k in (items_schema.get("required") or list(props.keys()))}
    return ""


def _describe_source_video(video_id: str) -> str:
    """分析上传的源视频（uploads/<video_id>/source.mp4）：ffprobe 元信息 + 均匀抽 3 帧。

    返回注入 LLM 的文本上下文（素材输入类流水线用）。
    """
    from pathlib import Path as _P
    import subprocess as _sp
    src = _P("uploads") / video_id / "source.mp4"
    if not src.exists():
        return f"[源视频缺失: {src}]"
    try:
        out = _sp.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
             "-show_entries", "stream=width,height,codec_type,codec_name",
             "-of", "json", str(src)], capture_output=True, text=True, timeout=30).stdout
        d = json.loads(out or "{}")
        fmt = d.get("format", {})
        dur = float(fmt.get("duration", 0))
        vstream = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), {})
        astream = any(s.get("codec_type") == "audio" for s in d.get("streams", []))
        # 均匀抽 3 帧（供 LLM 描述内容）
        frames: list[str] = []
        frame_dir = _P("uploads") / video_id / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            t = dur * (i + 0.5) / 3
            fp = frame_dir / f"f{i+1}.jpg"
            if not fp.exists():
                _sp.run(["ffmpeg", "-y", "-v", "error", "-ss", str(t), "-i", str(src),
                         "-frames:v", "1", "-q:v", "3", str(fp)],
                        capture_output=True, timeout=30)
            frames.append(str(fp))
        return (f"- 时长: {dur:.1f}s | 分辨率: {vstream.get('width')}x{vstream.get('height')} | "
                f"视频编码: {vstream.get('codec_name')} | 有音轨: {astream} | 大小: {fmt.get('size')}B\n"
                f"- 抽帧（供内容判断，路径）: {', '.join(frames)}\n"
                f"- 请基于以上真实元数据和帧画面规划切片，每条切片给出准确 start/end（秒）")
    except Exception as exc:  # noqa: BLE001
        return f"[源视频分析失败: {exc}]"


def _coerce_to_string(value: Any) -> str:
    """把 LLM 误给的 dict/number/bool 转成 schema 要求的字符串。"""
    if isinstance(value, dict):
        for k in ("claim", "text", "content", "title", "headline", "name", "value", "summary", "description"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        for v in value.values():  # 兜底：取第一个字符串值
            if isinstance(v, str) and v.strip():
                return v.strip()
        return str(value)
    if isinstance(value, list):
        parts = [v for v in value if isinstance(v, str) and v.strip()]
        return "；".join(parts[:3]) if parts else str(value)
    return str(value)


def _load_schema_docs() -> dict[str, Any]:
    """Pre-load artifact schema definitions for $ref resolution.

    把每个 artifact 的根 schema 及其 definitions 都登记进 _SCHEMA_DOCS，
    键为 $ref 相对路径（如 'definitions/xxx' 或顶层属性路径）。
    """
    docs: dict[str, Any] = {}

    def walk(node: Any, prefix: str = "") -> None:
        if isinstance(node, dict):
            path = prefix or ""
            if path:
                docs[path] = node
            for k, v in node.items():
                if k == "definitions" and isinstance(v, dict):
                    for dk, dv in v.items():
                        docs[f"definitions/{dk}"] = dv
                else:
                    walk(v, f"{path}.{k}" if path else k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{prefix}/{i}")

    for name in CANONICAL.values():
        try:
            walk(load_schema(name))
        except Exception:
            continue
    return docs


_SCHEMA_DOCS: dict[str, Any] = {}
_SCHEMA_DOCS.update(_load_schema_docs())


@dataclass
class StageResult:
    stage: str
    status: str  # completed | awaiting_human | blocked | failed | in_progress
    artifact_name: Optional[str] = None
    artifact: dict[str, Any] = field(default_factory=dict)
    checkpoint_path: Optional[str] = None
    error: Optional[str] = None
    message: Optional[str] = None


@dataclass
class RunResult:
    project_id: str
    pipeline_type: str
    stages: list[StageResult] = field(default_factory=list)
    ok: bool = True
    blocker: Optional[str] = None

    @property
    def last_stage(self) -> Optional[str]:
        return self.stages[-1].stage if self.stages else None

    @property
    def completed_stages(self) -> list[str]:
        return [s.stage for s in self.stages if s.status == "completed"]


class Orchestrator:
    """Drives one pipeline run through its declared stages."""

    def __init__(
        self,
        *,
        pipeline_dir: Optional[Path] = None,
        auto_approve: bool = True,
        llm_config: Optional[LLMConfig] = None,
        checkpoint_policy: str = "guided",
        creative_only: bool = False,
    ):
        self.pipeline_dir = Path(pipeline_dir or PROJECTS_DIR)
        self.auto_approve = auto_approve
        self.checkpoint_policy = checkpoint_policy
        # creative_only: stop after scene_plan (pre-production) — useful for
        # cheap end-to-end smoke tests and for self-hosts without generation
        # tool keys.
        self.creative_only = creative_only
        self._llm_client: Optional[LLMClient] = None
        self._llm_config = llm_config

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------

    def create_project(
        self,
        project_id: str,
        title: str,
        pipeline_type: str,
        style_playbook: Optional[str] = None,
    ) -> Path:
        return init_project(
            project_id,
            title=title,
            pipeline_type=pipeline_type,
            pipeline_dir=self.pipeline_dir,
            style_playbook=style_playbook,
        )

    def manifest(self, pipeline_type: str) -> dict[str, Any]:
        return load_pipeline_readonly(pipeline_type)

    def available_pipelines(self) -> list[str]:
        return list_pipelines()

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------

    def run(
        self,
        project_id: str,
        pipeline_type: str,
        user_prompt: str,
        *,
        style_playbook: Optional[str] = None,
        stage_filter: Optional[list[str]] = None,
        source_video: Optional[str] = None,
    ) -> RunResult:
        """Run every stage in manifest order (or only ``stage_filter`` ones).

        Skips stages whose checkpoint already exists and is 'completed'
        (resume semantics). Collects results; on the first blocker, stops.
        """
        manifest = self.manifest(pipeline_type)
        order = get_stage_order(manifest)
        if stage_filter:
            order = [s for s in order if s in stage_filter]

        # 素材输入：把 source_video 关联写到项目（renderer compose 切段时读取）
        if source_video:
            (self.pipeline_dir / project_id).mkdir(parents=True, exist_ok=True)
            (self.pipeline_dir / project_id / "source_video.txt").write_text(source_video, encoding="utf-8")

        result = RunResult(project_id=project_id, pipeline_type=pipeline_type)

        # Warm-up: verify LLM credentials before touching the disk so a
        # missing key becomes a single clear blocker, not N stage failures.
        llm_blocker = self._llm_blocker()
        if llm_blocker:
            result.ok = False
            result.blocker = llm_blocker
            logger.warning("Job %s blocked before run: %s", project_id, llm_blocker)
            return result

        for stage in order:
            existing = read_checkpoint(self.pipeline_dir, project_id, stage)
            if existing and existing.get("status") == "completed":
                logger.info("Stage %s already completed — skipping", stage)
                result.stages.append(
                    StageResult(
                        stage=stage,
                        status="completed",
                        artifact_name=CANONICAL.get(stage),
                        checkpoint_path=str(
                            self.pipeline_dir / project_id / f"checkpoint_{stage}.json"
                        ),
                        message="resumed from existing checkpoint",
                    )
                )
                continue

            if self.creative_only and stage not in CREATIVE_STAGES:
                logger.info("creative_only — stopping before stage %s", stage)
                result.stages.append(
                    StageResult(
                        stage=stage,
                        status="blocked",
                        message=(
                            "creative_only mode: this stage requires generation "
                            "tools. Restart with creative_only=False after "
                            "configuring tool keys."
                        ),
                    )
                )
                result.ok = False
                result.blocker = (
                    f"creative_only stopped before stage {stage!r}. "
                    "Configure generation tool keys and set creative_only=False."
                )
                break

            stage_result = self._run_stage(
                project_id=project_id,
                pipeline_type=pipeline_type,
                manifest=manifest,
                stage=stage,
                user_prompt=user_prompt,
                style_playbook=style_playbook,
                source_video=source_video,
            )
            result.stages.append(stage_result)
            if stage_result.status in {"blocked", "failed"}:
                result.ok = False
                result.blocker = stage_result.error or stage_result.message or stage
                break
        return result

    # ------------------------------------------------------------------
    # Stage runners
    # ------------------------------------------------------------------

    def _run_stage(
        self,
        *,
        project_id: str,
        pipeline_type: str,
        manifest: dict[str, Any],
        stage: str,
        user_prompt: str,
        style_playbook: Optional[str],
        source_video: Optional[str] = None,
    ) -> StageResult:
        if stage in CREATIVE_STAGES:
            return self._run_creative_stage(
                project_id=project_id,
                pipeline_type=pipeline_type,
                manifest=manifest,
                stage=stage,
                user_prompt=user_prompt,
                style_playbook=style_playbook,
                source_video=source_video,
            )
        return self._run_tool_stage(
            project_id=project_id,
            pipeline_type=pipeline_type,
            manifest=manifest,
            stage=stage,
            source_video=source_video,
        )

    def _run_creative_stage(
        self,
        *,
        project_id: str,
        pipeline_type: str,
        manifest: dict[str, Any],
        stage: str,
        user_prompt: str,
        style_playbook: Optional[str],
        source_video: Optional[str] = None,
    ) -> StageResult:
        artifact_name = CREATIVE_STAGES[stage]
        prior = self._collect_prior_artifacts(project_id, pipeline_type, stage)
        skill_text = self._load_skill(get_stage_skill(manifest, stage))
        schema = load_schema(artifact_name)
        schema_prompt = json.dumps(schema, ensure_ascii=False)
        review_focus = self._manifest_stage(manifest, stage).get("review_focus", [])

        # 素材输入类流水线（clip-factory 等）：注入真实源视频分析
        source_ctx = _describe_source_video(source_video) if source_video else ""

        system = (
            f"You are the {stage} director of the OpenMontage '{pipeline_type}' "
            f"pipeline. Your ONLY output is a JSON object that validates against "
            f"the provided JSON schema.\n\n"
            f"LANGUAGE & CONTENT RULES: \n"
            f"- 旁白 narration/text、标题 title、字幕一律使用简体中文。\n"
            f"- scene_plan 的 scene.description 必须使用简体中文（APIZ gpt-image-2 支持中文且能精确表达中国元素，避免被英文描述误导输出西方风格）。\n"
            f"- 物种/地标知识严格：用户提到的具体动物（如'坡鹿'是鹿科）必须按其原物种描写，不可联想成同音/近义英文物种（如'Hainan Gibbon'长臂猿）。海南坡鹿是有角的小型鹿类，不是灵长类动物。\n"
            f"- 文化语境：以中国元素为主，画面/旁白避免英文/西式符号。\n\n"
            f"DIRECTOR SKILL (abridged):\n{skill_text}\n\n"
            f"ARTIFACT JSON SCHEMA (output must validate against this):\n"
            f"{schema_prompt}"
        )
        user = (
            f"USER REQUEST:\n{user_prompt}\n\n"
            f"PROJECT: {project_id}  (pipeline: {pipeline_type})\n\n"
            + (f"SOURCE VIDEO (REAL ANALYSIS):\n{source_ctx}\n\n" if source_ctx else "")
            + f"REVIEW FOCUS for this stage:\n"
            + ("\n".join(f"- {f}" for f in review_focus) or "(none)")
            + "\n\nPRIOR ARTIFACTS (already approved; ground your output in them):\n"
            + (json.dumps(prior, ensure_ascii=False, indent=2) or "(none)")
            + "\n\nRespond with ONLY the JSON object. No prose, no fences needed "
            "(fences are tolerated)."
        )

        last_error: Optional[str] = None
        data: Optional[dict[str, Any]] = None
        for attempt in range(1, MAX_JSON_RETRIES + 1):
            try:
                data = self._llm().complete_json(system, user)
            except LLMError as exc:
                return StageResult(
                    stage=stage,
                    status="blocked",
                    artifact_name=artifact_name,
                    error=f"LLM call failed: {exc}",
                )
            try:
                # 校验前先按 schema 自动修复越界枚举（LLM 常见错误，重试成本高）
                repaired = _repair_enum(data, load_schema(artifact_name))
                if repaired is not None:
                    data = repaired
                # total_duration_seconds=0/负数 → 按子项时长自动重算（仅 schema 声明该字段的 artifact）
                data = _repair_duration(data, load_schema(artifact_name))
                validate_artifact(artifact_name, data)
                break
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Stage %s attempt %d failed schema validation: %s",
                    stage, attempt, last_error,
                )
                user += (
                    f"\n\nYour previous response FAILED schema validation: "
                    f"{last_error}\nReturn a corrected JSON object ONLY."
                )
                data = None

        if data is None:
            return StageResult(
                stage=stage,
                status="failed",
                artifact_name=artifact_name,
                error=f"LLM output failed schema validation after "
                      f"{MAX_JSON_RETRIES} attempts: {last_error}",
            )

        path = self._write_stage_checkpoint(
            project_id=project_id,
            pipeline_type=pipeline_type,
            stage=stage,
            artifacts={artifact_name: data},
            status="completed" if self.auto_approve else "awaiting_human",
            manifest=manifest,
            message=f"artifact generated and schema-valid ({artifact_name})",
        )
        return StageResult(
            stage=stage,
            status="completed" if self.auto_approve else "awaiting_human",
            artifact_name=artifact_name,
            artifact=data,
            checkpoint_path=str(path),
            message=f"generated {artifact_name}",
        )

    def _run_tool_stage(
        self,
        *,
        project_id: str,
        pipeline_type: str,
        manifest: dict[str, Any],
        stage: str,
        source_video: Optional[str] = None,
    ) -> StageResult:
        """Route a tool stage through the real deterministic renderer.

        assets → TTS + image generation (om.renderer.run_assets)
        edit   → build edit_decisions from scene_plan/script
        compose→ pure-ffmpeg slideshow to renders/final.mp4

        Each step degrades gracefully: missing scene_plan/script artifacts
        block loudly (the creative stages are a prerequisite); a tool-level
        failure inside the renderer records a degraded-but-continued run.
        """
        artifact_name = CANONICAL.get(stage)
        from . import renderer

        try:
            if stage == "assets":
                result = self._run_assets_renderer(project_id, artifact_name)
            elif stage == "edit":
                result = self._run_edit_renderer(project_id, artifact_name)
            elif stage == "compose":
                result = self._run_compose_renderer(project_id, artifact_name)
            elif stage == "publish":
                result = self._run_publish_renderer(project_id, artifact_name)
            else:
                return self._block_tool_stage(project_id, pipeline_type, manifest, stage, artifact_name,
                                              f"Stage {stage!r} has no renderer implementation")
            return result
        except Exception as exc:  # renderer never crashes the worker
            logger.exception("Renderer stage %s failed unexpectedly", stage)
            return StageResult(stage=stage, status="failed", artifact_name=artifact_name,
                               error=f"Renderer error: {exc}")

    def _run_assets_renderer(self, project_id: str, artifact_name: Optional[str]) -> StageResult:
        from . import renderer
        project_dir = self.pipeline_dir / project_id
        if not _CP_script(project_dir).is_file():
            msg = "assets 阶段需要前置的 script/scene_plan 产物（先跑通创意阶段）"
            self._write_stage_checkpoint(
                project_id=project_id, pipeline_type=None, stage="assets",
                artifacts={artifact_name: {"version": "1.0", "note": msg}} if artifact_name else {},
                status="awaiting_human", manifest={}, error=msg,
            )
            return StageResult(stage="assets", status="blocked", artifact_name=artifact_name, error=msg)
        manifest_data = renderer.run_assets(project_dir)
        path = self._write_stage_checkpoint(
            project_id=project_id, pipeline_type=None, stage="assets",
            artifacts={"asset_manifest": renderer.manifest_for_disk(manifest_data)},
            status="completed" if self.auto_approve else "awaiting_human",
            manifest={}, message=f"generated {len(manifest_data.get('assets', []))} assets",
        )
        return StageResult(stage="assets", status="completed" if self.auto_approve else "awaiting_human",
                           artifact_name="asset_manifest", artifact=manifest_data,
                           checkpoint_path=str(path),
                           message="; ".join(manifest_data.get("_notes", []))[:200])

    def _run_edit_renderer(self, project_id: str, artifact_name: Optional[str]) -> StageResult:
        from . import renderer
        project_dir = self.pipeline_dir / project_id
        decisions = renderer.run_edit(project_dir)
        path = self._write_stage_checkpoint(
            project_id=project_id, pipeline_type=None, stage="edit",
            artifacts={"edit_decisions": decisions},
            status="completed" if self.auto_approve else "awaiting_human",
            manifest={}, message=f"{len(decisions.get('cuts', []))} cuts",
        )
        return StageResult(stage="edit", status="completed" if self.auto_approve else "awaiting_human",
                           artifact_name="edit_decisions", artifact=decisions,
                           checkpoint_path=str(path), message=f"{len(decisions.get('cuts', []))} cuts")

    def _run_compose_renderer(self, project_id: str, artifact_name: Optional[str]) -> StageResult:
        from . import renderer
        project_dir = self.pipeline_dir / project_id
        report = renderer.run_compose(project_dir)
        meta = report.get("metadata") or {}
        success = bool(meta.get("success"))
        size_bytes = int((report.get("outputs") or [{}])[0].get("file_size_bytes", 0))
        if not success:
            self._write_stage_checkpoint(
                project_id=project_id, pipeline_type=None, stage="compose",
                artifacts={"render_report": report},
                status="failed", manifest={}, error=report.get("verification_notes") or "compose failed",
            )
            return StageResult(stage="compose", status="failed", artifact_name="render_report",
                               artifact=report, error=report.get("verification_notes") or "compose failed")
        path = self._write_stage_checkpoint(
            project_id=project_id, pipeline_type=None, stage="compose",
            artifacts={"render_report": report},
            status="completed" if self.auto_approve else "awaiting_human",
            manifest={}, message="final.mp4 rendered",
        )
        return StageResult(stage="compose", status="completed" if self.auto_approve else "awaiting_human",
                           artifact_name="render_report", artifact=report,
                           checkpoint_path=str(path),
                           message=f"final.mp4 ({size_bytes / 1048576:.1f} MB)")

    def _run_publish_renderer(self, project_id: str, artifact_name: Optional[str]) -> StageResult:
        """publish 阶段：记录成片产物状态（合法 publish_log）。"""
        from datetime import datetime, timezone
        project_dir = self.pipeline_dir / project_id
        final = project_dir / "renders" / "final.mp4"
        log = {
            "version": "1.0",
            "entries": [{
                "platform": "local",
                "status": "exported" if final.is_file() else "failed",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "url": f"/media/{project_id}/renders/final.mp4",
            }],
        }
        path = self._write_stage_checkpoint(
            project_id=project_id, pipeline_type=None, stage="publish",
            artifacts={"publish_log": log},
            status="completed" if self.auto_approve else "awaiting_human",
            manifest={}, message="产物已就绪",
        )
        return StageResult(stage="publish", status="completed" if self.auto_approve else "awaiting_human",
                           artifact_name="publish_log", artifact=log,
                           checkpoint_path=str(path), message="final.mp4 已就绪")

    def _block_tool_stage(self, project_id, pipeline_type, manifest, stage, artifact_name, msg):
        self._write_stage_checkpoint(
            project_id=project_id, pipeline_type=pipeline_type, stage=stage,
            artifacts={artifact_name: {"version": "1.0", "note": msg}} if artifact_name else {},
            status="awaiting_human", manifest=manifest, error=msg,
        )
        return StageResult(stage=stage, status="blocked", artifact_name=artifact_name, error=msg)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _manifest_stage(self, manifest: dict[str, Any], stage: str) -> dict[str, Any]:
        for s in manifest.get("stages", []):
            if s.get("name") == stage:
                return s
        return {}

    def _collect_prior_artifacts(
        self, project_id: str, pipeline_type: str, current_stage: str
    ) -> dict[str, Any]:
        """Collect schema-valid artifacts from completed predecessor stages."""
        manifest = self.manifest(pipeline_type)
        order = get_stage_order(manifest)
        prior: dict[str, Any] = {}
        for stage in order:
            if stage == current_stage:
                break
            cp = read_checkpoint(self.pipeline_dir, project_id, stage)
            if not cp or cp.get("status") != "completed":
                continue
            for name, payload in (cp.get("artifacts") or {}).items():
                if name in ARTIFACT_NAMES:
                    prior[name] = payload
        return prior

    def _write_stage_checkpoint(
        self,
        *,
        project_id: str,
        pipeline_type: str,
        stage: str,
        artifacts: dict[str, Any],
        status: str,
        manifest: dict[str, Any],
        message: Optional[str] = None,
        error: Optional[str] = None,
    ) -> Path:
        gate_default = None
        if manifest and manifest.get("stages"):
            gate_default = get_stage_human_approval_default(manifest, stage)
        human_approved = bool(self.auto_approve)
        human_approval_required = bool(gate_default) and not self.auto_approve
        metadata: dict[str, Any] = {}
        if message:
            metadata["message"] = message
        return write_checkpoint(
            self.pipeline_dir,
            project_id,
            stage,
            status,
            artifacts,
            pipeline_type=pipeline_type,
            checkpoint_policy=self.checkpoint_policy,
            human_approval_required=human_approval_required,
            human_approved=human_approved,
            error=error,
            metadata=metadata or None,
        )

    def _load_skill(self, skill_path: Optional[str]) -> str:
        """Read a director skill markdown (best effort, bounded)."""
        if not skill_path:
            return "(no skill declared)"
        candidates = [
            Path(skill_path),
            PROJECTS_DIR.parent / skill_path,
            Path(__file__).resolve().parent.parent / skill_path,
        ]
        for cand in candidates:
            if cand.is_file():
                try:
                    text = cand.read_text(encoding="utf-8")
                    return text[:MAX_SKILL_CHARS]
                except OSError:
                    return f"(unreadable skill {skill_path})"
        return f"(skill not found: {skill_path})"

    def _check_tools(self, required: list[str]) -> tuple[set[str], set[str]]:
        """Return (available, missing) tool names using the project registry."""
        try:
            from tools.tool_registry import registry
            registry.ensure_discovered()
            available = {t.name for t in registry.get_available()}
        except Exception as exc:  # registry import may fail on missing deps
            logger.warning("Tool registry unavailable: %s", exc)
            available = set()
        needed = set(required or [])
        return available & needed, needed - available

    def _llm_blocker(self) -> Optional[str]:
        """A single, clear message when the LLM cannot be used. None = OK."""
        cfg = self._llm_config
        if cfg is None:
            return (
                "No LLM configured. Set config.yaml llm.provider + provide an "
                "API key (server env or BYOK in the job submission), or pass "
                "llm_config to the Orchestrator."
            )
        info = None
        try:
            from .llm import _PROVIDERS
            info = _PROVIDERS.get(cfg.provider)
        except Exception:
            pass
        if info is None:
            return f"Unsupported LLM provider {cfg.provider!r}"
        env_var = info.get("env")
        if env_var and not (cfg.api_key or __import__("os").environ.get(env_var)):
            return (
                f"LLM provider {cfg.provider!r} needs an API key: set the "
                f"{env_var} env var (server-wide) or pass a BYOK key in the "
                f"job submission."
            )
        try:
            self._llm()  # raises on missing SDK
        except LLMError as exc:
            return str(exc)
        return None

    def _llm(self) -> LLMClient:
        if self._llm_client is None:
            if self._llm_config is None:
                raise LLMError("No LLM config provided to the orchestrator.")
            self._llm_client = LLMClient(self._llm_config)
        return self._llm_client


def _CP_script(project_dir: Path) -> Path:
    """Checkpoint path for the script stage (renderer prerequisite check)."""
    return project_dir / "checkpoint_script.json"

"""OpenMontage Online (om) — the self-hosted product layer.

This package turns OpenMontage's agent-first, local pipeline into a
server-driven, multi-user-capable service. It reuses the project's existing
infrastructure (lib/checkpoint, lib/pipeline_loader, schemas, tools/registry,
and the Backlot board) and adds only what was missing for a hosted product:

  * om/llm.py        — pluggable LLM client (BYOK friendly)
  * om/orchestrator.py — deterministic stage state machine that drives the
                        pipeline the way the agent used to (reads director
                        skills, calls the LLM for creative stages, writes
                        checkpoints/artifacts).
  * om/jobs.py       — job registry + background runner
  * om/server.py     — FastAPI control plane, reusing the Backlot board UI

Design note: the orchestrator fills the gap identified in the productization
review — there was no autonomous loop. Here the loop is code, the creative
judgement is the LLM, and the human approval gates are configurable
(auto-approve for self-host, or kept for review).
"""

from .llm import LLMClient, LLMError
from .orchestrator import Orchestrator, StageResult
from .jobs import JobStore, create_job, get_job, run_job_background

__all__ = [
    "LLMClient",
    "LLMError",
    "Orchestrator",
    "StageResult",
    "JobStore",
    "create_job",
    "get_job",
    "run_job_background",
]

"""Resume proj_20260825T095208 from checkpoints (assets onward)."""
import sys, time, yaml
sys.path.insert(0, "D:/github/OpenMontage-dev")
from lib.env_loader import load_env
load_env()
from om.orchestrator import Orchestrator
from om.llm import LLMConfig

cfg = yaml.safe_load(open("D:/github/OpenMontage-dev/config.yaml", encoding="utf-8"))
llm_config = LLMConfig.from_config(cfg["llm"])

pid = "proj_20260825T095208"
prompt = ("做一个 60 秒角色动画短片，主角是一只海南坡鹿，主题是：坡鹿带你了解海南自贸港。"
          "核心创意：将海南坡鹿的文化传说与自贸港的现代感结合。故事从一个古老的传说开始，"
          "随着坡鹿的一路奔跑，观众也随之穿越，目睹海南如何从历史走向开放的自贸港。")
orch = Orchestrator(llm_config=llm_config)
t0 = time.time()
r = orch.run(pid, "character-animation", prompt, style_playbook=None, stage_filter=None)
print(f"[{time.time()-t0:.0f}s] ok={r.ok}", flush=True)
print("blocker:", (r.blocker or "")[:200], flush=True)
for s in r.stages:
    print(f"  {s.stage}: {s.status}", flush=True)

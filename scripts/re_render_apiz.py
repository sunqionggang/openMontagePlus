"""Re-render proj_20260820T062529 with APIz images (replace placeholders)."""
import sys, time
sys.path.insert(0, "D:/github/OpenMontage-dev")
from lib.env_loader import load_env
load_env()
from pathlib import Path
from om import renderer

pd = Path("D:/github/OpenMontage-dev/projects/proj_20260820T062529")
t0 = time.time()
m = renderer.run_assets(pd)
print(f"[{time.time()-t0:.0f}s] assets: {len(m['assets'])} 素材", flush=True)
for a in m["assets"]:
    print(" -", a["id"], a.get("source_tool"), flush=True)
e = renderer.run_edit(pd)
print(f"edit: {len(e['cuts'])} cuts", flush=True)
r = renderer.run_compose(pd)
print(f"[{time.time()-t0:.0f}s] compose: success={r.get('success')}", flush=True)
final = pd / "renders" / "final.mp4"
print("final.mp4:", final.exists(), round(final.stat().st_size/1048576, 2) if final.exists() else 0, "MB", flush=True)

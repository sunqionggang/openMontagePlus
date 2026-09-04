"""容器内可灵视频实测（探错用）"""
import os, sys, json

KEY = ""
try:
    with open("/app/projects/_data/om_data.json", encoding="utf-8") as f:
        data = json.load(f)
    for u in data.get("users", []):
        if u.get("username") == "SQG":
            KEY = (u.get("api_keys") or {}).get("KLING_API_KEY", "")
            break
except Exception:
    pass

out = "/tmp/kling_probe.log"
log = open(out, "w", encoding="utf-8")
def say(*a):
    print(*a)
    log.write(" ".join(str(x) for x in a) + "\n")
    log.flush()

say("key_len:", len(KEY))
if not KEY:
    say("NO_KLING_KEY")
    log.close()
    sys.exit(0)
os.environ["KLING_API_KEY"] = KEY
try:
    from tools.video.kling_official_video import KlingOfficialVideo
    say("import ok")
    res = KlingOfficialVideo().execute({
        "prompt": "一只憨态可掬的熊猫在竹林里吃竹子，中国风，画面唯美",
        "output_path": "/tmp/kling_probe.mp4",
        "duration": 5,
    })
    say("success:", res.success)
    say("error:", (res.error or "")[:800])
    say("output exists:", os.path.exists("/tmp/kling_probe.mp4"))
except Exception as exc:
    say("EXCEPTION:", str(exc)[:800])
log.close()

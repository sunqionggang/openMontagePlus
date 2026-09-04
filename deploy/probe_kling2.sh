#!/usr/bin/env bash
# 服务器端后台探测可灵（setsid 防断连），日志 /tmp/kling_probe.log
set -e
KEY=$(python3 - <<'EOF'
import json
d = json.load(open('/opt/openmontage/projects/_data/om_data.json', encoding='utf-8'))
for u in d.get('users', []):
    if u.get('username') == 'SQG':
        print((u.get('api_keys') or {}).get('KLING_API_KEY', ''))
        break
EOF
)
rm -f /tmp/kling_probe.log
setsid docker exec -e KLING_API_KEY="$KEY" openmontage python3 - <<'EOF' > /tmp/kling_probe.log 2>&1 &
import os, json, sys
os.environ.setdefault("KLING_API_KEY", os.environ.get("KLING_API_KEY",""))
print("KEY set:", bool(os.environ.get("KLING_API_KEY")))
try:
    from tools.video.kling_official_video import KlingOfficialVideo
    res = KlingOfficialVideo().execute({
        "prompt": "一只憨态可掬的熊猫在竹林里吃竹子，中国风，画面唯美",
        "output_path": "/tmp/kling_probe.mp4",
        "duration": 5,
    })
    print("success:", res.success)
    print("error:", (res.error or "")[:600])
    import os as o
    print("output exists:", o.path.exists("/tmp/kling_probe.mp4"))
except Exception as exc:
    print("EXCEPTION:", str(exc)[:600])
EOF
echo "探测已后台启动，日志: /tmp/kling_probe.log"

#!/usr/bin/env bash
# 容器内实测 kling_official_video（真实调用，看具体错误）
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
echo "key 长度: ${#KEY}"
if [ -z "$KEY" ]; then echo "!! 无 KLING key"; exit 1; fi
docker exec -e KLING_API_KEY="$KEY" openmontage python3 - <<'EOF'
import os, sys
from tools.video.kling_official_video import KlingOfficialVideo
print("KEY 存在:", bool(os.environ.get("KLING_API_KEY")))
try:
    res = KlingOfficialVideo().execute({
        "prompt": "一只憨态可掬的熊猫在竹林里吃竹子，中国风，画面唯美",
        "output_path": "/tmp/kling_probe.mp4",
        "duration": 5,
    })
    print("success:", res.success)
    print("error:", (res.error or "")[:400])
    import os as o
    print("output 存在:", o.path.exists("/tmp/kling_probe.mp4"))
except Exception as exc:
    print("EXCEPTION:", str(exc)[:400])
EOF

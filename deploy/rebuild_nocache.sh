#!/usr/bin/env bash
# 强制无缓存重建镜像并重启容器（解决 .dockerignore 变更后上下文缓存残留）
set -euo pipefail
cd /opt/openmontage
LOG=/var/log/om_build.log
echo "==> build --no-cache  $(date)" | tee -a $LOG
docker compose -f docker-compose.yml build --no-cache 2>&1 | tee -a $LOG
echo "==> up -d" | tee -a $LOG
docker compose -f docker-compose.yml up -d 2>&1 | tee -a $LOG
echo "==> BUILD_DONE $(date)" | tee -a $LOG

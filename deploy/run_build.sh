#!/usr/bin/env bash
# 服务器端构建+启动脚本（nohup 后台运行，日志 /var/log/om_build.log）
set -euo pipefail

cd /opt/openmontage
LOG=/var/log/om_build.log

echo "==============================================" | tee -a $LOG
echo "==> docker compose build  $(date)" | tee -a $LOG
echo "==============================================" | tee -a $LOG
docker compose -f docker-compose.yml build 2>&1 | tee -a $LOG

echo "==============================================" | tee -a $LOG
echo "==> docker compose up -d  $(date)" | tee -a $LOG
echo "==============================================" | tee -a $LOG
docker compose -f docker-compose.yml up -d 2>&1 | tee -a $LOG

echo "==> compose ps:" | tee -a $LOG
docker compose -f docker-compose.yml ps 2>&1 | tee -a $LOG

echo "==> BUILD_DONE $(date)" | tee -a $LOG

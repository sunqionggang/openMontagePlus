#!/usr/bin/env bash
# 本地打包脚本（在 Windows Git Bash 或 Linux 上执行）
# 用法： bash deploy/package.sh
# 产物： openmontage-deploy.tar.gz（约 100MB，scp 到服务器后解压部署）
set -euo pipefail

cd "$(dirname "$0")/.."

# 注意：.env 会打进包（含 API key），私有部署可接受；公网开源请去掉。
tar czf openmontage-deploy.tar.gz \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='.venv_bak' \
  --exclude='node_modules' \
  --exclude='remotion-composer' \
  --exclude='projects' \
  --exclude='docs/*.mp4' \
  --exclude='docs/images' \
  --exclude='assets' \
  --exclude='.workbuddy' \
  --exclude='.om' \
  --exclude='om.db' \
  --exclude='*.log' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='openmontage-deploy.tar.gz' \
  .

echo "=============================================="
echo "打包完成: $(pwd)/openmontage-deploy.tar.gz"
echo "下一步（在本地执行，把包传到服务器）："
echo '  scp openmontage-deploy.tar.gz root@<服务器IP>:/opt/'
echo "=============================================="

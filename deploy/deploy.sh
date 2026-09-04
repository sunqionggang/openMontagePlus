#!/usr/bin/env bash
# OpenMontage 腾讯云一键部署脚本（在服务器项目根目录执行）
# 用法： bash deploy/deploy.sh
set -euo pipefail

cd "$(dirname "$0")/.."
echo "=============================================="
echo "  OpenMontage 部署到腾讯云"
echo "=============================================="

# 1. 检查/安装 Docker
if ! command -v docker >/dev/null 2>&1; then
  echo "==> 未检测到 Docker，正在安装..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker || true
fi
docker --version

# 2. 检查 docker compose 插件
if ! docker compose version >/dev/null 2>&1; then
  echo "!! 缺少 docker compose 插件，请执行："
  echo "   apt install -y docker-compose-plugin"
  exit 1
fi

# 3. 生成生产环境变量（过滤注释/空行，只保留有值的 KEY=VALUE）
if [ -f .env ]; then
  echo "==> 生成 .env.prod ..."
  grep -vE '^\s*(#|$)' .env | grep -E '=' > .env.prod || true
  echo "    共 $(wc -l < .env.prod) 个有效变量"
else
  echo "!! 缺少 .env，请先上传（含 API key 的本地 .env 文件）"
  exit 1
fi

# 4. 构建并启动
echo "==> docker compose up -d --build ..."
docker compose -f docker-compose.yml up -d --build

# 5. 健康检查
echo "==> 等待服务启动..."
for i in $(seq 1 10); do
  sleep 3
  if curl -fsS http://localhost:8000/api/health >/dev/null 2>&1; then
    echo "✅ 服务已就绪"
    curl -s http://localhost:8000/api/health
    echo
    echo "=============================================="
    echo "  访问地址: http://<服务器公网IP>:8000"
    echo "  看板:     http://<服务器公网IP>:8000/board/"
    echo "  原型:     http://<服务器公网IP>:8000/prototype"
    echo "  （记得在腾讯云安全组放行 TCP 8000）"
    echo "=============================================="
    exit 0
  fi
done

echo "!! 健康检查超时，查看日志: docker compose -f docker-compose.yml logs -f"
exit 1

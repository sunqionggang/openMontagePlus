#!/usr/bin/env bash
# 腾讯云服务器安装 Docker CE + Compose v2（走腾讯云内网镜像源）
set -euo pipefail

echo "==> 配置 Docker CE 官方源（腾讯云镜像）..."
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://mirrors.tencentyun.com/docker-ce/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.tencentyun.com/docker-ce/linux/ubuntu noble stable" > /etc/apt/sources.list.d/docker.list

echo "==> apt update + 安装 docker-ce / compose..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin

echo "==> 启动 Docker..."
systemctl enable --now docker

echo "==> 验证版本："
docker --version
docker compose version

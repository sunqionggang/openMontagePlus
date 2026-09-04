#!/usr/bin/env bash
# 清理残留的 docker buildx 构建进程和缓存
set -u

echo "==> 清理残留构建进程 ..."
pkill -f '/usr/libexec/docker/cli-plugins/docker-buildx' 2>/dev/null || true
pkill -f '/var/lib/docker/buildkit' 2>/dev/null || true
sleep 2

echo "==> 残留进程数: $(ps aux | grep -E 'cli-plugins/docker-buildx|runc.*buildkit' | grep -v grep | wc -l)"

echo "==> 清理 BuildKit 缓存 ..."
docker buildx prune -f 2>&1 | tail -2 || true

echo "==> 完成"

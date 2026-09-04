#!/usr/bin/env bash
# 停掉服务器端正在进行的构建（由脚本文件执行，避免命令行匹配误杀 ssh）
set -u
pkill -f '/opt/openmontage/deploy/run_build.sh' 2>/dev/null || true
pkill -f '/usr/libexec/docker/cli-plugins/docker-buildx' 2>/dev/null || true
pkill -f '/var/lib/docker/buildkit' 2>/dev/null || true
sleep 2
echo "remaining: $(ps aux | grep -E 'run_build|docker-buildx|buildkit' | grep -v grep | wc -l)"

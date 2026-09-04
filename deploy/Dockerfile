# OpenMontage 自托管在线层 —— 生产镜像
# 依赖要点：
#   ffmpeg  —— om/renderer.py 视频合成/切片/混音（video-first 链路必需）
#   curl    —— tools/video/apiz_video.py 走 curl 调 APIZ（Cloudflare 拦截 urllib）
#   git     —— 部分工具/脚本需要
# 构建提示：
#   BuildKit 不读 docker daemon 的 registry-mirrors，海外机/国内机直连
#   Docker Hub 会超时。构建时用 compose build.args 传 BASE_IMAGE 走镜像加速器：
#     BASE_IMAGE=mirror.ccs.tencentyun.com/library/python:3.13-slim（腾讯云内网）
ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

# 基础镜像（Debian）apt 源换清华镜像：国内直连 deb.debian.org 下载 Packages/ffmpeg 极慢
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list 2>/dev/null || true

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
        git \
        ca-certificates \
        fonts-noto-cjk \
        fontconfig \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装 Python 依赖（利用 Docker 层缓存，改代码不用重装依赖）
# 用腾讯云 PyPI 镜像加速（服务器在腾讯云内网，构建更快；公网构建也可用）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple -r requirements.txt \
    && pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple "uvicorn[standard]"

# 复制项目代码（大目录由 .dockerignore 排除）
COPY . .

# 安全强制：构建上下文的 .env 绝不允许进入镜像（密钥由运行时 env_file 注入）。
# 注：部分 buildx 版本对 .dockerignore 的 .* 隐藏文件排除失效，这里硬删除兜底。
RUN rm -f /app/.env

# 数据目录：任务状态 + 作品数据（均落盘 projects/，通过卷持久化）
RUN mkdir -p /app/projects/_data /app/.om/state

EXPOSE 8000

CMD ["uvicorn", "om.server:app", "--host", "0.0.0.0", "--port", "8000"]

# OpenMontage 部署到腾讯云（Docker 方案）

> 目标：把 OpenMontage 自托管在线层（FastAPI 控制面 + 视频生成流水线）跑在腾讯云
> Linux 服务器上，通过 `http://IP:8000` 公网访问。无需域名/HTTPS（可选后续加）。

## 一、服务器准备（腾讯云控制台）

| 项 | 建议 | 说明 |
|---|---|---|
| 产品 | CVM 或 轻量应用服务器 | 轻量服务器性价比高，够用 |
| 系统 | **Ubuntu 22.04/24.04** | 脚本针对 Ubuntu；CentOS 需改 apt→yum |
| 配置 | 2核4G 起步 | LLM/API 生成是云端算，本地不吃 GPU |
| 带宽 | 5M 起 | 出片下载/上传素材用 |
| 安全组 | **放行 TCP 8000**（或改端口） | 入口规则加一条 8000 即可 |
| 登录 | 密钥对 或 密码 | 记下公网 IP |

## 二、上传代码（本地执行）

在项目根目录（Windows 用 Git Bash，或直接用 WorkBuddy 代执行）：

```bash
bash deploy/package.sh            # 生成 openmontage-deploy.tar.gz（~100MB）
scp openmontage-deploy.tar.gz root@<服务器IP>:/opt/
```

> 包内已含 `.env`（所有 API key），私有部署安全；若对外开源请先脱敏。

## 三、服务器上解压 + 一键部署

```bash
ssh root@<服务器IP>
cd /opt && mkdir -p openmontage && tar xzf openmontage-deploy.tar.gz -C openmontage
cd openmontage
bash deploy/deploy.sh
```

`deploy.sh` 会自动：
1. 安装 Docker（未装时）
2. 从 `.env` 过滤生成 `.env.prod`（只保留有效 KEY=VALUE）
3. `docker compose up -d --build` 构建并启动
4. 健康检查 `GET /api/health`

## 四、验证

```bash
# 服务器本机
curl http://localhost:8000/api/health

# 本地浏览器
http://<服务器公网IP>:8000/
http://<服务器公网IP>:8000/prototype    # 产品原型（真实数据）
http://<服务器公网IP>:8000/board/       # Backlot 看板
```

提交一个冒烟任务验证流水线：

```bash
curl -X POST http://<IP>:8000/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Explain HTTP vs HTTPS in 60s","pipeline_type":"animated-explainer","creative_only":true}'
curl http://<IP>:8000/api/jobs/<job_id>     # 查状态
```

## 五、运维命令

```bash
docker compose -f docker-compose.yml logs -f      # 看日志
docker compose -f docker-compose.yml restart      # 重启
docker compose -f docker-compose.yml down         # 停止
docker compose -f docker-compose.yml up -d --build  # 更新代码后重建
```

数据都在 `./projects`（任务/作品/checkpoint）——**升级/重建容器前先备份**：
```bash
tar czf backup-projects-$(date +%F).tar.gz projects
```

## 六、常见问题

| 现象 | 处理 |
|---|---|
| 浏览器打不开 | ① 腾讯云控制台安全组/防火墙放行 TCP 8000（CVM→安全组，轻量→防火墙）；② 服务器 `curl localhost:8000/api/health` 通但公网不通 = 一定是云控制台规则问题 |
| `/api/health` 报 LLM 未配置 | `.env.prod` 里缺 MINIMAX_API_KEY（config.yaml 的 provider） |
| 视频任务 blocked | 缺对应生成 key（APIZ_API_KEY / DASHSCOPE_API_KEY / 等），在 .env 补后重建 |
| 容器启动崩溃 `python-multipart` | requirements.txt 需含 `python-multipart>=0.0.9`（FastAPI 文件上传接口必需） |
| `/prototype`、`/marketing` 404 | 路由读 `docs/product-prototype.html` / `docs/marketing.html`，**打包和 .dockerignore 都不能排除 docs/ 的 html**（只排除 docs/*.mp4 和 docs/images） |
| 构建卡在拉基础镜像 | **BuildKit 不读 daemon 的 registry-mirrors**，国内直连 Docker Hub 超时。compose `build.args.BASE_IMAGE=mirror.ccs.tencentyun.com/library/python:3.13-slim`（腾讯云内网加速器） |
| 构建卡在 apt-get | 容器内 Debian 源换清华：Dockerfile `sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources` |
| 需要 Remotion 渲染 | 上传 remotion-composer/ 并在 compose 挂载（当前镜像走 ffmpeg + apiz 视频链路） |

## 七、后续升级方向（可选）

- 域名 + HTTPS：Nginx 反代 8000，certbot 免费证书
- 数据卷独立：projects 挂到云硬盘（CBS），换机不丢数据
- 自动更新：GitHub Actions + Docker Hub 镜像，服务器 `watchtower` 自动拉新

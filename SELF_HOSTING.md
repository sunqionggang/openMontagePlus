# OpenMontage 自托管版（Self-Hosted Online Layer）

> 本文档说明如何把 OpenMontage 以**服务端自主生产**的方式跑起来——用户提交一句话，
> 服务器自动跑完创意流水线并产出可审阅的 artifact（checkpoint/JSON），后续可接生成工具
> 完成成片渲染。这是「开源产品化」的第一步（仓库模式 A）：可自托管的在线层。

## 这是什么

原版 OpenMontage 是 **agent-first** 的：生产流程由 AI 编码助手在对话里驱动，代码里没有
"给一句话就自动跑完"的服务端循环。`om/` 包补上了这层缺口：

```
om/
├── llm.py          # 可插拔 LLM 客户端（openai/anthropic/gemini/ollama/openrouter…，BYOK）
├── config.py       # 读 config.yaml，构建 LLM/预算/checkpoint 配置
├── orchestrator.py # 确定性阶段状态机：读 pipeline_defs + director skill，
│                   #   创意阶段 LLM 生成 → schemas 校验 → checkpoint 落盘
├── jobs.py         # 任务注册表 + 后台线程执行器（BYOK key 只存内存）
└── server.py       # FastAPI 控制面：POST /api/jobs 提交、查状态；复用 Backlot 看板
```

复用关系：编排器**不另起炉灶**，直接调用项目已有的 `lib/checkpoint`（审批门/前置校验/
历史归档）、`lib/pipeline_loader`（pipeline_defs 清单）、`schemas/artifacts`
（每个 artifact 的 JSON Schema 校验）、`tools/tool_registry`（工具可用性探测）、
`backlot`（看板 UI 与状态推导）。

## 快速开始

### 1. 依赖

```bash
pip install -r requirements.txt        # 项目已有依赖（fastapi/uvicorn/pyyaml/jsonschema…）
pip install "uvicorn[standard]"        # 若未装
# LLM 客户端：按需安装 SDK
pip install openai                     # provider=openai/openrouter/mistral/minimax/gemini/ollama
# pip install anthropic                # provider=anthropic 时
```

### 2. 配置 LLM（两种方式任选）

**方式 A：服务器级 key（所有任务共用）**

编辑 `config.yaml`：

```yaml
llm:
  provider: openai        # openai | anthropic | gemini | openrouter | ollama | mistral | minimax
  model: gpt-4o-mini      # null = 用 provider 默认
  temperature: 0.7
  max_tokens: 4096
```

并设置对应环境变量，例如 `export OPENAI_API_KEY=sk-...`。

**方式 B：BYOK（用户自带 key，推荐公共实例）**

提交任务时在 `POST /api/jobs` 里带 `api_keys`：

```json
{
  "prompt": "Explain HTTP vs HTTPS in 60s",
  "pipeline_type": "animated-explainer",
  "api_keys": { "OPENAI_API_KEY": "sk-user-provided-..." }
}
```

key 只在该任务运行期间注入进程环境，**不落盘、不返回给任何 API**。

### 3. 启动

```bash
uvicorn om.server:app --host 0.0.0.0 --port 8000
```

或者用进程管理（systemd / supervisor）跑。可选环境变量：

| 变量 | 作用 |
|------|------|
| `OPENMONTAGE_PROJECTS_DIR` | 项目目录（默认 `./projects`，改它可做数据隔离/卷挂载） |

浏览器打开 `http://localhost:8000/` 就是**自助配置台**（也可访问 `/config`）：
按能力分组展示所有工具，每个工具标注"服务器已配置 ✓ / 待填写"，你在页面上填好自己的
API key（图像/视频/TTS/音乐/创意 LLM），提交任务时这些 key 作为 BYOK 只注入本次
运行。勾选"记住"后 key 仅保存在你浏览器 localStorage（可选，服务器不存）。

**产品原型（真实数据模式）**：`http://localhost:8000/prototype` —— 原型与 API 同源，
自动进入"已连接后端"模式：登录/作品/广场全部读写 SQLite（演示账号自动创建）。
直接双击打开 `docs/product-prototype.html` 则为"演示模式"（内存 mock，无后端也可看）。

**营销页**：`http://localhost:8000/marketing` —— 定价/案例/FAQ，案例视频直接播放
`projects/` 下的真实成片。

### 4. 使用

```bash
# 健康检查（含 preflight：LLM 是否已配置）
curl http://localhost:8000/api/health

# 注册 / 登录（SQLite 多用户，返回 bearer token）
curl -X POST http://localhost:8000/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username":"demo","email":"demo@x.com","password":"demo1234","plan":"专业版"}'
curl -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' -d '{"email":"demo@x.com","password":"demo1234"}'
# → {"token":"...","user":{...}}，后续请求带 Authorization: Bearer <token>

# 我的作品（用户隔离 CRUD）
curl http://localhost:8000/api/me/works -H "Authorization: Bearer $TOKEN"
curl -X POST http://localhost:8000/api/me/works -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"title":"我的第一个视频","dur":"60s","emoji":"🎬"}'
curl -X PATCH http://localhost:8000/api/me/works/1 -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"published":true,"tags":["#AI生成"],"platform":"gallery"}'

# 公共广场（所有已发布作品，带作者）
curl http://localhost:8000/api/gallery

# 列出可用 pipeline / 风格 playbook
curl http://localhost:8000/api/pipelines
curl http://localhost:8000/api/playbooks

# 工具能力清单（每个工具的所需 key + 服务器配置状态，缓存 5 分钟）
curl http://localhost:8000/api/tools

# 提交任务（creative_only：只跑创意阶段，不触发生成工具，最快验证）
curl -X POST http://localhost:8000/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Explain HTTP vs HTTPS in 60s","pipeline_type":"animated-explainer","creative_only":true}'

# 带 BYOK 提交：用户自选 LLM provider + 自带 key
curl -X POST http://localhost:8000/api/jobs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Explain HTTP vs HTTPS in 60s","pipeline_type":"animated-explainer",
       "llm_provider":"openai","api_keys":{"OPENAI_API_KEY":"sk-user-...","APIZ_API_KEY":"sk-..."}}'

# 查任务状态（返回 stages 数组，每阶段 status/artifact/error）
curl http://localhost:8000/api/jobs/<job_id>

# 看板（复用 Backlot UI，实时显示 checkpoint 进度）
open http://localhost:8000/board/
```

## 编排器行为说明（重要）

**阶段状态机**：按 pipeline manifest 的 stage order 依次跑。

- **创意阶段**（`research` / `proposal` / `idea` / `script` / `scene_plan`）：
  LLM 生成 → `schemas/artifacts/*.schema.json` 严格校验（最多重试 2 次）→
  通过则 `lib/checkpoint.write_checkpoint` 落盘。产出与项目本地跑完全同构，
  Backlot 看板可直接浏览。
- **工具阶段**（`assets` / `edit` / `compose` / `publish`）：通过
  `tools/tool_registry` 探测所需工具（如 `tts_selector`、`image_selector`）。
  当前 MVP **不自动调用付费生成 API**——检测到必需工具缺失时会**明确报 blocker**
  （带缺失工具名单），不会静默跳过。配置好工具 key 后重跑即可续进
  （前置 completed checkpoint 会被跳过，天然断点续跑）。
- **`creative_only: true`**：跑完 `scene_plan` 即停，用于低成本验证端到端链路。

**审批门**：
- `auto_approve: true`（默认）：自托管无人工值守，阶段自动标记
  `completed + human_approved`，可一路跑到底。
- `auto_approve: false`：阶段停在 `awaiting_human`，等人工确认后再继续。

**缺 key 的降级（fail-kind）**：没有 LLM key 时，任务不会崩——编排器在任何阶段前
先做一次 preflight，命中即返回 `status: "blocked"` + 明确 `blocker` 文案
（例如"需要 OPENAI_API_KEY，可设环境变量或在提交时传 BYOK"）。

**BYOK 与多任务**：用户提交的 key 只注入当次任务运行的进程环境，任务结束即还原。
为避免并发任务互相读到对方的 key，任务队列为**全局串行**（单 worker 逐个执行）——
自托管 MVP 够用；要并发吞吐时改为每任务独立进程/容器即可。

## 从创意阶段到成片（下一步）

创意阶段产出的 `scene_plan` 是渲染的完整蓝图。要真正出片，需要：

1. 为 `assets` 阶段配置生成工具（TTS / 图像 / 视频 / 音乐 API key），
   以及本地渲染环境（FFmpeg / Node + remotion-composer）。
2. 在 `om/orchestrator.py` 的 `_run_tool_stage` 里接入工具执行
   （当前是"探测 + blocker"骨架，执行逻辑按你的工具选型填充）。
3. 若要多租户/计费：`tools/cost_tracker.py` 已能估算/预占/对账，
   配一个用户表 + 额度即可。

## 常见问题

**Q: 提交任务后一直 `running`？**
先看 `GET /api/jobs/<id>` 的 `blocker` 字段；无 blocker 则在等 LLM 响应
（首次请求可能较慢）。日志里搜 `om.` 前缀能看到阶段推进。

**Q: 为什么我的 pipeline 卡在 `assets` blocked？**
该阶段需要生成工具，见上文「工具阶段」说明。MVP 不自动调付费 API。

**Q: 任务重启后还在吗？**
job 状态持久化在 `.om/state/jobs.json`；checkpoint 持久化在 `projects/<id>/`。
重启服务后任务可继续（已 completed 的阶段会被跳过）。

**Q: 许可证？**
派生自上游 `calesthio/OpenMontage`（AGPLv3）。你以开源方式托管即自动满足
AGPL 的网络服务条款。注意 `OpenMontage` 名称与 Monty 吉祥物可能受上游商标
保护——公开托管时建议用你自己的品牌名并保留上游署名（见 README）。

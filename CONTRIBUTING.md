# 为 OpenMontage Plus 做贡献 (Contributing to OpenMontage Plus)

感谢你考虑改进 OpenMontage Plus。我们不要求贡献者签署 CLA（贡献者许可协议）、DCO（开发者原产地证书）或任何单独的贡献合同。

提交 Pull Request 即表示你确认有权贡献其内容。贡献内容须与仓库的 [AGPLv3 许可证](LICENSE) 兼容，且被接受的贡献将保持在该许可证下可用。

请保持 PR 聚焦单一主题，并在行为变更时附带测试。

---

## 这是什么项目

OpenMontage Plus = 上游 [OpenMontage](https://github.com/calesthio/OpenMontage) 的 agentic 视频流水线引擎 **+** 一个额外的 Web 平台（`om/`）。

- **引擎层**（上游）：`tools/`、`pipeline_defs/`、`skills/`、`remotion-composer/`、`lib/`、`schemas/`
- **平台层**（本 fork 新增）：`om/` —— 账户、真实成本计量、托管代付、作品广场、模板市场、服务器工具状态

---

## 常见贡献类型

### 1. 新增工具（引擎层）

1. 在对应的 `tools/` 子目录创建 Python 文件
2. 继承 `BaseTool` 并实现工具契约
3. 注册表会自动发现，无需手动注册
4. 若工具需要用法指引，补充对应 skill 文件

### 2. 新增流水线（引擎层）

1. 在 `pipeline_defs/` 创建 YAML 清单
2. 在 `skills/pipelines/<your-pipeline>/` 创建阶段导演技能
3. 引用已有工具，或按需新增工具

### 3. 修改 Web 平台（平台层 `om/`）

Web 平台涉及账户与资金，改动时请注意：

- **账户 / 计量 / 任务**（`om/db.py`、`om/meter.py`、`om/jobs.py`、`om/server.py`）：任何影响成本账本或托管模式的改动，请在 PR 描述中说明对以下链路的影响：
  - 计量精度（成本以 6 位小数记账，避免微额任务被归零）
  - `hosted`（平台代付）与 `byok`（自带 Key 自付）的判定
  - 余额扣减与充值接口
- **前端原型**（`docs/product-prototype.html`）：UI 改动请截图说明。
- **本地验证**：可用 `python -m om.server` 启动后在 `http://localhost:8000` 手动验证账户中心 / 作品广场 / 模板市场。

---

## 行为准则

- 保持 PR 小而聚焦，便于审查。
- 修改涉及成本、计费、鉴权时，务必附带可复现的验证步骤。
- 提交信息清晰说明"为什么"，而不只是"改了什么"。

---

问题反馈、功能请求请使用 [GitHub Issues](https://github.com/sunqionggang/openMontagePlus/issues)；方案讨论请使用 [GitHub Discussions](https://github.com/sunqionggang/openMontagePlus/discussions)。

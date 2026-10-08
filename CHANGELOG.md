# Changelog

## [0.1.0] - 2026-10-08

### Features

- 首个可用版本：NoName Agent Harness 作为 DeepSeek Harness 插件（Hybrid 架构——Python sidecar 内核 + TS 原生工具/UI 层）。
- Python sidecar 桥（`kernel.ts`）：`python -m noname_harness` 子进程 + stdout JSON 契约（raw 模式支持 `package`/`ledger` 非 JSON 输出），错误按 spawn_failed/nonzero_exit/bad_json/timeout/aborted 分类，含 pre-aborted signal 守卫与幂等 init。
- 8 个模型可见工具：record/package/search/state/inbox/taste_add/verify/extract。
- 事件自动入账（可配置开关 + minimal/verbose 级别 + callId 幂等去重 + 失败不影响宿主会话）。
- 账本面板：右侧 sidebar tab 嵌入 NoName 五视图 HTML（iframe，内核自渲染保真）。
- 插件配置（zod）：pythonPath/dbDir/autoIngest/ingestLevel，设置卡片可编辑。

### Design Rationale

- **Hybrid 而非纯 TS 重写**：NoName 内核 11,360 行生产代码 + 12,400 行测试经 28 轮对抗性审查 + 2 轮暴力测试加固；重写意味着数周工作 + 翻译走样风险 + 放弃已验证资产。内核以 git submodule 零改动引入，TS 层只做薄适配。
- **UI 嵌入内核 HTML 而非 TS 重渲染**：账本五视图是 NoName 自己的设计哲学（克制、渐进披露、"审核是签署"），TS 重绘只会失真；iframe 嵌入保持与 `ledger-html` 完全一致。

### Notes & Caveats

- 17 项插件层测试全绿（vitest，含真内核端到端：record→extract→propose→package 跨会话接续、五视图渲染、verify）；内核 628 测试保持全绿（submodule 不动）。
- 依赖系统 python3（内核无外部依赖）；DSH 插件运行时需要 child_process.spawn 能力。
- 首版不含：审批门在 DSH 侧的专用 UI、品味卡图像面板、多 profile 共享账本。

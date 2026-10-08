# NoName Agent Harness · DeepSeek Harness 插件

> 把「上下文」当作资产，带进 DeepSeek Harness。

这个插件把 [NoName Agent Harness](https://github.com/yuchenlogin/NoNameAgentHarness) 的完整能力接入 DeepSeek Harness：**append-only 证据流、可版本化记忆、一等公民的品味、账本支撑的审批门**——全部作为 DSH 的模型可见工具与会话事件闭环，不重写任何内核逻辑。

## 架构（Hybrid：Python sidecar + TS 原生层）

```
DeepSeek Harness
  └─ noname-dsh-plugin (TypeScript, Cordis)
       ├─ 工具层    ctx.tools.register ── 12 个模型可见工具
       ├─ 事件观察  ctx.on('tools/result') ── DSH 事件自动入账
       └─ 账本访问  noname_ledger 工具 ── 五视图 HTML（真机验证：host 侧无可达 sidebar seam）
            │ child_process: python -m noname_harness <cmd>（stdout JSON）
            ▼
       NoName 内核（vendor/noname-harness，Python 标准库 + SQLite）
       ── 证据只增不改、双时序、品味双轨、审批门、沙箱（628 测试全绿）
```

内核是**唯一事实来源**，以 git submodule 引入、零改动。TS 层只是适配：工具是薄桥接，UI 嵌入 NoName 自己生成的 HTML。这样 NoName 的 28 轮对抗性审查 + 2 轮暴力测试的成果完整保留。

## 前置条件

- **DeepSeek Harness** 本体（`dsh` CLI；见 [官方文档](https://deepseek-harness.github.io/deepseek-harness/guide/quickstart)）
- **Node.js ≥ 18** 与 **Python ≥ 3.10**（NoName 内核无外部依赖，标准库即可）

## 安装

```bash
# 克隆（含内核 submodule）
git clone --recursive https://github.com/yuchenlogin/noname-dsh-plugin.git
cd noname-dsh-plugin
npm install          # prepare 脚本自动运行 npm run build

# 安装进 DSH profile
dsh plugin --profile web add /path/to/noname-dsh-plugin
```

`prepare` 脚本会在安装/打包时自动编译 TypeScript，因此从 GitHub 安装也能直接加载。

## 配置（DSH 设置卡片）

| 字段 | 默认 | 说明 |
|---|---|---|
| `pythonPath` | `python3` | sidecar 解释器（建议填绝对路径：DSH 自带的 Python 3.12） |
| `dbDir` | 空 = 按工作区作用域 | 留空时账本跟随会话所属的 DSH 工作区：`<项目>/.noname`；填了则固定用该目录 |
| `autoIngest` | `true` | DSH 工具结果自动写入证据流（按真实会话 id 归属，可溯源） |
| `timeoutMs` | `30000` | sidecar 调用超时（毫秒） |

配置由 `@deepseek-ai/schemastery` 在加载边界校验——非法值响亮报错，不会以 `mkdir("")` 之类的深层失败出现。

## 账本粒度：一个工作区一本账

粒度是**工作区（项目）**，不是会话，也不是全局：

```
DSH 工作区（项目）
  ├─ 会话 A ─┐
  ├─ 会话 B ─┼─→ <项目>/.noname/harness.db   同一本账，按 session_id 区分归属
  └─ 会话 C ─┘
另一个项目 ──────→ <那个项目>/.noname/harness.db   完全隔离
```

- 会话 → 工作区的映射直接读 DSH 自己的 `ctx.workspaceRegistry`（`workspace.path` / `workspace.sessionIds`），不另造一套分组。
- `<项目>/.noname/` 就是内核 quickstart 的约定（`init --db .noname/harness.db --root .`）：账本随项目走，且内核的 workspace boundary 指向真实项目根——所以 `snapshot` 记录的是你项目的 git 状态，接续包里的边界行也是真项目路径。
- 账本 HTML 同样落在 `<项目>/.noname/noname-ledger.html`（内核强制生成物必须在项目边界内）。
- 解析不到工作区时（例如没有工作区服务的 headless 组合）**不猜**：回落到旧的 `$DSH_HOME/noname`，绝不把一个项目的证据写进另一个项目的账本。
- 建议把 `.noname/` 写进 `.gitignore`（本仓库与 NoName 内核仓库都已这么做）。

## 提供的工具（模型可见）

| 工具 | 作用 |
|---|---|
| `noname_record` | 把决定/发现/约束记入 append-only 证据流 |
| `noname_package` | 组装跨会话接续包（换模型不换事实来源） |
| `noname_search` | 全文检索证据与记忆 |
| `noname_state` | 查看已批准的法典（项目宪法） |
| `noname_inbox` | 审核收件箱（审核是签署，不是点按钮） |
| `noname_taste_add` | 记录**自述品味**（Authored，最高权威，立即激活） |
| `noname_taste_propose` | 从「眼前一亮的模型时刻」提出**采纳品味**候选（Adopted，人审核） |
| `noname_taste_review` | 品味生命周期审核（adopt/edit/pause/resume/retire） |
| `noname_card_queue` | 品味卡复核队列（「这还是现在的我吗」） |
| `noname_ledger` | 渲染账本五视图为 HTML 并返回路径（真机可达的账本形态） |
| `noname_verify` | 校验账本完整性 |
| `noname_extract` | 从会话事件抽取记忆候选（绝不自我确认） |

品味是双轨的：`taste_add` 是你主动写下的态度；`taste_propose` 是模型从它令你眼前一亮的表现中提出、经你选择后跨项目延续的候选——后者绝不伪装成你的原话。

## 账本访问（真机验证后的形态）

**真机验证发现**（在真实 DeepSeek Harness 0.2.0-rc.2 web profile 上实测）：`sidebarRightTabs` 服务**不在 host 侧**——它属于 client-UI 侧，工具/服务类插件无法触及。因此 sidebar 面板无法从本插件注册，宣称它能就是撒谎。

所以账本在 DSH 里的诚实形态是一个**工具**：`noname_ledger` 调用内核 `ledger-html` 生成五视图 HTML（审核收件箱 / 状态 / 版本演进 / 因果图 / 时间线，NoName 自己的克制暗色设计、离线单文件），返回文件路径，模型或用户保存即可查看。这条路径已在真机上验证加载与产出。

若 DSH 未来暴露 host 可达的面板 seam，同一份 `buildLedgerView()` 输出可直接接入。

## 已知边界（Roadmap）

- **审批门**：NoName 的审批动作（`review`/`grant_approval`）刻意不暴露为模型工具——它们留在内核与人手里（「核心不可谈判」）。DSH 侧的审批 UI 属后续版本。
- **品味卡图像**：`card-image` 的视觉层在 DSH 面板中的呈现属后续版本。
- **多 profile 账本**：当前每个 profile 独立账本；跨 profile 共享属后续版本。

## 开发

```bash
npm run build          # 编译 TS → dist/
npm test               # vitest（24 项，含真内核端到端与 ingest 诚实性）
npm run test:kernel    # NoName 内核 628 测试
```

### 升级内核

```bash
git submodule update --remote vendor/noname-harness
npm run test:kernel    # 内核回归
npm test               # 桥接契约回归
```

桥接层与内核共用同一份 CLI/JSON 契约，因此 submodule 升级只需两步验证。

## 设计哲学

NoName 的内核不可谈判（沙箱、审批、账本是物理强制，不是模型不做）；插件只是把它带进 DSH 的能力结晶。它不绕过 NoName 的任何保障——证据只增不改，记忆由人批准，品味不伪装成事实。

## 许可

本插件层以 [MIT](LICENSE) 发布。内核 NoName Agent Harness（`vendor/noname-harness` submodule）的许可以其仓库为准——集成与分发时请同时遵守两者的许可条款。

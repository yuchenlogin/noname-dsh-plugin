# NoName Agent Harness · DeepSeek Harness 插件

> 把「上下文」当作资产，带进 DeepSeek Harness。

这个插件把 [NoName Agent Harness](https://github.com/yuchenlogin/NoNameAgentHarness) 的完整能力接入 DeepSeek Harness：**append-only 证据流、可版本化记忆、一等公民的品味、账本支撑的审批门**——全部作为 DSH 的模型可见工具与会话事件闭环，不重写任何内核逻辑。

## 架构（Hybrid：Python sidecar + TS 原生层）

```
DeepSeek Harness
  └─ noname-dsh-plugin (TypeScript, Cordis)
       ├─ 工具层    ctx.tools.register ── 8 个模型可见工具
       ├─ 事件观察  ctx.on('tools/result') ── DSH 事件自动入账
       └─ UI 面板   右侧 sidebar ── 账本五视图（iframe 嵌入）
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
| `pythonPath` | `python3` | sidecar 解释器 |
| `dbDir` | `$DSH_HOME/noname`（无 DSH_HOME 时 `~/.dsh/noname`） | 账本 db 目录（首次自动 init，绝不写宿主 CWD 或内核 submodule） |
| `autoIngest` | `true` | DSH 工具结果自动写入证据流（按真实会话 id 归属，可溯源） |
| `timeoutMs` | `30000` | sidecar 调用超时（毫秒） |

配置由 `@deepseek-ai/schemastery` 在加载边界校验——非法值响亮报错，不会以 `mkdir("")` 之类的深层失败出现。

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
| `noname_verify` | 校验账本完整性 |
| `noname_extract` | 从会话事件抽取记忆候选（绝不自我确认） |

品味是双轨的：`taste_add` 是你主动写下的态度；`taste_propose` 是模型从它令你眼前一亮的表现中提出、经你选择后跨项目延续的候选——后者绝不伪装成你的原话。

## 账本面板

面板按 DSH 真实 sidebar 契约（`ctx.sidebarRightTabs` 静态定义 + keyed slot 正文）注册 `noname-ledger` tab，渲染五视图：审核收件箱 / 状态 / 版本演进 / 因果图 / 时间线。HTML 由 NoName 内核 `ledger-html` 生成（它自己的克制暗色设计、离线单文件），与命令行 `ledger-html` 输出完全一致；具体渲染方式（iframe/webview）由 DSH 宿主的 slot 渲染器决定。

> **诚实标注**：sidebar 的 keyed-slot 正文接线需要在真实 DSH 宿主中验证；当前版本以文档化的静态定义注册，若宿主 sidebar API 有差异，插件会警告并降级为「无面板、工具不受影响」，绝不因面板失败而拖垮整个插件。

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

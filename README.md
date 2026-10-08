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

## 安装

```bash
# 克隆（含内核 submodule）
git clone --recursive https://github.com/yuchenlogin/noname-dsh-plugin.git
cd noname-dsh-plugin
npm install && npm run build

# 需要 python3（内核无外部依赖，标准库即可）
python3 -c "import noname_harness" 2>/dev/null || echo "确保 vendor/noname-harness 在 PYTHONPATH"

# 安装进 DSH profile
dsh plugin --profile web add /path/to/noname-dsh-plugin
```

## 配置（DSH 设置卡片）

| 字段 | 默认 | 说明 |
|---|---|---|
| `pythonPath` | `python3` | sidecar 解释器 |
| `dbDir` | `$DSH_HOME/noname` | 账本 db 目录（首次自动 init） |
| `autoIngest` | `true` | DSH 事件自动写入证据流 |
| `ingestLevel` | `minimal` | `minimal`（工具结果）/ `verbose`（全部可观察事件） |

## 提供的工具（模型可见）

| 工具 | 作用 |
|---|---|
| `noname_record` | 把决定/发现/约束记入 append-only 证据流 |
| `noname_package` | 组装跨会话接续包（换模型不换事实来源） |
| `noname_search` | 全文检索证据与记忆 |
| `noname_state` | 查看已批准的法典（项目宪法） |
| `noname_inbox` | 审核收件箱（审核是签署，不是点按钮） |
| `noname_taste_add` | 记录自述品味（最高权威） |
| `noname_verify` | 校验账本完整性 |
| `noname_extract` | 从会话事件抽取记忆候选（绝不自我确认） |

## 账本面板

右侧 sidebar 的 **NoName 账本** tab 渲染五视图：审核收件箱 / 状态 / 版本演进 / 因果图 / 时间线。HTML 由 NoName 内核生成（它自己的克制暗色设计、离线单文件），面板用 iframe 嵌入，保真度与 `ledger-html` 完全一致。

## 开发

```bash
npm run build          # 编译 TS → dist/
npm test               # vitest（17 项，含真内核端到端）
npm run test:kernel    # NoName 内核 628 测试
```

## 设计哲学

NoName 的内核不可谈判（沙箱、审批、账本是物理强制，不是模型不做）；插件只是把它带进 DSH 的能力结晶。它不绕过 NoName 的任何保障——证据只增不改，记忆由人批准，品味不伪装成事实。

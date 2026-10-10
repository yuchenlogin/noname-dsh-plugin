# Changelog

## [0.4.1] - 2026-10-08

### Developer workflow

- 新增 `npm run sync:kernel`：从 `vendor/noname-harness` submodule 的精确 commit 导出 `vendor/noname_harness_pkg`，同步 `KERNEL_VERSION.txt`，支持 `--commit <sha|ref>`、`--dry-run`，并在需要提交 gitlink 时给出明确提示。
- 新增 `npm run check:kernel`：校验 submodule pin、快照内容和版本声明是否一致；快照漂移或手工伪造版本会失败，内核远端 main 领先时给出非阻塞提示。
- CI 在安装前运行快照检查，并对实际分发的 `vendor/noname_harness_pkg` 做 import smoke test；不再只测试插件并未使用的 submodule 目录。
- 支持 `NONAME_KERNEL_REPO=/path/to/NoNameAgentHarness` 作为离线开发/本地 checkout 的 fetch 源。

### Tests

- 覆盖快照版本伪造、快照内容篡改、submodule 落后、干净状态、dry-run 约束等场景；插件全套 41 项测试通过。
- 移除插件仓的 `test:kernel` npm script：它依赖未声明的 pytest，且会把内核平台测试与插件桥接 CI 混在一起。内核完整测试归 `NoNameAgentHarness` 仓库；插件仓的 `npm test` 负责桥接契约，CI 负责 vendored snapshot 检查与 import smoke。

## [0.4.0] - 2026-10-08

### Fixes（真机验收暴露的缺陷，来自"12 工具全量驱动 + 恶意账本"测试）

上一轮验收把 12 个工具逐条打穿，并直接驱动插件自己的 `runNoname` 喂进一本被篡改的账本，暴露出 4 个缺陷（3 个在 TS 层，1 个需要内核配合）：

- **[高] `noname_verify` 的 FAILED 分支是死代码**：内核 `verify` 的契约是"打印完整报告 JSON，然后 exit 1"；桥接层对非零退出码一律 reject，于是 `r.ok ? OK : FAILED` 里的 FAILED 分支永远不可达——`noname_verify` 精心构造的「本次用的是哪本账」（`describeLedgerTarget`，正是为修上一轮那次静默回落才加的）恰好在最需要它的时刻被丢弃。新增 `NonameRunOptions.acceptExitCodes`：调用方显式声明"这个退出码仍带有效 stdout"。`noname_verify` 现在报出 FAILED、账本位置、被改动的行 id 与哈希覆盖度；`pingKernel` 也改用它——此前账本损坏会被误报成「kernel unreachable」，怪错了对象。
- **[中] 报告 "bytes" 实际是 UTF-16 字符数**：`view.html.length` 对满屏中文的账本低估约 6%（实测报 39679，真实 42294 字节）。改为 `Buffer.byteLength(html, "utf8")`。读者无法信任的尺寸比不给尺寸更糟。
- **[中] `taste_review` 把"新 head id"埋在 JSON 里**：每次审核都会写一条**新的** head 记录并 supersede 旧的——`adopt` 如此，`pause` 也如此。返回原始 JSON 让这件事不可见：调用方采纳 `tst_A` 之后再暂停 `tst_A`，得到的是 "has been superseded"。现在输出首行显式给出 `head=<新 id> supersedes=<旧 id>`，并提示后续用新 id；JSON 仍附在后面。
- **[低] 空 text 被静默接受**：`noname_record` 把 `{"text": ""}` 原样交给内核，写下一行永远无法被召回的证据。适配层现在直接拒绝并说明原因（内核侧也加了同名守卫，见下）。
- **`noname_search` 描述补上检索语义**：中文/日文/韩文按子串匹配（多词 AND），其它语言按 token 匹配——模型需要知道"搜不到"和"没有记忆"不是一回事。

### Vendored kernel

- 内核快照推进到 `dd34e92`（NoName Agent Harness 0.34.0，schema v7 → v8），带来三项内核侧修复：
  - **完整性哈希覆盖溯源**：v1 的 content_hash 只签 `{event_type, payload}`，丢掉 append-only 触发器后改写 `session_id`/`seq`/`occurred_at`/`id` 仍报 `ok: true`——账本卖的就是可溯源，却恰好没签溯源。新增 `hash_version` 与版本化哈希（v2 覆盖整行）；v1 行按 v1 公式继续可验（append-only 不允许重写历史行），未知版本记入 `unverified_*_ids` 而非 `ok`。`verify` 同时返回 `hash_coverage`，如实披露还有多少行只有 payload 级签名。
  - **中文检索不再静默返回空**：FTS5 unicode61 把无空格的中文整段索引为单个 token，`"账本"` 匹配不到 `项目账本`，用户搜自己刚写下的词得到 `[]`。
  - **空事件守卫 + LIKE 通配符转义**（`100%` 不再命中全库）。
- **快照不再包含 `__pycache__`**：上一版 `git add vendor/` 把 31 个 `.pyc` 一起提交了。字节码不进版本库（`.gitignore` 已补），首次 import 会自行重编译。

### Tests

- 新增 5 项针对上述缺陷的回归测试：FAILED 分支可达且报出账本位置、健康账本披露覆盖度、空 text 被拒且不留痕、账本大小等于真实字节数、`taste_review` 首行给出新 head id（并证明旧 id 确实已失效）。
- 5 项在修复前全部失败，修复后全绿；另有 10 项针对已编译 `dist/` 的交付验收（修复前 8 项失败）。
- 全套 41 测试通过（真内核 dd34e92，内核 654 测试全绿）。

## [0.3.0] - 2026-10-08

### Features（账本粒度：一个工作区一本账）

旧行为是**全局一本账**：`dbDir` 默认 `$DSH_HOME/noname`，与工作区无关——所有项目、所有会话共用一个 db，只在 db 内部用 `session_id` 区分。用户要的是项目粒度：一个工作区共享一本账。

- **账本跟随 DSH 工作区**：会话 → 工作区的映射直接读 `ctx.workspaceRegistry`（`workspace.path` / `workspace.sessionIds`），落到 `<项目>/.noname/`（内核 quickstart 的同一约定，两个仓库都已 gitignore）。
- **内核边界改为真实项目根**：`init --root <项目>`，此前是 `--root <dbDir>`——导致接续包里写着 `Workspace boundary: /Users/…/.dsh/noname`，`snapshot` 也因为在不存在的 git 仓库里跑而永远 `git_available: false`。
- **解析不到工作区时不猜**：回落到旧的 `$DSH_HOME/noname`；多个工作区且无会话上下文时同样回落，绝不把一个项目的证据写进另一个项目的账本。
- 账本 HTML 落在 `<项目>/.noname/noname-ledger.html`：内核 `validate_workspace_path` 强制生成物在项目边界内，所以账本与它的产物必须同处项目内。
- `dbDir` 现在是**可选覆盖**：留空 = 工作区作用域；填了 = 固定目录（老行为，测试与自定义布局仍可用）。

### Fixes（真机调试暴露的三个缺陷）

- **内核目录在模块加载时冻结**：`resolveKernelRoot()` 的结果存成模块常量，模块一旦从随后被替换/删除的路径加载（插件管理器在 app 运行期间的 add/remove/add 循环），之后每次 spawn 都以 `ENOENT` 失败，而报错只提解释器、完全掩盖真因（用 `/bin/echo` 才反证出不是解释器的问题）。改为**调用时解析**并校验，缺包时抛 `kernel_missing` 并点名检查过的路径。
- **`ensureNonameInit` 把"db 文件存在"当成"已初始化"**：任何读命令（`verify`/`state`/`search`）都会先创建空 db 文件，于是 init 被跳过，之后 `record`/`package`/`extract`/`ledger` 全部报 `project is not initialized`。改为**总是调用幂等 init**（实测内核 init 幂等且不覆盖已有 project 行）。
- **spawn 失败信息无法区分病因**：`spawn <path> ENOENT` 分不清解释器还是 cwd，改为带上 `python/exists/cwd/cwdExists/pid`。
- **自动入账的会话归属落进共享桶**：DSH 0.2.0-rc.2 的 `ToolExecution` 没有顶层 `sessionId`，旧探针静默回落到 `dsh-unknown`，26 条真机事件全部记错归属——与"按真实会话 id 归属、绝不落共享桶"的承诺矛盾。旧测试恰好伪造了 `sessionId`，所以从没覆盖真机形状。已修正并补真机形状的回归测试。
- **会话 id 的真机取法（第二轮真机验证）**：`ToolExecution.agent` 的**类型声明**里有 `sessionId`，但真机对象上没有这个属性——DSH 官方代码（`dsh-tool-present`）一律用 `exec.agent.session`，`Session.id` 是 getter；全仓 `agent.sessionId` 出现 0 次。只读 `agent.sessionId` 会静默拿不到会话 → 账本无法定位工作区而回落全局账本、自动入账继续落 `dsh-unknown`。现在按 `agent.session.id` 读，并保留其它形状兼容。
- **回落不再无声**：`noname_verify` 现在报出本次调用用了哪本账以及为什么——`scope=workspace title=… session=…`，或 `scope=global (session groups under no workspace: registry=yes workspaces=2)`。一次静默回落过去看起来和"工作区作用域正常工作"完全一样，只靠事件条数才露馅。

### Tests

- 新增 `tests/workspace.test.ts`（9 项）：会话→工作区映射、多个工作区时拒绝猜测、显式 dbDir 覆盖、`<项目>/.noname` 落点、内核边界=项目根、**两个项目互不串账**、**同一工作区的两个会话共享一本账**、自动入账落到调用方项目的账本、账本 HTML 落在项目边界内、回落原因可读。
- 新增 `ensureNonameInit` 空 db 文件回归测试、真机会话形状（`agent.session.id`）回归测试。
- 全套 36 测试通过（真内核）。

## [0.2.0] - 2026-10-08

### Features（真实 DSH 宿主验证驱动）

在真实 DeepSeek Harness 0.2.0-rc.2 web profile 上做了端到端真机验证，修正了两个只有真机才能暴露的问题：

- **peerDependencies 版本兼容**：首版钉死 `dsh-tools@0.0.1-rc.1`，真实 DSH 运行时（0.2.0-rc.2）因 peer 版本不兼容**直接禁用插件**（`disabling profile plugin row`）。改为兼容范围（`cordis ^4.0.4`、`dsh-tools >=0.0.1-rc.1`），devDeps 对齐宿主配套版本（dsh-tools 0.2.0-rc.2 + cordis 4.0.4）——真机上插件不再被禁用，`kernel ready` 日志确认加载。
- **账本形态改为真机可达的 `noname_ledger` 工具**：真机实测发现 `sidebarRightTabs` 服务**不在 host 侧**（属 client-UI 侧，工具/服务插件无法触及），sidebar 面板无法从本插件注册。诚实地把账本从「宣称的 sidebar 面板」改为「真机可达的工具」——`noname_ledger` 调内核 `ledger-html` 生成五视图 HTML 返回路径。消除「未真机验证的面板」这一评审保留项。工具数 11 → 12。

### Design Rationale

- **为什么改工具而非继续等 sidebar**：评审者（DSH QA 96、架构 97）保留的分数都指向「sidebar keyed-slot 未真机验证」。真机验证给出决定性答案后，最诚实的选择是把账本做成真机上真实可达的形态，而不是留一个在真机里不存在的面板承诺。「界面是地图，但地图必须真实存在。」

### Notes & Caveats

- 真机验证（DSH web profile 启动日志）：插件加载、`kernel ready: context is an asset.`、**未被禁用**。
- `noname_ledger` 端到端验证：真实产出五视图 HTML（含 收件箱/状态/版本演进/因果图/时间线）。
- 25 测试全绿；CI 8/8 矩阵全绿。
- 若 DSH 未来暴露 host 可达面板 seam，`buildLedgerView()` 输出可直接接入。

## [0.1.2] - 2026-10-08

### Fixes（复审驱动：架构 74→90 后的剩余 10 分）

- **ingest 串行化**：并发 ingest 竞争 SQLite 单写者锁（`database is locked`）导致证据丢失 + dedup 测试确定性失败——用 promise 链串行化写入，证据流有序不丢；新增 `drainIngests` 测试钩子消除时序竞态。
- **截断标记**：ingest 500 字截断现在追加 `… [truncated, full in DSH transcript]`——「压缩可以有损，呈现不能撒谎」，读者知道这是采样（理念诚实的最后 1%）。
- **inFlight 死代码删除**：`index.ts` 的 AbortController 池从未接线到任何调用——卸载时 abort 一个永远为空的集合是谎言。删除并注释说明（sidecar 调用短生命周期 + 各自 abort 路径已覆盖），宣称的卸载行为与实现一致。
- **git 历史清洗**：filter-repo 移除首 commit 误提交的 node_modules/dist（.git 21MB → 904KB，1675 个对象清零），开箱体验干净。
- **分支与 CI 对齐**：master → main，与 workflow 触发器一致。
- **README**：补 license 章节（落实 CHANGELOG 0.1.1 的宣称）、iframe 表述对齐（渲染方式由宿主决定）。

### Notes & Caveats

- 24 测试全绿（ingest 5/5 稳定）；真实 Cordis Context 加载冒烟通过。
- 剩余已知边界（诚实标注、非缺陷）：sidebar keyed-slot 正文待真实 DSH 宿主验证、审批门/品味卡图像/多 profile 账本属后续版本。

## [0.1.1] - 2026-10-08

### Fixes（三方评审驱动：DSH 官方 QA 28/100、GitHub 开发者 52/100、架构 74/100）

本轮修复全部针对「宣称与实现断裂」——对一个把诚实刻进账本哲学的系统，这类问题比 bug 更重。

**加载阻断级（critical）**
- **inject 缺失即崩**：`apply()` 在真实 Cordis `Context` 下访问未注入的 `ctx.sidebarRightTabs` 直接抛错，整个插件加载失败（stub 测试给了虚假信心）。修复：sidebar 访问收进 `ctx.effect` 内做防御性降级，真实 Cordis 加载冒烟验证通过（apply OK、11 工具注册、kernel ping 成功）。
- **dbDir 默认值空头支票**：README 承诺 `$DSH_HOME/noname`，实现是 `?? ""`——零配置下每次调用都 `mkdir("")` ENOENT。修复：`resolveConfig` 在单一边界落地真实默认值（`$DSH_HOME/noname` 或 `~/.dsh/noname`），绝不写宿主 CWD 或内核 submodule。
- **Git 安装路径必坏**：`main` 指向 `dist/` 但无 `prepare`。修复：补 `prepare`/`prepack` 自动 build，git 安装可直接加载。

**协议/理念不符（high）**
- **sidebar 注册凭空猜 API**：改为按真实契约（静态定义 `sidebarRightTabs.register` + 明确标注 keyed-slot 正文需真机验证），不再假装有 `render()` 全功能。
- **Config 假 zod**：接真实 `@deepseek-ai/schemastery` schema，加载边界校验，非法值响亮报错。
- **ingest 丢溯源**：所有事件灌进硬编码 `session "dsh"`，抹掉谱系——改为按真实 DSH 会话 id 归属（`exec.sessionId`）。
- **ingest 静默丢证据**：catch 后无声——改为 warn + 计数 + 重入队（失败不标记 seen），「绝不影响宿主」但「绝不无痕丢失」。
- **假 verbose 档**：`minimal`/`verbose` 行为全同——删除该枚举，只保留真实的一档。
- **品味只铺单轨**：补 `taste_propose`（adopted 轨，模型从「眼前一亮的时刻」发起）、`taste_review`（生命周期）、`card_queue`（复核队列）——双轨与复核链完整。
- **HTML 只增不减**：`noname-ledger-${Date.now()}.html` 无限堆积——改为固定文件名 + `--overwrite` 覆盖写。

**工程基建（GitHub 开发者视角）**
- 补 MIT LICENSE（内核 license 兼容性在 README 标注）；package.json 补 repository/bugs/author/homepage/engines；files 补 README/LICENSE/CHANGELOG。
- 新增 `.github/workflows/test.yml`：2 OS × 2 Node × 2 Python 矩阵，插件测试（真内核）+ 内核 628 测试。

### Design Rationale

- **为什么 sidebar 降级而非硬凑**：真实 keyed-slot + useTabInfo 契约无法在无 DSH 宿主时验证；与其交付一个「画出来的地图」，不如按文档化的静态定义注册并诚实标注待真机验证——界面是地图，但地图不能是画的。
- **为什么 ingest 失败要 warn 而非静默**：「绝不影响宿主会话」与「绝不无痕丢证据」不矛盾——一个以「证据不丢」为存在理由的系统，丢证据时必须留痕。

### Notes & Caveats

- 测试 17 → 24 全绿（新增 ingest 诚实性 4 项 + 插件加载面 3 项）；真实 Cordis `Context` 加载冒烟通过。
- 待办：git 历史清洗（首 commit 误含 node_modules，发布前 filter-repo）、推送远端激活 README 链接、sidebar keyed-slot 真机验证。

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

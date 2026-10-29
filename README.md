# NoName for DeepSeek Harness

**让下一次对话，接得上这一次的判断。**

NoName 为 DSH 工作区带来可追溯的项目记忆：决定与理由、当前进度、经过确认的品味，以及能看懂、能审核的本地账本。项目沿用 [NoName Agent Harness](https://github.com/yuchenlogin/NoNameAgentHarness) 的记忆基座，以 TypeScript 插件连接本地 Python sidecar。

**生成前写作偏好**默认开启：每次模型请求都带上结论先行、限定具体、保留关键证据的规则，减少重复免责和惯性转折。规则与 NoName 主仓共用，不依赖模型选择，也无需先调用记忆工具。

正常工作时留下来路；重要阶段提出少量候选；你确认后，下一次上下文包带上当前版本。**工具结果留证，不等于长期记忆。**

## 快速开始

需要带 `@deepseek-ai/dsh-system-prompt` 0.2.0-rc.2 兼容接口的 DeepSeek Harness、Node.js 18+ 与 Python 3.10+。Python 基座仅使用标准库与 SQLite，无需另配模型 API key。

```sh
git clone --recursive https://github.com/yuchenlogin/noname-dsh-plugin.git
cd noname-dsh-plugin
npm install
npm run build
dsh plugin --profile web add /absolute/path/to/noname-dsh-plugin
```

从源码安装时明确运行 `npm run build`，让 `dist/` 对应当前 TypeScript。DSH 安装与 profile 的更多说明见 [DSH 官方指南](https://deepseek-harness.github.io/deepseek-harness/guide/quickstart)。

启用插件后，在 DSH 项目工作区中试一次：

1. 让 agent 调用 `noname_package` 查看当前项目。初次使用会创建空账本，空记忆不会伪装成已经记住了什么。
2. 正常讨论一个决定，例如“首版用 SQLite，因为只在本地运行”。让 agent 用 `noname_record` 留下决定和理由，取得 `evt_…` 来源 ID。
3. 用 `noname_remember` 提出候选：稳定键 `storage`、人能读懂的决定、来源 ID 和保留理由。
4. 调用 `noname_ledger`，打开输出的 `live ledger` URL，查看来源并签署。
5. 在下一个会话让 agent 调用 `noname_package`，确认已批准内容与来源出现在上下文包中。

记忆审核在本地网页由人签署。日常继续工作时，agent 可以按工具描述选择性地记录、提议和组装；插件没有后台 LLM 自动提炼服务，也不保证宿主会在每次会话开始时自动调用 `noname_package`。

## 哪些值得记住

写作偏好在生成前由 `systemPrompt.section()` 自动加入；下文的项目记忆继续按证据与审核流程管理。

| 内容 | 合适的去处 |
|---|---|
| 项目约束、关键决定及原因 | `noname_remember` 的 `high` 候选，确认后成为项目记忆 |
| 阶段成果、阻塞与下一步 | `mid` 候选，保留可继续工作的最小信息 |
| 用户明确表达的判断与例子 | 自述品味；必须来自用户明确要求 |
| 从模型表现中观察到的喜好 | 品味候选，用户明确采纳后生效 |
| 普通工具输出、暂时想法、每一轮聊天 | 需要时留作证据，不逐条变成长期规则 |

决定改变时，复用同一个逻辑键。例如 `storage` 从 SQLite 修正为 PostgreSQL；新候选确认后取代旧的当前版本，旧决定保留在来路里。使用 `storage-v2` 会把同一件事拆成两个主题。

品味是软影响，不是事实或永久命令。`user` 范围目前也保存在当前账本内，尚未自动跨项目同步。

## 能读懂的交互式账本

![NoName 本地账本：当前决定、工作接续与需要确认的变化](docs/assets/ledger-overview.png)

`noname_ledger` 返回本机审核网页与离线 HTML 路径。网页与 NoName 主仓共享渲染逻辑：

- **此刻**：当前决定和工作接续。
- **等你判断**：候选、理由、来源与确认后的变化。
- **品味与偏好**：明确区分自述与待采纳的观察。
- **来路与变化**：修正链及审核来源。
- **活动档案**：按需展开原始事件与技术信息。

项目决定与工作进度支持接受、拒绝、暂缓、修改后接受；品味与卡片遵循各自的审核生命周期。签署人和理由写入同一本账。离线快照只读，审核请使用 `live ledger` 链接；静态文件导出失败时，工具会提示警告，本地审核页仍可使用。

服务仅监听 `127.0.0.1`，完整 URL 带访问令牌，不要公开分享。相同插件上下文和账本会复用服务，插件卸载时停止；一小时无活动后自动退出，再次调用工具可获得新链接。

远程 DSH 需将返回 URL 的端口转发到本机**相同端口**，保持 `127.0.0.1`、端口和令牌路径。例如：

```sh
ssh -L 45678:127.0.0.1:45678 your-host
```

浏览器审核没有多人账号系统，签署人是本机审计标签。当前 DSH 的 host 插件不能注册 client 的 `sidebarRightTabs`；账本通过浏览器入口打开。完整使用说明见 [账本指南](https://github.com/yuchenlogin/NoNameAgentHarness/blob/main/docs/ledger.md)。

## 配置与数据位置

| 字段 | 默认 | 说明 |
|---|---|---|
| `pythonPath` | `python3` | 本机 Python 解释器；建议用绝对路径 |
| `dbDir` | 空 | 默认使用会话所属工作区的 `<项目>/.noname`；指定后固定为该目录 |
| `autoIngest` | `true` | 将宿主工具结果写入证据流；跳过 NoName 自身读取和投影工具，避免回声与审核 URL 留档 |
| `timeoutMs` | `30000` | 每次 Python sidecar 调用超时，单位毫秒 |
| `writingStyle` | `direct` | 生成前注入直接写作规则；设为 `off` 可关闭 |

一个工作区一本账，多会话共享，通过真实 `session_id` 区分来源。路径由 DSH 的 `workspaceRegistry` 解析；无法获得工作区时回落到 `$DSH_HOME/noname`，不会猜测其他项目。HTML 放在工作区边界内的 `.noname/noname-ledger.html`。建议把 `.noname/` 加入 `.gitignore`。

默认隔离单位是工作区路径，不能假设 profile 必然隔离：若不同 profile 指向同一个工作区，也可能使用同一账本。需要独立数据时显式配置不同 `dbDir`。

### 写作偏好

在 DSH 的 NoName 插件配置中设置 `writingStyle: direct` 或 `writingStyle: off`，由宿主保存配置并重载插件。该设置作用于插件所在的服务范围，独立于项目账本；`autoIngest: false` 不会关闭写作偏好。升级保留显式的 `off`，省略该字段时使用 `direct`。

插件在加载时从内核快照读取 `noname_harness/policies/direct-writing.md`，注册具名 system prompt section；每次模型请求组装都使用它，卸载或重载时由 Cordis 撤销旧注册。切换模型、推理档位或工作区无需重设。生成结果原样返回，规则保留必要否定、统计不确定性、不利结果与当前用户指定格式。

宿主显式使用 `complete` 全量系统提示时，会覆盖其他 sections，包括此规则。停用后新请求不再加入规则；新聊天可避开已有历史中的旧规则。服务接口变更由加载依赖与集成回归检查；文风效果需结合具体模型任务验证。

共享规则、Codex 设置和独立原型迁移见 [NoName 写作偏好说明](https://github.com/yuchenlogin/NoNameAgentHarness/blob/main/docs/output-preferences.md)。插件使用固定内核快照，更新规则后运行 `npm run sync:kernel -- --commit <kernel-sha>`、构建并重载。

## 工具参考

| 工具 | 用途 |
|---|---|
| `noname_record` | 留下有用的决定、发现或约束作为证据；返回来源事件 ID |
| `noname_remember` | 由证据提出项目决定或工作进度候选；不会自动批准 |
| `noname_package` | 组装项目记忆、偏好与相关证据，供新会话接续 |
| `noname_search` | 查找证据与记忆 |
| `noname_state` | 查看已批准状态；可选 `layer`，不填则返回 high 与 mid 两层 |
| `noname_inbox` | 查看候选及其来源 |
| `noname_taste_add` | 用户明确自述的品味，直接激活；不能用来写模型推断 |
| `noname_taste_propose` | 由模型表现提出待采纳品味 |
| `noname_taste_review` | 品味 adopt / edit / pause / resume / retire；须有用户明确决定 |
| `noname_card_queue` | 查看需要复核的品味卡 |
| `noname_ledger` | 获取本地可审核页面和离线快照 |
| `noname_verify` | 校验账本完整性及哈希覆盖范围 |
| `noname_extract` | 默认预览抽取结果；显式 `create_candidates: true` 才创建待审候选 |

`noname_remember` 的必填参数是 `layer`（`high` / `mid`）、稳定主题 `key`、可读 `text`、一个 `source_event` 事件 ID，以及说明为何值得保留的 `reason`。可选 `next_step` 保存接续指针。先记录再引用真实 ID，不编造证据。相同的已生效内容返回 `unchanged`，相同的最新待审内容返回 `already_pending`，避免重复提议。

品味审核会生成新记录并替代旧 head。后续操作请使用工具返回的 `head=<新 id>`；旧 ID 会报 `has been superseded`。`edit` 必须携带 `content` JSON string；它保留版本链，不直接改历史。

## 信任与现有边界

插件是记忆与证据适配层。DSH 继续负责模型、工具执行、权限和调度；NoName 独立 runtime 的执行审批门与沙箱不会自动接管 DSH。

`autoIngest` 捕获工具结果，不是全量聊天归档。长输出保留受限摘要；`noname_record` 用于主动保存有价值的来源。NoName 读取和投影结果不再重复写入，记忆写操作仍留证用于审计。自动留证、提出候选、人工确认是三个不同阶段。

完整性校验按每行写入时的哈希版本验证。较新 v2 哈希覆盖整行溯源字段；旧 v1 仅覆盖 payload，不能发现所有元数据篡改。`noname_verify` 的 `hash_coverage` 如实报告覆盖范围。数据在本机，不表示它自动加密或替你脱敏。

## 两个仓库如何一起更新

[NoName Agent Harness](https://github.com/yuchenlogin/NoNameAgentHarness) 维护 Python 内核，包括记忆、证据、审核、共享账本展示和写作规则；本仓维护接入 DSH 的 TypeScript 插件，包括工具注册、工作区映射、配置和生成前注入。**插件固定使用内核的一个提交；内核主仓更新后，由维护者选择版本、同步、适配并发布插件。** 两个项目的版本号独立，完整提交 SHA 确定实际使用的内核。

### 固定版本与随包快照

| 位置 | 作用 |
|---|---|
| `vendor/noname-harness` | 指向内核仓的 Git submodule；插件提交记录它的固定 SHA，供同步与校验使用 |
| `vendor/noname_harness_pkg/noname_harness` | 从该 SHA 导出的 Python 包；随插件分发，是正常运行时优先加载的内核 |
| `vendor/noname_harness_pkg/KERNEL_VERSION.txt` | 同步脚本生成的完整 SHA 与源码链接，方便查看版本来源 |
| `src/` → `dist/` | DSH 接入代码及构建产物；`dist/` 随插件分发并提交到仓库 |

因此，只在独立的 NoName 主仓执行 `git pull`，已安装的 DSH 插件仍运行原有快照；只移动 submodule 的版本，也还需要重新导出快照。运行时在快照缺失时才回退到开发用 submodule。随包快照让 npm/git 安装即使没有初始化 submodule，也能取得内核。

之前已在 [`d0b89e1`](https://github.com/yuchenlogin/noname-dsh-plugin/commit/d0b89e1) 加入 `sync:kernel` 和 CI 一致性检查，随后在 [`0481777`](https://github.com/yuchenlogin/noname-dsh-plugin/commit/0481777) 明确测试职责：内核完整测试在主仓运行，插件仓测试实际分发快照与 DSH 接口。现有自动化负责选定版本后的导出与校验；目前没有由内核提交自动触发插件升级或适配的流水线。

### 本次 0.7.0 更新了什么

| 插件版本 | 固定内核 | 更新内容 |
|---|---|---|
| 0.6.0 | [`a0673a9`](https://github.com/yuchenlogin/NoNameAgentHarness/commit/a0673a9d7181c030ba8c2de415451f3a4dbb6060) | NoName 0.37.0 的记忆体验与本机账本服务修复 |
| **0.7.0（当前）** | [`c95cae0`](https://github.com/yuchenlogin/NoNameAgentHarness/commit/c95cae017744efceb9677994b26fed930a1af3e4) | NoName 0.38.0 的共享写作规则，以及 DSH 的生成前注入适配 |

本次先完成并推送内核，再把插件 submodule 和随包快照同步到 `c95cae017744efceb9677994b26fed930a1af3e4`。插件侧另外增加 `writingStyle` 配置、`systemPrompt.section()` 注册与对应宿主依赖，构建 `dist/`，更新插件版本到 0.7.0 后提交推送。写作规则保存在内核，DSH 负责把规则送入模型请求。

按改动类型决定适配范围：

| 内核或宿主变化 | 插件侧工作 |
|---|---|
| 内部修复，现有 CLI、返回格式和资源路径保持兼容 | 同步快照，构建并验证插件 |
| 新增能力，或命令、参数、返回格式、资源路径发生变化 | 同步快照，同时修改相关工具、配置或注入代码，再验证 |
| DSH 宿主服务接口或版本要求变化 | 检查并调整 TypeScript 接入与依赖声明，再用宿主接口验证 |

### 维护者升级步骤

1. 在 NoName 主仓完成内核改动、运行内核测试、提交并推送；记录目标提交的**完整 40 位 SHA**。发布用的提交应能从内核远端取得。
2. 在插件仓同步目标内核。下面以本次版本为例，下次升级时替换 SHA：

   ```sh
   git submodule update --init vendor/noname-harness
   npm ci
   npm run sync:kernel -- --commit c95cae017744efceb9677994b26fed930a1af3e4
   ```

   脚本会 fetch 目标提交、让 submodule 固定到该提交，再通过 `git archive` 导出已提交的 `noname_harness/`。它会同步删除上游已移除的包文件，并重写 `KERNEL_VERSION.txt`；内核工作区尚未提交的修改不会进入快照。

3. 根据上表完成插件接口适配；需要发布新版本时，同步更新 `package.json`、`package-lock.json` 和 `CHANGELOG.md`。随后运行：

   ```sh
   npm run build
   npm test
   npm run check:kernel
   git diff --submodule=log
   ```

4. 将 **submodule 固定版本与整个快照一起提交**。本次涉及的 `src/`、`dist/`、版本文件和说明文档也应一并提交。仅同步内核时，可使用：

   ```sh
   git add vendor/noname-harness vendor/noname_harness_pkg
   git diff --cached --submodule=log
   git commit -m "chore: sync NoName kernel"
   git push origin main
   ```

5. 检查插件仓 CI；使用者更新已安装的插件并重载 DSH 后，新的内核和接入代码一起生效。从本地源码使用插件时，拉取插件仓后运行 `npm ci`、`npm run build`，再让 DSH 加载更新后的插件。

### 同步命令与检查结果

| 命令或结果 | 含义 |
|---|---|
| `npm run sync:kernel -- --commit <完整 SHA>` | 获取并固定到明确提交，然后重新导出快照；发布时推荐这种方式 |
| `npm run sync:kernel -- --commit main` | 获取执行时的上游 `main`，并固定到当时解析出的 SHA；以后更新仍需再次同步 |
| `npm run sync:kernel` | 从 submodule 当前 HEAD 导出；不会主动选择上游最新版本 |
| `npm run sync:kernel -- --dry-run` | 预览当前 submodule HEAD 与快照的内容差异；需先初始化 submodule，单独使用，不与 `--commit` 合用 |
| `npm run check:kernel` | 校验版本记录、submodule 当前 HEAD 与快照内容；需已初始化 submodule |
| `STALE` | 版本记录或快照不一致，检查失败，需修复后提交 |
| `NOTE … upstream main is … ahead` | 上游有新提交；固定的旧版本仍可通过检查，由维护者决定何时升级 |

`check:kernel` 在成功获取上游信息时报告版本落后；它不执行升级。检查通过也要把 submodule 变更加入提交，否则其他人检出仓库时仍会拿到旧的固定版本。

插件 CI 在 Linux/macOS、Node.js 18/20、Python 3.10/3.12 共 8 种组合中运行快照检查、构建、插件回归与分发内核的导入检查。内核完整测试继续由 NoName 主仓负责。共享内核逻辑在主仓修改，再经同步脚本进入插件。

产品初心见 [NoName 愿景](https://github.com/yuchenlogin/NoNameAgentHarness/blob/main/docs/vision.md)，版本变化见 [CHANGELOG](CHANGELOG.md)。本插件采用 [MIT](LICENSE)；内核遵守其原仓库许可。

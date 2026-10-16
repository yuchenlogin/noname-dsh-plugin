# NoName for DeepSeek Harness

**让下一次对话，接得上这一次的判断。**

NoName 为 DSH 工作区带来可追溯的项目记忆：决定与理由、当前进度、经过确认的品味，以及能看懂、能审核的本地账本。项目沿用 [NoName Agent Harness](https://github.com/yuchenlogin/NoNameAgentHarness) 的记忆基座，以 TypeScript 插件连接本地 Python sidecar。

正常工作时留下来路；重要阶段提出少量候选；你确认后，下一次上下文包带上当前版本。**工具结果留证，不等于长期记忆。**

## 快速开始

需要 DeepSeek Harness、Node.js 18+ 与 Python 3.10+。Python 基座仅使用标准库与 SQLite，无需另配模型 API key。

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

一个工作区一本账，多会话共享，通过真实 `session_id` 区分来源。路径由 DSH 的 `workspaceRegistry` 解析；无法获得工作区时回落到 `$DSH_HOME/noname`，不会猜测其他项目。HTML 放在工作区边界内的 `.noname/noname-ledger.html`。建议把 `.noname/` 加入 `.gitignore`。

默认隔离单位是工作区路径，不能假设 profile 必然隔离：若不同 profile 指向同一个工作区，也可能使用同一账本。需要独立数据时显式配置不同 `dbDir`。

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

## 开发与内核快照

```sh
npm run build
npm test
npm run check:kernel
```

运行时使用 `vendor/noname_harness_pkg/` 快照；`vendor/noname-harness` submodule 和 `KERNEL_VERSION.txt` 提供可追溯来源。正常体验改进应在插件或共享展示层完成，不复制内核审核规则。

0.6.0 的基座固定为 [`a0673a9`](https://github.com/yuchenlogin/NoNameAgentHarness/commit/a0673a9d7181c030ba8c2de415451f3a4dbb6060)，包含 macOS 本地服务启动修复。共享展示层已从该提交导出，submodule pin、`KERNEL_VERSION.txt` 与运行快照保持一致，`npm run check:kernel` 已通过。构建、66 项插件回归和独立安装验证也已通过。

需要主动升级基座时：

```sh
npm run sync:kernel -- --commit <sha>
npm test
npm run check:kernel
```

`--dry-run` 只预览；不带 `--commit` 时从 submodule 当前精确 HEAD 导出。不要手工混合多个提交的快照。发布前应让源码、快照和版本声明一致；内核测试在主仓运行，插件测试验证实际 Python CLI 与 TypeScript 的桥接。

产品初心见 [NoName 愿景](https://github.com/yuchenlogin/NoNameAgentHarness/blob/main/docs/vision.md)，版本变化见 [CHANGELOG](CHANGELOG.md)。本插件采用 [MIT](LICENSE)；内核遵守其原仓库许可。

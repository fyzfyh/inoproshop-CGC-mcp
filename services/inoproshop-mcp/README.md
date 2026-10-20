# PLC 智能工程助手｜维护资料

本文面向开发、部署与维护人员。客户评估请阅读[产品首页](../../README.md)及[客户使用与评估手册](docs/OFFLINE_README.md)。

软件能力与既有接口不因 v2026.10.20.1 文档修订而改变；下方保留原有技术安装、验证与恢复资料，其中发行文件名和路径属于历史记录，当前交付以产品首页为准。

---

# InoProShop MCP 本地控制服务

## 2026-10-20 工程能力扩展

当前提供 26 项工具。新增 `replace_st_sections` 受控声明/实现编辑、`ino_list_project_templates` / `ino_create_project` 模板目录与原生控制器复核，以及不依赖图数据库的 `context_search_text`。保留原有预览、编译、检查点恢复及未知结果不重放机制。使用与验收边界见[扩展说明](docs/ENGINEERING_EXTENSIONS.md)。下文首版 23 工具的记录保留为历史基线。

这是已有 Demo 宿主能力的正式 MCP 入口：OMP 负责模型和对话，服务负责明确的工程身份、快照、作业与工具契约，InoProShop 负责工程读写和原生编译。该服务不连接 PLC，不提供下载、强制或在线修改。

当前发布 26 项工具，接口见 `config/tools.schema.json`。服务只监听 `127.0.0.1:8765/mcp`，使用官方 Python MCP SDK 1.30.0 的 Streamable HTTP。已安装 OMP 18.8.7 的原生 HTTP 客户端可直接连接，不需要每个聊天启动一个写入器或 stdio 代理。

## 安装与启动

当前完整离线包为 `InoProShop-MCP-Offline-Windows-x64-20261020.zip`，已带 Python、全部锁定依赖、Java 和 Neo4j，无需依赖下载。完整解压后运行根目录 `Connect-Ino.cmd`；离线安装、三项新功能、本地模板登记及旧版升级见[离线使用说明](docs/OFFLINE_README.md)。下述 Python/uv 安装流程适用于源码精简包。

非技术用户可从完整发布包根目录运行 `Connect-Ino.cmd`。它自动准备固定依赖、数据库、MCP 和 IDE 桥接，配置并打开 OMP；后续重复执行会复用环境。`Connect-Ino.cmd -Check` 只诊断，`-NoOmp` 只准备连接及配置。详见[一键接入说明](../../CONNECT-INOPROSHOP.md)。下述独立脚本继续保留供开发与单独维护使用。

环境：Windows、Python 3.11–3.13、uv、InoProShop 1.10.0.3 / CODESYS 3.5 SP11；宿主桥接仍使用厂商已有 IronPython 2.7 入口。现代 Python 依赖仅装在本目录 `.venv`。

客户端实测组合为 Bun 1.4.3、OMP 18.8.7（`@oh-my-pi/pi-coding-agent@18.8.7`）。服务安装脚本不修改用户已有 OMP 或模型账户；需要复现客户端版本时，在用户选定的环境安装该固定版本，并先完成模型配置。

```powershell
cd E:/AI-PLC/services/inoproshop-mcp
./scripts/Install.ps1
./scripts/Start-Service.ps1
```

`Install.ps1` 用 `uv.lock` 固定完整服务、测试与 CGC 依赖，再运行本地测试；`-WithoutGraph` 只安装读取/宿主部分，图工具届时会明确受阻。正常桌面进程需要访问本机回环端口；受限执行环境的套接字拒绝不等同于服务故障。服务保持运行，前台启动时按 Ctrl+C 退出；无需管理员权限，不修改安装目录。

默认数据位置：

| 内容 | 位置 |
|---|---|
| 持久控制状态、作业、快照、备份与导出 | `E:/AI-PLC/work/mcp_p0/state` |
| 允许写入的工程副本根目录 | `E:/AI-PLC/work/mcp_p0/projects` |
| 本机 token | `E:/AI-PLC/work/mcp_p0/state/token` |
| 实测报告 | `E:/AI-PLC/artifacts/mcp_p0` |

路径默认随源码解压根目录计算，上表是本机示例；CLI 还支持 `INO_MCP_WORKSPACE`、`INO_MCP_STATE`、`INO_MCP_PROJECTS` 环境变量。可用 `Start-Service.ps1 -StateDir ... -ProjectRoot ... -Port ...` 切换独立测试环境。**同一个 state 目录只能运行一个服务。** 服务先取得操作系统文件锁再初始化 Core；重复启动会退出，不会对账或更改正在运行的作业。

token 首次生成后复用，不写入配置或日志。Windows 下其文件 ACL 只授予当前账户与 LocalSystem。跨用户连接不是首版能力；切勿把 token 放入命令行、截图或源代码。HTTP 同时检查回环来源、Host、Bearer token 与 Origin；OMP 无 Origin 的本地请求正常支持，浏览器的任意来源不能调用该服务。没有反向代理部署模式。

## 加载宿主桥接与选择工程

先按[宿主桥接说明](../../adapters/inoproshop/bridge/README.md)在预期 InoProShop 实例中加载桥接，state 目录必须与服务一致。Bridge 加载后会注册本轮 IDE 的 `host_id`、`host_instance_id` 与心跳；发现 Windows 进程并不代表桥接已加载。所有工程写入仅允许在工程副本根目录内。

通过工具依次发现宿主、发现工程、采集快照、读取对象：

1. `ino_list_hosts` → 注册且活跃的实例。
2. `ino_list_projects` → 明确工程与宿主绑定；必要时 `ino_open_project` 创建并打开新副本。
3. `ino_capture_snapshot(project_id)` → 固定 `snapshot_id`。
4. `ino_read_object(project_id,snapshot_id,object_id)` → 真实 GUID、区段、源码与覆盖。

加载桥接后的原生验证由 `scripts/native*` 完成，具体入口随桥接文档交付。不要直接在服务之外初始化另一份 Core 来操作相同 state；使用 MCP 工具完成集成调用。

## OMP 配置

```powershell
./scripts/Start-OMP.ps1
```

此入口仅在默认独立目录 `work/mcp_p0/omp/.omp` 合并 `inoproshop` 连接并复制 PLC Skill，保留已有其他服务配置；用户全局 OMP 配置保持原样。token 以本次 OMP 子进程的环境变量 `INOAI_MCP_AUTHORIZATION` 传递，连接配置仅保存变量名称。可通过 `-Workspace` 指定另一个用户授权的工作目录。

配置样例为 `config/omp.mcp.example.json`。在 OMP 内可用 `/mcp list`、`/mcp test inoproshop` 核对连接。读取演示请求：`读取已绑定工程的 PLC_PRG，并解释源码来源及读取覆盖；先不要改代码。` 编译需显式目标工程，长期操作返回 operation_id 后继续查询。模型访问设置由 OMP 管理，服务不存储模型密钥。

## 核验与记录

```powershell
.venv/Scripts/python.exe -m pytest tests -q
.venv/Scripts/python.exe scripts/protocol-smoke.py
bun scripts/omp-smoke.ts
.venv/Scripts/python.exe scripts/evaluate.py --output E:/AI-PLC/artifacts/mcp_p0/evaluation_fixture_validation.json
```

协议脚本调用正在运行的真实 Core；加 `--project-id <已绑定ID>` 可继续经 MCP 采集原生快照和读对象。`omp-smoke.ts` 导入**已安装 OMP 包自身**的 MCP 客户端，不使用手写 OMP 替身；默认包路径可用 `--package-root` 修改。这些脚本不调用模型，报告明确记录 `model_invoked=false`。其通过仅证明客户端/协议层，不能代替原生工程写入与编译验收。

`fixtures/evaluation` 含 26 个合成脱敏任务和预期断言，包含 2 个 FB 创建及完整调用映射任务。`evaluate.py` 只校验固定任务集和统计已有真实记录；未运行的模型指标返回 null。

真实模型生成入口如下，需事先在 OMP 配置指定模型：

```powershell
.venv/Scripts/python.exe scripts/run-model-evaluation.py --output <本次独立结果目录> --model <供应商/模型>
```

运行器使用已安装 OMP SDK、`toolNames=[]`、`restrictToolNames=true`、`enableMCP=false` 和空上下文，模型只能看到合成输入；没有权限自由读工程或执行工具。每题保存原始回答、用量、耗时、候选差异和退出原因。原生驱动另行装载合成 fixture 并经 MCP 应用已验证候选；不是把模型回答当作编译证据。

另有 `scripts/omp-native-tool-worker.ts` 的受限模型工具验证，由根目录 `scripts/native_live_omp_stage.py` 在合成 fixture 已装载时启动。它仅开放 5 个显式自定义工具，通过已安装 OMP MCP 客户端转发到公开服务；固定工程与对象、核验源码后只返回合成内容，预览只能接受该题明确授权的改动，应用最多一次。此入口检验模型主动读取、预览、应用、查询诊断的链路；不能用无工具生成记录代替其验收，也不能将完整工程快照回传给模型。

修复用 `--repair-round 1` 或 `2`，必须指定 `--tasks` 和 `--diagnostics-dir`。契约拒绝可先加 `--prepare-contract-feedback` 生成绑定原响应 SHA 的反馈；编译修复要求真实原生诊断，并按 `synthetic_object_map` 限制为合成对象的错误消息，不向模型传完整工程快照。运行器拒绝第 3 轮、覆盖已有轮次和无证据修复；首次契约率、首次编译率及两轮内完成率独立计算。

`evaluate.py --results <模型结果目录> --native-evidence <真实原生验收的 model_native/summary.json>` 可汇总编译证据；它校验候选文件 SHA、合成 fixture SHA 和公开 MCP 调用链，保留原始模型记录，并单独报告实际参与编译的题号及分母。尚未编译的题目不计入编译成功率。

汇总还区分每轮模型工作进程的耗时、原生编译耗时和整个原生阶段耗时。只有模型候选与原生编译快照中对应合成对象的源码逐字节一致，才将候选人工改动量记为 0；证据不足或不一致记为 null。夹具的程序名和 I/O 占位声明适配另列，语义未完成任务的人工业务修正量保持 null。

任务意图审阅与编译验收独立记录。可编译但擅自改变业务条件、阈值或输出赋值的候选不计为任务完成；本版评估建立真实基线，不预设通过百分比。`tests/test_server_process.py` 的 7 个真实控制器进程强退点和 1 个 MCP 会话断线案例使用明确标记的假宿主与本地 IR，只证明作业持久化及不重放机制，不能替代原生 IDE 或 Neo4j 故障证据。

图索引由真实 CGC/Neo4j 后端承担，部署与验收见 [图后端说明](docs/GRAPH_BACKEND.md)，固定版本见 `cgc-runtime-lock.json`。`Start-Service.ps1` 可从本机 `work/mcp_p0/neo4j/credentials.json`（或 `-Neo4jCredentials`）读连接信息，仅注入服务进程环境；缺失则沿用已有 `NEO4J_*` 环境变量。图后端不可达须返回明确阻塞，不能把本地 IR 测试伪装为 CGC 集成通过。

## 源码交付包

`scripts/build-release.py` 生成固定时间戳的源码 ZIP、逐文件 SHA-256 清单和校验报告。保留压缩包中 `services`、`adapters`、`demo`、`scripts` 的相对布局，然后执行服务安装脚本。包内包含宿主桥接依赖的 `demo/v2/ai_package.py`、`diagnostics.py`、`core.py` 等源码；单独服务 wheel 不含完整宿主部署，不能仅装 wheel 就声称可连接 IDE。

交付包排除虚拟环境、运行时、凭据、token、work 状态、原始 `.project` 工程和测试输出。原生验收仍需本机安装厂商软件与其合法示例工程；历史真实工程回归在源码包不带该资料时显式 skipped，合成金标准和服务测试仍可独立运行。

安装脚本支持 `-TestReport <绝对路径>`，将本次安装后的同一次测试写为 JUnit 报告；最终交付检查应同时保存包 SHA-256 与该报告，避免把旧源码包的测试结果算到新包。

Windows 上的路径长度限制会影响深层目录中的快照和临时文件。安装测试使用系统临时目录下新建的短随机路径，不复用或删除其他测试目录，也不改变服务运行状态目录。此调整只解决安装测试的目录叠加；运行时仍应选择较短的源码、状态和工程副本路径，必要时通过 `Start-Service.ps1 -StateDir <短路径> -ProjectRoot <短路径>` 设置。不要把安装测试通过解释为任意长路径均受支持。

完整范围以包内[需求基线](../../docs/requirements/PRD-InoProShop-MCP.md)为准，逐项证据及未完成项见[验收矩阵](docs/ACCEPTANCE_MATRIX.md)。

## 修改和恢复规则

`ino_preview_change` 返回不可变 patch_id、patch_hash 和 diff。修改只能经 `ino_apply_change`，带明确用户任务授权与幂等键；其流程包含原文检查、备份、原生回读、编译与条件恢复。应用与保存分开报告：内存修改成功不等于已经保存。

严格模式要求原始编译通过；修复模式须显式声明，保留原始诊断与目标错误改善。读取作业返回 `state=FAILED` 时，MCP 查询本身可以成功，但原作业失败必须向用户说明。没有编译完成依据时状态为 unknown。

主机退出、服务异常终止或写入响应丢失后，先启动同一 state 的唯一服务、读取作业状态及实时宿主版本；`OUTCOME_UNKNOWN` / `NEEDS_RECOVERY` 不得换新幂等键盲目重放。恢复点须匹配当前版本；人工后续修改不匹配时拒绝恢复，保留证据后人工决策。

保存响应丢失时，可显式调用 `ino_get_operation(project_id,operation_id,reconcile=true)` 请求只读对账；服务检查保存证据，不重放原生写入。普通查询默认 `reconcile=false`，查询成功仍不等于原作业已成功。

## 范围与已知限制

- 原生编译只覆盖活动 Application 和实际被引用的程序，不证明任意工程树对象都被编译。
- IDE 原生 LD/SFC 图形 XML 回读在历史环境受阻；P0 不开放通用图形修改。
- 捕获未保存源码需要编辑器已同步到宿主对象模型；编辑缓冲区可见性未知时明确标注。
- 当前支持同一 ST POU 的受控声明/实现修改，以及显式调用的新 FB 创建；多对象任意事务、跨引用改名不在当前范围。
- 检索/静态规则/原生编译/模型理解/PLC 实际运行是不同证据层；本服务无设备运行验收。

协议与配置参考：[官方 Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)、[OMP MCP 配置](https://github.com/can1357/oh-my-pi/blob/main/docs/mcp-config.md)。

# InoProShop MCP 完整离线包 · 使用说明

发布版本：**2026-10-20，26 项工具**。交付文件：`InoProShop-MCP-Offline-Windows-x64-20261020.zip`，配套 `.sha256`、`.verification.json` 和 `.acceptance.md/.json`。

适用平台：Windows x64。已验证的组合：InoProShop 1.10.0.3、ohmypi 18.8.7。

本包已携带 Python 3.12.7、全部锁定的 MCP/CGC Python 依赖、Java 21.0.6+7 和 Neo4j Community 5.26.12。接收方无需另装 Python、Java、Neo4j、uv 或 Docker，首次启动也不会下载这些依赖。包内不含厂商 IDE、ohmypi、模型账号、个人工程或开发者数据库。

**最快操作：安装并配置好 InoProShop 和 ohmypi → 完整解压 → 双击 `Connect-Ino.cmd` → 在新打开的 IDE 中打开工程 → 在 ohmypi 中提出读取请求。以后仍双击同一文件。**

## 新版功能

| 功能 | 能做什么 | 使用边界 |
|---|---|---|
| 受控声明编辑 | 编辑同一 ST 程序对象的声明和/或实现，分别查看差异，应用后回读并编译 | 保留对象名称和类型，省略的区段保留；仍执行原有授权、版本检查、检查点与失败恢复。 |
| 模板目录与控制器复核 | 列出登记模板、校验文件哈希，创建新副本后核对真实根设备身份 | 只有 ready 模板可创建；控制器不符或无法核实时禁止源码写入和保存；不会自动替换控制器。 |
| 独立文本搜索 | 在固定快照中按声明/实现、对象、大小写查找字面文本，返回行列和上下文 | 不需要图数据库或实时 IDE 调用；未读取的图形内容不属于搜索覆盖，语义查询和修改预览仍要求图索引。 |

接口分别为 `ino_preview_change(change.type="replace_st_sections")`、`ino_list_project_templates` / `ino_create_project`、`context_search_text`。详细说明在包内 `services/inoproshop-mcp/docs/ENGINEERING_EXTENSIONS.md`。声明编辑继续保留预览、编译、恢复和未知结果不重放机制。

本包携带模板目录、登记说明与 catalog，不附带厂商 `.project` 或个人工程。catalog 中的 AM600 本机示例在缺少文件时会显示 `missing`，不能直接用于创建。请按 `templates/README.md` 提供自己的参考工程副本，读取原生 `device_identity`，核对实际型号后登记文件 SHA-256 与控制器 type/id/version；不要照抄示例型号或身份。设备描述版本不等于固件版本。

## 1. 使用前准备

1. 安装并授权 InoProShop，确认它可以正常打开工程。本版原生验证版本为 1.10.0.3；其他版本需单独验证兼容性。
2. 安装 ohmypi，确认在新开的命令窗口输入 `omp` 可以运行；已验证版本为 18.8.7。若安装器需要 Bun/Node，请随 ohmypi 自身安装要求完成。
3. 在 ohmypi 中完成模型配置和登录，先确认能正常对话。模型账号由接收方提供，本包不会覆盖账号或赠送模型额度。
4. 准备可写的本地目录，例如 `D:\InoMCP`。避免放在 Program Files、只读目录、网络共享盘或层层嵌套的长路径下；保留至少 3 GB 可用空间供解压、数据库和使用数据。

“离线”指 MCP 依赖准备和本机数据库运行无需访问互联网。使用云模型时，ohmypi 仍需要访问模型供应商；完全断网对话需要接收方提前在 ohmypi 配好可用的本地模型。本包不携带模型权重。

## 2. 解压后应看到什么

请先完整解压 ZIP，再打开包含以下文件的目录。不要在压缩软件中直接运行，也不要只复制启动文件。

```text
InoProShop-MCP-Offline-Windows-x64-20261020/
  Connect-Ino.cmd              ← 双击启动
  README.md                   ← 本说明
  THIRD-PARTY-NOTICES.md       ← 第三方组件及许可位置
  runtime/python/             ← 独立 Python 与已安装的全部依赖
  offline/archives/java.zip   ← Java 21 原始运行包
  offline/archives/neo4j.zip  ← Neo4j 原始运行包
  offline/manifest.json       ← 逐文件完整性校验清单
  services/                   ← MCP 服务、配置与脚本
  adapters/                   ← InoProShop 桥接
  templates/                  ← 本地模板 catalog 与登记说明
  demo/                       ← 桥接所需的配套模块
  scripts/、docs/             ← 验证与维护资料
```

`work` 目录会在本机首次运行时生成；全新发布包不包含它。`runtime`、`offline`、`services`、`adapters`、`demo` 均应保留在原相对位置。

## 3. 首次启动的完整流程

### 第一步：双击入口

双击 `Connect-Ino.cmd`。普通使用不需要管理员权限。

### 第二步：等待本地准备完成

窗口依次显示以下阶段：

1. **离线文件校验**：检查包内文件和 Python 依赖；不会安装到系统 Python，也不会联网补文件。
2. **数据库准备**：首次从包内解压 Java 和 Neo4j，为本机生成新密码，初始化并启动数据库。后续会验证并复用现有数据库。
3. **MCP 启动**：启动本地服务，验证认证和 26 项工具。
4. **InoProShop 接入**：自动定位并打开带桥接的 IDE；已有对应桥接实例时会复用。
5. **ohmypi 打开**：自动准备本包专用的 MCP 连接配置，进入对话界面。

首次解压运行环境和初始化数据库可能需要几分钟。请保持窗口打开，等待各阶段显示结果。

无法唯一定位 IDE 时，按窗口提示选择编号或输入安装路径。遇到 IDE 自身的启动、授权或缺少设备组件弹窗，请在 IDE 中处理，再等待或重新双击入口。

若此前手动打开的 IDE 没有桥接，启动器会另开一个带桥接的 IDE。请使用入口此次打开或明确选择的那个窗口。

### 第三步：在 IDE 中打开工程

看到“连接已就绪，请在 IDE 打开工程”表示接入成功。请在对应 InoProShop 窗口打开你的工程，然后回到 ohmypi。

建议先用工程副本试用。需要 AI 修改和保存时，请把副本放在本包的 `work\mcp_p0\projects` 目录中；其他位置的工程可读取，但修改权限仍受工程副本目录限制。新电脑还需单独完成原生写入/编译能力验证，连接成功不会直接放开全部写入能力。

### 第四步：先发一条只读请求

在 ohmypi 中输入：

> 读取当前接入的 InoProShop 工程中的 PLC_PRG，解释代码含义并注明源码来源，先不要修改。

如果工程没有 `PLC_PRG`，替换为实际程序名，或先输入：

> 列出当前接入的 InoProShop 工程及其程序对象，先不要修改或编译。

能返回真实工程、对象和源码来源，说明读取链路可用。此后再根据实际任务明确提出检索、诊断或修改请求。

入口本身只准备连接，不会自动读取整个工程、修改代码、编译或调用模型。本服务不提供 PLC 下载、强制和在线设备修改。

## 4. 以后每天怎么用

1. 双击同一份 `Connect-Ino.cmd`。
2. 等待环境检查完成；可用的数据库、MCP 和桥接 IDE 会复用。
3. 在对应 IDE 中打开或切换工程。
4. 回到 ohmypi 对话。

退出 ohmypi 后，数据库、MCP 和 IDE 可以继续运行；按 IDE 的正常方式保存并关闭工程。电脑重启后再次双击入口即可。不需要每天解压发布包，也不需要手动运行桥接文件。

使用期间不要移动、覆盖或删除正在运行的包目录。需要换目录时，先正常退出相关程序；优先在新位置重新解压原始 ZIP，再做本机初始化。

## 5. 检查状态和特殊安装路径

在包目录的地址栏输入 `powershell` 并回车，可执行：

```powershell
# 只检查，不初始化、不启动组件
.\Connect-Ino.cmd -Check

# 指定自己的 InoProShop 安装位置；示例路径请按实际修改
.\Connect-Ino.cmd -InoExe "D:\Inoproshop\CODESYS\Common\InoProShop.exe" -Profile "InoProShop(V1.10.0.3)"

# 准备连接并配置 ohmypi，完成后退出，不进入对话
.\Connect-Ino.cmd -NoOmp
```

`-Check` 在尚未首次启动时报告组件未就绪是正常现象。`-NoOmp` 仍可能打开带桥接的 IDE。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| 提示找不到 `omp` | 完成 ohmypi 安装后新开命令窗口，确认 `omp` 可运行，再双击入口。 |
| 找不到 InoProShop 或发现多个版本 | 按提示选择，或使用上述 `-InoExe` 和 `-Profile`。 |
| 离线校验失败、Python 或压缩包缺失 | 从原始 ZIP 完整解压到新的短路径后重试；离线模式不会下载替代文件。也检查安全软件是否隔离了文件。 |
| 显示端口占用、密码不正确 | 默认端口为 MCP `8765`、数据库 `17687`。保留原进程和数据，根据提示检查原安装；不要删除数据库或凭据碰运气。 |
| 显示 `MCP_LEGACY_RESTART_REQUIRED` | 在原 MCP 终端正常按 Ctrl+C 退出旧服务一次，再运行新入口。 |
| 显示 `MCP_CONTRACT_MISMATCH`、`HOST_VERSION_MISMATCH` 或占用端口的是旧包 | 等待作业结束，保留工程和未保存修改，正常退出旧 MCP 与旧桥接后再启动新包；不要覆盖正在使用的目录或删除旧状态。 |
| 模板状态为 `missing` 或 `hash_mismatch` | 按 `templates/README.md` 放入并核实本地模板，再更新 catalog 的实际 SHA-256 与原生控制器身份；离线包不会下载厂商工程。 |
| 提示 `CONTROLLER_MISMATCH` / `CONTROLLER_NOT_VERIFIED` | 保留副本和失败作业记录，核对登记模板与 IDE 中的真实设备；不能跳过控制器复核继续写入。 |
| 连接成功但没有工程 | 在入口选定的 IDE 中打开工程，再发一次列出工程/对象的请求。 |
| IDE 弹出缺少 AM600、设备包或库提示 | 根据工程实际需要安装厂商对应组件；离线 MCP 包不包含 InoProShop 的设备包和工程库。 |
| 模型无法回复或登录失败 | 在 ohmypi 中检查模型配置、账号和网络。本地 MCP 连接就绪与模型服务是否可达分别处理。 |
| 提示不能修改或编译 | 确认使用允许目录内的工程副本，并完成本机所需能力验证；不要通过移除凭据或篡改验收记录绕过。 |

日志与本机数据位置：

| 内容 | 包目录内的相对位置 |
|---|---|
| 最近一次启动报告 | `work\mcp_p0\state\launcher\last-run.json` |
| MCP 日志 | `work\mcp_p0\state\launcher\logs\controller.log` |
| 数据库日志、凭据及数据 | `work\mcp_p0\neo4j` |
| 作业、快照与备份 | `work\mcp_p0\state` |
| 允许写入的工程副本 | `work\mcp_p0\projects` |
| 本包专用 ohmypi 工作目录 | `work\mcp_p0\omp` |

需要支持时可先提供启动报告和必要的脱敏日志；不要发送 `token`、`credentials.json`、模型密钥或整个使用后的 `work` 目录。

## 7. 转发给其他人的方式

直接转发原始离线 ZIP 和旁边的 SHA-256 校验文件。每个人各自解压、首次初始化，使用自己的 InoProShop 授权和模型账号。不要把已经使用过、包含 `work` 的文件夹直接压缩转发。

第三方组件的原始许可随包保留，位置见 `THIRD-PARTY-NOTICES.md`。本机验收的具体范围及结果以压缩包旁的验收报告为准；本包不宣称已经在未实际测试的 Windows 电脑上完成验证。

## 8. 从旧版升级

1. 将新版 ZIP 解压到新的可写短路径，保留旧包及其 `work`。新包不包含也不会自动迁移个人工程、自动保存文件、数据库或凭据。
2. 在旧连接中等待所有作业结束；如存在 `OUTCOME_UNKNOWN` / `NEEDS_RECOVERY`，先按原作业记录处理，不要换状态目录重放写入。
3. 在 IDE 中按正常流程保留未保存修改，再正常退出旧桥接所在 IDE 与旧 MCP；启动器不会替你强制关闭它们。仅关闭 ohmypi 不会退出后台组件。
4. 从新目录运行 `Connect-Ino.cmd`。旧数据库仍运行且可验证时可复用；凭据无法访问或端口冲突会明确停止，不会重置密码或删除数据。需要继续使用旧状态时，应由维护者明确指定其状态、工程和数据库配置，不要直接覆盖目录。
5. 用 `Connect-Ino.cmd -Check` 核对 26 项工具与新版桥接，打开你选定的工程副本后先做只读验证。新机器仍需单独完成原生写入/编译能力验证，旧机器的验收状态不随本包转发。

重装和换目录不会自动恢复 IDE 的未保存缓冲区。请先在原 IDE 中保留修改，并保留原工程及 `.autosave` 等恢复文件。

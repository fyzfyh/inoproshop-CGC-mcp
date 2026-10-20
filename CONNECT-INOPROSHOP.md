# InoProShop 一键接入 ohmypi

先安装并授权 InoProShop，安装 ohmypi（`omp`）并完成模型配置。首版验收组合为 Windows x64、InoProShop 1.10.0.3、OMP 18.8.7。

## 日常只需一条命令

将完整发布包解压到可写、较短的路径，在该目录运行，或直接双击 `Connect-Ino.cmd`：

```powershell
.\Connect-Ino.cmd
```

完整离线包已经带齐 Python、Python 依赖、Java 和 Neo4j，首次只校验并解压本地文件，不访问下载服务器；详细流程见离线包根目录 README.md。源码精简包首次会联网下载固定版本的工具和依赖。之后均使用同一入口，不需要再次安装，也不需要手动连接 Neo4j、编辑 MCP 配置或在 IDE 中运行桥接文件。

入口依次准备 Python 环境、验证并启动数据库、验证并启动 MCP、打开带桥接的可见 IDE，最后打开 ohmypi。正常退出 ohmypi 后，IDE、数据库和 MCP 保持运行，下一次直接复用。

在自动打开的 InoProShop 中照常打开工程，然后在 ohmypi 中输入：

> 读取当前接入的 InoProShop 工程中的 PLC_PRG，解释代码含义并注明源码来源，先不要修改。

“连接已就绪，请在 IDE 打开工程”表示接入已完成，不是错误。启动器不会自动采集整个工程、编译、修改程序或调用模型。实际读取由你的对话请求触发。

## 第一次可能需要选择的内容

- 无法自动定位 InoProShop 时，按提示使用 `-InoExe` 指定安装程序路径；有多个安装或多个可用 IDE 时，选择编号。选择会保存在本机。
- 已经手动打开但没有桥接的 IDE 会保留，入口另开一个带桥接的 IDE。请在新打开的窗口选择工程。
- 如果 IDE 显示启动弹窗，按提示在对应窗口处理；重新运行入口会继续检查同一进程，不会不断新开窗口。
- 已经在运行的旧版 MCP 没有新的运行身份记录时，诊断会显示 `MCP_LEGACY_RESTART_REQUIRED`。在原 MCP 终端正常按 Ctrl+C 退出一次，再运行本入口；保留数据库和 IDE。入口不会擅自终止旧服务或正在进行的操作。

## 查看连接状态

```powershell
.\Connect-Ino.cmd -Check
```

检查模式分别显示 ohmypi、Python、数据库、MCP、IDE 和工程状态。它不会安装、下载、启动组件或更新本地配置。尚未安装 Python 时会说明需要先运行普通入口。

普通启动的诊断报告在状态目录的 `launcher/last-run.json`，MCP 日志在 `launcher/logs/controller.log`，数据库启动日志在 `work/mcp_p0/neo4j`。报告不含密码或 MCP token。不要对外发送凭据文件、token 或整个 `work` 目录。

出现端口冲突、错误密码或数据已存在但凭据丢失时，入口会停止该阶段并给出原因，不重置数据库或关闭其他进程。网络下载失败时可直接重新运行，只有校验通过的缓存才会复用。

## 可选参数

这些参数用于特殊安装位置或独立验证；正常使用不需要填写。

```powershell
.\Connect-Ino.cmd -InoExe "D:\Inoproshop\CODESYS\Common\InoProShop.exe" -Profile "InoProShop(V1.10.0.3)"
.\Connect-Ino.cmd -Workspace "E:\PLC助手"
.\Connect-Ino.cmd -NoOmp
```

`-NoOmp` 完成环境准备和 OMP 配置后退出，不打开交互会话，也不调用模型。`-NonInteractive` 禁止出现编号/路径询问；无法唯一选择时明确失败。还支持 `-StateDir`、`-ProjectRoot` 和 `-Port`；不同状态目录的控制器需要使用不同 MCP 端口。工程副本目录与 OMP 工作目录、端口会随该状态目录记住，后续连接必须保持一致。

## 默认位置与兼容范围

所有路径均随解压目录计算，不依赖开发者电脑的盘符：

| 内容 | 相对位置 |
|---|---|
| 服务 Python 环境 | `services/inoproshop-mcp/.venv` |
| uv 与专用 Python 下载缓存 | `work/mcp_p0/launcher-runtime` |
| Java、Neo4j、图数据及凭据 | `work/mcp_p0/neo4j` |
| MCP 状态、token 和启动器记录 | `work/mcp_p0/state` |
| 允许修改的工程副本 | `work/mcp_p0/projects` |
| OMP 专用工作目录 | `work/mcp_p0/omp` |

默认使用本机端口 `17687` 和 `8765`。不安装 Windows 系统服务、不要求 Docker、不修改系统 PATH 或系统 Java。运行入口时不需要管理员权限；企业策略若禁止脚本或下载，需要遵守组织的安装规则。

OMP 的现有模型账号和其他 MCP 配置会保留；连接项使用环境变量传递认证，不保存明文 token。连接配置合并前会保留上一版 `mcp.json.inoproshop-backup`。每个 OMP 会话获得独立的 IDE 身份提示，不能因为另一个启动窗口选择了不同 IDE 就切换工程。

允许读取用户明确选择的已打开工程。所有工程修改、恢复及保存仍限于指定工程副本目录，保持原有权限和能力验证要求。新机器缺少原生验收记录时，读取结果与写入/编译能力验证分别报告；连接成功不代表所有原生能力均已验证。

发布包保留 `services`、`adapters`、`demo` 等相对布局，不要只复制单个脚本。完整离线包还须保留 `runtime` 和 `offline` 目录。厂商 IDE 安装、ohmypi、模型账号和模型权重由使用者自行准备；本包不提供工程自动恢复或向任意运行中的 IDE 注入桥接。

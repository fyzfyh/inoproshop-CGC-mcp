# inoproshop-CGC-mcp

面向 InoProShop 的本机 MCP 工程工具，结合 CodeGraphContext / Neo4j 与 ohmypi，提供 26 项明确工具。当前版本为 **2026-10-20**。

## 下载完整离线包

- [Windows x64 完整离线 ZIP（约 222 MiB）](https://github.com/fyzfyh/inoproshop-CGC-mcp/releases/download/v2026.10.20/InoProShop-MCP-Offline-Windows-x64-20261020.zip)
- [本次 Release 与校验文件](https://github.com/fyzfyh/inoproshop-CGC-mcp/releases/tag/v2026.10.20)
- [完整使用说明](services/inoproshop-mcp/docs/OFFLINE_README.md) · [交付验收](docs/releases/20261020/ACCEPTANCE.md)

离线包内置 Python 3.12.7、全部锁定 MCP/CGC Python 依赖、Java 21.0.6+7、Neo4j Community 5.26.12。完整解压到可写短路径，双击 `Connect-Ino.cmd`，在接入的 IDE 中打开工程，再回到 ohmypi 提出请求。`Connect-Ino.cmd -Check` 只检查连接，`-NoOmp` 准备连接和配置后退出。

使用者仍需预先安装并授权 InoProShop、安装 ohmypi 并配置自己的模型；云模型需要对应网络，本包不含模型权重。实测组合：Windows x64、InoProShop 1.10.0.3、OMP 18.8.7。

## 功能

| 能力 | 说明 |
|---|---|
| 读取与检索 | 显式宿主/工程身份、固定快照、源码来源、真实 PLC 关系图、引用和影响分析。 |
| 受控声明编辑 | `replace_st_sections` 编辑同一 ST POU 的声明和/或实现，保留名称与类型，继续经过预览、授权、回读、编译和检查点恢复。 |
| 模板与控制器复核 | `ino_list_project_templates` / `ino_create_project` 校验模板哈希与原生根控制器身份；不匹配或未知时禁止源码修改及保存。 |
| 独立文本搜索 | `context_search_text` 在固定快照中查找字面文本并返回行列和上下文，无需先建图。 |
| 作业与恢复 | 持久作业、幂等、版本冲突保护、诊断、保存副本、编译失败自动恢复与未知结果不重放。 |

模板工程由使用者在本机核实并登记；本仓库和离线包仅包含 catalog 与[登记说明](templates/README.md)，不附带厂商 `.project`。缺少模板时如实显示 `missing`。服务处理离线工程副本，不提供 PLC 下载、强制或在线修改。

## 验证

本离线 ZIP 的 7,801 个文件校验通过；随包测试按唯一用例去重后 **273 项通过、1 项缺少历史工程资料而跳过、0 失败**。中文和空格目录下首次数据库初始化、重启持久化、重复启动复用、只读检查、真实 IDE / 图索引 / OMP 读取与 7 项新功能检查通过。完整声明应用、原生编译和恢复另有相同逻辑源码的 27 项原生功能验收；与离线启动证据分别记录，不叠加为同一组测试。

本机验证没有覆盖干净 Windows 虚拟机、第二台实体电脑或真实 PLC 运行。具体边界见[本包验收](docs/releases/20261020/ACCEPTANCE.md)与[功能验收](services/inoproshop-mcp/docs/ENGINEERING_EXTENSIONS_ACCEPTANCE.md)。

## 源码目录与安装

`services/inoproshop-mcp` 包含服务、测试、固定依赖和脚本，`adapters/inoproshop/bridge` 是原生桥接，`demo/v2` 是桥接所需配套模块，`scripts` 是原生验证驱动。仓库提交包含安装包中的完整产品源码与说明；预装运行环境通过 Release ZIP 交付。

直接使用 Git 源码时，`Connect-Ino.cmd` 首次准备固定依赖需要联网；完整离线使用请下载 Release ZIP，保留 `runtime`、`offline`、`services` 等相对目录。开发和独立安装见[服务 README](services/inoproshop-mcp/README.md)。

## 隐私与升级

[.gitignore](.gitignore) 排除工作状态、token、密码、模型账户、数据库、IDE 工程及自动保存、运行环境和构建产物。上传内容从已验收的安装包源码白名单准备，并另行扫描；完整离线 ZIP 作为 Release 附件上传。[隐私与发布说明](docs/PRIVACY.md)记录具体范围。

升级时在新目录完整解压，保留旧 `work`、工程和恢复文件；等待现有作业结束，在 IDE 中保留未保存修改，再正常退出旧服务/桥接后切换。不要覆盖运行中的目录，也不要复制开发机原生验收凭据来放开另一台机器的写入。

## 第三方组件

[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)记录固定运行组件及随包许可位置；离线 ZIP 保留上游许可。厂商 IDE、ohmypi、模型账号、模型权重及个人工程不在本仓库或离线附件内。

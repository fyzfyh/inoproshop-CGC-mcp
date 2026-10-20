# PLC 图索引：运行、版本与验证

图索引是 P0 必需能力。正式 `PLCIndex` 默认连接真实 CGC/Neo4j，连接缺失返回 `INDEX_BACKEND_UNAVAILABLE`，不会静默改用 BM25 或本地假数据库。`backend=False` 只用于明确的本地 IR 单元测试；其证据标记为 `deterministic_only`。

## 固定组合

- CPython 3.12.7；CodeGraphContext 0.6.13；Neo4j Python driver 5.28.2；pytz 2025.2。
- Neo4j Community 5.26.12；Java 21。已实测运行时为 JBR 21.0.6+9-b895.109。
- `JAVA_HOME` 可以指向单独安装的兼容 Java 21，不要求安装 IntelliJ IDEA。开发机恰好已有其附带 JBR。
- 下载来源、SHA-256、PLC parser 和图 schema 版本见 `../cgc-runtime-lock.json`。升级任一项后重跑下述真实数据库验收。

本适配器真实导入并调用 CGC 的 `codegraphcontext.core.database.DatabaseManager`，复用其 Neo4j 驱动、会话和数据库选择；通过 PLC 专有节点、关系和 Cypher 补齐领域支持。没有把 ST 当 Pascal，也没有将变量和实例塞进通用 Class。CGC 原生 MCP 服务不在此路径中；MCP 对外接口仍由本项目统一提供。

## 安装

正式服务在服务目录运行，复用已安装的 `uv`，从 `uv.lock` 安装完整锁定依赖：

```powershell
uv sync --frozen --extra cgc
```

以下独立精简环境只用于图适配验证：

```powershell
uv venv .venv-cgc --python 3.12
uv pip install --python .venv-cgc/Scripts/python.exe --no-deps -r requirements-cgc-adapter.lock
```

该精简环境仅安装当前适配器实际导入的依赖。它不是完整 CGC CLI/其他语言解析器/多数据库安装，不应用它运行 CGC 原生 MCP。不能假定旁边的 `.venv-cgc` 会自动被正式服务加载。

从锁文件的官方 URL 下载 Neo4j ZIP，检查 `Get-FileHash -Algorithm SHA256` 与锁文件一致后解压到专用目录。准备以下最小 `conf/neo4j.conf`，所有数据目录归该专用实例，不复用用户其他图数据库：

```ini
server.default_listen_address=127.0.0.1
server.bolt.enabled=true
server.bolt.listen_address=127.0.0.1:17687
server.bolt.advertised_address=127.0.0.1:17687
server.http.enabled=false
server.https.enabled=false
server.memory.heap.initial_size=256m
server.memory.heap.max_size=512m
server.memory.pagecache.size=128m
dbms.security.auth_enabled=true
dbms.usage_report.enabled=false
```

将 `$env:JAVA_HOME` 指向 Java 21 根目录。在首次启动前，用 `bin/neo4j-admin.bat dbms set-initial-password` 设置专用随机密码；用 `bin/neo4j.bat console` 启动专用实例。自动后台启动必须隐藏窗口，并记录其进程身份；停止操作只针对该专用实例。密码不进入代码、版本锁、命令日志或验收报告。

承载 Core 的服务进程需要三个环境变量：`NEO4J_URI`、`NEO4J_USERNAME`、`NEO4J_PASSWORD`。可选的 `NEO4J_DATABASE` 指定数据库；不配置时使用 Neo4j 默认数据库。MCP 服务的本机授权 token 与 Neo4j 密码是不同凭据，不可混用。

本开发机凭据只保存在 `work/mcp_p0/neo4j/credentials.json`。该文件不属于交付锁，不能提交或共享。读取字段后赋给进程环境变量，不打印内容。图适配器初始化时创建两个仅作用于 `PLCIndexRevision`/`PLCIRNode` 标签的唯一约束，数据库账号需有相应建约束与读写权限。

## 验证

在服务目录配置好 `NEO4J_*` 后：

```powershell
$env:PYTHONPATH = 'src'
.venv-cgc/Scripts/python.exe -m unittest discover -s tests -p test_index.py -v
$env:INOPROSHOP_RUN_CGC_TESTS = '1'
.venv-cgc/Scripts/python.exe -m unittest discover -s tests -p test_index_cgc.py -v
```

第一组验证确定性 IR、金标准、1,000 对象分页、快照隔离和失败发布；AM600/AM402 历史源码仅在本机固定资料包存在时附加验证，源码交付包不分发业务样例，缺失时该历史测试显式 skipped。第二组必须连真实数据库，验证 CGC 连接、PLC 节点/关系入库、真实 Cypher 符号/引用查询、工程和快照隔离、旧版可查、故意坏关系写入的事务回滚。未设置 opt-in 时第二组显示 skipped；skipped 不算真实验收通过。

真实数据库成功报告写入工作区 `artifacts/mcp_p0/cgc_integration.json`，包含版本、夹具哈希、索引版本和检查项，不包含密码。本地测试通过不代表 InoProShop 编译或 OMP 模型调用通过；这些由其他验收链路分别证明。

### 数据库已提交、本地尚未发布的中断

使用正式服务环境运行额外故障测试（仍须显式配置 `NEO4J_*` 和 `INOPROSHOP_RUN_CGC_TESTS=1`）：

```powershell
.venv/Scripts/python.exe -m pytest tests/test_index_commit_gap.py -q
```

该文件先运行纯本地发布测试，再运行真实 CGC/Neo4j 测试。每组覆盖四种组合：首次构建/已有旧 active，以及写 revision 文件前/切换 active 前。专用 Python 子进程确认数据库实际提交后使用明确退出码立即结束，不经过异常恢复或正常清理；随后新建数据库连接和 `PLCIndex` 从持久状态读取并重建。每个场景使用独立合成工程 ID，不启动 IDE、不重启数据库或服务；桥接调用被测试断言禁止。

验收结果写入 `artifacts/mcp_p0/cgc_commit_gap.json`。真实层四个场景确认：旧 active 和显式旧版仍可查；首次构建没有 active 时返回 `INDEX_NOT_FOUND`；重复重建产生同一 revision，图节点/边数量无累积重复。未开真实数据库选项时此层显示 skipped，不能记作通过。沙箱若隔离本机网络，应在有权连接该数据库的环境运行，不能据此误判服务已停止或擅自重启。

这是发布边界的真实进程退出测试，不证明操作系统断电、磁盘损坏或网络文件系统的持久性。索引属于可重建资料，恢复动作只重建图，不重新执行宿主写入。

## 支持范围与证据

读取声明、局部/全局作用域、中文标识符、大小写、标量变量与直接地址元数据、静态 FB 实例、已知成员、赋值读写、IF、命名参数与显式输出绑定；覆盖既有 AM600 和 AM402 样例中的 `S=`/`R=`。项目、Application、任务、POU、变量、类型与实例分别表示。任务可达只由真实 task_calls 和确定静态调用传播。

库内部、未知接口成员、地址别名、指针/间接寻址、数组与复杂类型等未支持结构会保留原始源码和明确 `unresolved/ambiguous/unsupported`，不编造确定关系。解析允许工程已有编译错误；解析结果不是类型检查或运行行为证明。

每条边携带工程、快照、原生对象、区段、位置和 parser 版本。同名不同作用域及不同 FB 实例有不同稳定符号 ID。完整快照内容指纹被核验，不能复用同一 snapshot_id 提交变化内容。Neo4j 发布在单事务内完成，完整性检查通过后才写本地 revision 文件，最后切换 active 指针。数据库已提交但 revision 文件不存在时，该版本不可通过索引查询；revision 文件完整而 active 尚未切换时，默认查询继续使用旧 active，指定新 revision 可读取完整已验证的图。没有旧 active 的首次构建默认返回 `INDEX_NOT_FOUND`。重新构建同一快照和 parser 版本收敛到同一 revision，不修改旧版、不重放宿主写。图是可重建派生数据，不能当作工程备份。

目录名使用短哈希以兼容 Windows 路径长度限制，文件内仍核对完整工程身份、快照身份和内容指纹。没有向 Agent 暴露任意 Cypher、图写入或宿主脚本执行。

# PLC 索引能力与金标准映射

这是**受限静态关系解析**的验收矩阵，不是完整 IEC ST 编译器或程序行为验证。纯数据测试与真实数据库测试分开记录。

| 能力 | 冻结证据 / 用例 | 边界 |
|---|---|---|
| 项目、Application、POU/GVL、任务、变量、基本类型 | `golden.json`；`test_golden_scopes_instances_and_sources` | 原生对象 GUID 保留；任务来源只能是已确定的宿主关系 |
| 局部/全局同名、不同 Application 同名、大小写 | `golden.json` 中两 Application、三个 value 作用域 | 全局冲突返回 ambiguous，不任选一个 |
| FB 类型、两个实例、命名输入/输出、实例成员 | `Motor`、`a/b`、`a.active/b.active` | 实例成员 ID 独立；缺外部库接口时不猜成员 |
| READS、WRITES、CALLS、任务静态可达 | 主程序、Motor、任务 goldens；行号集合断言 | 不做路径条件可行性、运行次数或实时行为判断 |
| FUNCTION 返回变量与静态调用 | `test_function_result_is_a_variable_not_a_write_to_the_pou` | 标量返回类型；不把对函数返回值赋值记成修改 POU 本身 |
| 中文、Tab、嵌套注释、字符串屏蔽 | `test_comments_strings_and_line_columns` | 索引位置用 1-based Unicode code point 列，不等价于编译器 UTF-16 offset 或屏幕 Tab 宽度 |
| 非完整语法保留并显示范围 | `test_incomplete_lexical_input_never_claims_complete_coverage`、`test_ambiguous_globals_and_unsupported_structures_not_guessed` | 未闭合注释、复杂循环/类型不标成已完整解析 |
| AM600 既有源码 | `test_historical_am600_and_am402_source_fixtures` 读取固定历史资料包 | PROGRAM、INT/BYTE、初始化、嵌套 IF、算术比较、2# 常量；非重新运行宿主 |
| AM402“未命名1”既有源码 | 同上，读取固定原生快照 | 中文标识符、VAR_GLOBAL、AT/%IX/%QX、S=/R=、空输出绑定、5 个库 FB 调用；库内部效果仍 unknown |
| 坏编译基线可建图 | `test_unknown_library_and_invalid_baseline_are_retained` | 保留确定引用及未知库/成员，不把静态提取当编译通过 |
| 符号稳定与快照/工程隔离 | `test_revision_project_snapshot_isolation_and_stable_symbols` | 加注释不改同一声明的符号 ID；不同 snapshot/revision 不串用 |
| 同 snapshot 内容不可被替换 | `test_source_mutation_cannot_reuse_a_snapshot_even_with_same_claimed_hash`、`test_immutable_snapshot_hash_mismatch_rejected` | 同时核对完整内容指纹和声明哈希；路径目录短哈希不能替代身份核验 |
| 全量发布失败保留 active | `test_failed_publication_does_not_replace_active_revision` | 未完成版本不替代可用版本；增量更新不在 P0 |
| 分页、空命中与未建索引区分 | `test_explicit_local_evidence_paging_and_no_index_are_distinct` | 查询稳定 symbol_id；不靠名字自由选一个引用目标 |
| 1,000 对象、3,000 检索命中 | `test_thousand_object_fixture_pagination_and_rebuild` | 无遗漏/重复；可重复全量构建产生同一 revision；不是 IDE 大工程性能成绩 |
| CGC/Neo4j 实际入库与查询 | `test_real_cgc_publication_query_isolation_and_atomic_failure` | 真正调用 CGC DatabaseManager 和原生 Cypher；隔离工程/快照、保留旧版本、坏边事务回滚 |
| 1,000 对象实际数据库路径 | `test_real_thousand_object_fixture` | 输出 `artifacts/mcp_p0/cgc_scale.json`；记录本机时间，不将单次测量当 SLA |
| 数据库已提交、本地 revision/active 发布前进程退出 | `test_index_commit_gap.py` 中本地及真实 CGC 两项测试，各四个场景 | 已有旧 active 可查、首次无 active 明确未发布、重建收敛、节点/边不重复；真实报告 `artifacts/mcp_p0/cgc_commit_gap.json`；不等价断电试验，不启动宿主 |

历史输入路径：

- `artifacts/v2/ai_package_verification/ai_packages/AI_20260915075653_7f7d94b023/project.json`
- `artifacts/example_open_test/20261010_unnamed1/project_snapshot.json`

这两项由仓库根目录解析，不复制或更改原始验证证据，也不随源码交付包分发。任一历史文件缺失时，仅历史样例测试显式 skipped，不能算作该样例验收通过。`golden.json` 和 1,000 对象合成测试在干净安装中仍为必跑项；`golden.json` 是不含客户信息的人工小型夹具，新增语法必须配对应金标准。

仍未覆盖：完整 ST 类型检查、所有 CODESYS 扩展、地址别名等价、数组/结构和指针间接访问、继承和动态派发、加密库内部、图形网络、HMI/通信外部访问、静态条件恒真/恒假分析、仿真与设备执行。搜索无命中不能推断工程不存在外部引用。数据库完整性通过不能代替真实宿主操作、编译、保存或模型能力验证。

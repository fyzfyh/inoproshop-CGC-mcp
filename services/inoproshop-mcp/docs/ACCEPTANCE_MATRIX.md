# P0 能力与验收证据矩阵

状态：**首期功能验收证据已冻结；进入发布前最后交付校验**。依据 `docs/requirements/PRD-InoProShop-MCP.md` 1.1 版，更新至 2026-10-12。AM402 A15、完整模型夹具、真实 OMP 工具会话、服务全量测试、最终桥接哈希门槛和原生覆盖/自动补偿/模态专项均已有固定结果。最终源码包的移位安装结果由外部 `source_release_v2_smoke` 报告补充。不同源码版本和证据层分别记账；历史失败不删除，也不冒称所有业务检查均在同一最终哈希重跑。

本文路径相对工作区根目录 `E:/AI-PLC`；`S/` 是 `services/inoproshop-mcp/` 的缩写。源码 ZIP 不包含业务工程、原始诊断全文、凭据及工作目录；外部证据路径保留用于本机追溯，不保证随交付包提供。

## 范围与判定规则

- 23 个 P0 工具均有公开 schema 和 Core 路由；“已注册”不等于全部业务场景验收通过。
- 写入范围是已有 ST 单对象实现替换，以及限定的新建 ST 功能块加一个任务可达调用者的两对象补丁。实例声明和调用插入仅服务该受控创建流程；不开放通用声明、任意多对象或任意宿主脚本修改。
- 图索引是 P0 必交能力。正式路径使用真实 CGC/Neo4j；纯数据测试中的 `backend=False` 不可替代图数据库验收。
- 恢复或保存完成后，如要报告“当前索引已更新”，必须显式重新采集快照，再提交 `context_index_snapshot` 并等到作业完成；不能以旧索引仍可查询或恢复/保存成功代替此验证。
- 编译 `passed`、`failed`、`unknown` 分开；作业受理成功、JSON 结构合法、候选生成成功、图解析成功均不构成原生编译通过。
- `skipped`、`running`、`answer_requires_review`、`candidate_validated` 保留原义，不汇总为 P0 passed。旧版本一次成功不能覆盖后续版本失败。
- 模型结构有效、任务意图符合和原生编译通过是三个独立维度。E10/E11/E13 候选语义失败，即使后续能编译也不能算任务完成；本轮建立评测基线，没有另设百分比放行门槛。
- P0 放行包含 A01–A20 首期子项、A24 基本模型/工具联调和限次、A25、A27、A28 宿主稳定，以及创建接入的两对象恢复/幂等/中断对账。A09 增量更新及完整 UI、通用多对象、模型审查/测试生成效果属后续范围。只读编译演示不等于全部 P0 放行。

## 证据层与固定报告

| 层 | 含义 | 不能据此推断 |
|---|---|---|
| L1 | 确定性单元测试、合成夹具、FakeHost 故障注入 | 真实 IDE API、真实编译和设备执行正确 |
| L1P | 真实控制器子进程强退/重启和 HTTP 会话断线，宿主与图使用 FakeHost/local IR | 真实厂商 IDE 崩溃或真实 Neo4j 提交；进程真实不使模拟宿主变成原生证据 |
| L2 | 官方 SDK / 已安装 OMP 的真实 HTTP MCP 连接 | 模型已调用，或已读写原生源码；受控 Core 的 HTTP 测试也只证明协议 |
| L3 | CGC DatabaseManager + Neo4j 实际入库、Cypher 和事务 | IDE 写入、编译及模型能力 |
| L4a | 直接桥接管道调用真实 InoProShop | 公开 MCP → Core 的完整编排及当前版本全部通过 |
| L4b | 公开 MCP → Core → 桥接 → 真实 InoProShop | 模型自主发起调用、仿真或 PLC 执行 |
| L5 | OMP 中的真实模型，限脱敏/合成输入 | 候选已写入、原生编译通过，或测试已在 PLC 执行 |
| R | Codex 对固定模型输出按 expected 做语义/来源/范围审阅 | 用户业务验收、真实工具调用、编译或 PLC 执行 |

| 证据 ID | 固定路径或可复现入口 | 当前读到的事实 |
|---|---|---|
| P-SDK | `artifacts/mcp_p0/sdk_protocol.json`；`S/scripts/protocol-smoke.py` | 2026-10-12 06:36 UTC，passed；官方 MCP SDK 1.30.0，23 工具发现、能力/宿主/工程查询和结构化错误；`model_invoked=false`、`native_source_read=false` |
| P-OMP | `artifacts/mcp_p0/omp_protocol.json`；`S/scripts/omp-smoke.ts` | 06:36 UTC，passed；已安装 OMP 18.8.7 客户端连接、23 工具和结构化结果/错误；模型和原生源码读取均未发生 |
| S-FULL | `artifacts/mcp_p0/tests_final_20261012.xml` | 最终服务回归：123 passed、3 skipped、2 个历史样例 subtests passed，0 failure/error，39.30 秒；包含 W01、H06、M02/M03 和慢快照去抖回归。3 个 CGC opt-in skip 有独立真实数据库报告，不能将 skip 计为本批通过 |
| S-BOUNDARY | `artifacts/mcp_p0/workflow_boundaries.junit.xml` | W01，15 passed；PID 复用不复活旧绑定、同路径两宿主拒写、依赖变化拒旧补丁、非法创建零写入及空消息/异常编译 unknown 分类；明确为 FakeHost 层 |
| S-PROCESS | `artifacts/mcp_p0/controller_process_faults.junit.xml`；`artifacts/mcp_p0/controller_process_faults/{write,create,attach,apply_compile,compile,save,index,disconnect}.json` | L1P，8 项通过：7 个阶段实际控制器强退后状态正确、同键去重且零重放；第 8 项 HTTP/MCP 会话断开不取消持久作业。全部明确 `native_evidence=false` |
| G-REAL | `artifacts/mcp_p0/cgc_integration.json` | 06:52 UTC，passed；CGC 0.6.13、Neo4j 5.26.12、driver 5.28.2、Java 21；22 节点/45 边；实际发布、查询、隔离和失败事务回滚 |
| G-SCALE | `artifacts/mcp_p0/cgc_scale.json` | 06:52 UTC，passed；1,000 合成对象、2,002 图节点、5,000 边、3,000 分页命中，无遗漏/重复；本机单次计时，不是 IDE 大工程 SLA |
| G-GAP | `artifacts/mcp_p0/cgc_commit_gap.json` | L3，passed；真实数据库提交后，专用 Python 进程在 revision/active 发布前退出。首次构建/已有旧 active 共 4 个真实场景；重启旧版可查、重建收敛、节点/边不重复、0 宿主调用。不是断电或磁盘损坏试验 |
| N-BASE | `artifacts/mcp_p0/native/20261012_143551_f456497a/report.json` | L4a，旧桥接源码哈希的 16 项通过；含 68 节点快照、读写、创建/删除/两对象补偿、编译、dirty 拒绝切换、保存重开。未包含后续新增宿主稳定检查，不作为当前版本放行 |
| N-RETRY | `artifacts/mcp_p0/native/20261012_150125_c17f1ca4/report.json` | L4a，早期重试 failed，保留管道结束及此前局部成功，不删除失败记录 |
| N-BRIDGE | `artifacts/mcp_p0/native/20261012_160558_92dcf225/report.json` | **最终桥接哈希** L4a，整体 passed：19 checks、两个owned IDE exit0；覆盖脚本返回/UI可用、读写、创建调用/负向编译覆盖、删除/手动两对象恢复、dirty拒绝、失败打开保旧工程、双IDE隔离、保存重开。证据文件已更新至当前桥接哈希 |
| N-BRIDGE-HISTORY | `artifacts/mcp_p0/native/20261012_150647_764868d1/report.json` | 历史桥接哈希的19 checks+两个IDE exit0保留；当前门槛引用N-BRIDGE。中文名称导入回读不等于AM600编译支持中文FB名称 |
| N-CORE | `artifacts/mcp_p0/native_core/20261012_151029_fc5bd65f/report.json` | 历史桥接哈希 L4b，整体 failed。编辑编译、检查点恢复、单对象失败自动恢复、创建接入/主动两对象恢复、合成坏基线 repair、17 组诊断、只读导出具名通过；后段模型夹具基线失败保留。早期宽松监测不能替代修后严格验证 |
| N-MON-FAIL | `artifacts/mcp_p0/native_core/20261012_152346_b42507e7/report.json` | L4b，整体 failed：更严格监测检查发现去抖起点早于慢快照返回，可能提前发布；已修复并新增 C18，不能再用前次宽松检查宣称最终去抖通过 |
| N-T6 | `artifacts/mcp_p0/native_core/20261012_152855_2445f118/report.json` | 修后监测分段 passed，最后写入回包至事件 14.434102535 秒，去抖10秒，最终第3次源码匹配，随后11秒无重复；整体 run 后段A15选择器失败。与 N-MODEL 两次监测合为同监测实现的 n=3 基线 |
| N-A15 | `artifacts/mcp_p0/native_core/20261012_154116_36ec41a0/report.json` | L4b，整体 passed、宿主 exit0；真实 AM402 2 原始/2 目标/0 新增/0 剩余错误。首次编译自动新增系统对象导致 unknown→恢复；核对后新快照重新预览并修复通过，保存重开编译通过；原工程文件哈希不变 |
| N-LOCATIONS | `artifacts/mcp_p0/diagnostic_location_summary.json` | N-CORE 的 17 组诊断共有 20 条 Error；20/20 与诊断快照对象/区段/源码行精确匹配。18 条行列、2 条仅行号且列号 null；保留中文/Tab 原生样例，无虚构列号 |
| N-MODEL-SETUP | `artifacts/mcp_p0/native_core/20261012_151029_fc5bd65f/model_native/summary.json` | 旧模型夹具原生基线 failed，失败原件保留；后续新版夹具已在 N-MODEL 全部执行，不再标为仍运行 |
| M-FIXTURE | `artifacts/mcp_p0/evaluation_fixture_validation.json` | 26 道合成任务夹具通过结构校验；`model_invoked_by_this_command=false`，原生编译分母 0 |
| M-R0 | `artifacts/mcp_p0/model_evaluation_v1_20261012/summary_round_0.json` | L5，26 次真实模型任务，20 个响应结构有效；其余 6 个非法输出；`tool_calls=0`、`native_compile_tasks=0`。后续语义结果见 M-REVIEW |
| M-R1 | `artifacts/mcp_p0/model_evaluation_v1_20261012/summary_round_1.json` | L5，对上述 6 个非法输出做第 1 轮契约反馈，6 个候选结构有效；仍无工具调用或原生编译；不能计为编译错误修复成功率 |
| M-REVIEW | `artifacts/mcp_p0/model_semantic_review_20261012/review.json`、`review.md` | R，26 题已审：17 非修改回答 6 符合/9 失败/2 unknown；9 候选意图 6 符合/3 失败（E10/E11/E13）。104 个输入/输出工件哈希复核，未改候选、未新增模型/工具/IDE 调用；不是用户业务验收 |
| P-NATIVE | `artifacts/mcp_p0/sdk_native_source_final.json`；`omp_native_source_final.json` | 官方 SDK 8 checks、OMP 客户端6 checks均 passed；23工具、原生绑定快照及POU源码读取，`native_source_read=true`、`model_invoked=false`。协议读取与模型自主调用分开，后者见 M-LIVE |
| S-CAPS | `artifacts/mcp_p0/adapter_capabilities_fixed.junit.xml` | H06，实际生产 adapter 的10项纯数据测试 passed：匹配证据支持、OEM/桥哈希变化、缺失/损坏证据均unknown+reason；根/checks/单项检查三层形状校验已修。旧 `adapter_capabilities.junit.xml` 的7 passed/1 failed保留 |
| S-NATIVE-PROTOCOL | `scripts/native_protocol_tests.py` | 最终8/8通过；帧边界、绑定失效、超时结果unknown且不重发、目录边界、共享锁下心跳发布恢复。无IDE的实际协议/adapter方法测试，不是原生编译证据 |
| S-MODEL-BOUNDARY | `artifacts/mcp_p0/model_failure_boundary.junit.xml`；S-FULL | M01/M02共8项通过：合成诊断投影隔离、模拟worker断网/超时/上下文超限/损坏响应持久留证、第3轮修复进入worker前拒绝。失败注入是软件层，不声称实际断开模型供应商网络 |
| N-MODEL | `artifacts/mcp_p0/native_core/20261012_155218_b63be7e1/report.json`；同目录 `model_native/summary.json` | **历史桥接哈希** L4b，整体 passed、66 checks、宿主exit0；70节点以17/页读5页无重复，图搜索/引用/影响来源核对，配置缺项明确；9/9模型候选实际编译并经公开检查点恢复，E25/E26新对象已删除。业务任务意图6/9，不能把66检查或9次编译称为9任务全部完成 |
| M-LIVE | `artifacts/mcp_p0/native_core/20261012_155218_b63be7e1/model_native/live_omp_tools/report.json` | L5+L4b，真实OMP模型E07额外会话 passed：模型主动调用 read_object、preview_change、apply_change、get_operation、get_diagnostics 共5次，随后验收驱动通过公开接口恢复至RESTORED；58.445秒。不是新增评测题，不并入26题生成分母 |
| M-METRICS | `artifacts/mcp_p0/model_evaluation_metrics_20261012_final.json`；`S/tests/test_evaluation_metrics.py` | 26题首轮契约有效20/26，6题经第1轮反馈后有效；32轮累计worker墙钟343.034秒（不是并行总历时）。9/9候选原生编译，意图完成6/9，9/9回读源码UTF-8哈希完全匹配、候选人工改动0。M03的3项金标准已纳入S-FULL；未完成任务的业务修正行数不虚构为0 |
| N-NATIVE-BOUNDARIES | `artifacts/mcp_p0/native_core/20261012_160930_f3d9a650/report.json` | **最终桥接哈希** L4b，整体 passed、23 checks、宿主exit0。错误FB未引用时不冒称已覆盖，接入后原生失败定位GUID，恢复后编译通过；公开MCP创建坏body自动RESTORED、新GUID删除、调用者声明/实现hash复原、失败/恢复诊断分存、同幂等键不重建。真实模态期间报告IDE_MODAL_OR_DISABLED且请求仍排队，解除后同请求成功、源码不变；另重验分页/配置/图查询/保存 |

S-FULL 已是当前冻结服务源码的完整回归。A09/A20新增7项、W01 15项、H06 10项、M03 3项均包含在123 passed中，不能另加重复计算。2个历史AM600/AM402子测试在本机通过；干净源码包缺少业务工件时仅历史测试skip，合成golden/1,000对象仍必跑。3个CGC测试默认opt-in skip，其独立真实通过证据为G-REAL/G-SCALE/G-GAP。

## 23 个公开工具

所有工具的精确参数以 `S/config/tools.schema.json` 为准；实现路由见 `S/src/inoproshop_mcp/core.py`。下表的用例编号在后一节映射到实际测试函数。

| # | 工具 | 首期能力与边界 | 对应需求/验收 | 当前证据及待办 |
|---|---|---|---|---|
| 1 | `ino_get_capabilities` | 返回工具、宿主和能力状态/原因；图形编辑不在 P0 | R01；A01 | P-SDK/P-OMP/P-NATIVE、H01/H06；版本和损坏证据负例已补，新桥哈希N-BRIDGE原生gate通过 |
| 2 | `ino_list_hosts` | 已注册实例、存活及绑定信息 | R02；A02 | P-NATIVE、N-BRIDGE两IDE隔离；W01证明PID复用不能复活旧宿主身份 |
| 3 | `ino_list_projects` | 工程身份绑定；并发发现不产生重复 identity | R02；A02 | C14并发唯一identity、C16保存后身份延续；W01同路径双宿主拒写 |
| 4 | `ino_open_project` | 明确来源副本、长作业和binding更新 | R02；A02 | N-BRIDGE打开/dirty拒绝/失败保旧工程；N-CORE公开MCP打开通过 |
| 5 | `ino_capture_snapshot` | 固定快照、源码/配置事实、dirty与editor_sync边界 | R03/R04；A03/A04 | C01/C11、N-A15真实66节点、N-BRIDGE真实68节点、N-MODEL夹具70节点；editor_sync仍unknown |
| 6 | `ino_read_object` | 原生GUID、声明/实现分区、快照分页 | R03/R04；A03/A04 | P-NATIVE原生源码读取；N-MODEL70节点/5页/每页17、GUID无重复；AM402中文源来自N-A15 |
| 7 | `ino_read_configuration` | tasks/devices/libraries/I/O可获取元数据及缺失说明 | R05/R06；A05 | N-MODEL公开配置：task分类3项（1配置节点+2任务）、device5项；library/io空集合并显式missing，周期/优先级及空单位如实保留 |
| 8 | `ino_export_context` | 固定快照目录/ZIP、source map、manifest哈希与发布核验 | R07/R24；A06 | C06；N-CORE/N-MON-FAIL导出分段通过，源码区段一致且dirty不变 |
| 9 | `context_index_snapshot` | 异步构建PLC IR，全量发布真实CGC图版本 | R08/R09；A07–A09 | I01–I07、G-REAL/G-SCALE/G-GAP；N-MODEL真实CGC入库及公开检索通过 |
| 10 | `context_search` | 文本/符号分开，来源、覆盖、版本与分页 | R08；A07 | N-MODEL图搜索来源对齐同snapshot/index_revision；I01/I03/I04/I06、G-REAL/G-SCALE，C15明确历史性 |
| 11 | `context_find_references` | 精确symbol_id读写/调用；不猜同名目标 | R09；A08/A09 | N-MODEL实际引用来源核对；I01/I02/I03/I06、G-REAL金标准，未知库明确说明 |
| 12 | `context_get_impact` | 指定快照有界静态影响、分页/覆盖 | R09；A08/A09 | N-MODEL实际影响结果来源核对；I01/I03/I06有界静态金标准，不代表设备运行行为 |
| 13 | `ino_preview_change` | 单实现或受控创建+调用者；不写入；strict/repair、任务可达 | R11/R12/R13/R17；A10/A11/A15 | C02/C04/C05/H03/W01；N-A15真实坏基线和N-MODEL9候选公开预览通过 |
| 14 | `ino_apply_change` | 指定patch/hash授权、幂等、检查点、回读、编译/条件恢复 | R13/R14/R17/R18/R30；A11/A12/A15/A16/A17 | N-CORE编辑/创建/修复分段；N-MODEL9候选编译回读及M-LIVE模型主动应用通过；N-NATIVE-BOUNDARIES创建失败自动两对象补偿通过 |
| 15 | `ino_compile` | 长作业原生编译，结果分类及范围 | R15；A13/A14 | N-LOCATIONS17组/20定位、N-A15、N-MODEL9次编译；W01空消息/异常分类，N-NATIVE-BOUNDARIES未引用/接入对照通过 |
| 16 | `ino_get_diagnostics` | 作业/当前状态分页；源变更清当前保历史 | R16；A14 | N-LOCATIONS20/20源码行准确，2条无列号不编造；C03/C11保留失败快照历史 |
| 17 | `ino_restore_checkpoint` | 条件恢复、保护人工后改/新引用、未知归属冻结 | R18/R30；A16/A17 | N-CORE主动单/双对象恢复、N-MODEL9候选恢复与两创建对象删除；C07–C09/C17保护后改/新引用/未知GUID；N-NATIVE-BOUNDARIES自动创建失败专项通过 |
| 18 | `ino_save_copy` | 明确新路径保存，区分内存修改/持久化失败 | R19；A18 | N-A15公开MCP保存重开、精确源码及原生编译passed；N-BRIDGE历史保存重开，C10/C16/S-PROCESS故障对账 |
| 19 | `ino_get_operation` | 持久状态/结果；显式reconcile核对未知保存 | R30；A17/A27 | H05/C16、S-PROCESS；reconcile只读核对同宿主/源码/clean/目标文件后更新台账，0写重放，明确disk_reopen_verified=false |
| 20 | `ino_cancel_operation` | 请求取消；排队取消不写，已发生动作不伪称撤销 | R30；A27 | C12验证写入前取消；进入原生调用后accepted=false并说明不打断。S-PROCESS证明聊天断线不取消持久作业 |
| 21 | `ino_set_monitor` | 轮询/去抖，默认不自动编译，可关闭 | R20；A19 | C13/C18；N-T6+N-MODEL同监测实现n=3，默认不自动编译、最终源一致且稳定11秒无重复 |
| 22 | `ino_get_changes` | 游标读取变化事件 | R20；A19 | 修后延迟秒数14.434102535/12.854956865/14.014432430；n=3小样本，不宣称SLA |
| 23 | `ino_check_rules` | 版本1 PLC001/002/003词法建议及行号；非编译器 | R21；A20 | Q01四项规则金标准通过；注释/字符串按lexical_advice，已修复/0.5、/0_1误报；不宣称语义分析 |

## 可定位的测试索引

以下是测试映射，不是新的通过声明。复现时在服务目录使用锁定环境；最终批次应保留运行时间、软件版本、源码哈希、通过/失败/跳过列表。

| 编号 | 文件 | 实际测试函数 |
|---|---|---|
| H01 | `S/tests/test_server_http.py`、`test_server_cli.py` | `test_official_handshake_discovery_and_chinese_read`；`test_contract_covers_same_core_tools`；`test_schema_export_is_public_contract` |
| H02 | `S/tests/test_server_http.py` | `test_invalid_args_never_reach_core_and_have_domain_error`；`test_domain_error_and_failed_operation_are_distinct`；`test_unexpected_errors_do_not_disclose_paths_or_secrets`；`test_http_access_rejections` |
| H03 | `S/tests/test_server_http.py` | `test_create_change_requires_typed_caller_and_no_arbitrary_script`；`test_create_caller_condition_must_be_explicit_and_null_survives` |
| H04 | `S/tests/test_server_cli.py` | `test_token_is_persistent_and_never_printed`；`test_short_token_is_rejected_without_rotating`；`test_same_state_cannot_have_two_controllers` |
| H05 | `S/tests/test_server_http.py` | `test_index_optional_idempotency_key_reaches_core`；`test_reconcile_is_explicit_query_only_argument` |
| H06 | `S/tests/test_adapter_capabilities.py` | `test_matching_oem_and_bridge_evidence_supports_only_verified_capabilities`；`test_unverified_adapter_evidence_reports_unknown_with_reason`（8种）；`test_matching_report_does_not_authorize_an_unverified_individual_action` |
| C01 | `S/tests/test_core.py` | `test_success_is_idempotent_and_sources_are_immutable`；`test_cross_project_artifact_rejected` |
| C02 | 同上 | `test_stale_preview_never_overwrites_manual_edit`；`test_parameter_statement_injection_rejected`；`test_no_reachable_target_no_write_preview` |
| C03 | 同上 | `test_negative_compile_restores_and_keeps_both_diagnostics` |
| C04 | 同上 | `test_bad_baseline_strict_rejects_repair_succeeds` |
| C05 | 同上 | `test_create_and_attach_compensates_both_objects`；`test_create_gate_cannot_be_bypassed_by_model` |
| C06 | 同上 | `test_export_uses_fixed_snapshot_without_host_calls`；`test_export_archive_failure_never_publishes_output_directory` |
| C07 | 同上 | `test_unknown_write_is_frozen_until_conditional_restore`；`test_restore_does_not_cover_later_manual_changes` |
| C08 | 同上 | `test_new_reference_prevents_created_object_deletion`；`test_failed_compensating_delete_freezes_new_writes` |
| C09 | 同上 | `test_restore_resumes_after_native_delete_response_lost`；`test_restart_marks_inflight_unknown_without_replay`；`test_restart_distinguishes_prewrite_from_unknown_mutation`（参数化） |
| C10 | 同上 | `test_save_failure_keeps_applied_state_explicit`；`test_index_failure_never_repeats_successful_mutation` |
| C11 | 同上 | `test_changed_source_clears_current_but_retains_historical_diagnostic`；`test_edits_during_native_compile_never_publish_passing_current_result` |
| C12 | 同上 | `test_cancel_queued_work_never_mutates_host`；`test_unknown_read_only_job_does_not_freeze_project_mutation` |
| C13 | 同上 | `test_monitor_debounces_without_automatic_compile_or_save` |
| C14 | 同上 | `test_concurrent_discovery_creates_one_project_identity` |
| C15 | 同上 | `test_historical_context_remains_queryable_with_explicit_staleness` |
| C16 | 同上 | `test_save_lost_response_reconciliation_never_replays_or_overwrites`（成功及后改dirty/源码/实例/目标文件缺失5种情况） |
| C17 | 同上 | `test_import_guid_response_lost_never_deletes_by_name_or_reimports` |
| C18 | 同上 | `test_monitor_debounce_starts_after_slow_snapshot_response`（已进入最终S-FULL） |
| W01 | `S/tests/test_workflow_boundaries.py` | `test_reused_pid_cannot_revive_previous_host_instance`；`test_two_live_hosts_with_same_project_path_are_not_writable`；`test_dependency_change_rejects_old_patch_even_when_target_unchanged`；`test_invalid_create_preview_has_no_native_write`（7种）；`test_compile_completion_is_not_inferred_from_acceptance_or_empty_messages`（5种） |
| I01 | `S/tests/test_index.py` | `test_golden_scopes_instances_and_sources`；`test_comments_strings_and_line_columns`；`test_function_result_is_a_variable_not_a_write_to_the_pou` |
| I02 | 同上 | `test_unknown_library_and_invalid_baseline_are_retained`；`test_ambiguous_globals_and_unsupported_structures_not_guessed`；`test_incomplete_lexical_input_never_claims_complete_coverage` |
| I03 | 同上 | `test_revision_project_snapshot_isolation_and_stable_symbols`；`test_explicit_local_evidence_paging_and_no_index_are_distinct`；`test_failed_publication_does_not_replace_active_revision`；`test_immutable_snapshot_hash_mismatch_rejected`；`test_source_mutation_cannot_reuse_a_snapshot_even_with_same_claimed_hash` |
| I04 | 同上 | `test_thousand_object_fixture_pagination_and_rebuild`；`test_historical_am600_and_am402_source_fixtures`（历史文件缺失时仅后者 skip） |
| I05 | `S/tests/test_index_cgc.py` | `test_real_cgc_publication_query_isolation_and_atomic_failure`；`test_real_thousand_object_fixture` |
| I06 | `S/tests/test_acceptance_edges.py` | `test_a09_deleted_object_removes_its_edges_but_old_snapshot_remains_queryable`；`test_a09_renamed_object_updates_names_without_rewriting_old_snapshot`；`test_a09_parser_upgrade_publishes_new_revision_and_keeps_explicit_old_revision` |
| I07 | `S/tests/test_index_commit_gap.py` | `test_local_publication_crash_preserves_old_active_and_rebuild_converges`；`test_real_cgc_commit_gap_survives_process_exit_and_rebuild`（每项4个场景；真实层独立报告G-GAP） |
| Q01 | `S/tests/test_acceptance_edges.py` | `test_a20_rule_goldens_preserve_line_numbers_and_label_comments_strings_as_lexical_advice`；`test_a20_nonzero_numeric_literals_do_not_become_literal_zero_advice`；`test_a20_unknown_rule_version_is_explicitly_rejected`；`test_a20_rules_cannot_read_another_projects_snapshot` |
| P01 | `S/tests/test_server_process.py` | `test_hard_exit_restart_never_replays_native_call`（write/create/attach/apply_compile/compile/save/index 7种）；`test_client_disconnect_does_not_cancel_durable_job` |
| M01 | `S/tests/test_server_evaluation.py` | `test_model_repair_drops_oem_snapshot_and_unmapped_diagnostics`；`test_model_repair_requires_fixture_guid_mapping`；`test_model_repair_rejects_mapping_to_an_object_outside_fixture` |
| M02 | `S/tests/test_server_evaluation.py` | `test_fake_worker_failure_is_durable_and_never_native_proof`（4种）；`test_repair_round_three_rejected_before_any_worker` |
| M03 | `S/tests/test_evaluation_metrics.py` | `test_candidate_zero_edits_requires_exact_native_source`；`test_new_fb_declaration_is_part_of_manual_edit_proof`；`test_missing_or_invalid_time_never_reports_zero_elapsed` |

索引每项语法/关系的金标准另见 `S/fixtures/index/README.md`、`golden.json`；运行组合及重新验证方式见 `S/docs/GRAPH_BACKEND.md`。复杂类型、指针/地址别名、动态调用、库内部、图形/HMI 和运行行为不得从此金标准外推为支持。

## PRD 逐项验收映射

下表在首期范围内核对功能验收；最后一列保留证据层与能力边界。原生通过、软件故障注入、模型语义评测和最终包安装分别判断，不扩张通过范围。

| 验收 | 需求 | 当前验证映射/证据层 | 边界与交付校验 |
|---|---|---|---|
| A01 | R01/R32 | H01/H02/H04/H05/H06、P-NATIVE原生源码协议、S-CAPS实际adapter版本/形状负例均通过 | N-BRIDGE当前哈希已重验；版本不符仍拒绝旧能力证据，图形能力blocked |
| A02 | R02 | C14/C16、W01 PID复用/同路径双宿主拒写；N-BRIDGE两IDE隔离、dirty拒绝、失败打开保旧及重开 | 软件负例与真实双IDE分别记账，当前哈希N-BRIDGE已重跑 |
| A03 | R03/R04 | N-A15真实66节点；N-BRIDGE真实68节点；N-MODEL扩展夹具70节点以17/页读5页、GUID无遗漏重复；P-NATIVE源码读取 | 70是补夹具后的节点数，不冒充原始66/68；中文业务源不随源码包分发 |
| A04 | R04 | C01/C11不可变快照；N-CORE修改/回读，N-T6最终第3次未保存源码进入变化事件 | 活动编辑器未提交缓冲区仍为editor_sync unknown；对象模型事实不能冒充编辑器全部缓冲已同步 |
| A05 | R05/R06 | N-MODEL公开configuration核对task分类3项（1任务配置+2任务）、5设备；周期/优先级取原生API，空interval_unit保持空 | library/io显式missing及available_metadata_only；空集合不证明实际不存在，完整配置属P2 |
| A06 | R07/R24 | C06哈希核验、固定快照、不调用宿主、失败不发布；N-CORE与N-MON-FAIL的`mcp_native_export`分段通过，manifest/源码区段/dirty有证据 | 最终版本保留导出工件及只读副作用核查；其余模块失败不使导出检查变成失败，也不能反推整体通过 |
| A07 | R08 | I01/I03/I04/I06、G-REAL/G-SCALE；N-MODEL公开search到同snapshot/index_revision源码对齐，C15历史性提示 | 不将未同步编辑缓冲视为已索引，也不把文本命中当语义关系 |
| A08 | R09 | I01/I02/I05、G-REAL作用域/实例/读写/调用金标准；N-MODEL公开references/impact来源对齐 | 限定ST静态语法，不扩张到复杂类型、库内部、图形或运行行为 |
| A09 | R09 | I03/I05/I06/I07、C10/C15；删除/更名/parser升级及旧版重开可查已补；G-GAP真实DB提交后进程退出与重建4场景通过；S-PROCESS index覆盖控制器层 | P0全量索引相关确定性缺口已补；增量等于全量属P1，断电/磁盘损坏不在本轮故障证据内 |
| A10 | R11/R12 | C02/C05/H03/W01受控创建、非法XML/接口/重名/非文本/越界位置零写入；N-MODEL E25/E26真实创建接入 | 只允许受控接口及一个同Application任务可达调用者；不开放通用声明编辑，中文FB名原生编译未获支持 |
| A11 | R13 | C01/C02跨工程/目标后改、W01依赖后改拒旧补丁；N-BRIDGE真实source_conflict | 拒绝发生在写入前；不把软件注入人工修改描述为真实IDE键盘编辑 |
| A12 | R14 | N-CORE及N-MODEL真实写入、创建GUID、调用者和回读；M-METRICS9/9候选精确哈希一致 | 支持范围外声明/类型修改在写前拒绝；编译成功不代表任务意图正确 |
| A13 | R15 | N-CORE17组、N-A15真实坏基线修复、N-MODEL9候选原生编译；W01空消息/异常/仅受理分类 | passed/failed/unknown严格分开，旧模型夹具失败仍保留 |
| A14 | R15/R16 | N-LOCATIONS17组20Error精确定位，18条行列/2条仅行号，中文/Tab真实保留；N-NATIVE-BOUNDARIES错误FB未引用→接入后失败定位GUID→恢复编译通过 | 未引用FB未进入有效编译路径时，Application编译通过不等于该FB已验证；覆盖unknown/未覆盖如实返回 |
| A15 | R17 | C04严格拒绝/repair；I02坏基线可建图；N-A15真实AM402修复/回读/保存重开全部通过 | 首次零错误编译新增空PersistentVars时未误认稳定源码成功，恢复后核对仅系统对象新增，再生成新补丁通过；没有重放过期补丁 |
| A16 | R18 | C03/C04/C07；N-CORE单对象失败自动恢复；N-A15坏基线错误/恢复保留；N-MODEL9候选主动恢复；N-NATIVE-BOUNDARIES创建失败自动两对象补偿通过 | 失败候选诊断与恢复诊断分别保留；坏基线恢复不要求清零。恢复后需fresh capture+完成index作业才能报告当前索引更新 |
| A17 | R14/R18/R30 | C05/C07–C12/C16/C17；S-PROCESS控制器7阶段強退零重放、G-GAP真实DB提交后退出；N-NATIVE-BOUNDARIES自动补偿及同幂等键不重建通过 | 三层不拼成厂商IDE各阶段实际崩溃；未知GUID不按名删除/重建，人工后改/新引用阻止自动覆盖 |
| A18 | R19 | N-BRIDGE真实保存/绑定更新/关闭重开；N-A15公开MCP保存后重开、原文比对及原生编译通过；C10/C16/S-PROCESS save | reconcile返回saved_copy_reconciled且disk_reopen_verified=false本身仍不能算关闭重开；实际重开证明来自N-A15 |
| A19 | R20 | C11/C13/C18；N-T6与N-MODEL修后监测n=3：14.434102535、12.854956865、14.014432430秒；最终源码正确、各稳定11秒无重复 | 去抖10秒，min12.855/median14.014/max14.434秒，小样本非SLA；旧失败版本不混入分布，关闭与默认不自动编译有证据 |
| A20 | R21 | Q01四项金标准通过：规则1/PLC001–003、CRLF/中文/Tab行号、注释/字符串词法边界、未知版本及跨工程拒绝；/0.5和/0_1误报已修 | 结果是lexical_advice/not_a_compiler；注释/字符串命中仍可能有建议，不宣称语义循环分析或完整除零检测 |
| A24 | R10/R22/R23/R32/R33/R34 | P-NATIVE、M-R0/M-R1、M01/M02、M-METRICS；N-MODEL9候选原生编译/恢复，M-LIVE真实模型5工具调用 | 9/9编译与6/9意图完成分开；17回答语义6通过/9失败/2unknown。契约反馈和软件故障注入不冒称真实编译修复或供应商断网实测；无PLC执行 |
| A25 | R24 | C03/C06/C09/C11；N-CORE/N-A15/N-MODEL/N-NATIVE-BOUNDARIES固定作业/诊断/恢复；M-REVIEW104工件hash、M-METRICS候选原件与回读hash | 失败原件与latest固定指向保留；最终交付归档/安装结果见外部source_release_v2_smoke，不覆盖本表的固定原生证据 |
| A27 | R30/R32 | C01/C09/C12/H04/H05/C16；S-PROCESS实际HTTP断线和控制器重启8项通过，同键异参/写前取消软件验证；N-NATIVE-BOUNDARIES失败创建同键不重建 | 原生调用不被强行中断，已进入写入/编译阶段取消返回accepted=false并解释；未知结果不重放。控制器强退证据不冒充厂商IDE崩溃 |
| A28 | R31 | N-BRIDGE最终哈希脚本返回/UI可用/双IDE exit0；N-NATIVE-BOUNDARIES真实模态暂停、明确等待、解除后同请求成功且源码不变、host exit0；协议心跳恢复通过 | 当前受控宿主稳定范围完成；窗口激活、原生编辑定位/内联标红仍属P1，不从UI enabled推断完整编辑体验 |

## 最终冻结记录

| 项目 | 冻结状态与交付校验 |
|---|---|
| 交付版本/源码哈希、完整依赖锁 | 服务与bridge生产源码冻结；adapter.py SHA256 `bb8bb1c70f7b914e6557ef2b4e95c1552d03b231db58e572fefb019f541d2193`；entry.py `34f35cb4013effd1f5dae488f664ebfb06ee337b6250ab6fd53ef661108ce1c0`。依赖见 `S/cgc-runtime-lock.json`、`S/uv.lock`；最终ZIP/安装报告由发布负责人补充 |
| 全量自动测试固定结果 | S-FULL=123 passed、3 skipped、2 subtests passed，39.30秒；S-NATIVE-PROTOCOL另8/8。跳过仅CGC opt-in，独立真实报告仍单列 |
| 当前桥接 L4a 固定报告 | 最终N-BRIDGE 160558整体passed，19 checks+两个IDE exit0，匹配最终adapter/entry源码哈希；150647只保留历史用途 |
| 公开 MCP + Core + CGC + IDE 的 L4b 固定报告 | 格式校验修复前：N-CORE整体failed但17组诊断等分段通过，N-A15整体passed，N-MODEL整体66 checks passed+exit0。最终4行证据形状修复后：N-BRIDGE回归原生流程，N-NATIVE-BOUNDARIES23 checks passed+exit0；Core/index/读写编译方法未改，不冒称旧66检查已在新hash重跑 |
| 实际进程/数据库故障 | S-PROCESS 8项控制器强退/断线（FakeHost层），G-GAP真实DB提交后退出4场景通过；证据层不得互相冒充 |
| 模型输出语义 | M-REVIEW：17回答6符合/9失败/2unknown，9候选6意图符合/3失败；Codex审阅非用户业务验收，无硬百分比门槛 |
| 真实模型 + 工具/原生对齐报告 | N-MODEL9/9编译及恢复、意图6/9、候选0人工改动；M-LIVE另E07真实OMP会话5工具+RESTORED；M-METRICS已固定。模型生成与工具自主调用分母分开 |
| 发布前最后交付校验 | 功能必交证据已齐。最终ZIP源码清单/哈希、干净移位安装和包内测试由外部source_release_v2_smoke补充；安装验证不重新计算模型、原生编译或数据库通过数 |
| P0 放行结论 | **首期功能验收通过；最终交付包以source_release_v2_smoke及发布清单完成最后校验后放行。** 模型任务意图6/9是如实保留的效果基线，不宣称全部任务正确或PLC运行行为已验证 |

首个正式候选包在深解压目录安装时因 Windows 测试临时路径过长失败（68 passed / 54 failed / 4 skipped）；原包与 source_release_smoke.json 保留失败证据。Install.ps1 改用短的系统临时测试目录，运行数据路径未改。最终 v2 包须在同等深度新目录复验，以 source_release_v2_smoke.json 绑定的 ZIP SHA-256 为放行对象；原生桥和 Core 生产代码未因安装脚本修复改变。

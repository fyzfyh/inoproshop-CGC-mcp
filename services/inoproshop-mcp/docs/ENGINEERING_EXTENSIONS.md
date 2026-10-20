# 声明编辑、工程模板和独立文本搜索

2026-10-20 扩展：公开工具从首期 23 项增加为 26 项；原有工具名称保持兼容。验收结果另见 `ENGINEERING_EXTENSIONS_ACCEPTANCE.md`，不能沿用旧版本的通过数。

## 同一 ST 对象的受控声明编辑

继续使用 `ino_preview_change`，增加一种修改类型：

```json
{
  "project_id": "已选择的工程ID",
  "snapshot_id": "已采集且已建立图索引的快照ID",
  "change": {
    "type": "replace_st_sections",
    "object_id": "原生对象GUID",
    "declaration": "PROGRAM PLC_PRG\nVAR\n    x : BOOL;\n    y : BOOL;\n    timer1 : TON;\nEND_VAR\n"
  }
}
```

`declaration`、`implementation` 至少提供一个。省略保留原文；实现体传空字符串表示明确清空；null 被拒绝。每个新字段最多 200000 个字符。声明必须保留 POU 类型和名称，不能用此接口重命名对象、改变 PROGRAM/FB/Function 类型、创建其他工程对象或输入多个 POU 头。

预览同时返回声明与实现差异、旧版本前提、补丁哈希及候选声明的静态解析覆盖。候选解析结果有独立 candidate_id，不冒充原生快照或编译结果。数组、库类型等未覆盖关系仍会标为 unsupported/unresolved；不能据此声称完整类型分析。

应用仍用 `ino_apply_change`，授权绑定预览返回的 project_id、patch_id、patch_hash，并指定 source=user_task。原有任务授权可覆盖请求内的具体改动，不增加无必要的二次确认。保持原有身份/快照检查、基线编译、检查点、写后回读、候选编译、失败条件恢复和未知结果不重放机制。应用成功和保存成功继续分开报告。

新建 FB 的原有 `create_st_pou` 接口仍使用首期受控创建范围。本次不把它扩大为任意复杂接口或多对象事务。

## 模板目录与控制器复核

默认读取源码根目录的 `templates/catalog.json`，也可以用服务进程环境变量 `INO_MCP_TEMPLATES` 指向独立目录。登记格式与操作说明见根目录 `templates/README.md`。源码发布包包含目录说明和登记文件，不打包厂商 `.project` 文件。

1. `ino_list_project_templates` 返回已登记模板、model、预期 controller、源文件 SHA256、选择摘要 template_hash 和状态。ready 才能创建；missing/hash_mismatch 必须先处理。
2. `ino_create_project` 必须指定 template_id、上一步的 template_hash、新 destination、明确的 host_id/host_instance_id 和 idempotency_key。
3. 复用现有打开副本作业。桥接复制后、关闭旧工程前，核对复制字节的 SHA256；目标已存在或当前工程未保存时拒绝。打开后从实际对象模型读取唯一根设备的 type/id/version，与登记值比较。
4. 只有 controller_verification.status=passed 才完成创建。不同、缺失、不完整或多根设备都会明确失败；副本保留供检查，不自动替换控制器。该工程后续源码写入及保存被阻止，成功验证的模板工程在后续写入前也会重新核对。
5. 未收到打开结果时保留 OUTCOME_UNKNOWN，同一幂等键不重开。重新发现宿主不得给未完成验证的副本生成一个绕过验证的新身份。读取、检查和打开另一个明确工程仍可按原有流程操作。

controller.version 是设备描述版本，不能当成控制器固件版本。model 是由登记人核实的型号说明，不以文件名或模型猜测代替原生身份。设备包未安装、身份 API 不可用等情况下，不报告创建成功。

## 无图数据库的快照文本搜索

```json
{
  "project_id": "已选择的工程ID",
  "snapshot_id": "已采集的快照ID",
  "query": "MotorEnable",
  "section": "both",
  "case_sensitive": false,
  "context_lines": 1,
  "offset": 0,
  "limit": 50
}
```

工具名 `context_search_text`。query 是单行字面文本，不执行正则；section 可为 both/declaration/implementation；可用 object_id 限定对象。每个命中行返回一次，含首个命中的一基行列、源码、最多前后各 5 行上下文、对象/区段/快照和源哈希。limit 为 1..500，按稳定对象顺序分页。

此工具只读固定快照，不访问 IDE、不创建图索引、不连接 CGC/Neo4j。工程后续变化不会改写旧快照的结果。新鲜度只基于最近已采集快照，不能据此推断实时编辑缓冲已同步。

返回 match_type=literal_text、semantic_references=false、coverage_scope=available_text_only，并保留无法读取、非文本和原采集问题。零命中不证明图形、加密或未暴露内容中不存在该名称。原来的 context_search/find_references/get_impact 继续保留语义图契约；预览和写入的影响分析要求没有因本次搜索而降低。

## 验证入口

服务测试包括新增故障边界与原有回归。真实原生验收分别运行 `scripts/native_verify.py --state <独立状态目录>` 和 `scripts/native_engineering_extensions.py --state <同一独立状态目录>`。后者启动测试自有隐藏 IDE 与独立 MCP 服务，使用本机真实 CGC/Neo4j、厂商示例的独立副本和用户已授权的测试改动；不调用模型，不连接 PLC。

原生验收要求本机已安装匹配 InoProShop 和图数据库运行环境。源码包缺少厂商参考工程时，不能用 Mock 结果代替原生编译证据。

# 本地工程模板目录

此目录只登记已经由用户核实型号的 `.project` 参考工程。不把通用 Standard.project 当作所有型号模板。厂商工程文件仅留在本机，不随源码包分发。

2026-10-20 完整离线包同样只附带本目录说明和 catalog，不附带厂商或个人 `.project`。其中 `am600-native-reference` 是开发机本地登记项；在接收方未放入并核对参考文件前，其状态为 `missing`。这不影响 MCP 与桥接连接，模板创建须等相应条目变为 `ready`。请按下面的实际设备身份与文件哈希登记自己的模板，不把示例元数据当作本机模板验证结果。

在目录中放置工程副本，并在 `catalog.json` 的 `templates` 中登记：

```json
{
  "template_id": "am600-reference",
  "name": "本机验证的 AM600 参考工程",
  "model": "实际订货型号",
  "project_file": "am600/reference.project",
  "sha256": "填入该文件实际的64位小写SHA256",
  "controller": {
    "type": 4102,
    "id": "从原生device_identity读取的实际标识",
    "version": "从原生device_identity读取的实际版本"
  }
}
```

示例不是可直接使用的设备身份。先对用户明确选择的参考工程使用 `ino_capture_snapshot` 和 `ino_read_configuration(category="device")`，读取根设备 `device_identity` 的 type/id/version；再核对 IDE 显示的实际型号并登记。`version` 是设备描述版本，不能当成控制器固件版本。路径必须在本目录内，可以用子目录；不接受 `..` 或目录联接逃逸。

`ino_list_project_templates` 返回 ready/missing/hash_mismatch 和不可变选择摘要 `template_hash`。创建时必须传 `template_id` 和本次选择的 `template_hash`，不能只凭型号字符串猜模板。文件变化后必须重新核实并更新登记哈希。

`ino_create_project` 只向工程副本目录创建新文件。复制内容校验通过后打开，再核对唯一根设备的原生身份。失败或无法读取时保留副本供检查，作业明确失败，禁止该模板工程的源码修改。它不会为了得到通过结果自动替换控制器。后续写入还会重新核对控制器。

默认目录随源码根目录计算。可用 `INO_MCP_TEMPLATES` 为独立控制器指定其他模板目录。

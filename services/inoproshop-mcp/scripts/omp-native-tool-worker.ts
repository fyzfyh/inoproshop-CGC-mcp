/** Live OMP model calls narrowly projected tools backed by the real MCP service. */
import { parseArgs } from "node:util";
import { readFile, writeFile, rename } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import { pathToFileURL } from "node:url";

const { values } = parseArgs({ options: { input: { type: "string" }, output: { type: "string" } } });
if (!values.input || !values.output) throw new Error("input/output required");
const input = JSON.parse(await readFile(values.input, "utf8"));
if (input.data_scope !== "synthetic_inputs_only" || input.task_id !== "E07-live-tools") throw new Error("Invalid fixture scope");
const root = join(homedir(), ".bun/install/global/node_modules/@oh-my-pi/pi-coding-agent");
const norm = (value: string) => value.replace(/\r\n/g, "\n").trimEnd();
const report: any = { layer: "real_omp_model_to_projected_custom_tools_to_public_mcp_to_native",
  task_id: input.task_id, data_scope: input.data_scope, model_invoked: false, tool_calls: [], public_mcp_calls: [],
  write_dispatched: false, operation_id: null, status: "running", started_at: new Date().toISOString() };
let session: any, connection: any, mcp: any, snapshot: any, patch: any, lastOperation: any;
let previewAttempts = 0;
let persistQueue = Promise.resolve();
let persistSequence = 0;
const persist = () => {
  persistQueue = persistQueue.then(async () => {
    const temporary = values.output! + ".pending-" + (++persistSequence);
    await writeFile(temporary, JSON.stringify(report, null, 2) + "\n", "utf8");
    await rename(temporary, values.output!);
  });
  return persistQueue;
};
const terminal = new Set(["COMPLETED", "FAILED", "REJECTED", "RESTORED", "OUTCOME_UNKNOWN", "NEEDS_RECOVERY", "CANCELLED"]);
async function call(name: string, args: any) {
  const response = await mcp.callTool(connection, name, args);
  report.public_mcp_calls.push({ name, is_error: !!response.isError, at: new Date().toISOString() });
  await persist();
  if (response.isError) throw new Error(response.structuredContent?.error?.code ?? "MCP_ERROR");
  return response.structuredContent;
}
function operationView(operation: any) {
  const result = operation.result ?? {};
  return { operation_id: operation.operation_id, state: operation.state,
    edit_status: result.edit_status ?? null, readback_status: result.readback_status ?? null,
    compile_status: result.compile_status ?? null, persistence_status: result.persistence_status ?? null,
    index_status: result.index_status ?? null, error_code: operation.error?.code ?? null };
}
const emptySchema = { type: "object", properties: {}, additionalProperties: false };
function tool(name: string, description: string, parameters: any, action: (args: any) => Promise<any>, writes = false) {
  return { name, label: name, description, parameters, loadMode: "essential", approval: writes ? "write" : "read",
    async execute(_id: string, args: any) {
      if (report.tool_calls.length >= 24) throw new Error("TOOL_CALL_LIMIT");
      try {
        const result = await action(args);
        report.tool_calls.push({ name, arguments: args, result, at: new Date().toISOString() });
        await persist();
        return { content: [{ type: "text", text: JSON.stringify(result) }], details: { projected: true } };
      } catch (error) {
        // Only an error code is returned; native exceptions can contain OEM source/paths.
        const result = { error_code: error instanceof Error && /^[A-Z_]+$/.test(error.message) ? error.message : "PROJECTED_TOOL_FAILED" };
        report.tool_calls.push({ name, arguments: args, result, at: new Date().toISOString() });
        await persist();
        return { content: [{ type: "text", text: JSON.stringify(result) }], details: { projected: true } };
      }
    } };
}
try {
  mcp = await import(pathToFileURL(join(root, "src/mcp/client.ts")).href);
  const token = (await readFile(join(input.state_dir, "token"), "utf8")).trim();
  connection = await mcp.connectToServer("inoproshop-synthetic-evaluation", {
    type: "http", url: input.url, headers: { Authorization: `Bearer ${token}` }, timeout: 180000 });
  const customTools = [
    tool("ino_read_object", "读取唯一授权的合成 PLC_PRG 的声明和实现。参数已绑定，不可读取其他对象。", emptySchema, async () => {
      snapshot = await call("ino_capture_snapshot", { project_id: input.project_id });
      const actual = await call("ino_read_object", { project_id: input.project_id, snapshot_id: snapshot.snapshot_id, object_id: input.object_id, limit: 500 });
      const declaration = actual.sources.declaration.items.join("\n"), implementation = actual.sources.implementation.items.join("\n");
      if (actual.sources.declaration.next_offset !== null || actual.sources.implementation.next_offset !== null ||
          norm(declaration) !== norm(input.expected_native_declaration) || norm(implementation) !== norm(input.synthetic_implementation)) throw new Error("FIXTURE_MISMATCH");
      const indexing = await call("context_index_snapshot", { project_id: input.project_id, snapshot_id: snapshot.snapshot_id });
      for (let i = 0; i < 120; i++) {
        const operation = await call("ino_get_operation", { project_id: input.project_id, operation_id: indexing.operation_id });
        if (terminal.has(operation.state)) { if (operation.state !== "COMPLETED") throw new Error("INDEX_FAILED"); break; }
        await Bun.sleep(500);
      }
      return { object_id: "program-main", declaration: input.synthetic_declaration, implementation,
        source: "actual_native_readback_verified_against_synthetic_fixture", physical_declaration_remap_excluded: true };
    }),
    tool("ino_preview_change", "预览本次许可条件修改；输入完整实现，保持其他语句。不会写入。", {
      type: "object", properties: { implementation: { type: "string" } }, required: ["implementation"], additionalProperties: false }, async (args) => {
      if (++previewAttempts > 3) throw new Error("REPAIR_LIMIT_REACHED");
      if (!snapshot) throw new Error("READ_REQUIRED");
      if (typeof args.implementation !== "string" || norm(args.implementation) !== norm(input.authorized_implementation)) throw new Error("TASK_SCOPE_MISMATCH");
      patch = await call("ino_preview_change", { project_id: input.project_id, snapshot_id: snapshot.snapshot_id, mode: "strict",
        change: { type: "replace_st_implementation", object_id: input.object_id, implementation: args.implementation } });
      return { patch_id: patch.patch_id, patch_hash: patch.patch_hash, before: input.synthetic_implementation, after: args.implementation, writes: false };
    }),
    tool("ino_apply_change", "按已授权任务应用刚预览的补丁；只受理一次，随后必须查询原作业。", {
      type: "object", properties: { patch_id: { type: "string" } }, required: ["patch_id"], additionalProperties: false }, async (args) => {
      if (!patch || args.patch_id !== patch.patch_id) throw new Error("PREVIEW_REQUIRED");
      if (report.write_dispatched) {
        if (report.operation_id) return { operation_id: report.operation_id, deduplicated: true };
        throw new Error("OUTCOME_UNKNOWN");
      }
      report.write_dispatched = true; await persist();
      const operation = await call("ino_apply_change", { project_id: input.project_id, patch_id: patch.patch_id,
        idempotency_key: input.idempotency_key, authorization: { project_id: input.project_id, patch_id: patch.patch_id, patch_hash: patch.patch_hash, source: "user_task" } });
      report.operation_id = operation.operation_id; await persist();
      return { operation_id: operation.operation_id, state: operation.state };
    }, true),
    tool("ino_get_operation", "查询已受理的作业并等待终态，分别报告编译、保存与索引状态。", emptySchema, async () => {
      if (!report.operation_id) throw new Error("APPLY_REQUIRED");
      for (let i = 0; i < 150; i++) {
        lastOperation = await call("ino_get_operation", { project_id: input.project_id, operation_id: report.operation_id });
        if (terminal.has(lastOperation.state)) break;
        await Bun.sleep(800);
      }
      report.operation = operationView(lastOperation);
      return report.operation;
    }),
    tool("ino_get_diagnostics", "读取本次作业诊断摘要，只保留合成对象的编译器错误。", emptySchema, async () => {
      if (!report.operation_id) throw new Error("APPLY_REQUIRED");
      const result = await call("ino_get_diagnostics", { project_id: input.project_id, operation_id: report.operation_id, limit: 100 });
      return { reports: result.items.map((item: any) => ({ status: item.status, errors: item.errors,
        messages: (item.messages ?? []).filter((row: any) => row.object_id === input.object_id).map((row: any) => ({
          object_id: "program-main", severity: row.severity, number: row.number, text: row.text,
          location: row.location ? { section: row.location.section, line: row.location.line, column: row.location.column } : null })) })) };
    }),
  ];
  const { createAgentSession } = await import(pathToFileURL(join(root, "src/sdk.ts")).href);
  const { SessionManager } = await import(pathToFileURL(join(root, "src/session/session-manager.ts")).href);
  const { Settings } = await import(pathToFileURL(join(root, "src/config/settings.ts")).href);
  const names = customTools.map(item => item.name);
  const created = await createAgentSession({ cwd: input.workspace, modelPattern: input.model, thinkingLevel: "off",
    settings: Settings.isolated({ "retry.enabled": false, "compaction.enabled": false }),
    sessionManager: SessionManager.inMemory(input.workspace), toolNames: names, restrictToolNames: true,
    allowRestrictedCustomTools: true, customTools, enableMCP: false, enableLsp: false, enableIrc: false,
    extensions: [], disableExtensionDiscovery: true, skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
    preloadedCustomToolPaths: [], preloadedExtensionPaths: [], skipPythonPreflight: true, cacheWarming: false,
    autoTitle: false, hasUI: false, deadline: Date.now() + 210000,
    systemPrompt: "你是PLC合成样例验收助手。用户已授权本次离线修改。只能使用给定工具，实际读取后再生成候选、预览、应用、查询原作业直到终态并取诊断。没有调用工具不能声称读取或编译。不能把内存修改说成已保存或PLC运行；不申请重复确认。严格保留未要求改动的语句与注释。" });
  session = created.session;
  report.active_tools = session.getActiveToolNames();
  if (report.active_tools.length !== names.length || report.active_tools.some((name: string) => !names.includes(name)) || created.mcpManager) throw new Error("ISOLATION_FAILED");
  if (input.isolation_check_only === true) {
    report.status = "isolation_checked";
  } else {
  report.model_invoked = true;
  await persist();
  await session.prompt("读取合成 PLC_PRG，在 xRun 的赋值中增加 xPermit 许可条件，改为 xRun := xStart AND xPermit AND NOT xStop;。其他代码和注释必须原样保留。通过工具完成读取、预览、应用、查询终态、读取诊断，然后用中文报告真实编译及保存状态。");
  const final = session.state.messages.filter((message: any) => message.role === "assistant").at(-1);
  report.model = final ? `${final.provider}/${final.model}` : null;
  report.final_response = final?.content.filter((part: any) => part.type === "text").map((part: any) => part.text).join("\n");
  report.usage = final?.usage ?? null;
  const invoked = new Set(report.tool_calls.map((item: any) => item.name));
  report.status = names.every(name => invoked.has(name)) && lastOperation?.state === "COMPLETED" && lastOperation?.result?.compile_status === "passed" ? "passed" : "failed";
  }
} catch (error) {
  report.status = "failed";
  report.error_type = error instanceof Error ? error.constructor.name : "UnknownError";
} finally {
  if (session) await session.dispose().catch(() => {});
  if (connection && mcp) await mcp.disconnectServer(connection).catch(() => {});
  report.finished_at = new Date().toISOString();
  await persist();
  console.log(JSON.stringify({ status: report.status, model_invoked: report.model_invoked, calls: report.tool_calls.length }));
}

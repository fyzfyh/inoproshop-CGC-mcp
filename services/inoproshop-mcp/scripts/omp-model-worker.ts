/** One isolated OMP model generation. No MCP, tools, discovered context or extensions. */
import { parseArgs } from "node:util";
import { readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { homedir } from "node:os";
import { pathToFileURL } from "node:url";

const { values } = parseArgs({ options: {
  input: { type: "string" }, output: { type: "string" },
  "package-root": { type: "string", default: join(homedir(), ".bun/install/global/node_modules/@oh-my-pi/pi-coding-agent") },
} });
if (!values.input || !values.output) throw new Error("--input and --output are required");
const input = JSON.parse(await readFile(values.input, "utf8"));
if (input.data_scope !== "synthetic_inputs_only") throw new Error("Only synthetic fixtures are permitted");
const report: Record<string, unknown> = {
  layer: "real_omp_sdk_model_generation", task_id: input.task_id,
  model_invoked: false, data_scope: "synthetic_inputs_only", tool_calls: 0,
  started_at: new Date().toISOString(), active_tools: [], mcp_enabled: false,
};
let session: any;
const started = performance.now();
try {
  const root = values["package-root"]!;
  const sdk = await import(pathToFileURL(join(root, "src/sdk.ts")).href);
  const { SessionManager } = await import(pathToFileURL(join(root, "src/session/session-manager.ts")).href);
  const { Settings } = await import(pathToFileURL(join(root, "src/config/settings.ts")).href);
  const packageInfo = JSON.parse(await readFile(join(root, "package.json"), "utf8"));
  report.omp_version = packageInfo.version;
  const created = await sdk.createAgentSession({
    cwd: input.workspace, modelPattern: input.model, thinkingLevel: "off",
    settings: Settings.isolated({ "retry.enabled": false, "compaction.enabled": false }),
    sessionManager: SessionManager.inMemory(input.workspace),
    toolNames: [], restrictToolNames: true, enableMCP: false, enableLsp: false, enableIrc: false,
    customTools: [], extensions: [], disableExtensionDiscovery: true,
    skills: [], rules: [], contextFiles: [], promptTemplates: [], slashCommands: [],
    preloadedCustomToolPaths: [], preloadedExtensionPaths: [],
    skipPythonPreflight: true, cacheWarming: false, autoTitle: false, hasUI: false,
    deadline: Date.now() + input.timeout_seconds * 1000,
    systemPrompt: "你是PLC离线评估助手。只根据本次给出的完全合成源码回答，不读文件，不调用任何工具。不要声称执行过编译、MCP或PLC。只输出一个JSON对象，字段必须为 task_id、status（answered/candidate/refused）、answer（中文）、citations（含object_id/section/line的数组）、implementation（编辑或修复题填写完整目标实现，其他为null）、creation（创建FB题填写对象，其他为null）、proposed_tools（拟议工具名数组）、limitations（字符串数组）。creation必须含type=create_st_pou、name、declaration（完整ST功能块声明）、implementation、caller。caller必须含object_id、instance_name、inputs（参数名到表达式的对象）、outputs（输出参数名到现有变量的对象）、insert_after_line、condition（明确给出字符串或null）。普通编辑候选只能修改指定对象的实现，不修改声明；保留未要求改变的语句。需要拒绝或无法证实时明确回答。",
  });
  session = created.session;
  const active = session.getActiveToolNames();
  report.active_tools = active;
  if (active.length !== 0 || created.mcpManager) throw new Error("Isolation contract failed");
  session.subscribe((event: any) => {
    if (event.type === "tool_execution_start") {
      report.tool_calls = Number(report.tool_calls) + 1;
      void session.abort();
    }
  });
  report.model_invoked = true;
  await session.prompt(input.prompt);
  const messages = session.state.messages.filter((message: any) => message.role === "assistant");
  const final = messages.at(-1);
  if (!final) throw new Error("No assistant response");
  report.model = `${final.provider ?? "unknown"}/${final.model ?? "unknown"}`;
  report.usage = final.usage ?? null;
  report.stop_reason = final.stopReason ?? null;
  report.raw_response = final.content;
  report.response_text = final.content.filter((part: any) => part.type === "text").map((part: any) => part.text).join("\n");
  if (Number(report.tool_calls) > 0) throw new Error("A tool was attempted in a restricted session");
  report.status = final.stopReason === "error" || final.stopReason === "aborted" ? "model_failed" : "response_received";
} catch (error) {
  report.status = "model_failed";
  report.error_type = error instanceof Error ? error.constructor.name : "UnknownError";
  // Provider errors may attach HTTP request headers; do not serialize errors.
} finally {
  if (session) await session.dispose().catch(() => {});
  report.completed_at = new Date().toISOString();
  report.elapsed_seconds = (performance.now() - started) / 1000;
  await writeFile(values.output, JSON.stringify(report, null, 2) + "\n", "utf8");
  console.log(JSON.stringify({ task_id: input.task_id, status: report.status, tool_calls: report.tool_calls }));
}


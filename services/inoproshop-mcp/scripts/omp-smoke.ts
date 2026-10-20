/** Exercise the installed OMP MCP client itself; never invokes a model. */
import { parseArgs } from "node:util";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { pathToFileURL } from "node:url";

const { values } = parseArgs({ options: {
  "package-root": { type: "string", default: join(homedir(), ".bun/install/global/node_modules/@oh-my-pi/pi-coding-agent") },
  "state-dir": { type: "string", default: "E:/AI-PLC/work/mcp_p0/state" },
  "url": { type: "string", default: "http://127.0.0.1:8765/mcp" },
  "project-id": { type: "string" },
  "output": { type: "string", default: "E:/AI-PLC/artifacts/mcp_p0/omp_protocol.json" },
} });

const report: Record<string, unknown> = {
  at: new Date().toISOString(),
  layer: "real_installed_omp_mcp_client",
  model_invoked: false,
  native_source_read: false,
  checks: [],
};
const checks = report.checks as Record<string, unknown>[];
let connection: any;
let client: any;
try {
  const packageRoot = values["package-root"]!;
  const packageInfo = JSON.parse(await readFile(join(packageRoot, "package.json"), "utf8"));
  report.omp_version = packageInfo.version;
  const token = (await readFile(join(values["state-dir"]!, "token"), "utf8")).trim();
  if (token.length < 32) throw new Error("Invalid local service token");
  client = await import(pathToFileURL(join(packageRoot, "src/mcp/client.ts")).href);
  connection = await client.connectToServer("inoproshop", {
    type: "http", url: values.url, headers: { Authorization: `Bearer ${token}` }, timeout: 180000,
  });
  report.server = connection.serverInfo;
  checks.push({ name: "omp_initialize", passed: true });
  const tools = await client.listTools(connection);
  if (tools.length !== 26) throw new Error("Expected exactly 26 engineering tools");
  report.tools = tools.map((tool: any) => tool.name);
  checks.push({ name: "omp_tools_list_26", passed: true });
  const caps = await client.callTool(connection, "ino_get_capabilities", {});
  if (caps.isError || !caps.structuredContent) throw new Error("Capabilities lacked a successful structured result");
  checks.push({ name: "omp_structured_capabilities", passed: true });
  const hosts = await client.callTool(connection, "ino_list_hosts", {});
  if (hosts.isError) throw new Error("Host discovery failed");
  report.host_discovery = hosts.structuredContent;
  const invalid = await client.callTool(connection, "ino_read_object", {});
  if (!invalid.isError || invalid.structuredContent?.error?.code !== "INVALID_ARGUMENT") throw new Error("Structured tool-error mapping failed");
  checks.push({ name: "omp_structured_error", passed: true });
  if (values["project-id"]) {
    const project_id = values["project-id"];
    const capture = await client.callTool(connection, "ino_capture_snapshot", { project_id });
    if (capture.isError) {
      report.domain_error = capture.structuredContent?.error?.code;
      throw new Error("Native snapshot capture failed");
    }
    const snapshot = capture.structuredContent;
    if (!snapshot.snapshot_id) throw new Error("Capture did not return a snapshot identity");
    const read = await client.callTool(connection, "ino_read_object", { project_id, snapshot_id: snapshot.snapshot_id });
    if (read.isError || !read.structuredContent) throw new Error("Native object read failed");
    const target = read.structuredContent.items.find((node: any) => node.kind === "pou");
    if (!target) throw new Error("Snapshot contains no POU to read");
    const source = await client.callTool(connection, "ino_read_object", { project_id, snapshot_id: snapshot.snapshot_id, object_id: target.id, limit: 500 });
    if (source.isError || !source.structuredContent?.sources?.declaration?.items?.length) throw new Error("Native POU source read failed");
    report.project_id = project_id;
    report.snapshot_id = snapshot.snapshot_id;
    report.native_source_read = true;
    report.read_result = read.structuredContent;
    report.source_result = source.structuredContent;
    report.native_snapshot_tree_read = true;
    checks.push({ name: "omp_bound_native_snapshot_read", passed: true });
    checks.push({ name: "omp_native_pou_source_read", passed: true });
  }
  report.status = "passed";
} catch (error) {
  report.status = "failed";
  // Do not serialize thrown transport errors: request headers can be attached.
  report.error_type = error instanceof Error ? error.constructor.name : "UnknownError";
  report.error = "OMP protocol verification did not complete; no credentials were recorded.";
  process.exitCode = 1;
} finally {
  if (connection && client) await client.disconnectServer(connection).catch(() => {});
  await mkdir(dirname(values.output!), { recursive: true });
  await writeFile(values.output!, JSON.stringify(report, null, 2) + "\n", "utf8");
  console.log(JSON.stringify({ status: report.status, checks: checks.length, output: values.output, model_invoked: false }));
}

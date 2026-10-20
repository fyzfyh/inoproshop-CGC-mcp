"""Run official MCP client checks against a running real Core service.

Unlike test_server_http.py this does not inject a controlled Core. Optional
--project-id also captures and reads a native-bound snapshot through MCP.
"""
import argparse
import asyncio
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def run(args):
    report = {"at": datetime.now(timezone.utc).isoformat(), "layer": "official_sdk_to_real_core", "mcp_sdk_version": version("mcp"), "model_invoked": False, "native_source_read": False, "checks": []}
    token = (args.state_dir / "token").read_text(encoding="ascii").strip()
    try:
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + token}, timeout=180, trust_env=False) as client:
            async with streamable_http_client(args.url, http_client=client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    init = await session.initialize()
                    report["protocol_version"] = init.protocolVersion
                    report["server"] = init.serverInfo.model_dump()
                    report["checks"].append({"name": "initialize", "passed": True})
                    tools = await session.list_tools()
                    assert len(tools.tools) == 26
                    report["tools"] = [tool.name for tool in tools.tools]
                    report["checks"].append({"name": "26_explicit_tools", "passed": True})
                    for name in ("ino_get_capabilities", "ino_list_hosts", "ino_list_projects"):
                        result = await session.call_tool(name, {})
                        assert not result.isError and isinstance(result.structuredContent, dict)
                        report[name] = result.structuredContent
                        report["checks"].append({"name": name, "passed": True})
                    invalid = await session.call_tool("ino_read_object", {})
                    assert invalid.isError and invalid.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
                    report["checks"].append({"name": "structured_domain_error", "passed": True})
                    if args.project_id:
                        capture = await session.call_tool("ino_capture_snapshot", {"project_id": args.project_id})
                        if capture.isError:
                            report["domain_error"] = (capture.structuredContent or {}).get("error", {}).get("code")
                        assert not capture.isError
                        snapshot_id = capture.structuredContent["snapshot_id"]
                        result = await session.call_tool("ino_read_object", {"project_id": args.project_id, "snapshot_id": snapshot_id})
                        assert not result.isError
                        target = next(node for node in result.structuredContent["items"] if node["kind"] == "pou")
                        source = await session.call_tool("ino_read_object", {"project_id": args.project_id, "snapshot_id": snapshot_id, "object_id": target["id"], "limit": 500})
                        assert not source.isError and source.structuredContent["sources"]["declaration"]["items"]
                        report.update(project_id=args.project_id, snapshot_id=snapshot_id, read_result=result.structuredContent,
                                      source_result=source.structuredContent, native_snapshot_tree_read=True, native_source_read=True)
                        report["checks"].append({"name": "bound_native_snapshot_read", "passed": True})
                        report["checks"].append({"name": "native_pou_source_read", "passed": True})
        report["status"] = "passed"
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__, error="Protocol verification did not complete; credentials are not recorded.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "checks": len(report["checks"]), "output": str(args.output), "model_invoked": False}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--state-dir", type=Path, default=Path("E:/AI-PLC/work/mcp_p0/state"))
    parser.add_argument("--project-id")
    parser.add_argument("--output", type=Path, default=Path("E:/AI-PLC/artifacts/mcp_p0/sdk_protocol.json"))
    raise SystemExit(asyncio.run(run(parser.parse_args())))

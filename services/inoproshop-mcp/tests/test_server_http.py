"""Real TCP + official MCP client contract checks, independent of a native IDE.

The controlled Core in these tests verifies protocol behavior only. Native
integration reports are deliberately produced by separate bridge tests.
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from inoproshop_mcp.common import DomainError
from inoproshop_mcp.server import TOOL_MODELS, create_app


TOKEN = "protocol-test-only-not-a-real-credential-0123456789"


class ControlledCore:
    def __init__(self):
        self.calls = []

    def call(self, name, args):
        self.calls.append((name, args))
        if name == "ino_read_object":
            return {"project_id": args["project_id"], "snapshot_id": args["snapshot_id"], "object_id": "original-guid", "implementation": "// 中文源码\n灯 := 启动 AND NOT 停止;", "source": "controlled_protocol_fixture"}
        if name == "ino_get_capabilities":
            return {"transport": "streamable-http", "native_host": "not_connected_in_this_test"}
        if name == "ino_get_operation":
            return {"operation_id": args["operation_id"], "state": "FAILED", "compile": {"status": "failed"}}
        if name == "ino_compile":
            raise DomainError("STALE_SNAPSHOT", "Snapshot has changed.", {"expected": "snapshot-before"})
        if name == "ino_save_copy":
            raise RuntimeError("INTERNAL_TOKEN_SECRET and C:/private/project.project")
        return {"hosts": [], "source": "controlled_protocol_fixture"}


@pytest.fixture
def http_service():
    core = ControlledCore()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(core, TOKEN, port), log_level="error", access_log=False, proxy_headers=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.02)
    assert server.started, "HTTP server did not start"
    yield f"http://127.0.0.1:{port}/mcp", core
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive(), "HTTP server did not shut down"


@contextlib.asynccontextmanager
async def session_for(url):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, trust_env=False) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                yield session, initialized


@pytest.mark.asyncio
async def test_official_handshake_discovery_and_chinese_read(http_service):
    url, core = http_service
    async with session_for(url) as (session, initialized):
        assert initialized.serverInfo.name == "inoproshop-mcp"
        listing = await session.list_tools()
        assert len(listing.tools) == 26
        assert {tool.name for tool in listing.tools} == set(TOOL_MODELS)
        for tool in listing.tools:
            assert tool.inputSchema["type"] == "object"
            assert tool.inputSchema["additionalProperties"] is False
            assert "args" not in tool.inputSchema["properties"]
        result = await session.call_tool("ino_read_object", {"project_id": "project-a", "snapshot_id": "snap-a", "object_id": "original-guid"})
        assert result.isError is False
        assert "中文源码" in result.structuredContent["implementation"]
        assert json.loads(result.content[0].text) == result.structuredContent
        assert core.calls[-1][1]["offset"] == 0
        assert core.calls[-1][1]["limit"] == 100


@pytest.mark.asyncio
async def test_invalid_args_never_reach_core_and_have_domain_error(http_service):
    url, core = http_service
    async with session_for(url) as (session, _):
        for args in [{}, {"project_id": "p", "snapshot_id": "s", "limit": -1}, {"project_id": "p", "snapshot_id": "s", "script": "PRIVATE_SOURCE"}]:
            result = await session.call_tool("ino_read_object", args)
            assert result.isError is True
            assert result.structuredContent["error"]["code"] == "INVALID_ARGUMENT"
            assert "PRIVATE_SOURCE" not in result.content[0].text
        assert core.calls == []


@pytest.mark.asyncio
async def test_domain_error_and_failed_operation_are_distinct(http_service):
    url, _ = http_service
    async with session_for(url) as (session, _):
        failed = await session.call_tool("ino_compile", {"project_id": "p", "idempotency_key": "key"})
        assert failed.isError is True
        assert failed.structuredContent["error"]["code"] == "STALE_SNAPSHOT"
        polled = await session.call_tool("ino_get_operation", {"project_id": "p", "operation_id": "op"})
        assert polled.isError is False
        assert polled.structuredContent["state"] == "FAILED"


@pytest.mark.asyncio
async def test_unexpected_errors_do_not_disclose_paths_or_secrets(http_service):
    url, _ = http_service
    async with session_for(url) as (session, _):
        result = await session.call_tool("ino_save_copy", {"project_id": "p", "expected_snapshot_id": "s", "destination": "E:/copy.project", "idempotency_key": "k"})
        assert result.isError is True
        assert result.structuredContent["error"]["code"] == "INTERNAL_ERROR"
        assert "INTERNAL_TOKEN_SECRET" not in result.content[0].text
        assert "private" not in result.content[0].text


@pytest.mark.parametrize("headers,status", [
    ({}, 401),
    ({"Authorization": "Bearer wrong"}, 401),
    ({"Authorization": f"Bearer {TOKEN}", "Origin": "https://hostile.example"}, 403),
    ({"Authorization": f"Bearer {TOKEN}", "Origin": "null"}, 403),
    ({"Authorization": f"Bearer {TOKEN}", "Host": "hostile.example"}, 421),
])
def test_http_access_rejections(http_service, headers, status):
    url, core = http_service
    result = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers, trust_env=False)
    assert result.status_code == status
    assert core.calls == []
    assert TOKEN not in result.text


@pytest.mark.asyncio
async def test_create_change_requires_typed_caller_and_no_arbitrary_script(http_service):
    url, core = http_service
    async with session_for(url) as (session, _):
        result = await session.call_tool("ino_preview_change", {"project_id": "p", "snapshot_id": "s", "change": {"type": "create_st_pou", "name": "FB_Example", "declaration": "FUNCTION_BLOCK FB_Example", "implementation": ""}})
        assert result.isError is True
        assert not core.calls


@pytest.mark.asyncio
async def test_create_caller_condition_must_be_explicit_and_null_survives(http_service):
    url, core = http_service
    arguments = {"project_id": "p", "snapshot_id": "s", "change": {"type": "create_st_pou", "name": "FB_Example", "declaration": "FUNCTION_BLOCK FB_Example", "implementation": "", "caller": {"object_id": "caller", "instance_name": "fbNew", "inputs": {}, "outputs": {}, "insert_after_line": 2}}}
    async with session_for(url) as (session, _):
        result = await session.call_tool("ino_preview_change", arguments)
        assert result.isError is True
        assert not core.calls
        arguments["change"]["caller"]["condition"] = None
        result = await session.call_tool("ino_preview_change", arguments)
        assert result.isError is False
        assert "condition" in core.calls[-1][1]["change"]["caller"]
        assert core.calls[-1][1]["change"]["caller"]["condition"] is None


@pytest.mark.asyncio
async def test_index_optional_idempotency_key_reaches_core(http_service):
    url, core = http_service
    async with session_for(url) as (session, _):
        result = await session.call_tool("context_index_snapshot", {"project_id": "p", "snapshot_id": "s", "idempotency_key": "index-1"})
        assert result.isError is False
        assert core.calls[-1][1]["idempotency_key"] == "index-1"


@pytest.mark.asyncio
async def test_reconcile_is_explicit_query_only_argument(http_service):
    url, core = http_service
    arguments = {"project_id": "p", "operation_id": "op", "reconcile": True}
    async with session_for(url) as (session, _):
        queried = await session.call_tool("ino_get_operation", arguments)
        assert queried.isError is False
        assert core.calls[-1][1]["reconcile"] is True
        called = len(core.calls)
        cancelled = await session.call_tool("ino_cancel_operation", arguments)
        assert cancelled.isError is True
        assert len(core.calls) == called


@pytest.mark.asyncio
async def test_new_engineering_tools_have_real_protocol_schemas(http_service):
    url, core = http_service
    async with session_for(url) as (session, _):
        listed = {tool.name for tool in (await session.list_tools()).tools}
        assert {"ino_list_project_templates", "ino_create_project", "context_search_text"} <= listed
        text = await session.call_tool("context_search_text", {"project_id": "p", "snapshot_id": "s", "query": "中文"})
        assert not text.isError and core.calls[-1][0] == "context_search_text"
        change = {"type": "replace_st_sections", "object_id": "o", "declaration": "PROGRAM Existing\nVAR x : INT; END_VAR"}
        preview = await session.call_tool("ino_preview_change", {"project_id": "p", "snapshot_id": "s", "change": change})
        assert not preview.isError and core.calls[-1][1]["change"] == change
        called = len(core.calls)
        change["implementation"] = None
        invalid = await session.call_tool("ino_preview_change", {"project_id": "p", "snapshot_id": "s", "change": change})
        assert invalid.isError and len(core.calls) == called
        invalid = await session.call_tool("ino_create_project", {"template_id": "t", "destination": "new.project", "host_id": "h", "host_instance_id": "hi", "idempotency_key": "once"})
        assert invalid.isError and len(core.calls) == called
        valid = await session.call_tool("ino_create_project", {"template_id": "t", "template_hash": "a" * 64, "destination": "new.project", "host_id": "h", "host_instance_id": "hi", "idempotency_key": "once"})
        assert not valid.isError and core.calls[-1][0] == "ino_create_project"


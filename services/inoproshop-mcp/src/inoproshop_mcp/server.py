"""Official MCP SDK transport and explicit, validated P0 tool contracts.

The transport owns no project state. Every tool delegates to the shared Core.
No generic Python/script execution, online PLC operation, or path-read tool is
published. HTTP is authenticated and loopback-only, including Origin checks.
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import json
import logging
from typing import Annotated, Any, Literal

import anyio
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_serializer, model_validator
from starlette.applications import Starlette
from starlette.routing import Route


Identifier = Annotated[str, Field(min_length=1, max_length=256)]
FilePath = Annotated[str, Field(min_length=1, max_length=4096)]
Limit = Annotated[int, Field(ge=1, le=500)]
Offset = Annotated[int, Field(ge=0)]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class HostFilter(Input):
    host_id: Identifier | None = None
    host_instance_id: Identifier | None = None


class Capabilities(HostFilter):
    project_id: Identifier | None = None


class Project(HostFilter):
    project_id: Identifier


class Snapshot(Project):
    snapshot_id: Identifier


class IndexSnapshot(Snapshot):
    idempotency_key: Identifier | None = None


class OpenProject(Input):
    source: FilePath
    destination: FilePath
    host_id: Identifier
    host_instance_id: Identifier
    idempotency_key: Identifier


class ReadObject(Snapshot):
    object_id: Identifier | None = None
    section: Literal["both", "declaration", "implementation"] = "both"
    offset: Offset = 0
    limit: Limit = 100


class CreateProject(Input):
    template_id: Identifier
    template_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    destination: FilePath
    host_id: Identifier
    host_instance_id: Identifier
    idempotency_key: Identifier


class TextSearch(Snapshot):
    query: Annotated[str, Field(min_length=1, max_length=8192)]
    section: Literal["both", "declaration", "implementation"] = "both"
    object_id: Identifier | None = None
    case_sensitive: bool = False
    context_lines: Annotated[int, Field(ge=0, le=5)] = 0
    offset: Offset = 0
    limit: Limit = 50


class ReadConfiguration(Snapshot):
    category: Literal["task", "device", "library", "io"] = "task"


class ExportContext(Snapshot):
    destination: FilePath | None = None


class Search(Snapshot):
    query: Annotated[str, Field(min_length=1, max_length=8192)]
    limit: Limit = 50
    offset: Offset = 0
    index_revision: Identifier | None = None


class References(Snapshot):
    symbol_id: Identifier
    relation: Literal["READS", "WRITES", "CALLS", "DECLARES", "EXECUTES", "TYPE_OF", "INSTANCE_OF", "CONTAINS"] | None = None
    index_revision: Identifier | None = None
    limit: Limit = 50
    offset: Offset = 0


class Impact(Snapshot):
    object_id: Identifier
    max_depth: Annotated[int, Field(ge=1, le=10)] = 3
    index_revision: Identifier | None = None
    limit: Limit = 50
    offset: Offset = 0


class ReplaceST(Input):
    type: Literal["replace_st_implementation"]
    object_id: Identifier
    implementation: Annotated[str, Field(max_length=1000000)]


class Caller(Input):
    object_id: Identifier
    instance_name: Annotated[str, Field(min_length=1, max_length=128)]
    inputs: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)
    insert_after_line: Annotated[int, Field(ge=0)]
    condition: str | None


class ReplaceSTSections(Input):
    type: Literal["replace_st_sections"]
    object_id: Identifier
    declaration: Annotated[str, Field(max_length=200000)] | None = None
    implementation: Annotated[str, Field(max_length=200000)] | None = None

    @model_validator(mode="after")
    def explicit_sections(self):
        supplied = self.model_fields_set & {"declaration", "implementation"}
        if not supplied or any(getattr(self, key) is None for key in supplied):
            raise ValueError("Supply at least one string section; omit a section to preserve it. Null is not source text.")
        return self

    @model_serializer(mode="wrap")
    def supplied_only(self, handler):
        return {key: value for key, value in handler(self).items() if value is not None}


class CreateST(Input):
    type: Literal["create_st_pou"]
    name: Annotated[str, Field(min_length=1, max_length=80)]
    declaration: Annotated[str, Field(min_length=1, max_length=1000000)]
    implementation: Annotated[str, Field(max_length=1000000)]
    caller: Caller


class PreviewChange(Snapshot):
    change: Annotated[ReplaceST | ReplaceSTSections | CreateST, Field(discriminator="type")]
    mode: Literal["strict", "repair"] = "strict"
    allow_partial_repair: bool = False


class Authorization(Input):
    project_id: Identifier
    patch_id: Identifier
    patch_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    source: Literal["user_task"]


class ApplyChange(Project):
    patch_id: Identifier
    idempotency_key: Identifier
    authorization: Authorization
    save_destination: FilePath | None = None


class Compile(Project):
    idempotency_key: Identifier
    expected_snapshot_id: Identifier | None = None
    application_id: Identifier | None = None


class Diagnostics(Project):
    operation_id: Identifier | None = None
    current: bool = False
    offset: Offset = 0
    limit: Limit = 100


class Restore(Project):
    checkpoint_id: Identifier
    expected_snapshot_id: Identifier
    idempotency_key: Identifier


class SaveCopy(Project):
    expected_snapshot_id: Identifier
    destination: FilePath
    idempotency_key: Identifier


class Operation(Project):
    operation_id: Identifier


class GetOperation(Operation):
    reconcile: bool = False


class Monitor(Project):
    enabled: bool
    poll_seconds: Annotated[float, Field(ge=0.5, le=3600)] = 1.0
    debounce_seconds: Annotated[float, Field(ge=0.5, le=3600)] = 2.0
    auto_compile: bool = False


class Changes(Project):
    after_cursor: Offset = 0
    limit: Limit = 100


class CheckRules(Snapshot):
    rule_set_version: Literal["1"] = "1"


# This table is also exported as a machine-readable contract by `schemas`.
TOOL_MODELS: dict[str, tuple[type[Input], str, bool]] = {
    "ino_get_capabilities": (Capabilities, "Inspect verified/limited/blocked capabilities; do not infer capabilities from method names.", True),
    "ino_list_hosts": (Input, "List registered InoProShop bridge instances, not arbitrary Windows processes.", True),
    "ino_list_projects": (HostFilter, "List registered projects and their explicit host bindings and memory state.", True),
    "ino_open_project": (OpenProject, "Open an isolated project copy on the specified host. Never overwrite a dirty user project.", False),
    "ino_list_project_templates": (Input, "List explicit local templates with pinned bytes, model and expected native controller identity. Missing or changed templates are not ready.", True),
    "ino_create_project": (CreateProject, "Open a new copy of a hash-bound template and verify its native root controller. Failed or unknown verification blocks source writes.", False),
    "ino_capture_snapshot": (Project, "Capture current host-object-model content, including synchronized unsaved source, into an immutable snapshot.", True),
    "ino_read_object": (ReadObject, "Read object tree or paginated ST/GVL source from a fixed snapshot with GUID and source locations.", True),
    "ino_read_configuration": (ReadConfiguration, "Read supported configuration facts from a fixed snapshot; missing fields remain explicitly unknown.", True),
    "ino_export_context": (ExportContext, "Export a fixed snapshot as an AI context package with source and coverage manifests.", True),
    "context_index_snapshot": (IndexSnapshot, "Build and publish a versioned PLC symbol graph for this immutable snapshot.", True),
    "context_search": (Search, "Search the PLC snapshot graph with source evidence, bounded results, and index revision.", True),
    "context_search_text": (TextSearch, "Search available immutable snapshot text literally without any graph backend or host calls. Text matches are not semantic references.", True),
    "context_find_references": (References, "Find scope-aware references using a stable symbol ID; distinguish uncertain candidate edges.", True),
    "context_get_impact": (Impact, "Return a bounded dependency/impact graph with coverage and uncertain relations.", True),
    "ino_preview_change": (PreviewChange, "Preview a single ST implementation or declaration/implementation edit preserving POU identity, or a new FB with an explicit caller. Return immutable diffs and exact patch hash without writing source.", True),
    "ino_apply_change": (ApplyChange, "Apply an explicitly authorized immutable patch with backup, readback and native compile. Never retry an unknown write outcome.", False),
    "ino_compile": (Compile, "Compile the bound active Application and preserve actual scope, completion evidence and original diagnostics.", True),
    "ino_get_diagnostics": (Diagnostics, "Read paginated diagnostics tied to their original compilation snapshot; do not treat an empty message list as success.", True),
    "ino_restore_checkpoint": (Restore, "Conditionally restore a checkpoint only if the expected current version still matches, then verify recovery.", False),
    "ino_save_copy": (SaveCopy, "Persist the expected in-memory version to an allowed project-copy destination and report binding changes.", False),
    "ino_get_operation": (GetOperation, "Poll a durable operation and inspect editing, compilation, saving and index statuses separately. Explicit reconcile=true reconciles an unknown save outcome with read-only evidence; it never replays the native write.", True),
    "ino_cancel_operation": (Operation, "Request cancellation; this cannot interrupt an in-flight proprietary host API call.", False),
    "ino_set_monitor": (Monitor, "Enable/disable bounded change monitoring. Automatic compile is opt-in; monitoring never changes or saves source.", False),
    "ino_get_changes": (Changes, "Read changes after a cursor; resynchronize on expired cursors rather than silently skipping events.", True),
    "ino_check_rules": (CheckRules, "Run deterministic lexical rules. Results are distinct from native compile and cannot establish logic or runtime safety.", True),
}


def tool_definitions() -> list[Tool]:
    return [
        Tool(
            name=name,
            description=description,
            inputSchema=model.model_json_schema(),
            outputSchema={"type": "object", "additionalProperties": True},
            annotations=ToolAnnotations(readOnlyHint=readonly, destructiveHint=not readonly, openWorldHint=False),
        )
        for name, (model, description, readonly) in TOOL_MODELS.items()
    ]


def tool_result(data: dict[str, Any], *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))],
        structuredContent=data,
        isError=is_error,
    )


def error_result(code: str, message: str, details: dict | None = None) -> CallToolResult:
    return tool_result({"error": {"code": code, "message": message, "details": details or {}}}, is_error=True)


def create_server(core: Any) -> Server:
    server = Server("inoproshop-mcp", version="0.1.0")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return tool_definitions()

    # Pydantic is the single validator so invalid input also gets a structured
    # domain error. The official SDK still owns protocol parsing and framing.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        from .common import DomainError

        definition = TOOL_MODELS.get(name)
        if definition is None:
            return error_result("UNSUPPORTED_TOOL", "This tool is not exposed by the P0 service.")
        try:
            parsed = definition[0].model_validate(arguments)
            # Only omit optional top-level arguments. Nested explicit nulls
            # (notably an unconditional caller.condition) are contractual.
            validated = {key: value for key, value in parsed.model_dump().items() if value is not None}
        except ValidationError as exc:
            issues = [{"path": list(err["loc"]), "type": err["type"]} for err in exc.errors(include_input=False, include_url=False)]
            return error_result("INVALID_ARGUMENT", "Arguments do not match the published tool schema.", {"issues": issues})
        try:
            data = await anyio.to_thread.run_sync(core.call, name, validated)
            if not isinstance(data, dict):
                return error_result("INTERNAL_ERROR", "The operation returned an invalid result; inspect local operation records.")
            return tool_result(data)
        except DomainError as exc:
            return error_result(exc.code, exc.message, exc.details)
        except Exception:
            # Never return exceptions/tracebacks: they may contain local paths,
            # source text, request headers, or a transport credential.
            logging.getLogger(__name__).error("Core call failed with an unexpected exception for %s", name)
            return error_result("INTERNAL_ERROR", "The operation could not complete; inspect local operation records before retrying.")

    return server


class LoopbackAuthMiddleware:
    def __init__(self, app: Any, token: str, port: int, origins: list[str] | None = None):
        if len(token) < 32:
            raise ValueError("The local service token must contain at least 32 characters.")
        self.app = app
        self.authorization = ("Bearer " + token).encode("ascii")
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        self.origins = set(origins or [f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"])

    async def __call__(self, scope: dict, receive: Any, send: Any):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = scope.get("headers", [])
        grouped: dict[bytes, list[bytes]] = {}
        for key, value in headers:
            grouped.setdefault(key.lower(), []).append(value)
        peer = (scope.get("client") or ("", 0))[0]
        try:
            local = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local = False
        host = grouped.get(b"host", [])
        origin = grouped.get(b"origin", [])
        auth = grouped.get(b"authorization", [])
        if not local:
            return await self.reject(send, 403, "Loopback access required")
        if len(host) != 1 or host[0].decode("latin-1").lower() not in self.hosts:
            return await self.reject(send, 421, "Invalid Host header")
        if len(origin) > 1 or (origin and origin[0].decode("latin-1") not in self.origins):
            return await self.reject(send, 403, "Invalid Origin header")
        if len(auth) != 1 or not hmac.compare_digest(auth[0], self.authorization):
            return await self.reject(send, 401, "Local service authentication required")
        return await self.app(scope, receive, send)

    @staticmethod
    async def reject(send: Any, status: int, message: str):
        body = json.dumps({"error": message}).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": body})


def create_app(core: Any, token: str, port: int = 8765) -> Any:
    server = create_server(core)
    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"],
            allowed_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"],
        ),
    )

    async def endpoint(scope: dict, receive: Any, send: Any):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with manager.run():
            yield

    # A callable ASGI instance avoids Starlette interpreting a function as a
    # Request->Response endpoint. The SDK owns the entire HTTP exchange.
    class Endpoint:
        async def __call__(self, scope: dict, receive: Any, send: Any):
            await endpoint(scope, receive, send)

    app = Starlette(routes=[Route("/mcp", Endpoint(), methods=["GET", "POST", "DELETE"])], lifespan=lifespan)
    return LoopbackAuthMiddleware(app, token, port)


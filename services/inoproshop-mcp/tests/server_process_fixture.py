"""Owned controller subprocess with a persistent FAKE host; never native proof."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

import uvicorn

from inoproshop_mcp.__main__ import ensure_token, service_lock
from inoproshop_mcp.common import atomic_json
from inoproshop_mcp.core import Core
from inoproshop_mcp.server import create_app
from test_core import FakeHost, create_patch, preview, snapshot


def run(root, mode, port, restart):
    root.mkdir(parents=True, exist_ok=True)
    projects = root / "projects"
    projects.mkdir(exist_ok=True)
    state = root / "state"
    armed = False

    def marker(value):
        with (root / "fake_calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def crash(point):
        atomic_json(root / "crash.json", {"mode": mode, "point": point,
                    "pid": os.getpid(), "test_double": True, "native_evidence": False})
        # Exit the actual controller process without Core.close/Store.close.
        os._exit(86)

    class PersistentFakeHost(FakeHost):
        def __init__(self):
            super().__init__(projects)
            saved = root / "fake_host.json"
            if saved.exists():
                values = json.loads(saved.read_text(encoding="utf-8"))
                for key, value in values.items():
                    setattr(self, key, value)

        def call(self, command, params):
            result = super().call(command, params)
            atomic_json(root / "fake_host.json", {key: getattr(self, key) for key in ("nodes", "write_count", "created_count")})
            if command in {"replace_text", "import_st", "delete_object", "compile", "save_copy"}:
                marker({"command": command, "pid": os.getpid(), "fake": True})
            if armed and not restart:
                if mode == "compile" and command == "compile":
                    crash("COMPILING_fake_reply_lost")
                if mode == "apply_compile" and command == "compile" and self.write_count:
                    crash("COMPILING_after_persisted_write")
                if mode == "save" and command == "save_copy":
                    crash("SAVING_fake_file_written_reply_lost")
                if mode == "disconnect" and command == "compile":
                    atomic_json(root / "blocked.json", {"pid": os.getpid(), "test_double": True})
                    deadline = time.monotonic() + 30
                    while not (root / "release").exists():
                        if time.monotonic() > deadline:
                            raise TimeoutError("Test client did not release the fake compile")
                        time.sleep(0.02)
            return result

    with service_lock(state):
        token = ensure_token(state)
        host = PersistentFakeHost()
        core = Core(state, projects, lambda *_: host, index_backend=False)
        core._hosts = host.records
        try:
            boot_path = root / "boot.json"
            if not boot_path.exists():
                project_id = core.ino_list_projects()["projects"][0]["project_id"]
                captured = snapshot(core, project_id, indexed=False)
                if mode in {"write", "create", "attach", "apply_compile"}:
                    patch = create_patch(core, project_id) if mode in {"create", "attach"} else preview(core, project_id)
                    args = {"project_id": project_id, "patch_id": patch["patch_id"],
                            "idempotency_key": "crash-once", "authorization": dict({key: patch[key] for key in ("project_id", "patch_id", "patch_hash")}, source="user_task")}
                    tool = "ino_apply_change"
                elif mode == "save":
                    tool = "ino_save_copy"
                    args = {"project_id": project_id, "expected_snapshot_id": captured["snapshot_id"],
                            "destination": str(projects / "saved.project"), "idempotency_key": "crash-once"}
                elif mode == "index":
                    tool = "context_index_snapshot"
                    args = {"project_id": project_id, "snapshot_id": captured["snapshot_id"], "idempotency_key": "crash-once"}
                else:
                    tool = "ino_compile"
                    args = {"project_id": project_id, "expected_snapshot_id": captured["snapshot_id"], "idempotency_key": "crash-once"}
                atomic_json(boot_path, {"project_id": project_id, "tool": tool, "args": args})

            original_put = core.store.put
            def put(kind, key, value):
                original_put(kind, key, value)
                if armed and not restart and kind == "checkpoint_state":
                    if mode == "create" and value.get("phase") == "created":
                        crash("APPLYING_created_GUID_and_checkpoint_durable")
                    if mode in {"write", "attach"} and value.get("phase") == "written":
                        crash("APPLYING_" + mode + "_checkpoint_durable")
            core.store.put = put
            if mode == "index" and not restart:
                import inoproshop_mcp.index as index_module
                original_atomic = index_module._atomic
                def atomic(path, value):
                    original_atomic(path, value)
                    if armed and Path(path).name.startswith("idx_"):
                        crash("INDEXING_revision_written_before_active_publication")
                index_module._atomic = atomic
            armed = True
            atomic_json(root / "ready.json", {"pid": os.getpid(), "port": port, "restart": restart})
            uvicorn.run(create_app(core, token, port), host="127.0.0.1", port=port,
                        log_level="error", access_log=False, proxy_headers=False)
        finally:
            core.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("mode")
    parser.add_argument("port", type=int)
    parser.add_argument("--restart", action="store_true")
    args = parser.parse_args()
    run(args.root, args.mode, args.port, args.restart)

"""Command-line lifecycle; one controller per state directory."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sys


def database_fingerprint(token: str, environment=None) -> str:
    """Compare controller connection settings without persisting database secrets."""
    environment = os.environ if environment is None else environment
    values = {key: environment.get(key) for key in
              ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE")}
    encoded = json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(token.encode("ascii"), encoded, hashlib.sha256).hexdigest()


@contextlib.contextmanager
def service_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = (state_dir / "service.lock").open("a+b")
    try:
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        lock.close()
        raise RuntimeError("Another controller may already own this state directory.") from exc
    try:
        yield
    finally:
        lock.close()


def ensure_token(state_dir: Path) -> str:
    state_dir.mkdir(parents=True, exist_ok=True)
    token_path = state_dir / "token"
    if not token_path.exists():
        try:
            with token_path.open("x", encoding="ascii") as stream:
                stream.write(secrets.token_urlsafe(48))
            if os.name != "nt":
                token_path.chmod(0o600)
        except FileExistsError:
            pass
    token = token_path.read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise RuntimeError("Invalid local token file. Restore or explicitly rotate it before starting.")
    if os.name == "nt":
        # Tokens live only on this workstation. Remove inherited read access
        # and grant the current Windows account and LocalSystem explicitly.
        import win32api
        import win32con
        import win32security
        try:
            access_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
            try:
                user_sid = win32security.GetTokenInformation(access_token, win32security.TokenUser)[0]
            finally:
                access_token.Close()
            system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)
            acl = win32security.ACL()
            for sid in (user_sid, system_sid):
                acl.AddAccessAllowedAce(win32security.ACL_REVISION, win32con.GENERIC_ALL, sid)
            descriptor = win32security.GetFileSecurity(str(token_path), win32security.DACL_SECURITY_INFORMATION)
            descriptor.SetSecurityDescriptorDacl(1, acl, 0)
            win32security.SetFileSecurity(str(token_path), win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION, descriptor)
        except Exception as exc:
            raise RuntimeError("Could not restrict the local token file to the current Windows account.") from exc
    return token


def main(argv: list[str] | None = None) -> int:
    workspace = Path(os.environ.get("INO_MCP_WORKSPACE", Path(__file__).resolve().parents[4]))
    parser = argparse.ArgumentParser(description="Local InoProShop MCP controller")
    parser.add_argument("command", choices=["serve", "schemas"], nargs="?", default="serve")
    parser.add_argument("--state-dir", type=Path, default=Path(os.environ.get("INO_MCP_STATE", workspace / "work/mcp_p0/state")))
    parser.add_argument("--project-root", type=Path, default=Path(os.environ.get("INO_MCP_PROJECTS", workspace / "work/mcp_p0/projects")))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output", type=Path, help="Schemas output path; omit to print schemas")
    args = parser.parse_args(argv)
    from .server import create_app, tool_definitions

    if args.command == "schemas":
        result = json.dumps({"version": "0.1.0", "tools": [tool.model_dump(exclude_none=True) for tool in tool_definitions()]}, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(result + "\n", encoding="utf-8")
        else:
            print(result)
        return 0
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535")
    from .core import Core
    import uvicorn

    state_dir, project_root = args.state_dir.resolve(), args.project_root.resolve()
    with service_lock(state_dir):
        token = ensure_token(state_dir)
        from .common import atomic_json, utc
        from .launcher_database import process_identity
        atomic_json(state_dir / "service-runtime.json", {
            "schema_version": 1, "pid": os.getpid(), "started_at": utc(),
            "state_dir": str(state_dir), "project_root": str(project_root),
            "port": args.port, "executable": sys.executable,
            "process_identity": process_identity(os.getpid()),
            "database_fingerprint": database_fingerprint(token),
        })
        core = Core(state_dir, project_root)
        try:
            app = create_app(core, token, args.port)
            uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, proxy_headers=False, log_level="warning")
        finally:
            core.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)

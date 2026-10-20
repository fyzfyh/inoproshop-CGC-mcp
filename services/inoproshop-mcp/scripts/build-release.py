"""Build a deterministic, source-only distribution with an explicit manifest.

Includes the native IronPython bridge and its actual demo-module dependencies.
Never packages state, tokens, credentials, engineering projects, or runtimes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def release_files(root: Path):
    service = root / "services/inoproshop-mcp"
    allowed_suffixes = {".py", ".md", ".toml", ".lock", ".json", ".ps1", ".ts", ".cmd", ".st", ".xml"}
    selected = set()
    for path in (root / "templates").rglob("*"):
        if path.is_file() and path.suffix.lower() in {".md", ".json"}:
            selected.add(path)  # Catalog and instructions only; vendor .project files stay local.
    for directory in (service / "src", service / "tests", service / "scripts", service / "config", service / "fixtures", service / "skills", service / "docs", root / "adapters/inoproshop/bridge"):
        if directory.exists():
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix.lower() in allowed_suffixes and "__pycache__" not in path.parts and not any(part.startswith(".venv") for part in path.parts):
                    selected.add(path)
    for name in ("pyproject.toml", "uv.lock", "README.md", "CONTRACT.md", "cgc-runtime-lock.json", "launcher-runtime-lock.json", "requirements-cgc-adapter.lock"):
        path = service / name
        if path.exists():
            selected.add(path)
    for path in (root / "scripts").glob("native*"):
        if path.is_file() and path.suffix.lower() in allowed_suffixes:
            selected.add(path)
    database_smoke = root / "scripts/launcher-database-smoke.py"
    if database_smoke.is_file():
        selected.add(database_smoke)
    prd = root / "docs/requirements/PRD-InoProShop-MCP.md"
    if not prd.exists():
        raise RuntimeError("The release must include the current MCP requirements baseline")
    selected.add(prd)
    for name in ("Connect-Ino.cmd", "CONNECT-INOPROSHOP.md"):
        path = root / name
        if not path.is_file():
            raise RuntimeError("Missing one-click entry or user guide: " + name)
        selected.add(path)
    # ai_package and diagnostics import legacy core. Native acceptance imports
    # host and the ST diagnostic fixture driver; include only known source.
    for name in ("ai_package.py", "diagnostics.py", "core.py", "host.py", "verify_diagnostics.py", "boolean_ld.py"):
        path = root / "demo/v2" / name
        if path.exists():
            selected.add(path)
    if not (root / "demo/v2/ai_package.py") in selected or not (root / "demo/v2/core.py") in selected:
        raise RuntimeError("Required bridge dependencies are missing")
    for path in selected:
        relative = path.relative_to(root)
        if not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("A release file resolves outside the source checkout")
        if path.name.lower() in {"token", "credentials.json", "bridge.json"} or path.suffix.lower() in {".project", ".db", ".zip", ".exe", ".dll"}:
            raise RuntimeError("Non-source or credential file selected for release")
        yield relative.as_posix(), path.read_bytes()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output or root / "artifacts/mcp_p0/release/inoproshop-mcp-0.1.0-source.zip"
    files = dict(sorted(release_files(root)))
    manifest = {
        "schema_version": "1.0", "product": "inoproshop-mcp", "version": "0.1.0", "kind": "source_distribution",
        "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in files.items()},
        "excluded": ["work", "tokens", "credentials", "engineering_projects", "virtual_environments", "vendor_installations", "generated_test_outputs"],
        "installation": "Extract preserving all paths, then run Connect-Ino.cmd. InoProShop and OMP must already be installed. First run downloads pinned dependencies; see CONNECT-INOPROSHOP.md. A service wheel alone is not a complete desktop deployment.",
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    files["RELEASE-MANIFEST.json"] = manifest_bytes
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3  # Stable metadata across Windows/POSIX builders.
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for name, expected in manifest["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected["sha256"]
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_bytes(manifest_bytes)
    summary = {"status": "verified", "zip": str(output), "manifest": str(manifest_path), "files": len(manifest["files"]), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "bytes": output.stat().st_size}
    output.with_suffix(".verification.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()

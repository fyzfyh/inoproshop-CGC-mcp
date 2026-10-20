"""Build a relocatable Windows distribution from a clean Python and locked caches.

No virtual environment, user state or database data is copied into the release.
The recipient does not need uv, pip, Python, Java, Docker or network downloads.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(argv, env):
    subprocess.run(list(map(str, argv)), env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--python-home", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--uv-cache", type=Path, required=True)
    parser.add_argument("--java-archive", type=Path, required=True)
    parser.add_argument("--neo4j-archive", type=Path, required=True)
    args = parser.parse_args()
    root, destination = args.root.resolve(), args.destination.resolve()
    archive = destination.parent / (destination.name + ".zip")
    if destination.exists() or archive.exists():
        parser.error("Choose a new destination; existing deliverables are never overwritten.")
    lock = json.loads((root / "services/inoproshop-mcp/launcher-runtime-lock.json").read_text())
    for name, runtime_archive in (("java", args.java_archive), ("neo4j", args.neo4j_archive)):
        if digest(runtime_archive) != lock[name]["archive_sha256"]:
            parser.error(name + " archive does not match the release lock")
        with zipfile.ZipFile(runtime_archive) as package:
            if package.testzip():
                parser.error(name + " archive is corrupt")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("UV_") or key in ("PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE"):
            env.pop(key)
    env.update(UV_CACHE_DIR=str(args.uv_cache.resolve()), UV_OFFLINE="1", PYTHONDONTWRITEBYTECODE="1",
               PYTHONUTF8="1", PYTHONNOUSERSITE="1", UV_NO_CONFIG="1")
    result = subprocess.run([str(args.python_home / "python.exe"), "-I", "-B", "-c",
                             "import sys; print('.'.join(map(str,sys.version_info[:3])))"],
                            capture_output=True, text=True, check=True, env=env)
    if result.stdout.strip() != lock["python"]["version"]:
        parser.error("The clean Python interpreter does not match the runtime lock")
    destination.mkdir(parents=True)
    print("Copying release source and clean Python...", flush=True)
    spec = importlib.util.spec_from_file_location("source_release", Path(__file__).with_name("build-release.py"))
    source = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(source)
    for name, data in source.release_files(root):
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    readme = root / "README.md"
    shutil.copyfile(readme, destination / "README.md")
    python_home = destination / "runtime/python"
    shutil.copytree(args.python_home, python_home,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "Scripts", "*.log", "EXTERNALLY-MANAGED"))
    archives = destination / "offline/archives"
    archives.mkdir(parents=True)
    shutil.copyfile(args.java_archive, archives / "java.zip")
    shutil.copyfile(args.neo4j_archive, archives / "neo4j.zip")
    requirements = destination / "offline/python-requirements.lock"
    run([args.uv, "export", "--project", root / "services/inoproshop-mcp", "--frozen", "--extra", "cgc",
         "--extra", "test", "--no-emit-project", "--no-header", "--quiet", "--offline", "--output-file", requirements], env)
    print("Installing all locked Python dependencies from local cache only...", flush=True)
    # sync consumes the artifact URLs/hashes from uv.lock directly. Unlike an
    # index resolver it needs no cached PyPI index responses in offline mode.
    build_environment = destination / "offline/.build-venv"
    env["UV_PROJECT_ENVIRONMENT"] = str(build_environment)
    run([args.uv, "sync", "--project", destination / "services/inoproshop-mcp", "--python", python_home / "python.exe",
         "--offline", "--frozen", "--extra", "cgc", "--extra", "test", "--no-install-project", "--no-config",
         "--no-python-downloads", "--link-mode", "copy", "--no-progress"], env)
    shutil.copytree(build_environment / "Lib/site-packages", python_home / "Lib/site-packages", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("_virtualenv.py", "_virtualenv.pth", "__pycache__", "*.pyc"))
    if not build_environment.resolve().is_relative_to(destination.resolve() / "offline"):
        raise RuntimeError("Refusing cleanup outside the build environment")
    shutil.rmtree(build_environment)
    # Generated console scripts embed the build directory. The product launches
    # modules via python.exe; removing these prevents accidentally using them.
    if (python_home / "Scripts").exists():
        shutil.rmtree(python_home / "Scripts")
    for path in python_home.rglob("__pycache__"):
        shutil.rmtree(path)
    for path in python_home.rglob("*.pyc"):
        path.unlink()
    run([python_home / "python.exe", "-I", "-B", "-c",
         "import mcp,neo4j,codegraphcontext,pytest,win32api,win32security,uvicorn; "
         "from codegraphcontext.core.database import DatabaseManager; print('Portable Python imports passed')"], env)
    # Inventory comes from the packaged interpreter rather than the build host.
    inventory = subprocess.run([str(python_home / "python.exe"), "-I", "-B", "-c",
        "import importlib.metadata as m,json; print(json.dumps(sorted([{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()],key=lambda d:d['name'].lower()),indent=2))"],
        env=env, capture_output=True, text=True, check=True).stdout
    (destination / "offline/python-packages.json").write_text(inventory, encoding="utf-8")
    notices = root / "services/inoproshop-mcp/docs/THIRD_PARTY_OFFLINE.md"
    shutil.copyfile(notices, destination / "THIRD-PARTY-NOTICES.md")
    print("Hashing offline payload...", flush=True)
    files = {path.relative_to(destination).as_posix(): {"sha256": digest(path), "bytes": path.stat().st_size}
             for path in sorted(destination.rglob("*")) if path.is_file()}
    manifest = {"schema_version": 1, "kind": "offline_windows_x64", "product": "inoproshop-mcp",
                "python": lock["python"]["version"], "java": lock["java"]["version"],
                "neo4j": lock["neo4j"]["version"], "first_run_downloads": False, "files": files}
    (destination / "offline/manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run([python_home / "python.exe", "-I", "-B", destination / "services/inoproshop-mcp/scripts/verify-offline.py",
         "--root", destination], env)
    print("Compressing the complete offline folder...", flush=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as package:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                # Always include a top-level folder for Explorer's Extract All.
                name = destination.name + "/" + path.relative_to(destination).as_posix()
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with package.open(info, "w", force_zip64=True) as target, path.open("rb") as stream:
                    shutil.copyfileobj(stream, target)
    with zipfile.ZipFile(archive) as package:
        if package.testzip():
            raise RuntimeError("Final ZIP validation failed")
    summary = {"status": "verified", "folder": str(destination), "zip": str(archive),
               "zip_bytes": archive.stat().st_size, "uncompressed_bytes": sum(v["bytes"] for v in files.values()),
               "files": len(files), "sha256": digest(archive), "network_used_to_build": False}
    archive.with_suffix(".verification.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    archive.with_suffix(".sha256").write_text(summary["sha256"] + "  " + archive.name + "\n", encoding="ascii")
    print(json.dumps(summary, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()

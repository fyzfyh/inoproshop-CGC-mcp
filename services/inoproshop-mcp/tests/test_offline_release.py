"""Offline delivery boundaries: no download fallback, intact portable payload."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from inoproshop_mcp import launcher_database as db


def forbidden(*args, **kwargs):
    raise AssertionError("Offline execution attempted a network download")


def test_offline_missing_or_corrupt_archive_never_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "urlopen", forbidden)
    corrupt = tmp_path / "bad.zip"
    corrupt.write_bytes(b"incomplete")
    with pytest.raises(db.DatabaseLauncherError) as error:
        db._archive("https://example.invalid/java.zip", "a" * 64, tmp_path / "cache",
                    candidates=[corrupt], offline=True)
    assert error.value.code == "OFFLINE_ARCHIVE_MISSING"
    assert not (tmp_path / "cache").exists()


def test_offline_archive_uses_verified_bundled_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "urlopen", forbidden)
    bundled = tmp_path / "java.zip"
    bundled.write_bytes(b"verified")
    assert db._archive("https://example.invalid/java.zip", hashlib.sha256(b"verified").hexdigest(),
                       tmp_path / "cache", [bundled], offline=True) == bundled
    assert not (tmp_path / "cache").exists()


def test_offline_java_ignores_system_installation(tmp_path, monkeypatch):
    service = tmp_path / "services/inoproshop-mcp"
    service.mkdir(parents=True)
    (tmp_path / "offline").mkdir()
    (tmp_path / "offline/manifest.json").write_text("{}")
    java = {"version": "21.0.6+7", "archive_root": "jdk-21.0.6+7-jre",
            "archive_url": "https://example.invalid/java.zip", "archive_sha256": "a" * 64}
    (service / "launcher-runtime-lock.json").write_text(json.dumps({"java": java}))
    monkeypatch.setattr(db, "_find_java", forbidden)
    monkeypatch.setattr(db, "_java_version", lambda path: "21.0.6")
    home, version = db._ensure_java(tmp_path, tmp_path / "generated", forbidden)
    assert home == tmp_path / "generated/java/jdk-21.0.6+7-jre"
    assert version == "21.0.6"


def test_offline_manifest_detects_damage_and_relocation(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/verify-offline.py"
    spec = importlib.util.spec_from_file_location("offline_verifier", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = tmp_path / "first"
    (original / "offline").mkdir(parents=True)
    (original / "file.dat").write_bytes(b"payload")
    (original / "offline/manifest.json").write_text(json.dumps({"kind": "offline_windows_x64", "files": {
        "file.dat": {"sha256": hashlib.sha256(b"payload").hexdigest(), "bytes": 7}}}))
    moved = tmp_path / "中文 moved"
    original.rename(moved)
    assert module.verify(moved)["status"] == "verified"
    (moved / "file.dat").write_bytes(b"damaged")
    with pytest.raises(ValueError, match="checksum mismatch"):
        module.verify(moved)


def test_offline_manifest_rejects_parent_paths(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/verify-offline.py"
    spec = importlib.util.spec_from_file_location("offline_verifier_paths", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "root"
    (root / "offline").mkdir(parents=True)
    (tmp_path / "outside").write_bytes(b"private")
    (root / "offline/manifest.json").write_text(json.dumps({"kind": "offline_windows_x64", "files": {
        "../outside": {"sha256": hashlib.sha256(b"private").hexdigest(), "bytes": 7}}}))
    with pytest.raises(ValueError, match="invalid file"):
        module.verify(root)


def test_offline_builder_keeps_input_archives_and_dotted_destination(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[3]
    script = root / "services/inoproshop-mcp/scripts/build-offline-release.py"
    spec = importlib.util.spec_from_file_location("offline_builder_destination", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lock = json.loads((root / "services/inoproshop-mcp/launcher-runtime-lock.json").read_text())
    archives = {}
    for name in ("java", "neo4j"):
        path = tmp_path / (name + ".zip")
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("LICENSE", "unmodified input " + name)
        archives[name] = path
    originals = {path: path.read_bytes() for path in archives.values()}
    digest = module.digest
    monkeypatch.setattr(module, "digest", lambda path: lock[next(name for name, value in archives.items() if value == Path(path))]["archive_sha256"]
                        if Path(path) in originals else digest(path))
    python_home = tmp_path / "clean-python"
    python_home.mkdir()
    (python_home / "python.exe").write_bytes(b"test interpreter placeholder")
    destination = tmp_path / "Product-20261020.2"

    def fake_process(argv, **kwargs):
        args = list(map(str, argv))
        if "export" in args:
            Path(args[args.index("--output-file") + 1]).write_text("# test lock\n")
        if "sync" in args:
            (Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"]) / "Lib/site-packages").mkdir(parents=True)
        output = "3.12.7\n" if "sys.version_info" in args[-1] else "[]\n"
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_process)
    monkeypatch.setattr(sys, "argv", [str(script), "--root", str(root), "--destination", str(destination),
                        "--python-home", str(python_home), "--uv", "unused-uv", "--uv-cache", str(tmp_path / "cache"),
                        "--java-archive", str(archives["java"]), "--neo4j-archive", str(archives["neo4j"])])
    module.main()
    result = tmp_path / (destination.name + ".zip")
    assert result.is_file()
    assert not destination.with_suffix(".zip").exists()
    assert all(path.read_bytes() == data for path, data in originals.items())
    with zipfile.ZipFile(result) as package:
        assert destination.name + "/LICENSE" in package.namelist()

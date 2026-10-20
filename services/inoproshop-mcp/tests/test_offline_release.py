"""Offline delivery boundaries: no download fallback, intact portable payload."""
import hashlib
import importlib.util
import json
from pathlib import Path

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

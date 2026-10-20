"""Verify an offline distribution using only Python's standard library."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


def verify(root: Path):
    root = root.resolve()
    manifest = json.loads((root / "offline/manifest.json").read_text(encoding="utf-8"))
    if manifest.get("kind") != "offline_windows_x64" or not manifest.get("files"):
        raise ValueError("Invalid offline manifest")
    for name, expected in manifest["files"].items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("Missing or invalid file: " + name)
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != expected["bytes"] or digest != expected["sha256"]:
            raise ValueError("File checksum mismatch: " + name)
    return {"status": "verified", "files": len(manifest["files"]), "network_used": False}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.root), ensure_ascii=True))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print("Offline verification failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

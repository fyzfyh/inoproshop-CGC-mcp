"""Explicit local reference projects, pinned content, and native controller checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .common import DomainError, digest, inside


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def controller_identity(value):
    if (not isinstance(value, dict) or set(value) != {"type", "id", "version"} or
            type(value["type"]) is not int or value["type"] < 0 or
            any(not isinstance(value[k], str) or not value[k].strip() or len(value[k]) > 256 for k in ("id", "version"))):
        raise DomainError("TEMPLATE_CATALOG_INVALID", "Controller requires a native integer type, string id and device-description version.")
    return {"type": value["type"], "id": value["id"].strip(), "version": value["version"].strip()}


class TemplateCatalog:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def list(self):
        path = self.root / "catalog.json"
        if not path.exists():
            return {"templates": [], "catalog_status": "not_configured"}
        try:
            if path.stat().st_size > 1048576:
                raise ValueError("catalog too large")
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or data.get("schema_version") != "1.0" or not isinstance(data.get("templates"), list) or len(data["templates"]) > 500:
                raise ValueError("catalog shape")
            result, ids = [], set()
            for entry in data["templates"]:
                if not isinstance(entry, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", entry.get("template_id", "")):
                    raise ValueError("template id")
                if entry["template_id"].casefold() in ids:
                    raise ValueError("duplicate template id")
                ids.add(entry["template_id"].casefold())
                for key in ("name", "model", "project_file"):
                    if not isinstance(entry.get(key), str) or not entry[key].strip():
                        raise ValueError("missing " + key)
                if not re.fullmatch(r"[0-9a-f]{64}", entry.get("sha256", "")):
                    raise ValueError("template checksum")
                identity = controller_identity(entry.get("controller"))
                relative = Path(entry["project_file"])
                if relative.is_absolute() or relative.suffix.lower() != ".project":
                    raise ValueError("relative .project required")
                source = inside(self.root / relative, self.root)
                record = dict(entry, controller=identity, source=str(source))
                record["template_hash"] = digest({k: v for k, v in record.items() if k != "source"})
                record["status"] = "missing" if not source.is_file() else "ready" if file_sha256(source) == entry["sha256"] else "hash_mismatch"
                result.append(record)
            return {"templates": result, "catalog_status": "configured"}
        except (OSError, ValueError, TypeError, DomainError) as exc:
            raise DomainError("TEMPLATE_CATALOG_INVALID", "Template catalog is invalid or contains a path outside its directory.") from exc

    def select(self, template_id, template_hash):
        rows = [item for item in self.list()["templates"] if item["template_id"] == template_id]
        if len(rows) != 1:
            raise DomainError("TEMPLATE_NOT_FOUND", "Choose a template ID returned by ino_list_project_templates.")
        record = rows[0]
        if record["template_hash"] != template_hash:
            raise DomainError("STALE_TEMPLATE", "Catalog entry changed since it was selected; list templates again.")
        if record["status"] != "ready":
            raise DomainError("TEMPLATE_UNAVAILABLE", "Template is missing or its bytes differ from the registered checksum.", {"status": record["status"]})
        return record


def verify_controller(snapshot, expected):
    nodes = {n["id"]: n for n in snapshot["nodes"]}
    roots = []
    for node in nodes.values():
        if node.get("kind") != "device":
            continue
        parent, visited, nested = node.get("parent_id"), set(), False
        while parent in nodes and parent not in visited:
            visited.add(parent)
            if nodes[parent].get("kind") == "device":
                nested = True
                break
            parent = nodes[parent].get("parent_id")
        if not nested:
            roots.append(node)
    result = {"expected": expected, "root_device_count": len(roots), "status": "unknown",
              "basis": "native_root_device_identification", "device_version_is_firmware": False}
    if any(item.get("field") in {"children", "guid", "hierarchy", "child_name"} for item in snapshot.get("issues", [])):
        return dict(result, reason="Native hierarchy capture is incomplete; controller uniqueness is not proven.")
    if len(roots) != 1:
        return dict(result, reason="Expected exactly one root device; no first-device fallback.")
    root = roots[0]
    result.update(object_id=root["id"], path=root.get("path"))
    identity = root.get("device_identity") or {}
    if identity.get("status") != "read":
        return dict(result, reason="Native controller identity is unavailable.")
    try:
        actual = controller_identity({k: identity.get(k) for k in ("type", "id", "version")})
    except DomainError:
        return dict(result, reason="Native controller identity is incomplete.")
    matches = actual["type"] == expected["type"] and all(actual[k].casefold() == expected[k].casefold() for k in ("id", "version"))
    return dict(result, actual=actual, status="passed" if matches else "failed",
                reason="Native device identity matches." if matches else "Root controller differs from the selected template specification.")

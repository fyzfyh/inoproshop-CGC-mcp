from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class DomainError(Exception):
    def __init__(self, code: str, message: str, details=None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def as_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


def utc():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix):
    return prefix + "_" + uuid.uuid4().hex


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def node_hash(node):
    return digest([node.get("declaration"), node.get("implementation")])


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def inside(path, root):
    target, base = Path(path).resolve(), Path(root).resolve()
    if target == base or not target.is_relative_to(base):
        raise DomainError("PATH_OUTSIDE_ROOT", "Destination must be inside the configured project/output directory.")
    return target


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[^\W\d]\w*", value, re.UNICODE):
        raise DomainError("INVALID_IDENTIFIER", "Expected an IEC identifier.")
    return value


def page(items, offset=0, limit=100):
    if not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise DomainError("INVALID_ARGUMENT", "offset >= 0 and 1 <= limit <= 500 required.")
    return {"items": items[offset:offset + limit], "total": len(items),
            "next_offset": offset + limit if offset + limit < len(items) else None}

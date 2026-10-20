"""Literal, paginated search of immutable available text; no graph or host I/O."""
from __future__ import annotations

import hashlib
import re

from .common import DomainError, node_hash, page


def search_snapshot(snapshot, query, section="both", object_id=None, case_sensitive=False,
                    context_lines=0, offset=0, limit=50):
    page([], offset, limit)
    if (not isinstance(query, str) or not query.strip() or len(query) > 8192 or
            "\n" in query or "\r" in query or section not in {"both", "declaration", "implementation"} or
            not isinstance(case_sensitive, bool) or type(context_lines) is not int or not 0 <= context_lines <= 5):
        raise DomainError("INVALID_ARGUMENT", "Use one nonempty literal line, valid sections, and 0..5 context lines.")
    nodes = snapshot.get("nodes", [])
    if object_id is not None:
        nodes = [n for n in nodes if n["id"] == object_id]
        if len(nodes) != 1:
            raise DomainError("OBJECT_NOT_FOUND", "Object is not in this snapshot.")
    pattern = re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
    sections = ("declaration", "implementation") if section == "both" else (section,)
    hits, coverage, total = [], [], 0
    for node in sorted(nodes, key=lambda n: (n.get("path", ""), n["id"])):
        for part in sections:
            text = node.get(part)
            status = "read" if isinstance(text, str) else (node.get("source_status") or {}).get(part, "unavailable")
            coverage.append({"object_id": node["id"], "section": part, "status": status})
            if not isinstance(text, str):
                continue
            source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            lines = text.splitlines()
            for index, line in enumerate(lines):
                match = pattern.search(line)
                if match is None:
                    continue
                if offset <= total < offset + limit:
                    start, end = max(0, index - context_lines), min(len(lines), index + context_lines + 1)
                    hits.append({"object_id": node["id"], "path": node.get("path"), "section": part,
                        "line": index + 1, "column": match.start() + 1, "text": line,
                        "context": {"first_line": start + 1, "lines": lines[start:end]},
                        "source": {"project_id": snapshot["project_id"], "snapshot_id": snapshot["snapshot_id"],
                            "object_id": node["id"], "section": part, "line": index + 1,
                            "column": match.start() + 1, "content_hash": source_hash, "object_hash": node_hash(node)}})
                total += 1
    return {"project_id": snapshot["project_id"], "snapshot_id": snapshot["snapshot_id"],
        "query": query, "match_type": "literal_text", "semantic_references": False,
        "items": hits, "total": total, "offset": offset, "limit": limit,
        "next_offset": offset + limit if offset + limit < total else None,
        "coverage": coverage, "coverage_scope": "available_text_only", "issues": snapshot.get("issues", []),
        "editor_sync": snapshot.get("editor_sync", "unknown"), "graph_required": False}

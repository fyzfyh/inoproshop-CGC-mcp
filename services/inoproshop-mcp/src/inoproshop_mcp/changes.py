"""Deterministic, bounded edits. Candidates never execute code in this process."""
from __future__ import annotations

import copy
import difflib
import re

from .common import DomainError, digest, identifier, node_hash, uid, utc

SCALAR_TYPES = {"BOOL", "BYTE", "WORD", "DWORD", "SINT", "USINT", "INT", "UINT", "DINT", "UDINT", "REAL", "TIME"}


def lookup(snapshot, object_id):
    matches = [n for n in snapshot["nodes"] if n["id"] == object_id]
    if len(matches) != 1:
        raise DomainError("OBJECT_NOT_FOUND", "Expected one object in the specified snapshot.")
    return matches[0]


def diff(before, after, name):
    return "".join(difflib.unified_diff((before or "").splitlines(True), (after or "").splitlines(True),
                                       fromfile=name + "/before", tofile=name + "/after"))


def expression(value, output=False):
    if not isinstance(value, str) or not value.strip():
        raise DomainError("INVALID_BINDING", "Every parameter requires an explicit expression.")
    if any(x in value for x in (";", "\n", "\r", "//", "(*", "*)", ":=", "=>")):
        raise DomainError("INVALID_BINDING", "Binding must be one expression, not ST statements.")
    if output and not re.fullmatch(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*", value, re.UNICODE):
        raise DomainError("INVALID_BINDING", "Output bindings must be explicit variable/member references.")
    if value.count("(") != value.count(")"):
        raise DomainError("INVALID_BINDING", "Unbalanced binding expression.")
    return value


def interface(declaration, expected_name):
    source = re.sub(r"\(\*.*?\*\)|//[^\r\n]*", "", declaration, flags=re.S)
    header = re.match(r"\s*FUNCTION_BLOCK\s+([^\W\d]\w*)\b", source, re.I)
    if not header or header.group(1).casefold() != expected_name.casefold():
        raise DomainError("UNSUPPORTED_CAPABILITY", "P0 creation requires one matching FUNCTION_BLOCK declaration.")
    tail = source[header.end():]
    blocks = list(re.finditer(r"\b(VAR_INPUT|VAR_OUTPUT|VAR)\b(.*?)\bEND_VAR\b", tail, re.S | re.I))
    remainder = re.sub(r"\b(VAR_INPUT|VAR_OUTPUT|VAR)\b.*?\bEND_VAR\b", "", tail, flags=re.S | re.I)
    if remainder.strip():
        raise DomainError("UNSUPPORTED_CAPABILITY", "Creation supports scalar VAR/VAR_INPUT/VAR_OUTPUT blocks only.")
    result, names = {"inputs": [], "outputs": []}, set()
    for block in blocks:
        statements = block.group(2).split(";")
        if statements[-1].strip():
            raise DomainError("INVALID_DECLARATION", "Variable declaration requires a semicolon.")
        for statement in statements[:-1]:
            match = re.fullmatch(r"\s*([^:]+):\s*(\w+)\s*(?::=\s*(.+))?\s*", statement, re.S)
            if not match or match.group(2).upper() not in SCALAR_TYPES:
                raise DomainError("UNSUPPORTED_CAPABILITY", "Unsupported variable declaration/type in new FB.")
            for raw_name in match.group(1).split(","):
                name = identifier(raw_name.strip())
                if name.casefold() in names:
                    raise DomainError("INVALID_DECLARATION", "Duplicate variable name.")
                names.add(name.casefold())
                if block.group(1).upper() == "VAR_INPUT": result["inputs"].append(name)
                if block.group(1).upper() == "VAR_OUTPUT": result["outputs"].append(name)
    return result


def make_patch(snapshot, change, mode="strict", allow_partial_repair=False):
    if mode not in ("strict", "repair"):
        raise DomainError("INVALID_ARGUMENT", "mode must be strict or repair.")
    kind = change.get("type")
    patch = {"schema_version": "1.0", "patch_id": uid("patch"), "project_id": snapshot["project_id"],
             "snapshot_id": snapshot["snapshot_id"], "base_hash": snapshot["hash"],
             "binding_id": snapshot.get("binding_id"), "mode": mode,
             "allow_partial_repair": bool(allow_partial_repair), "type": kind, "created_at": utc(),
             "changes": [], "new_object": None}
    if allow_partial_repair:
        # Partial success needs semantic diagnostic matching; do not silently accept an unimplemented policy.
        raise DomainError("UNSUPPORTED_CAPABILITY", "P0 repair requires the candidate to compile fully; partial repair is not enabled.")
    if kind in ("replace_st_implementation", "replace_implementation"):
        node = lookup(snapshot, change.get("object_id"))
        implementation = change.get("implementation")
        if node.get("implementation") is None or not isinstance(implementation, str):
            raise DomainError("UNSUPPORTED_CAPABILITY", "Only existing textual ST implementation can be replaced.")
        if not re.match(r"\s*(PROGRAM|FUNCTION_BLOCK|FUNCTION)\b", node.get("declaration") or "", re.I):
            raise DomainError("UNSUPPORTED_CAPABILITY", "Object is not a supported ST POU.")
        after = dict(node, implementation=implementation)
        patch["target_object_id"] = node["id"]
        patch["changes"].append({"before": copy.deepcopy(node), "after": after})
    elif kind == "replace_st_sections":
        node = lookup(snapshot, change.get("object_id"))
        if not all(isinstance(node.get(section), str) for section in ("declaration", "implementation")):
            raise DomainError("UNSUPPORTED_CAPABILITY", "Both original ST source sections must be readable.")
        sections = {key: change[key] for key in ("declaration", "implementation") if key in change}
        if not sections or any(not isinstance(value, str) or len(value) > 200000 for value in sections.values()):
            raise DomainError("INVALID_ARGUMENT", "Supply at least one string section, at most 200000 characters each; omit sections to preserve them.")
        # Object identity changes are separate operations. Comments and strings
        # cannot smuggle a second POU header past this lexical identity check.
        from .index import tokenize
        def identity(source):
            tokens = tokenize(source)
            if len(tokens) < 2 or tokens[0].upper not in {"PROGRAM", "FUNCTION_BLOCK", "FUNCTION"} or tokens[1].kind != "id":
                raise DomainError("INVALID_DECLARATION", "Expected one ST POU declaration with its original type and name.")
            if any(t.kind == "id" and t.upper in {"PROGRAM", "FUNCTION_BLOCK", "FUNCTION"} for t in tokens[2:]):
                raise DomainError("INVALID_DECLARATION", "A declaration must contain only one POU header.")
            return tokens[0].upper, tokens[1].value.casefold()
        if identity(node["declaration"]) != identity(sections.get("declaration", node["declaration"])):
            raise DomainError("OBJECT_IDENTITY_CHANGE", "Declaration editing cannot rename a POU or change its kind.")
        after = dict(node, **sections)
        patch["target_object_id"] = node["id"]
        patch["changes"].append({"before": copy.deepcopy(node), "after": after})
        patch["edited_sections"] = list(sections)
    elif kind == "create_st_pou":
        if mode != "strict":
            raise DomainError("UNSUPPORTED_CAPABILITY", "Automatic create-and-attach requires a passing baseline.")
        name = identifier(change.get("name"))
        if len(name) > 80:
            raise DomainError("INVALID_IDENTIFIER", "New FB names must contain at most 80 characters.")
        declaration, implementation = change.get("declaration"), change.get("implementation")
        if not isinstance(declaration, str) or not isinstance(implementation, str):
            raise DomainError("INVALID_ARGUMENT", "New FB declaration and implementation are required.")
        if any(n["name"].casefold() == name.casefold() for n in snapshot["nodes"]):
            raise DomainError("NAME_CONFLICT", "An object already uses the requested POU name.")
        parameters = interface(declaration, name)
        caller = change.get("caller") or {}
        node = lookup(snapshot, caller.get("object_id"))
        if node.get("implementation") is None or not re.match(r"\s*PROGRAM\b", node.get("declaration") or "", re.I):
            raise DomainError("UNSUPPORTED_CAPABILITY", "Choose an existing task-reachable ST PROGRAM as caller.")
        instance = identifier(caller.get("instance_name"))
        if re.search(r"(?i)(?<!\w)" + re.escape(instance) + r"(?!\w)", node["declaration"]):
            raise DomainError("NAME_CONFLICT", "Instance identifier is already present in caller declaration.")
        parts = []
        for direction, operator in (("inputs", ":="), ("outputs", "=>")):
            bindings = caller.get(direction)
            if not isinstance(bindings, dict) or {k.casefold() for k in bindings} != {k.casefold() for k in parameters[direction]}:
                raise DomainError("INVALID_BINDING", "Provide every input and output binding explicitly.", {"required": parameters})
            folded = {k.casefold(): v for k, v in bindings.items()}
            for parameter in parameters[direction]:
                parts.append(parameter + " " + operator + " " + expression(folded[parameter.casefold()], direction == "outputs"))
        if "condition" not in caller:
            raise DomainError("INVALID_BINDING", "Specify condition=null for unconditional execution, or an explicit condition.")
        line = instance + "(" + ", ".join(parts) + ");"
        if caller["condition"] is not None:
            line = "IF " + expression(caller["condition"]) + " THEN\n    " + line + "\nEND_IF;"
        lines = node["implementation"].splitlines(True)
        position = caller.get("insert_after_line")
        if not isinstance(position, int) or isinstance(position, bool) or not 0 <= position <= len(lines):
            raise DomainError("INVALID_ARGUMENT", "Choose insert_after_line between 0 and the caller line count.")
        # Never split an existing physical line; line position is part of the immutable preview.
        prefix = "".join(lines[:position])
        if prefix and not prefix.endswith(("\n", "\r")): prefix += "\n"
        after = dict(node, declaration=node["declaration"].rstrip() + "\nVAR\n    " + instance + " : " + name + ";\nEND_VAR\n",
                     implementation=prefix + line + "\n" + "".join(lines[position:]))
        patch["target_object_id"] = node["id"]
        patch["changes"].append({"before": copy.deepcopy(node), "after": after})
        patch["new_object"] = {"name": name, "declaration": declaration, "implementation": implementation,
                               "application_id": node.get("application_id"), "original_exists": False}
        patch["caller_plan"] = copy.deepcopy(caller)
    else:
        raise DomainError("UNSUPPORTED_CAPABILITY", "Unsupported change type.")
    patch["application_id"] = patch["changes"][0]["before"].get("application_id")
    for item in patch["changes"]:
        item["before_hash"], item["after_hash"] = node_hash(item["before"]), node_hash(item["after"])
        item["diff"] = {s: diff(item["before"].get(s), item["after"].get(s), item["before"]["path"] + "/" + s)
                        for s in ("declaration", "implementation")}
    patch["patch_hash"] = digest(patch)
    return patch

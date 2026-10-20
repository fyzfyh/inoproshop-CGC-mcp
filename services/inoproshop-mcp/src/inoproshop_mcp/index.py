"""Bounded, evidence-bearing PLC graph extraction and immutable index publication.

This is a PLC-specific parser/IR extension, not CGC's generic language parser.
Production publication and retrieval use CGCNeo4jBackend. ``backend=False`` is
an explicit deterministic-test mode and never claims database integration.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PARSER_VERSION = "inoproshop-st-p0/1.0.0"
GRAPH_SCHEMA_VERSION = "plc-ir/1.0.0"
ELEMENTARY = set("BOOL BYTE WORD DWORD LWORD SINT INT DINT LINT USINT UINT UDINT ULINT REAL LREAL TIME LTIME DATE TIME_OF_DAY TOD DATE_AND_TIME DT STRING WSTRING".split())
KEYWORDS = set("IF THEN ELSE ELSIF END_IF TRUE FALSE AND OR XOR NOT MOD RETURN EXIT PROGRAM FUNCTION FUNCTION_BLOCK END_PROGRAM END_FUNCTION END_FUNCTION_BLOCK VAR VAR_INPUT VAR_OUTPUT VAR_IN_OUT VAR_GLOBAL VAR_TEMP VAR_STAT END_VAR CONSTANT RETAIN PERSISTENT AT".split()) | ELEMENTARY
UNSUPPORTED = set("FOR WHILE REPEAT CASE POINTER REFERENCE ARRAY STRUCT UNION METHOD PROPERTY INTERFACE ACTION EXTENDS IMPLEMENTS ADR REF __QUERYINTERFACE".split())
IDENT = re.compile(r"(?:[^\W\d]|_)\w*", re.UNICODE)
LEX = re.compile(r"(?P<space>\s+)|(?P<string>'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\")|(?P<address>%[IQM][XBWDL]?\d+(?:\.\d+)*)|(?P<number>(?:[A-Za-z_]+#)?(?:\d+#)?[0-9][0-9A-Fa-f_]*(?:\.[0-9]+)?(?:[eE][+-]?\d+)?(?:ms|us|ns|s|m|h|d)?)|(?P<op>:=|=>|>=|<=|<>|S=|R=)|(?P<id>(?:[^\W\d]|_)\w*)|(?P<punct>[;:,.()+*/=<>\-\[\]])|(?P<bad>.)", re.UNICODE | re.IGNORECASE)


def _error(code: str, message: str, details=None):
    from .common import DomainError
    raise DomainError(code, message, details or {})


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _snapshot_fingerprint(snapshot):
    return _digest({key: snapshot.get(key, []) for key in ("nodes", "relations", "tasks")})


def _atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".index-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@dataclass(frozen=True)
class Token:
    value: str
    kind: str
    start: int
    end: int
    line: int
    column: int

    @property
    def upper(self):
        return self.value.upper()


def _mask_comments(text: str, incomplete=None) -> str:
    """Mask nested IEC comments and // comments while preserving source offsets."""
    chars = list(text)
    pos = 0
    quote = None
    while pos < len(text):
        if quote:
            if text[pos] == "$" and pos + 1 < len(text):
                pos += 2
                continue
            if text[pos] == quote:
                if pos + 1 < len(text) and text[pos + 1] == quote:
                    pos += 2
                    continue
                quote = None
            pos += 1
            continue
        if text[pos] in "'\"":
            quote = text[pos]
            pos += 1
            continue
        end = pos
        if text.startswith("//", pos):
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
        elif text.startswith("(*", pos):
            end, depth = pos + 2, 1
            while end < len(text) and depth:
                if text.startswith("(*", end):
                    depth += 1
                    end += 2
                elif text.startswith("*)", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            if depth and incomplete is not None:
                incomplete.append(pos)
        if end > pos:
            for index in range(pos, end):
                if chars[index] not in "\r\n":
                    chars[index] = " "
            pos = end
        else:
            pos += 1
    return "".join(chars)


def tokenize(text: str) -> list[Token]:
    incomplete = []
    masked = _mask_comments(text, incomplete)
    result = []
    line, column, last = 1, 1, 0
    for match in LEX.finditer(masked):
        gap = masked[last:match.start()]
        if "\n" in gap:
            line += gap.count("\n")
            column = len(gap.rsplit("\n", 1)[1]) + 1
        else:
            column += len(gap)
        value = match.group()
        if match.lastgroup != "space":
            result.append(Token(value, match.lastgroup, match.start(), match.end(), line, column))
        if "\n" in value:
            line += value.count("\n")
            column = len(value.rsplit("\n", 1)[1]) + 1
        else:
            column += len(value)
        last = match.end()
    for start in incomplete:
        prefix = text[:start]
        result.append(Token("(*", "bad", start, start + 2, prefix.count("\n") + 1, len(prefix.rsplit("\n", 1)[-1]) + 1))
    return sorted(result, key=lambda token: token.start)


class Extractor:
    def __init__(self, snapshot: dict):
        self.snapshot = snapshot
        self.project = str(snapshot["project_id"])
        self.snapshot_id = str(snapshot["snapshot_id"])
        self.objects = {str(n["id"]): n for n in snapshot.get("nodes", [])}
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.issues: list[dict] = []
        self.locals = defaultdict(lambda: defaultdict(list))
        self.globals = defaultdict(lambda: defaultdict(list))
        self.pous = defaultdict(lambda: defaultdict(list))
        self.gvls = defaultdict(lambda: defaultdict(list))
        self.object_symbols = {}
        self.types = {}
        self._edge_ids = set()
        self.parents = {str(n["id"]): str(n["parent_id"]) for n in snapshot.get("nodes", []) if n.get("parent_id")}
        for rel in snapshot.get("relations", []):
            if str(rel.get("kind", "")).lower() == "contains":
                self.parents[str(rel.get("to_id"))] = str(rel.get("from_id"))

    def sid(self, *parts):
        return "sym_" + _digest([self.project, *parts])[:32]

    def application(self, object_id):
        node = self.objects.get(str(object_id), {})
        if node.get("application_id"):
            return str(node["application_id"])
        seen = set()
        current = str(object_id)
        while current not in seen:
            seen.add(current)
            parent = self.objects.get(current, {})
            if parent.get("is_application") or parent.get("kind") == "application":
                return current
            current = self.parents.get(current)
            if current is None:
                break
        path = str(node.get("path", ""))
        match = re.search(r"^(.*?/(?:Application|Application_[^/]+))(?:/|$)", path, re.I)
        return match.group(1) if match else str(self.snapshot.get("active_application_id") or "application:unknown")

    def source(self, object_id, section=None, token=None):
        result = {"project_id": self.project, "snapshot_id": self.snapshot_id, "object_id": str(object_id), "parser_version": PARSER_VERSION, "location_kind": "text" if section else "object"}
        if section:
            content = self.objects.get(str(object_id), {}).get(section) or ""
            result.update(section=section, content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest())
        if token:
            result.update(line=token.line, column=token.column, start=token.start, end=token.end, column_unit="unicode_code_point", offset_unit="unicode_code_point")
        return result

    def issue(self, object_id, section, token, reason, state="unresolved", **details):
        self.issues.append({"state": state, "reason": reason, "source": self.source(object_id, section, token), **details})

    def node(self, sid, **fields):
        if sid not in self.nodes:
            self.nodes[sid] = {"id": sid, "project_id": self.project, "snapshot_id": self.snapshot_id, **fields}
        return self.nodes[sid]

    def edge(self, source, target, kind, evidence, state="resolved", **fields):
        entry = {"source": source, "target": target, "kind": kind, "state": state, "evidence": evidence, **fields}
        entry["id"] = "edge_" + _digest(entry)[:32]
        if entry["id"] not in self._edge_ids:
            self._edge_ids.add(entry["id"])
            self.edges.append(entry)

    def prepare_objects(self):
        project_symbol = self.sid("project")
        self.node(project_symbol, kind="Project", name=self.project, object_id=None, state="resolved")
        for oid, raw in self.objects.items():
            declaration = raw.get("declaration") or ""
            tokens = tokenize(declaration)
            header = tokens[0].upper if tokens else ""
            name = tokens[1].value if header in {"PROGRAM", "FUNCTION", "FUNCTION_BLOCK"} and len(tokens) > 1 else str(raw.get("name", oid))
            kind = "POU" if header in {"PROGRAM", "FUNCTION", "FUNCTION_BLOCK"} else "GVL" if header == "VAR_GLOBAL" else "Task" if str(raw.get("kind", "")).lower() == "task" or raw.get("is_task") else "Application" if raw.get("is_application") or raw.get("kind") == "application" else "Object"
            sid = self.sid(oid, "object")
            self.object_symbols[oid] = sid
            self.node(sid, kind=kind, name=name, object_id=oid, application_id=self.application(oid), pou_kind=header if kind == "POU" else None, source=self.source(oid), state="resolved")
            if kind == "POU":
                self.pous[self.application(oid)][name.casefold()].append(sid)
            elif kind == "GVL":
                self.gvls[self.application(oid)][name.casefold()].append(oid)
        for oid, parent in self.parents.items():
            if oid in self.object_symbols and parent in self.object_symbols:
                self.edge(self.object_symbols[parent], self.object_symbols[oid], "CONTAINS", self.source(oid), evidence_type="host_hierarchy")
        for oid, symbol in self.object_symbols.items():
            if oid not in self.parents or self.parents[oid] not in self.object_symbols:
                self.edge(project_symbol, symbol, "CONTAINS", self.source(oid), evidence_type="host_snapshot_membership")

    def declarations(self, oid, raw):
        tokens = tokenize(raw.get("declaration") or "")
        for token in tokens:
            if token.kind == "bad":
                self.issue(oid, "declaration", token, "invalid_or_incomplete_token", "unsupported")
        if len(tokens) >= 4 and tokens[0].upper == "FUNCTION" and tokens[2].value == ":":
            name, type_token = tokens[1], tokens[3]
            sid = self.sid(oid, "FUNCTION_RESULT", name.value.casefold(), 0)
            result = self.node(sid, name=name.value, kind="Variable", object_id=oid, scope="FUNCTION_RESULT", application_id=self.application(oid), declared_type=type_token.value, source=self.source(oid, "declaration", name), state="resolved" if type_token.upper in ELEMENTARY else "unsupported")
            self.locals[oid][name.value.casefold()].append(sid)
            self.edge(self.object_symbols[oid], sid, "DECLARES", result["source"])
            if type_token.upper in ELEMENTARY:
                type_id = self.sid("builtin", type_token.upper)
                self.node(type_id, name=type_token.upper, kind="Type", state="resolved", builtin=True, object_id=None)
                self.edge(sid, type_id, "TYPE_OF", self.source(oid, "declaration", type_token))
            else:
                self.issue(oid, "declaration", type_token, "function_return_type_outside_p0_subset", "unsupported")
        index, block = 0, None
        while index < len(tokens):
            token = tokens[index]
            if token.upper.startswith("VAR") and token.upper in KEYWORDS:
                block = token.upper
                index += 1
                while index < len(tokens) and tokens[index].upper in {"CONSTANT", "RETAIN", "PERSISTENT"}:
                    index += 1
                continue
            if token.upper == "END_VAR":
                block = None
                index += 1
                continue
            if block is None:
                index += 1
                continue
            end = index
            while end < len(tokens) and tokens[end].value != ";" and tokens[end].upper != "END_VAR":
                end += 1
            item = tokens[index:end]
            colon = next((p for p, t in enumerate(item) if t.value == ":"), None)
            if colon is None or colon + 1 >= len(item):
                self.issue(oid, "declaration", token, "unsupported_or_incomplete_declaration", "unsupported")
                index = max(index + 1, end)
                continue
            name_tokens = []
            for name_token in item[:colon]:
                if name_token.upper == "AT":
                    break
                if name_token.kind == "id":
                    name_tokens.append(name_token)
            type_token = item[colon + 1]
            unsupported = any(t.upper in UNSUPPORTED or t.kind == "bad" for t in item)
            for name_token in name_tokens:
                # Declaration offsets are evidence, not identity: adding a
                # comment must not give an unchanged symbol a different ID.
                duplicate = len(self.locals[oid][name_token.value.casefold()])
                sid = self.sid(oid, block, name_token.value.casefold(), duplicate)
                symbol = self.node(sid, name=name_token.value, qualified_name=self.nodes[self.object_symbols[oid]]["name"] + "." + name_token.value, kind="Variable", object_id=oid, scope=block, application_id=self.application(oid), declared_type=type_token.value, source=self.source(oid, "declaration", name_token), state="unsupported" if unsupported else "resolved")
                address = next((t.value for t in item[:colon] if t.kind == "address"), None)
                if address:
                    symbol["address"] = address
                self.locals[oid][name_token.value.casefold()].append(sid)
                if block == "VAR_GLOBAL":
                    self.globals[self.application(oid)][name_token.value.casefold()].append(sid)
                self.edge(self.object_symbols[oid], sid, "DECLARES", symbol["source"])
                typename = type_token.upper
                matches = self.pous[self.application(oid)].get(type_token.value.casefold(), [])
                fb_matches = [match for match in matches if self.nodes[match].get("pou_kind") == "FUNCTION_BLOCK"]
                if typename in ELEMENTARY:
                    type_id = self.sid("builtin", typename)
                    self.node(type_id, name=typename, kind="Type", state="resolved", builtin=True, object_id=None)
                    self.edge(sid, type_id, "TYPE_OF", self.source(oid, "declaration", type_token))
                elif len(fb_matches) == 1:
                    symbol.update(kind="FBInstance", definition_object_id=self.nodes[fb_matches[0]]["object_id"])
                    self.edge(sid, fb_matches[0], "INSTANCE_OF", self.source(oid, "declaration", type_token))
                else:
                    symbol["type_state"] = "ambiguous" if len(fb_matches) > 1 else "unresolved"
                    self.issue(oid, "declaration", type_token, "type_definition_unavailable", symbol["type_state"], name=type_token.value, symbol_id=sid)
            if unsupported:
                self.issue(oid, "declaration", type_token, "declaration_outside_p0_subset", "unsupported")
            index = end + 1 if end < len(tokens) and tokens[end].value == ";" else end

    def resolve(self, oid, parts, section="implementation"):
        token = parts[0]
        key = token.value.casefold()
        candidates = self.locals[oid].get(key) or self.globals[self.application(oid)].get(key) or self.pous[self.application(oid)].get(key) or []
        gvls = self.gvls[self.application(oid)].get(key, [])
        if len(parts) > 1 and not candidates and len(gvls) == 1:
            candidates = self.locals[gvls[0]].get(parts[1].value.casefold(), [])
            parts = parts[1:]
        if len(candidates) != 1:
            self.issue(oid, section, token, "ambiguous_symbol" if candidates else "unresolved_symbol", "ambiguous" if candidates else "unresolved", name=".".join(t.value for t in parts), candidates=candidates)
            return None
        symbol = self.nodes[candidates[0]]
        for member in parts[1:]:
            definition = symbol.get("definition_object_id")
            members = self.locals[definition].get(member.value.casefold(), []) if definition else []
            if len(members) != 1:
                self.issue(oid, section, member, "member_definition_unavailable", "ambiguous" if len(members) > 1 else "unresolved", instance_id=symbol["id"], name=member.value)
                return None
            declared = self.nodes[members[0]]
            sid = self.sid("member", symbol["id"], declared["id"])
            symbol = self.node(sid, kind="InstanceMember", name=symbol["name"] + "." + member.value, object_id=symbol["object_id"], declaration_symbol_id=declared["id"], instance_id=symbol["id"], declared_type=declared["declared_type"], definition_object_id=declared.get("definition_object_id"), source=declared["source"], state=declared["state"])
        if symbol.get("state") != "resolved":
            self.issue(oid, section, token, "reference_to_unsupported_declaration", "unsupported", symbol_id=symbol["id"])
            return None
        return symbol

    @staticmethod
    def split_arguments(tokens):
        result, part, depth = [], [], 0
        for token in tokens:
            if token.value == "," and depth == 0:
                result.append(part)
                part = []
                continue
            depth += token.value == "("
            depth -= token.value == ")"
            part.append(token)
        result.append(part)
        return result

    def expression(self, oid, tokens, access="READS"):
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.kind != "id" or token.upper in KEYWORDS:
                if token.kind == "bad" or token.upper in UNSUPPORTED or token.value in {"[", "]"}:
                    self.issue(oid, "implementation", token, "expression_outside_p0_subset", "unsupported")
                index += 1
                continue
            if token.upper in UNSUPPORTED:
                self.issue(oid, "implementation", token, "expression_outside_p0_subset", "unsupported")
                index += 1
                continue
            parts = [token]
            end = index + 1
            while end + 1 < len(tokens) and tokens[end].value == "." and tokens[end + 1].kind == "id":
                parts.append(tokens[end + 1])
                end += 2
            symbol = self.resolve(oid, parts)
            if end < len(tokens) and tokens[end].value == "(":
                # The implicit FUNCTION result variable shadows the name for
                # assignment, but a recursive call still targets the function.
                if symbol and symbol.get("scope") == "FUNCTION_RESULT":
                    symbol = self.nodes[self.object_symbols[oid]]
                close, depth = end + 1, 1
                while close < len(tokens) and depth:
                    depth += tokens[close].value == "("
                    depth -= tokens[close].value == ")"
                    close += 1
                if depth:
                    self.issue(oid, "implementation", token, "unterminated_call", "unsupported")
                    return
                if symbol:
                    if symbol["kind"] == "Variable" and symbol.get("type_state"):
                        symbol["kind"] = "FBInstance"
                    callable_symbol = symbol["kind"] in {"FBInstance", "POU"}
                    if callable_symbol:
                        self.edge(self.object_symbols[oid], symbol["id"], "CALLS", self.source(oid, "implementation", token), source_object_id=oid, target_object_id=symbol.get("definition_object_id") or (symbol["object_id"] if symbol["kind"] == "POU" else None))
                    else:
                        self.issue(oid, "implementation", token, "call_target_not_statically_callable", "unsupported")
                    if not symbol.get("definition_object_id") and symbol["kind"] == "FBInstance":
                        self.issue(oid, "implementation", token, "external_call_effects_unknown", instance_id=symbol["id"])
                for arg in self.split_arguments(tokens[end + 1:close - 1]):
                    mode = "READS"
                    actual = arg
                    if len(arg) >= 2 and arg[0].kind == "id" and arg[1].value in {":=", "=>"}:
                        actual = arg[2:]
                        mode = "WRITES" if arg[1].value == "=>" else "READS"
                        if symbol and symbol.get("definition_object_id"):
                            formals = self.locals[symbol["definition_object_id"]].get(arg[0].value.casefold(), [])
                            if len(formals) == 1 and self.nodes[formals[0]]["scope"] == "VAR_IN_OUT":
                                self.expression(oid, actual, "WRITES")
                    self.expression(oid, actual, mode)
                index = close
                continue
            if symbol:
                self.edge(self.object_symbols[oid], symbol["id"], access, self.source(oid, "implementation", token), source_object_id=oid)
            index = end

    def implementation(self, oid, raw):
        tokens = tokenize(raw.get("implementation") or "")
        for token in tokens:
            if token.kind == "bad":
                self.issue(oid, "implementation", token, "invalid_or_incomplete_token", "unsupported")
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.upper in {"IF", "ELSIF"}:
                end = index + 1
                while end < len(tokens) and tokens[end].upper != "THEN":
                    end += 1
                self.expression(oid, tokens[index + 1:end])
                if end == len(tokens):
                    self.issue(oid, "implementation", token, "missing_then", "unsupported")
                index = end + 1
                continue
            if token.value == ";" or token.upper in {"ELSE", "END_IF", "RETURN", "END_PROGRAM", "END_FUNCTION", "END_FUNCTION_BLOCK"}:
                index += 1
                continue
            if token.upper in UNSUPPORTED:
                self.issue(oid, "implementation", token, "statement_outside_p0_subset", "unsupported")
                # Do not infer references from unsupported control structures.
                closing = {"FOR": "END_FOR", "WHILE": "END_WHILE", "REPEAT": "END_REPEAT", "CASE": "END_CASE"}.get(token.upper)
                end = index + 1
                while end < len(tokens) and (tokens[end].upper != closing if closing else tokens[end].value != ";"):
                    end += 1
                index = end + 1
                continue
            end, depth = index, 0
            while end < len(tokens):
                current = tokens[end]
                if depth == 0 and (current.value == ";" or current.upper in {"END_IF", "ELSE", "ELSIF"}):
                    break
                depth += current.value == "("
                depth -= current.value == ")"
                end += 1
            statement = tokens[index:end]
            assignment = next((p for p, t in enumerate(statement) if t.value in {":=", "S=", "R=", "s=", "r="}), None)
            # Named call arguments are not top-level assignments.
            if assignment is not None and any(t.value == "(" for t in statement[:assignment]):
                assignment = None
            if assignment is not None:
                lhs = statement[:assignment]
                if not lhs or any(t.value not in {"."} and t.kind != "id" for t in lhs):
                    self.issue(oid, "implementation", token, "unsupported_assignment_target", "unsupported")
                else:
                    self.expression(oid, lhs, "WRITES")
                    self.expression(oid, statement[assignment + 1:])
            elif statement:
                if any(t.value == "(" for t in statement):
                    self.expression(oid, statement)
                else:
                    self.issue(oid, "implementation", token, "unrecognized_statement", "unsupported")
            index = max(index + 1, end)

    def task_relations(self):
        for rel in self.snapshot.get("relations", []):
            if str(rel.get("kind", "")).lower() not in {"task_calls", "executes"}:
                continue
            source, target = str(rel.get("from_id", "")), str(rel.get("to_id", ""))
            if rel.get("resolution") not in {None, "resolved", "unique_program_in_application"} or rel.get("state") not in {None, "resolved"}:
                self.issue(source, None, None, "task_target_unresolved", candidates=rel.get("candidates", []))
                continue
            if source in self.object_symbols and target in self.object_symbols:
                self.edge(self.object_symbols[source], self.object_symbols[target], "EXECUTES", self.source(source), source_object_id=source, target_object_id=target, evidence_type="host_task_calls")
            else:
                self.issue(source, None, None, "task_target_unavailable", target_object_id=target)

    def extract(self):
        self.prepare_objects()
        for oid, raw in self.objects.items():
            if isinstance(raw.get("declaration"), str):
                self.declarations(oid, raw)
        for oid, raw in self.objects.items():
            if isinstance(raw.get("implementation"), str):
                self.implementation(oid, raw)
        self.task_relations()
        coverage = []
        for oid, raw in self.objects.items():
            issues = [issue for issue in self.issues if issue["source"]["object_id"] == oid]
            source_status = raw.get("source_status") or {}
            coverage.append({"object_id": oid, "declaration": "parsed" if isinstance(raw.get("declaration"), str) else source_status.get("declaration", "unavailable"), "implementation": "parsed" if isinstance(raw.get("implementation"), str) else source_status.get("implementation", "unavailable"), "state": "unsupported" if any(i["state"] == "unsupported" for i in issues) else "partial" if issues else "resolved", "issue_count": len(issues)})
        return {"schema_version": GRAPH_SCHEMA_VERSION, "parser_version": PARSER_VERSION, "project_id": self.project, "snapshot_id": self.snapshot_id, "snapshot_hash": self.snapshot.get("hash") or self.snapshot.get("content_hash"), "snapshot_fingerprint": _snapshot_fingerprint(self.snapshot), "nodes": sorted(self.nodes.values(), key=lambda n: n["id"]), "edges": sorted(self.edges, key=lambda e: e["id"]), "issues": self.issues, "coverage": coverage, "unresolved_count": len(self.issues), "limitations": ["Only statically resolved task calls establish task reachability.", "External library bodies, aliases, indirect access and runtime behavior are not resolved.", "Compiler acceptance is separate from graph extraction."]}


def extract_graph(snapshot: dict) -> dict:
    return Extractor(snapshot).extract()


def reachable_objects(snapshot: dict) -> set[str]:
    graph = extract_graph(snapshot)
    reached = {e["target_object_id"] for e in graph["edges"] if e["kind"] == "EXECUTES" and e["state"] == "resolved"}
    outgoing = defaultdict(set)
    for edge in graph["edges"]:
        if edge["kind"] == "CALLS" and edge["state"] == "resolved" and edge.get("target_object_id"):
            outgoing[edge["source_object_id"]].add(edge["target_object_id"])
    queue = deque(reached)
    while queue:
        for target in outgoing[queue.popleft()] - reached:
            reached.add(target)
            queue.append(target)
    return reached


class PLCIndex:
    _lock = threading.RLock()

    def __init__(self, root: Path, backend=None):
        self.root = Path(root) / "indexes"
        self.backend = backend

    def _directory(self, snapshot):
        # Hash identifiers before using paths; neither IDs nor query URIs are paths.
        # One 128-bit directory component avoids Windows MAX_PATH failures.
        # Full identities/fingerprints are verified inside every graph artifact.
        return self.root / _digest([str(snapshot["project_id"]), str(snapshot["snapshot_id"])])[:32]

    def _backend(self):
        if self.backend is None:
            from .cgc_backend import CGCNeo4jBackend
            self.backend = CGCNeo4jBackend()
        return self.backend

    def build(self, snapshot):
        graph = extract_graph(snapshot)
        revision = "idx_" + _digest(graph)[:32]
        graph["index_revision"] = revision
        backend = self._backend()
        evidence = {"kind": "local_ir", "evidence": "deterministic_only"}
        with self._lock:
            directory = self._directory(snapshot)
            if (directory / "active.json").exists():
                previous = json.loads((directory / "active.json").read_text(encoding="utf-8"))["index_revision"]
                prior_graph = json.loads((directory / (previous + ".json")).read_text(encoding="utf-8"))
                if prior_graph["project_id"] != snapshot["project_id"] or prior_graph["snapshot_id"] != snapshot["snapshot_id"]:
                    _error("INDEX_FAILED", "Index directory identity collision; refusing to replace another snapshot")
                if prior_graph.get("snapshot_fingerprint") != graph["snapshot_fingerprint"]:
                    _error("STALE_SNAPSHOT", "A snapshot ID cannot be reused for changed source content")
            if backend is not False:
                evidence = backend.publish(graph)
            _atomic(directory / (revision + ".json"), graph)
            # Publish active only after database + artifact are complete.
            _atomic(directory / "active.json", {"index_revision": revision, "backend": evidence})
        return {"project_id": snapshot["project_id"], "snapshot_id": snapshot["snapshot_id"], "index_revision": revision, "graph": graph, "backend": evidence, "coverage": graph["coverage"], "unresolved_count": graph["unresolved_count"], "stale": False}

    def _load(self, snapshot, index_revision=None):
        directory = self._directory(snapshot)
        if index_revision is None:
            active = directory / "active.json"
            if not active.exists():
                _error("INDEX_NOT_FOUND", "Snapshot has no published index", {"snapshot_id": snapshot["snapshot_id"]})
            index_revision = json.loads(active.read_text(encoding="utf-8"))["index_revision"]
        if not re.fullmatch(r"idx_[0-9a-f]{32}", index_revision):
            _error("INDEX_NOT_FOUND", "Invalid index revision")
        path = directory / (index_revision + ".json")
        if not path.exists():
            _error("INDEX_NOT_FOUND", "Index revision does not belong to this project and snapshot")
        backend = self._backend()
        graph = json.loads(path.read_text(encoding="utf-8")) if backend is False else backend.load(snapshot["project_id"], snapshot["snapshot_id"], index_revision)
        if graph["project_id"] != snapshot["project_id"] or graph["snapshot_id"] != snapshot["snapshot_id"]:
            _error("INDEX_FAILED", "Index identity mismatch")
        expected = snapshot.get("hash") or snapshot.get("content_hash")
        if (expected and graph.get("snapshot_hash") != expected) or graph.get("snapshot_fingerprint") != _snapshot_fingerprint(snapshot):
            _error("STALE_SNAPSHOT", "Immutable snapshot content does not match its index")
        return graph

    @staticmethod
    def _page(items, limit, offset):
        if isinstance(limit, bool) or isinstance(offset, bool) or not isinstance(limit, int) or not isinstance(offset, int) or not 1 <= limit <= 500 or offset < 0:
            _error("INVALID_ARGUMENT", "limit must be 1..500 and offset must be nonnegative")
        return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit, "next_offset": offset + limit if offset + limit < len(items) else None}

    @staticmethod
    def _meta(graph):
        return {"project_id": graph["project_id"], "snapshot_id": graph["snapshot_id"], "index_revision": graph["index_revision"], "coverage": graph["coverage"], "unresolved_count": graph["unresolved_count"], "stale": False}

    def search(self, snapshot, query, limit=50, offset=0, index_revision=None):
        graph = self._load(snapshot, index_revision)
        if not isinstance(query, str) or not query.strip():
            _error("INVALID_ARGUMENT", "query must be nonempty text")
        needle = query.casefold()
        backend = self._backend()
        symbol_ids = backend.symbol_ids(graph["project_id"], graph["snapshot_id"], graph["index_revision"], query) if backend is not False else None
        items = [{"match_type": "symbol", "symbol_id": node["id"], **node} for node in graph["nodes"] if (node["id"] in symbol_ids if symbol_ids is not None else needle in node.get("name", "").casefold() or needle in node.get("qualified_name", "").casefold())]
        for node in snapshot.get("nodes", []):
            for section in ("declaration", "implementation"):
                text = node.get(section)
                if not isinstance(text, str):
                    continue
                for line, content in enumerate(text.splitlines(), 1):
                    if needle in content.casefold():
                        items.append({"match_type": "text", "object_id": node["id"], "section": section, "line": line, "text": content, "source": {"project_id": snapshot["project_id"], "snapshot_id": snapshot["snapshot_id"], "object_id": node["id"], "section": section, "line": line, "content_hash": hashlib.sha256(text.encode()).hexdigest()}})
        return {**self._meta(graph), **self._page(items, limit, offset)}

    def references(self, snapshot, symbol_id, relation=None, index_revision=None, limit=50, offset=0):
        graph = self._load(snapshot, index_revision)
        matches = [node for node in graph["nodes"] if node["id"] == symbol_id]
        if not matches:
            _error("SYMBOL_NOT_FOUND", "Use a stable symbol_id from this snapshot's search results")
        allowed = {"READS", "WRITES", "CALLS", "DECLARES", "EXECUTES", "TYPE_OF", "INSTANCE_OF", "CONTAINS"}
        if relation is not None and (not isinstance(relation, str) or relation.upper() not in allowed):
            _error("INVALID_ARGUMENT", "Unsupported relation kind")
        targets = {symbol_id} | {node["id"] for node in graph["nodes"] if node.get("declaration_symbol_id") == symbol_id}
        backend = self._backend()
        edge_ids = backend.reference_ids(graph["project_id"], graph["snapshot_id"], graph["index_revision"], targets, relation.upper() if relation else None) if backend is not False else None
        items = [edge for edge in graph["edges"] if (edge["target"] in targets or edge["source"] == symbol_id) and (not relation or edge["kind"] == relation.upper()) and (edge_ids is None or edge["id"] in edge_ids)]
        return {**self._meta(graph), "symbol": matches[0], **self._page(items, limit, offset), "issues": [issue for issue in graph["issues"] if issue.get("symbol_id") in targets or issue.get("instance_id") in targets]}

    def impact(self, snapshot, object_id, max_depth=3, index_revision=None, limit=50, offset=0):
        graph = self._load(snapshot, index_revision)
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 0 <= max_depth <= 20:
            _error("INVALID_ARGUMENT", "max_depth must be 0..20")
        object_ids = {str(n["id"]) for n in snapshot.get("nodes", [])}
        if str(object_id) not in object_ids:
            _error("OBJECT_NOT_FOUND", "Impact target is not in this snapshot")
        by_id = {node["id"]: node for node in graph["nodes"]}
        reverse = defaultdict(list)
        for edge in graph["edges"]:
            if edge["kind"] not in {"READS", "WRITES", "CALLS", "EXECUTES"} or edge["state"] != "resolved":
                continue
            target = by_id[edge["target"]]
            member_declaration = by_id.get(target.get("declaration_symbol_id"), {})
            target_object = edge.get("target_object_id") or member_declaration.get("object_id") or target.get("object_id")
            source_object = edge.get("source_object_id") or by_id[edge["source"]].get("object_id")
            if target_object and source_object and target_object != source_object:
                reverse[target_object].append((source_object, edge))
        distances, evidence = {str(object_id): 0}, []
        queue = deque([str(object_id)])
        truncated = False
        while queue:
            current = queue.popleft()
            if distances[current] >= max_depth:
                truncated |= any(source not in distances for source, _ in reverse[current])
                continue
            for source, edge in reverse[current]:
                evidence.append(edge)
                if source not in distances:
                    distances[source] = distances[current] + 1
                    queue.append(source)
        items = [{"object_id": oid, "depth": depth} for oid, depth in sorted(distances.items(), key=lambda pair: (pair[1], pair[0]))]
        return {**self._meta(graph), **self._page(items, limit, offset), "max_depth": max_depth, "depth_truncated": truncated, "evidence": evidence, "issues": graph["issues"], "limitations": graph["limitations"]}

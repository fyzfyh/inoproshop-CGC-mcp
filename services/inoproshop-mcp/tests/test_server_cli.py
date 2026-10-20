import json
from pathlib import Path

import pytest

from inoproshop_mcp.__main__ import ensure_token, main, service_lock
from inoproshop_mcp.core import TOOLS
from inoproshop_mcp.server import TOOL_MODELS, tool_definitions


def test_contract_covers_same_core_tools():
    assert set(TOOLS) == set(TOOL_MODELS)
    assert len(TOOL_MODELS) == 26
    assert {"context_search_text", "ino_list_project_templates", "ino_create_project"} <= set(TOOL_MODELS)
    published = json.loads((Path(__file__).resolve().parents[1] / "config/tools.schema.json").read_text(encoding="utf-8"))
    assert published["tools"] == [tool.model_dump(exclude_none=True) for tool in tool_definitions()]


def test_token_is_persistent_and_never_printed(tmp_path, capsys):
    token = ensure_token(tmp_path)
    assert len(token) >= 32
    assert ensure_token(tmp_path) == token
    assert token not in capsys.readouterr().out


def test_short_token_is_rejected_without_rotating(tmp_path):
    path = tmp_path / "token"
    path.write_text("bad", encoding="ascii")
    with pytest.raises(RuntimeError, match="Invalid local token"):
        ensure_token(tmp_path)
    assert path.read_text() == "bad"


def test_same_state_cannot_have_two_controllers(tmp_path):
    with service_lock(tmp_path):
        with pytest.raises(RuntimeError, match="already own"):
            with service_lock(tmp_path):
                pytest.fail("Second writer acquired same state")
    with service_lock(tmp_path):
        pass


def test_schema_export_is_public_contract(tmp_path):
    output = tmp_path / "schema.json"
    assert main(["schemas", "--output", str(output)]) == 0
    schema = json.loads(output.read_text(encoding="utf-8"))
    assert len(schema["tools"]) == 26
    preview = next(t for t in schema["tools"] if t["name"] == "ino_preview_change")
    assert preview["inputSchema"]["properties"]["change"]["discriminator"]["propertyName"] == "type"


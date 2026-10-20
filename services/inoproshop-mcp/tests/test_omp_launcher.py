import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("shell_name", ["powershell", "pwsh"])
@pytest.mark.parametrize("initial_encoding", [None, "utf-8", "utf-8-sig"])
def test_omp_launcher_writes_strict_utf8_and_preserves_configuration(
    tmp_path, shell_name, initial_encoding
):
    shell = shutil.which(shell_name)
    if not shell:
        pytest.skip(f"{shell_name} is not installed")

    state = tmp_path / "state"
    state.mkdir()
    token = "synthetic-launcher-test-token-" + "a" * 32
    (state / "token").write_text(token, encoding="ascii")
    workspace = tmp_path / "workspace"
    config_file = workspace / ".omp" / "mcp.json"
    existing = {
        "mcpServers": {"fixture": {"command": "example", "args": ["中文工程"]}},
        "description": "保留已有配置",
    }
    if initial_encoding:
        config_file.parent.mkdir(parents=True)
        config_file.write_text(
            json.dumps(existing, ensure_ascii=False), encoding=initial_encoding
        )

    env = os.environ.copy()
    env.update(
        INO_TEST_LAUNCHER=str(
            Path(__file__).resolve().parents[1] / "scripts" / "Start-OMP.ps1"
        ),
        INO_TEST_STATE=str(state),
        INO_TEST_WORKSPACE=str(workspace),
    )
    # Run the real launcher; replace only the interactive model client.
    wrapper = r"""
$ErrorActionPreference = 'Stop'
function global:omp { $global:LASTEXITCODE = 0 }
& $env:INO_TEST_LAUNCHER -StateDir $env:INO_TEST_STATE -Workspace $env:INO_TEST_WORKSPACE
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
"""
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", wrapper],
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert token not in result.stdout + result.stderr

    raw = config_file.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    config = json.loads(raw.decode("utf-8"))
    if initial_encoding:
        assert config["description"] == existing["description"]
        assert config["mcpServers"]["fixture"] == existing["mcpServers"]["fixture"]
    assert config["mcpServers"]["inoproshop"]["url"] == "http://127.0.0.1:8765/mcp"
    assert config["mcpServers"]["inoproshop"]["headers"]["Authorization"] == "INOAI_MCP_AUTHORIZATION"
    assert token not in raw.decode("utf-8")
    assert (workspace / ".omp" / "skills" / "inoproshop" / "SKILL.md").is_file()


def test_omp_selected_host_is_session_specific_and_env_is_restored(tmp_path):
    shell = shutil.which("powershell")
    if not shell:
        pytest.skip("Windows PowerShell required")
    state = tmp_path / "state"
    state.mkdir()
    token = "synthetic-secret-" + "x" * 48
    (state / "token").write_text(token)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"state_dir": str(state), "url": "http://127.0.0.1:8765/mcp",
        "host_id": "selected-host", "host_instance_id": "selected-instance"}), encoding="utf-8")
    output = tmp_path / "captured.json"
    env = os.environ.copy()
    env.update(INO_TEST_STATE=str(state), INO_TEST_WORKSPACE=str(tmp_path / "workspace"),
        INO_TEST_CONTEXT=str(context), INO_TEST_CAPTURE=str(output),
        INO_TEST_LAUNCHER=str(Path(__file__).resolve().parents[1] / "scripts/Start-OMP.ps1"))
    wrapper = r'''
$ErrorActionPreference = 'Stop'
$env:INOAI_MCP_AUTHORIZATION = 'previous-environment-value'
function global:omp {
    [System.IO.File]::WriteAllText($env:INO_TEST_CAPTURE, (ConvertTo-Json -InputObject @($args)), (New-Object System.Text.UTF8Encoding($false)))
    if (-not $env:INOAI_MCP_AUTHORIZATION.StartsWith('Bearer synthetic-secret-')) { throw 'missing child credential' }
    $global:LASTEXITCODE = 0
}
& $env:INO_TEST_LAUNCHER -StateDir $env:INO_TEST_STATE -Workspace $env:INO_TEST_WORKSPACE -ContextFile $env:INO_TEST_CONTEXT
if ($env:INOAI_MCP_AUTHORIZATION -ne 'previous-environment-value') { throw 'parent environment was changed' }
'''
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", wrapper],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    args = json.loads(output.read_text(encoding="utf-8"))
    prompt = args[args.index("--append-system-prompt") + 1]
    assert "selected-host" in prompt and "selected-instance" in prompt
    assert token not in result.stdout + result.stderr + output.read_text(encoding="utf-8")


def test_invalid_existing_omp_json_is_preserved(tmp_path):
    shell = shutil.which("powershell")
    if not shell:
        pytest.skip("Windows PowerShell required")
    state = tmp_path / "state"
    state.mkdir()
    (state / "token").write_text("synthetic-token-" + "x" * 48)
    workspace = tmp_path / "workspace"
    config_dir = workspace / ".omp"
    config_dir.mkdir(parents=True)
    config = config_dir / "mcp.json"
    config.write_bytes(b'{"mcpServers": invalid existing config')
    before = config.read_bytes()
    launcher = Path(__file__).resolve().parents[1] / "scripts/Start-OMP.ps1"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(launcher),
        "-StateDir", str(state), "-Workspace", str(workspace), "-ConfigureOnly"], capture_output=True, timeout=30)
    assert result.returncode != 0
    assert config.read_bytes() == before

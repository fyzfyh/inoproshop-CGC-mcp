"""Run the real PowerShell bootstrap while replacing external applications only."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


SERVICE = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["powershell", "pwsh"])
def shell(request):
    executable = shutil.which(request.param)
    if not executable:
        pytest.skip(f"{request.param} is not installed")
    return executable


@pytest.fixture
def release(tmp_path):
    root = tmp_path / "中文 release"
    service = root / "services" / "inoproshop-mcp"
    (service / "scripts").mkdir(parents=True)
    shutil.copyfile(SERVICE.parents[1] / "Connect-Ino.cmd", root / "Connect-Ino.cmd")
    for name in ["Connect-Ino.ps1"]:
        shutil.copyfile(SERVICE / "scripts" / name, service / "scripts" / name)
    for name in ["launcher-runtime-lock.json", "uv.lock", "pyproject.toml"]:
        shutil.copyfile(SERVICE / name, service / name)
    (service / "scripts/Start-OMP.ps1").write_text(
        "param([string]$StateDir, [string]$Workspace, [string]$Url, [string]$ContextFile, [switch]$ConfigureOnly)\n"
        "if (-not $ConfigureOnly) { throw 'Tests must not launch interactive OMP' }\n"
        "if (-not (Test-Path -LiteralPath $ContextFile)) { throw 'Missing connection context' }\n"
        "Write-Output 'OMP configuration prepared; no model was invoked.'\n"
        "exit 0\n", encoding="ascii"
    )
    return root


def environment(release, tmp_path):
    env = os.environ.copy()
    env.update(
        INO_TEST_BOOTSTRAP=str(release / "services/inoproshop-mcp/scripts/Connect-Ino.ps1"),
        INO_TEST_STATE=str(release / "new state 中文"),
        INO_TEST_CAPTURE=str(tmp_path / "captured.json"),
        INO_TEST_RUNTIME=str(release / "work/mcp_p0/launcher-runtime"),
    )
    return env


def run_ps(shell, script, env):
    return subprocess.run(
        [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
    )


def assert_success(result):
    assert result.returncode == 0, result.stdout + result.stderr


def tree_contents(path):
    return {str(item.relative_to(path)): item.read_bytes() for item in path.rglob("*") if item.is_file()}


def test_check_missing_runtime_creates_nothing(shell, release, tmp_path):
    before = tree_contents(release)
    result = run_ps(shell, r"""
& $env:INO_TEST_BOOTSTRAP -Check -NoOmp -StateDir $env:INO_TEST_STATE
exit $LASTEXITCODE
""", environment(release, tmp_path))
    assert result.returncode == 1
    assert tree_contents(release) == before
    assert not (release / "new state 中文").exists()
    assert not (release / "work").exists()


def test_missing_offline_manifest_never_enters_download_flow(shell, release, tmp_path):
    (release / "offline").mkdir()
    env = environment(release, tmp_path)
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
function Get-InoUv { throw 'ONLINE_FALLBACK_IS_FORBIDDEN' }
try {
    $null = Initialize-InoEnvironment $script:InoServiceRoot $env:INO_TEST_RUNTIME @{}
    throw 'Offline initialization unexpectedly succeeded'
} catch {
    Write-InoJson $env:INO_TEST_CAPTURE @{ message = $_.Exception.Message }
}
""", env)
    assert_success(result)
    message = json.loads(Path(env["INO_TEST_CAPTURE"]).read_text("utf-8"))["message"]
    assert "离线" in message and "ONLINE_FALLBACK" not in message
    assert not (release / "work").exists()


def test_offline_validation_output_does_not_pollute_python_return_value(shell, release, tmp_path):
    import sys
    (release / "offline").mkdir()
    shutil.copyfile(SERVICE / "scripts/verify-offline.py", release / "services/inoproshop-mcp/scripts/verify-offline.py")
    payload = b"offline payload"
    (release / "payload").write_bytes(payload)
    (release / "offline/manifest.json").write_text(json.dumps({"kind": "offline_windows_x64", "files": {
        "payload": {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}}}))
    env = environment(release, tmp_path)
    env["INO_TEST_REAL_PYTHON"] = sys.executable
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
function Get-InoPythonPath { return $env:INO_TEST_REAL_PYTHON }
function Test-InoPython { return $true }
function Get-InoUv { throw 'OFFLINE MUST NOT DOWNLOAD' }
$python = Initialize-InoEnvironment $script:InoServiceRoot $env:INO_TEST_RUNTIME @{}
Write-InoJson $env:INO_TEST_CAPTURE @{ value = $python; count = @($python).Count }
""", env)
    assert_success(result)
    captured = json.loads(Path(env["INO_TEST_CAPTURE"]).read_text("utf-8"))
    assert captured == {"value": sys.executable, "count": 1}
    assert not (release / "work").exists()


def test_cmd_entry_works_from_another_directory_with_unicode_and_spaces(release, tmp_path):
    if os.name != "nt":
        pytest.skip("CMD entrypoint is Windows-only")
    before = tree_contents(release)
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(release / "Connect-Ino.cmd"), "-Check", "-NoOmp", "-NonInteractive"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "Connect-Ino.cmd" in result.stdout
    assert tree_contents(release) == before
    assert not (release / "work").exists()


@pytest.mark.parametrize("port", [1, 65536])
def test_invalid_port_is_rejected_before_startup(shell, release, tmp_path, port):
    env = environment(release, tmp_path)
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", env["INO_TEST_BOOTSTRAP"], "-NoOmp", "-NonInteractive", "-Port", str(port)],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
    )
    assert result.returncode != 0
    assert not (release / "work").exists()


def test_check_passes_exact_arguments_without_initialization(shell, release, tmp_path):
    env = environment(release, tmp_path)
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
function Test-InoPython { return $true }
function Initialize-InoEnvironment { throw 'CHECK MUST NOT INSTALL' }
function Invoke-InoPython([string]$Python, [string[]]$Arguments) {
    [IO.File]::WriteAllText($env:INO_TEST_CAPTURE, ($Arguments | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
    return 0
}
$result = Invoke-InoBootstrap -Check -NoOmp -NonInteractive -StateDir $env:INO_TEST_STATE -InoExe 'C:\IDE path\IDE.exe' -Profile 'Ino Profile' -Workspace 'C:\OMP space' -ProjectRoot 'C:\project copies' -Port 18876
exit $result
""", env)
    assert_success(result)
    arguments = json.loads(Path(env["INO_TEST_CAPTURE"]).read_text("utf-8"))
    assert arguments[:4] == ["-B", "-u", "-m", "inoproshop_mcp.launcher"]
    expected = {
        "--root": str(release), "--state-dir": env["INO_TEST_STATE"],
        "--context-out": str(Path(env["INO_TEST_STATE"]) / "launcher-context.json"),
        "--ino-exe": r"C:\IDE path\IDE.exe", "--profile": "Ino Profile",
        "--workspace": r"C:\OMP space", "--project-root": r"C:\project copies",
        "--port": "18876",
    }
    for name, value in expected.items():
        assert arguments[arguments.index(name) + 1] == value
    assert "--check" in arguments and "--non-interactive" in arguments
    assert not (release / "work").exists()


@pytest.mark.parametrize("launcher_exit", [0, 17])
def test_no_omp_propagates_launcher_exit_and_restores_environment(shell, release, tmp_path, launcher_exit):
    env = environment(release, tmp_path)
    env.update(INO_TEST_EXIT=str(launcher_exit), PYTHONPATH="fixture-original", PYTHONUTF8="0")
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
function Initialize-InoEnvironment { return 'fixture-python.exe' }
function Invoke-InoPython([string]$Python, [string[]]$Arguments) {
    if ($Python -ne 'fixture-python.exe') { throw 'wrong interpreter' }
    if ($env:PYTHONUTF8 -ne '1' -or $env:PYTHONDONTWRITEBYTECODE -ne '1') { throw 'missing Python settings' }
    $contextPath = $Arguments[[Array]::IndexOf($Arguments, '--context-out') + 1]
    New-Item -ItemType Directory -Path (Split-Path -Parent $contextPath) -Force | Out-Null
    Write-InoJson $contextPath @{}
    return [int]$env:INO_TEST_EXIT
}
$result = Invoke-InoBootstrap -NoOmp -StateDir $env:INO_TEST_STATE
if ($env:PYTHONPATH -ne 'fixture-original' -or $env:PYTHONUTF8 -ne '0') { throw 'environment changed' }
exit $result
""", env)
    assert result.returncode == launcher_exit, result.stdout + result.stderr


def test_omp_receives_selected_context_after_mutex_release(shell, release, tmp_path):
    env = environment(release, tmp_path)
    env["INO_TEST_SHELL"] = shell
    stub = release / "services/inoproshop-mcp/scripts/Start-OMP.ps1"
    stub.write_text(r"""
param([string]$StateDir, [string]$Workspace, [string]$Url, [string]$ContextFile, [switch]$ConfigureOnly)
$root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$env:INO_TEST_MUTEX = 'Local\InoProShopConnect-' + (Get-InoHash ([IO.Path]::GetFullPath($root).ToLowerInvariant()))
$childCode = '$mutex = New-Object Threading.Mutex($false, $env:INO_TEST_MUTEX); $acquired = $mutex.WaitOne(0); if ($acquired) { $mutex.ReleaseMutex() }; $mutex.Dispose(); if (-not $acquired) { exit 7 }'
& $env:INO_TEST_SHELL -NoLogo -NoProfile -NonInteractive -Command $childCode
if ($LASTEXITCODE -ne 0) { throw 'Startup mutex was not released before OMP' }
Write-InoJson $env:INO_TEST_CAPTURE @{ state = $StateDir; workspace = $Workspace; url = $Url; context = $ContextFile; configure_only = [bool]$ConfigureOnly }
exit 0
""", encoding="utf-8-sig")
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
function Initialize-InoEnvironment { return 'fixture-python.exe' }
function Invoke-InoPython([string]$Python, [string[]]$Arguments) {
    $contextPath = $Arguments[[Array]::IndexOf($Arguments, '--context-out') + 1]
    New-Item -ItemType Directory -Path (Split-Path -Parent $contextPath) -Force | Out-Null
    Write-InoJson $contextPath @{ state_dir = $env:INO_TEST_STATE; workspace = 'C:\selected OMP'; url = 'http://127.0.0.1:8765/mcp' }
    return 0
}
exit (Invoke-InoBootstrap -NoOmp -StateDir $env:INO_TEST_STATE)
""", env)
    assert_success(result)
    capture = json.loads(Path(env["INO_TEST_CAPTURE"]).read_text("utf-8"))
    assert capture["state"] == env["INO_TEST_STATE"]
    assert capture["workspace"] == r"C:\selected OMP"
    assert capture["url"] == "http://127.0.0.1:8765/mcp"
    assert capture["configure_only"] is True


@pytest.mark.parametrize("prepare_exit, expected_exit", [(0, 23), (17, 17)])
def test_top_level_interactive_handoff_preserves_output_and_exit(shell, release, tmp_path, prepare_exit, expected_exit):
    env = environment(release, tmp_path)
    env["INO_TEST_PREPARE_EXIT"] = str(prepare_exit)
    stub = release / "services/inoproshop-mcp/scripts/Start-OMP.ps1"
    stub.write_text(
        "param([string]$StateDir, [string]$Workspace, [string]$Url, [string]$ContextFile)\n"
        "Write-Output 'interactive-stdout-marker'\nexit 23\n", encoding="ascii"
    )
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
function Invoke-InoBootstrap {
    $script:InoPreparedContext = [pscustomobject]@{state_dir='fixture-state'; workspace='fixture-workspace'; url='http://127.0.0.1:8765/mcp'}
    $script:InoPreparedContextFile = 'fixture-context'
    return [int]$env:INO_TEST_PREPARE_EXIT
}
$NonInteractive = $true
$source = [IO.File]::ReadAllText($env:INO_TEST_BOOTSTRAP)
$entryMarker = 'if ($MyInvocation.InvocationName -ne ''.'') {'
# Execute the unchanged real entry block, replacing only component preparation.
& ([scriptblock]::Create($source.Substring($source.LastIndexOf($entryMarker))))
""", env)
    assert result.returncode == expected_exit, result.stdout + result.stderr
    assert ("interactive-stdout-marker" in result.stdout) is (prepare_exit == 0)


@pytest.mark.parametrize("child_exit", [0, 9])
def test_python_diagnostics_decode_utf8_and_restore_chinese_console(shell, release, tmp_path, child_exit):
    env = environment(release, tmp_path)
    env.update(INO_TEST_PYTHON=sys.executable, INO_TEST_EXIT=str(child_exit), PYTHONUTF8="1")
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
$script:captured = New-Object Collections.Generic.List[string]
# Replace only display: the real native pipeline must decode Python's bytes.
function Out-Host { process { $script:captured.Add([string]$_) } }
$previous = [Console]::OutputEncoding
try {
    [Console]::OutputEncoding = [Text.Encoding]::GetEncoding(936)
    $code = 'import sys; print(chr(20013)+chr(25991)+chr(24037)+chr(31243)); sys.exit(' + $env:INO_TEST_EXIT + ')'
    $exitCode = Invoke-InoPython $env:INO_TEST_PYTHON @('-c', $code)
    Write-InoJson $env:INO_TEST_CAPTURE @{captured=@($script:captured.ToArray()); restored_codepage=[Console]::OutputEncoding.CodePage; exit_code=$exitCode}
} finally { [Console]::OutputEncoding = $previous }
""", env)
    assert_success(result)
    report = json.loads(Path(env["INO_TEST_CAPTURE"]).read_text("utf-8"))
    assert report["captured"] == ["中文工程"]
    assert report["restored_codepage"] == 936
    assert report["exit_code"] == child_exit


def test_sync_only_on_initial_or_changed_lock(shell, release, tmp_path):
    env = environment(release, tmp_path)
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
$script:calls = New-Object Collections.Generic.List[object]
function Test-InoPython { return $true }
function Get-InoUv { return 'fixture-uv.exe' }
function Invoke-InoUv([string]$Uv, [string[]]$Arguments) { $script:calls.Add($Arguments) }
New-Item -ItemType Directory -Path $env:INO_TEST_RUNTIME -Force | Out-Null
$lock = Get-Content -LiteralPath (Join-Path $script:InoServiceRoot 'launcher-runtime-lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$null = Initialize-InoEnvironment $script:InoServiceRoot $env:INO_TEST_RUNTIME $lock
$null = Initialize-InoEnvironment $script:InoServiceRoot $env:INO_TEST_RUNTIME $lock
Add-Content -LiteralPath (Join-Path $script:InoServiceRoot 'uv.lock') -Value '# test lock update'
$null = Initialize-InoEnvironment $script:InoServiceRoot $env:INO_TEST_RUNTIME $lock
Write-InoJson $env:INO_TEST_CAPTURE @{ calls = @($script:calls.ToArray()) }
""", env)
    assert_success(result)
    calls = json.loads(Path(env["INO_TEST_CAPTURE"]).read_text("utf-8"))["calls"]
    assert len(calls) == 2
    for args in calls:
        assert args[0] == "sync"
        assert "--frozen" in args and "--no-python-downloads" in args
        assert args.count("--extra") == 2
        assert "cgc" in args and "test" in args
    raw = (Path(env["INO_TEST_RUNTIME"]) / "environment.json").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("valid_hash", [True, False])
def test_corrupt_download_retries_and_checks_hash(shell, release, tmp_path, valid_hash):
    env = environment(release, tmp_path)
    env["INO_TEST_SHA"] = hashlib.sha256(b"verified payload" if valid_hash else b"other payload").hexdigest()
    runtime = Path(env["INO_TEST_RUNTIME"])
    runtime.mkdir(parents=True)
    (runtime / "asset.zip").write_bytes(b"corrupt cached download")
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
$script:downloads = 0
function Invoke-WebRequest([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile, [int]$TimeoutSec) {
    $script:downloads++
    [IO.File]::WriteAllBytes($OutFile, [Text.Encoding]::UTF8.GetBytes('verified payload'))
}
$artifact = [pscustomobject]@{archive_url = 'https://example.invalid/locked.zip'; archive_sha256 = $env:INO_TEST_SHA}
$destination = Join-Path $env:INO_TEST_RUNTIME 'asset.zip'
try {
    Get-InoLockedArchive $artifact $destination $env:INO_TEST_RUNTIME
    Get-InoLockedArchive $artifact $destination $env:INO_TEST_RUNTIME
    if ($script:downloads -ne 1) { throw 'Unexpected repeated download' }
    exit 0
} catch { Write-Host $_.Exception.Message; exit 9 }
""", env)
    assert result.returncode == (0 if valid_hash else 9), result.stdout + result.stderr
    assert not (runtime / "asset.zip.partial").exists()
    if valid_hash:
        assert (runtime / "asset.zip").read_bytes() == b"verified payload"
    else:
        assert not (runtime / "asset.zip").exists()


def test_private_cleanup_refuses_parent_directory(shell, release, tmp_path):
    env = environment(release, tmp_path)
    result = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
try { Remove-InoRuntimeItem $script:InoServiceRoot $env:INO_TEST_RUNTIME; exit 12 }
catch { exit 0 }
""", env)
    assert_success(result)
    assert (release / "services/inoproshop-mcp/scripts/Connect-Ino.ps1").exists()


def test_concurrent_bootstrap_does_not_initialize_twice(shell, release, tmp_path):
    env = environment(release, tmp_path)
    first_script = r"""
. $env:INO_TEST_BOOTSTRAP
function Initialize-InoEnvironment {
    [IO.File]::WriteAllText($env:INO_TEST_CAPTURE, 'locked')
    Start-Sleep -Seconds 8
    return 'fixture-python.exe'
}
function Invoke-InoPython([string]$Python, [string[]]$Arguments) {
    $contextPath = $Arguments[[Array]::IndexOf($Arguments, '--context-out') + 1]
    New-Item -ItemType Directory -Path (Split-Path -Parent $contextPath) -Force | Out-Null
    Write-InoJson $contextPath @{}
    return 0
}
exit (Invoke-InoBootstrap -NoOmp -StateDir $env:INO_TEST_STATE)
"""
    first = subprocess.Popen(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", first_script],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not Path(env["INO_TEST_CAPTURE"]).exists() and time.monotonic() < deadline:
            if first.poll() is not None:
                out, err = first.communicate()
                pytest.fail(repr(out + err))
            time.sleep(0.05)
        assert Path(env["INO_TEST_CAPTURE"]).exists()
        second = run_ps(shell, r"""
. $env:INO_TEST_BOOTSTRAP
function Initialize-InoEnvironment { throw 'MUST NOT ENTER SECOND INITIALIZATION' }
exit (Invoke-InoBootstrap -NoOmp -StateDir $env:INO_TEST_STATE)
""", env)
        assert second.returncode == 1
        assert "MUST NOT ENTER" not in second.stdout + second.stderr
        out, err = first.communicate(timeout=15)
        assert first.returncode == 0, repr(out + err)
    finally:
        if first.poll() is None:
            first.terminate()
            first.communicate(timeout=10)


def test_runtime_pins_are_complete_and_neo4j_matches_existing_lock():
    lock = json.loads((SERVICE / "launcher-runtime-lock.json").read_text("utf-8"))
    for name in ("uv", "python", "java", "neo4j"):
        artifact = lock[name]
        assert artifact["archive_url"].startswith("https://")
        assert "/latest/" not in artifact["archive_url"]
        assert len(bytes.fromhex(artifact["archive_sha256"])) == 32
        assert artifact["version"]
    cgc = json.loads((SERVICE / "cgc-runtime-lock.json").read_text("utf-8"))
    assert lock["neo4j"]["archive_sha256"] == cgc["neo4j_archive_sha256"]
    assert lock["neo4j"]["version"] == cgc["neo4j_server"]

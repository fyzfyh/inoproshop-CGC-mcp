import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from inoproshop_mcp import launcher_host as launcher


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "安装目录 中文"
    bridge = root / "adapters/inoproshop/bridge"
    bridge.mkdir(parents=True)
    for name in ("entry.py", "adapter.py"):
        (bridge / name).write_text("# synthetic immutable bridge " + name, encoding="utf-8")
    state, projects = root / "state", root / "projects"
    state.mkdir()
    projects.mkdir()
    exe = root / "vendor/CODESYS/Common/InoProShop.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"synthetic executable; never run")
    profiles = exe.parent.parent / "Profiles"
    profiles.mkdir()
    (profiles / "InoProShop(V1.10.0.3).profile").write_text("synthetic", encoding="utf-8")
    (profiles / "CODESYS V3.5.profile").write_text("other OEM", encoding="utf-8")
    monkeypatch.setattr(launcher, "_registry_locations", lambda: [])
    monkeypatch.setattr(launcher, "_desktop_locations", lambda: [])
    monkeypatch.setattr(launcher, "_common_locations", lambda: [str(exe.parent.parent.parent)])
    monkeypatch.setattr(launcher, "_process_executable", lambda pid: str(exe))
    monkeypatch.setattr(launcher, "_process_alive", lambda pid: False)
    return SimpleNamespace(root=root, state=state, projects=projects, exe=exe, profile="InoProShop(V1.10.0.3)", expected=launcher._expected_version(root))


def host(env, instance="instance-one", pid=123, **values):
    return dict({"host_id": "inoproshop-" + str(pid), "host_instance_id": instance, "pid": pid,
                 "version": env.expected, "available": True, "test_owned": False,
                 "project_path": None, "binding_id": "binding-" + instance,
                 "registration_path": str(env.state / "hosts" / (instance + ".json"))}, **values)


class Session:
    def __init__(self, env, hosts, capabilities=None):
        self.env, self.hosts, self.overrides = env, hosts, capabilities or {}
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "ino_list_hosts":
            hosts = self.hosts() if callable(self.hosts) else self.hosts
            return SimpleNamespace(isError=False, structuredContent={"hosts": hosts})
        assert name == "ino_get_capabilities", "Startup must not read or change any project"
        override = self.overrides.get(arguments["host_instance_id"], {})
        if isinstance(override, Exception):
            raise override
        if "error" in override:
            return SimpleNamespace(isError=True, structuredContent=override)
        capability = {"version": self.env.expected, "write_project_root": str(self.env.projects), "test_owned": False,
                      "capabilities": {"snapshot": {"status": "unknown"}},
                      "runtime_health": {"script_entry_has_returned": True, "ide_enabled": True,
                                         "invoke_required": False, "modal_windows": 0}}
        capability.update(override)
        return SimpleNamespace(isError=False, structuredContent={"host": capability})


async def ensure(env, session, config=None, **kwargs):
    return await launcher.ensure_host(env.root, env.state, env.projects, session, config if config is not None else {}, progress=lambda message: None, **kwargs)


def test_discovery_bounds_to_vendor_executable_and_profiles(environment):
    env = environment
    found = launcher.discover_installations({}, str(env.exe))
    assert found == [{"exe": str(env.exe), "profile": env.profile}]
    assert launcher.discover_installations({}, str(env.root / "missing/InoProShop.exe")) == []


def test_embedded_windows_discovery_script_parses_in_actual_powershell(monkeypatch):
    shell = shutil.which("powershell.exe")
    if os.name != "nt" or not shell:
        pytest.skip("Windows PowerShell is required to validate the embedded discovery script")
    captured = {}
    real_run = subprocess.run

    def capture_script(arguments, **kwargs):
        captured["script"] = arguments[-1]
        return SimpleNamespace(stdout=b"[]", returncode=0)

    with monkeypatch.context() as capture:
        capture.setattr(launcher.subprocess, "run", capture_script)
        assert launcher._desktop_locations() == []
    env = os.environ.copy()
    env["INO_TEST_DISCOVERY_SCRIPT"] = captured["script"]
    parser = r'''
$tokens = $null
$errors = $null
$null = [System.Management.Automation.Language.Parser]::ParseInput($env:INO_TEST_DISCOVERY_SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Output $_.Message }; exit 1 }
exit 0
'''
    result = real_run([shell, "-NoProfile", "-NonInteractive", "-Command", parser], env=env, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace")


async def test_empty_healthy_host_with_unknown_evidence_is_reused(environment, monkeypatch):
    env = environment
    monkeypatch.setattr(launcher, "_start_ide", lambda *args: pytest.fail("Healthy host must be reused"))
    config = {"unrelated": "preserve"}
    session = Session(env, [host(env)])
    selected = await ensure(env, session, config)
    assert selected["reused"] is True and selected["project_path"] is None
    assert config["ide"]["last_host_instance_id"] == "instance-one"
    assert config["unrelated"] == "preserve"
    assert [name for name, _ in session.calls] == ["ino_list_hosts", "ino_get_capabilities"]


async def test_check_only_does_not_mutate_config_or_files(environment):
    env = environment
    config = {"ide": {"exe": str(env.exe), "profile": env.profile}, "keep": {"x": 1}}
    before_config, before_files = copy.deepcopy(config), list(env.root.rglob("*"))
    await ensure(env, Session(env, [host(env)]), config, check_only=True)
    assert config == before_config
    assert list(env.root.rglob("*")) == before_files


async def test_unknown_reused_installation_does_not_inherit_previous_host_path(environment, monkeypatch):
    env = environment
    monkeypatch.setattr(launcher, "_process_executable", lambda pid: None)
    config = {"ide": {"exe": str(env.exe), "profile": env.profile, "last_host_instance_id": "old-instance"}}
    selected = await ensure(env, Session(env, [host(env)]), config)
    assert selected["exe"] is None and selected["profile"] is None
    assert "exe" not in config["ide"] and "profile" not in config["ide"]
    assert config["ide"]["last_host_instance_id"] == "instance-one"


@pytest.mark.parametrize("change", [
    {"test_owned": True}, {"available": False}, {"version": {"bridge": "old"}},
    {"registration_path": "C:/different-state/hosts/host.json"},
])
async def test_test_stale_wrong_source_and_wrong_state_hosts_are_excluded(environment, change):
    env = environment
    with pytest.raises(launcher.LauncherHostError) as failure:
        await ensure(env, Session(env, [host(env, **change)]), check_only=True)
    assert failure.value.code == "HOST_NOT_RUNNING"


async def test_capability_root_must_match(environment):
    env = environment
    session = Session(env, [host(env)], {"instance-one": {"write_project_root": str(env.root / "other-projects")}})
    with pytest.raises(launcher.LauncherHostError) as failure:
        await ensure(env, session, check_only=True)
    assert failure.value.code == "HOST_NOT_RUNNING"


@pytest.mark.parametrize("override", [
    {"error": {"code": "HOST_BUSY", "message": "do not print remote content"}},
    {"runtime_health": {"script_entry_has_returned": True, "ide_enabled": False, "invoke_required": False, "modal_windows": 1}},
    RuntimeError("secret=credential-must-not-escape"),
])
async def test_blocked_registered_host_never_spawns_duplicate(environment, monkeypatch, override):
    env = environment
    monkeypatch.setattr(launcher, "_start_ide", lambda *args: pytest.fail("Busy host must not trigger another IDE"))
    session = Session(env, [host(env)], {"instance-one": override})
    with pytest.raises(launcher.LauncherHostError) as failure:
        await ensure(env, session)
    assert "不会重复启动" in str(failure.value)
    assert failure.value.message == str(failure.value)
    assert "credential" not in str(failure.value)


async def test_last_host_is_preferred_among_multiple_healthy_instances(environment, monkeypatch):
    env = environment
    config = {"ide": {"last_host_instance_id": "two", "exe": str(env.exe), "profile": env.profile}}
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Remembered host must not prompt"))
    selected = await ensure(env, Session(env, [host(env), host(env, "two", 456)]), config)
    assert selected["host_instance_id"] == "two"


async def test_multiple_instances_require_selection_in_noninteractive_mode(environment):
    env = environment
    with pytest.raises(launcher.LauncherHostError) as failure:
        await ensure(env, Session(env, [host(env), host(env, "two", 456)]), non_interactive=True)
    assert failure.value.code == "SELECTION_REQUIRED"


async def test_numeric_choice_is_respected(environment, monkeypatch):
    env = environment
    answers = iter(["invalid", "3", "2"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    selected = await ensure(env, Session(env, [host(env), host(env, "two", 456)]))
    assert selected["host_instance_id"] == "two"


async def test_start_builds_production_wrapper_and_waits_for_exact_pid(environment, monkeypatch):
    env = environment
    started = []
    original_hashes = {name: hashlib.sha256((env.root / "adapters/inoproshop/bridge" / name).read_bytes()).hexdigest() for name in ("entry.py", "adapter.py")}

    def start(exe, profile, wrapper):
        started.append((exe, profile, wrapper))
        return SimpleNamespace(pid=987, poll=lambda: None)

    monkeypatch.setattr(launcher, "_start_ide", start)
    session = Session(env, lambda: [host(env, "new-host", 987)] if started else [])
    config = {}
    selected = await ensure(env, session, config, timeout=2)
    assert selected["pid"] == 987 and selected["reused"] is False
    assert len(started) == 1 and started[0][:2] == (str(env.exe), env.profile)
    wrapper = started[0][2]
    source = wrapper.read_text(encoding="utf-8")
    assert "execfile" in source and "INOPROSHOP_MCP_CONFIG" in source
    assert "prompt_handling" not in source and "projects.open" not in source
    bridge_config = json.loads((wrapper.parent / "bridge.json").read_text(encoding="utf-8"))
    assert bridge_config["test_owned"] is False
    assert bridge_config["state_dir"] == str(env.state)
    assert "pending_start" not in config["ide"]
    record = json.loads((env.state / "launcher/ide-process.json").read_text(encoding="utf-8"))
    assert record["status"] == "ready" and record["pid"] == 987
    assert {name: hashlib.sha256((env.root / "adapters/inoproshop/bridge" / name).read_bytes()).hexdigest() for name in original_hashes} == original_hashes


async def test_start_timeout_preserves_owned_pid_and_never_terminates_ide(environment, monkeypatch):
    env = environment
    process = SimpleNamespace(pid=789, poll=lambda: None)
    monkeypatch.setattr(launcher, "_start_ide", lambda *args: process)
    config = {}
    with pytest.raises(launcher.LauncherHostError) as failure:
        await ensure(env, Session(env, []), config, timeout=0)
    assert failure.value.code == "IDE_START_TIMEOUT"
    assert config["ide"]["pending_start"]["pid"] == 789
    assert json.loads((env.state / "launcher/ide-process.json").read_text(encoding="utf-8"))["pid"] == 789
    monkeypatch.setattr(launcher, "_process_alive", lambda pid: pid == 789)
    monkeypatch.setattr(launcher, "_start_ide", lambda *args: pytest.fail("Pending owned startup must be reused"))
    with pytest.raises(launcher.LauncherHostError) as second:
        await ensure(env, Session(env, []), config)
    assert second.value.code == "HOST_START_PENDING"


async def test_explicit_profile_is_validated_before_start(environment):
    env = environment
    with pytest.raises(launcher.LauncherHostError) as failure:
        await ensure(env, Session(env, []), ino_exe=str(env.exe), profile="CODESYS V3.5")
    assert failure.value.code == "IDE_NOT_FOUND"


@pytest.mark.parametrize("profile", ["InoProShop(V1.10.0.3)", "InoProShop Test Profile"])
def test_process_launch_quotes_option_values_for_oem_raw_command_parser(environment, monkeypatch, profile):
    env = environment
    captured = {}

    def popen(args, **kwargs):
        captured.update(args=args, **kwargs)
        return SimpleNamespace(pid=456)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    wrapper = env.state / "bridge wrapper 中文.py"
    launcher._start_ide(str(env.exe), profile, wrapper)
    assert captured["cwd"] == str(env.exe.parent)
    assert captured["env"]["__COMPAT_LAYER"] == "RunAsInvoker"
    if launcher.os.name == "nt":
        assert captured["args"] == (subprocess.list2cmdline([str(env.exe)])
                                    + " --profile=" + subprocess.list2cmdline([profile])
                                    + " --runscript=" + subprocess.list2cmdline([str(wrapper)]))
        assert ' --runscript="' in captured["args"]
        assert '"--runscript=' not in captured["args"]
        assert "--noUI" not in captured["args"]
        assert captured["executable"] == str(env.exe)
        assert captured["startupinfo"].wShowWindow == 1
    else:
        assert captured["args"] == [str(env.exe), "--profile=" + profile, "--runscript=" + str(wrapper)]

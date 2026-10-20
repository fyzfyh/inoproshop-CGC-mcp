import importlib.util
from pathlib import Path


def test_release_includes_complete_launcher_without_runtime_or_credentials():
    root = Path(__file__).resolve().parents[3]
    script = root / "services/inoproshop-mcp/scripts/build-release.py"
    spec = importlib.util.spec_from_file_location("launcher_release", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    files = dict(module.release_files(root))
    required = {
        "Connect-Ino.cmd", "CONNECT-INOPROSHOP.md",
        "services/inoproshop-mcp/scripts/Connect-Ino.ps1",
        "services/inoproshop-mcp/scripts/Start-OMP.ps1",
        "services/inoproshop-mcp/launcher-runtime-lock.json",
        "services/inoproshop-mcp/src/inoproshop_mcp/launcher.py",
        "services/inoproshop-mcp/src/inoproshop_mcp/launcher_database.py",
        "services/inoproshop-mcp/src/inoproshop_mcp/launcher_host.py",
        "adapters/inoproshop/bridge/entry.py", "demo/v2/ai_package.py",
        "templates/catalog.json", "templates/README.md",
        "services/inoproshop-mcp/src/inoproshop_mcp/templates.py",
        "services/inoproshop-mcp/src/inoproshop_mcp/text_search.py",
        "services/inoproshop-mcp/docs/ENGINEERING_EXTENSIONS.md",
        "scripts/native_engineering_extensions.py",
    }
    assert required <= files.keys()
    assert not any(name.startswith(("work/", "artifacts/")) for name in files)
    assert not any(Path(name).name in {"token", "credentials.json", "service-runtime.json"} for name in files)
    assert not any(".venv" in name or name.endswith((".exe", ".project", ".db")) for name in files)

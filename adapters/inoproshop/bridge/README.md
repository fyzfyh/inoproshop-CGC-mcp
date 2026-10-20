# InoProShop native bridge

`entry.py` runs inside InoProShop's IronPython ScriptEngine. It returns after registering a WinForms timer. All project API calls execute in that timer on the IDE UI thread; the background thread handles only authenticated pipe traffic and heartbeat files. No model SDK is loaded in the IDE.

The working implementation reuses `demo/v2/ai_package.py` for native capture and `demo/v2/diagnostics.py` for source locations. Keep the repository layout when deploying these files.

## Configuration

Create `work/mcp_p0/bridge.json` with these values, then run `adapters/inoproshop/bridge/entry.py` from the IDE's script menu:

```json
{
  "state_dir": "E:/AI-PLC/work/mcp_p0/state",
  "project_root": "E:/AI-PLC/work/mcp_p0/projects",
  "native_evidence": "E:/AI-PLC/work/mcp_p0/state/native_evidence.json",
  "test_owned": false,
  "queue_timeout": 120
}
```

Start the MCP controller first so its random `state_dir/token` exists. Never commit or print that token. The named pipe grants access only to the current Windows user's SID and additionally checks the token on every request. `test_owned` enables only the private test-host shutdown command; it does **not** unlock native write capabilities.

Registrations live in `state_dir/hosts/<host_instance_id>.json`. Each registration carries PID, pipe name, binding ID, version and heartbeat. Opening/reopening a project or changing its saved path invalidates the old binding. A new IDE process always receives a fresh host instance. A modal or disabled IDE pauses the UI queue; queued commands expire with a known `HOST_BUSY`, while an already executing write with no result is unknown and cannot be retried automatically.

On this OEM the first build can race background device/compiler initialization after `open` returns. The bridge currently delays dispatch of compile commands until 45 seconds after bridge initialization and 30 seconds after an open, while leaving the ordinary IDE event loop running. This is a conservative observed startup allowance, not a verified readiness API; compilation still requires actual completion evidence and never converts initialization errors to success. Heartbeat publication is serialized and transient Windows sharing conflicts defer only the heartbeat.

## Native behavior and boundaries

- Project mutations are restricted to `.project` copies inside `project_root`. Opening never discards a dirty project; a failed open attempts to reopen the previous saved project and reports recovery explicitly.
- `replace_text` checks source/snapshot hashes and reads both text sections back exactly. The Core owns durable checkpoints and compensation policy.
- `import_st` creates a new ST FB through the verified PLCopen importer, then replaces and reads back its declaration and implementation. It does not add a task or call. The Core's explicit two-object plan adds the caller instance and invocation.
- Unicode source/name transport round-trips exactly. The tested AM600 sample compiler rejected a Chinese FB identifier (`FB_验证_...`); Unicode identifier compilation is project/OEM dependent and is not claimed as supported. The failed native report is retained. Standard identifier creation is verified separately.
- `delete_object` removes the exact GUID, checks its expected hash and verifies absence. Only the Core's reviewed compensation path should use it; it is not an unrestricted public deletion tool.
- `compile` records the active Application, source snapshot, raw diagnostics, completion evidence and timestamps. Empty messages and exceptions remain `unknown`; a passing Application build does not establish target reachability by itself.
- `save_copy` uses native `save_as`, reports the new active path/binding and verifies the clean saved state. This changes the active project path. The Core never presents this as two independently open engineering projects.
- Native evidence gates match the complete adapter/entry source hashes and actual OEM software version. New bridge code or a different OEM version requires rerunning native verification. Unsupported graphical readback remains blocked.

## Verification

Local protocol checks (no IDE): `python scripts/native_protocol_tests.py`.

Native adapter checks: `python scripts/native_verify.py`. This launches only its own hidden desktop-mode IDE, creates independent copies, records exact startup-dialog handling, verifies creation/compensation and save/reopen, then closes that owned process. The original sample's file hash must remain unchanged. `--noUI` is not used.

Public MCP acceptance requires the controller running on `127.0.0.1:8765`: run `services/inoproshop-mcp/.venv/Scripts/python.exe scripts/native_core_verify.py`. This uses the official MCP client for real tool calls and the historical 17-case diagnostic matrix. Private pipe mutations prepare diagnostic and synthetic-source fixtures; all candidate application, compilation and diagnostic reads go through MCP. Previously generated real-model candidates use only the synthetic evaluation fixture; no real engineering source is sent to a model. No online PLC is involved.

The runner also supports separate acceptance stages: `--am402-only` repairs the original failing AM402 sample in a new copy; `--fixture-only` verifies the private synthetic GVL/POU seed; `--extended-only --skip-am402 --monitor-cycles 2` runs public reads, real graph provenance, independent monitor bursts, nine model candidates and a restricted live OMP tool session. `--extended-only --skip-model --skip-monitor --skip-am402 --coverage-check --modal-check` checks unreferenced/referenced compile coverage, automatic failed-creation compensation, and one queued read during an automatically closed, test-owned modal window. These test-only fixture helpers do not add production bridge commands.

The fixture probe confirms this OEM exposes the ST document through `has_textual_implementation` and its ST embedded object type. The upstream `create_program(language=...)` API is absent in this installation; attempted IL/LD fixture imports did not produce objects, so those native negative probes remain unverified. Available task/device metadata, missing library/I/O details, compiler coverage and editor-buffer synchronization are reported separately; a passing snapshot read does not establish all of those capabilities.

Do not run the two native runners concurrently. Reports are under `artifacts/mcp_p0/native` and `artifacts/mcp_p0/native_core`. The gate evidence file is written only after the adapter acceptance run completes and its owned host exits cleanly.

Reference semantics: [ScriptObject.remove](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptObject.html) and [ScriptProject.save_as](https://content.helpme-codesys.com/en/ScriptingEngine/ScriptProject.html). The local OEM acceptance report, rather than upstream API existence, establishes support.

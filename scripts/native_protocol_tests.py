"""Local tests for native boundary failures. No IDE or PLC is launched."""
from __future__ import annotations
import importlib.util
import ast
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP = ROOT / 'work/mcp_p0/protocol_tmp'
TEST_TEMP.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "services/inoproshop-mcp/src"))
from inoproshop_mcp.common import DomainError, node_hash as shared_hash
from inoproshop_mcp.transport import encode_frame, read_frame, discover_hosts, PipeClient

spec = importlib.util.spec_from_file_location("bridge_adapter", ROOT / "adapters/inoproshop/bridge/adapter.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class ProtocolTests(unittest.TestCase):
    def test_fragmented_unicode_frame(self):
        value = {"ok": True, "result": {"path": "工程/程序一", "source": "换行\n变量"}}
        stream = io.BytesIO(encode_frame(value))
        self.assertEqual(read_frame(lambda size: stream.read(min(2, size))), value)

    def test_oversized_and_truncated_frames_rejected(self):
        with self.assertRaises(ValueError): read_frame(io.BytesIO(struct.pack("<I", 17 * 1024 * 1024)).read)
        with self.assertRaises(EOFError): read_frame(io.BytesIO(struct.pack("<I", 4) + b"{}").read)

    def test_shared_node_hash_compatible(self):
        item = {"declaration": "变量：BOOL;", "implementation": "变量 := TRUE;"}
        self.assertEqual(adapter.node_hash(item), shared_hash(item))

    def test_stale_registration_is_unavailable(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            hosts = Path(directory) / "hosts"; hosts.mkdir()
            record = {"host_id": "h", "host_instance_id": "i", "pid": 2, "pipe_name": "p", "heartbeat_at": time.time()-100}
            (hosts / "h.json").write_text(json.dumps(record), encoding="utf-8")
            (hosts / "bad.json").write_text("unfinished", encoding="utf-8")
            self.assertFalse(discover_hosts(directory)[0]["available"])

    def test_timeout_after_write_is_unknown_and_never_retried(self):
        class Pipe:
            writes = 0
            def __init__(self, *args): pass
            def write(self, value): Pipe.writes += 1
            def read(self, count): raise TimeoutError("lost result")
            def close(self): pass
        with patch("inoproshop_mcp.transport._WindowsPipe", Pipe):
            with self.assertRaises(DomainError) as result: PipeClient({"pipe_name": "p"}, "secret").call("replace_text", {})
        self.assertEqual(result.exception.code, "OPERATION_OUTCOME_UNKNOWN")
        self.assertEqual(Pipe.writes, 1)

    def test_known_rejection_preserves_domain_code(self):
        class Pipe:
            def __init__(self, *args): self.stream = io.BytesIO(encode_frame({"ok": False, "error": {"code": "SOURCE_CONFLICT", "message": "edited"}}))
            def write(self, value): pass
            def read(self, count): return self.stream.read(count)
            def close(self): pass
        with patch("inoproshop_mcp.transport._WindowsPipe", Pipe):
            with self.assertRaises(DomainError) as result: PipeClient({"pipe_name": "p"}, "secret").call("replace_text", {})
        self.assertEqual(result.exception.code, "SOURCE_CONFLICT")

    def test_path_restriction_uses_directory_boundary(self):
        with tempfile.TemporaryDirectory(dir=TEST_TEMP) as directory:
            with self.assertRaises(adapter.Rejected): adapter.inside(directory + "_other/x.project", directory)
            with self.assertRaises(adapter.Rejected): adapter.inside(str(Path(directory) / "x.txt"), directory)

    def test_heartbeat_sharing_conflict_does_not_abort_native_request(self):
        # Execute the actual method without importing the IDE's .NET assemblies.
        tree = ast.parse((ROOT / 'adapters/inoproshop/bridge/entry.py').read_text(encoding='utf-8'))
        bridge = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Bridge')
        method = next(n for n in bridge.body if isinstance(n, ast.FunctionDef) and n.name == 'publish')
        def locked_reader(*args): raise OSError(1175, 'Destination held by discovery reader')
        context = {'time': time, 'atomic_json': locked_reader}
        exec(compile(ast.Module(body=[method], type_ignores=[]), '<native publish>', 'exec'), context)
        class State: pass
        state = State()
        state.publish_lock, state.last_publish, state.running = threading.Lock(), 0, True
        state.cached, state.registration = {'binding_id': 'new-binding'}, 'unused.json'
        context['publish'](state)
        self.assertEqual(state.last_publish, 0)  # next heartbeat still retries publication
        writes = []
        context['atomic_json'] = lambda path, value: writes.append(value)
        context['publish'](state)
        self.assertEqual(writes[0]['binding_id'], 'new-binding')
        self.assertGreater(state.last_publish, 0)


if __name__ == "__main__": unittest.main(verbosity=2)

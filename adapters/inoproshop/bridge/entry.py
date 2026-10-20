# encoding: utf-8
"""Execute in InoProShop ScriptEngine. Returns immediately; UI Timer owns API calls.

Supply INOPROSHOP_MCP_CONFIG in globals (the launcher does so), or create
work/mcp_p0/bridge.json. No model SDK or third-party Python dependency is loaded.
"""
from __future__ import unicode_literals
import os
import sys
import io
import json
import struct
import threading
import time
import traceback
import uuid
import hashlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, 'demo', 'v2'))
from adapter import NativeAdapter, Rejected, text, hash_value
from ai_package import capture_project
from diagnostics import locate

import clr
clr.AddReference('System')
clr.AddReference('System.Core')
clr.AddReference('System.Windows.Forms')
from System import AppDomain, Array, Byte
from System.Diagnostics import Process
from System.IO import File
from System.IO.Pipes import NamedPipeServerStream, PipeDirection, PipeTransmissionMode, PipeOptions, PipeSecurity, PipeAccessRule, PipeAccessRights
from System.Security.AccessControl import AccessControlType
from System.Security.Principal import WindowsIdentity
from System.Threading import Thread
from System.Windows.Forms import Timer, Application

KEY = 'InoProShop.MCP.Bridge.v1'
MAX_FRAME = 16 * 1024 * 1024
MUTATING = ('replace_text', 'import_st', 'delete_object', 'save_copy', 'open_project', 'shutdown')


def atomic_json(path, value):
    parent = os.path.dirname(path)
    if not os.path.isdir(parent): os.makedirs(parent)
    temporary = path + '.' + uuid.uuid4().hex + '.tmp'
    with io.open(temporary, 'w', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2))
    if os.path.isfile(path): File.Replace(temporary, path, None)
    else: File.Move(temporary, path)


def constant_time_equal(left, right):
    # Equal length digests avoid leaking token prefix through comparison timing.
    a, b = hashlib.sha256(left.encode('utf-8')).digest(), hashlib.sha256(right.encode('utf-8')).digest()
    mismatch = 0
    for x, y in zip(bytearray(a), bytearray(b)): mismatch |= x ^ y
    return mismatch == 0


class Bridge(object):
    def __init__(self, config):
        self.config = config
        with io.open(os.path.join(config['state_dir'], 'token'), encoding='utf-8') as handle: self.token = handle.read().strip()
        if len(self.token) < 32: raise ValueError('Create a random service token before loading the bridge.')
        self.instance = uuid.uuid4().hex
        self.pid = Process.GetCurrentProcess().Id
        self.pipe_name = 'inoproshop-mcp-' + self.instance
        self.registration = os.path.join(config['state_dir'], 'hosts', self.instance + '.json')
        self.adapter = NativeAdapter(projects, system, config, capture_project, locate)
        self.running, self.busy, self.pending, self.pipe = True, False, None, None
        self.lock = threading.Lock()
        self.publish_lock = threading.Lock()
        self.cached = {}
        self.last_publish = 0
        self.ready_after = 0
        # This OEM initializes compiler/device services after project.open returns.
        # Keep pumping the normal IDE loop; never sleep on its UI thread.
        self.compile_ready_after = time.time() + 45.0
        self.ide = next((f for f in Application.OpenForms if f.Name == 'MainForm' and not f.IsDisposed), None)
        self.timer = Timer(); self.timer.Interval = 100
        self.timer.Tick += self.tick
        if self.ide is not None: self.ide.FormClosed += self.closed
        self.update_cache()
        self.publish()
        self.thread = threading.Thread(target=self.serve)
        self.thread.daemon = True
        self.thread.start()
        self.timer.Start()

    def update_cache(self):
        self.adapter.refresh_binding()
        self.cached = {'host_id': 'inoproshop-' + str(self.pid), 'host_instance_id': self.instance,
                       'pid': self.pid, 'pipe_name': self.pipe_name, 'project_path': self.adapter.project_path,
                       'binding_id': self.adapter.binding_id, 'version': self.adapter.version,
                       'test_owned': bool(self.config.get('test_owned')), 'busy': self.busy}

    def publish(self):
        # UI completion and the pipe waiter both publish. Discovery readers may
        # temporarily hold a Windows file handle without delete sharing. Neither
        # contention may abort an otherwise completed native command.
        with self.publish_lock:
            if time.time() - self.last_publish < 1 and self.running: return
            value = dict(self.cached, heartbeat_at=time.time(), stopped=not self.running)
            try:
                atomic_json(self.registration, value)
            except (IOError, OSError):
                return
            self.last_publish = time.time()

    def closed(self, sender=None, event=None):
        self.stop()

    def stop(self):
        self.running = False
        self.timer.Stop(); self.timer.Tick -= self.tick; self.timer.Dispose()
        if self.ide is not None: self.ide.FormClosed -= self.closed
        try:
            if self.pipe is not None: self.pipe.Dispose()
        except Exception: pass
        self.last_publish = 0; self.publish()
        if AppDomain.CurrentDomain.GetData(KEY) is self: AppDomain.CurrentDomain.SetData(KEY, None)

    def tick(self, sender, event):
        if self.busy or not self.running: return
        self.update_cache()
        if time.time() < self.ready_after:
            self.cached['waiting_reason'] = 'PROJECT_UI_SETTLING'
            return
        if (self.ide is not None and (self.ide.IsDisposed or not self.ide.Enabled)) or any(f.Modal for f in Application.OpenForms):
            self.cached['waiting_reason'] = 'IDE_MODAL_OR_DISABLED'
            return
        with self.lock:
            job = self.pending
            if job is not None and job['request']['command'] == 'compile' and time.time() < self.compile_ready_after:
                self.cached['waiting_reason'] = 'OEM_COMPILER_INITIALIZATION'
                return
            self.pending = None
        if job is None: return
        if job.get('cancelled'): return
        self.busy = True; job['started'] = True
        self.update_cache()
        request = job['request']
        try:
            if request['command'] in MUTATING:
                atomic_json(job['journal'], {'status': 'executing', 'request_hash': job['request_hash'],
                            'command_id': request['command_id'], 'command': request['command'], 'at': time.time()})
            if request['command'] == 'shutdown':
                self.adapter.check(request['params'], allow_empty=True)
                if not self.config.get('test_owned'): raise Rejected('NOT_TEST_OWNED', 'Only a service-owned test host can be shut down.')
                result = {'shutdown_requested': True}
                self.shutdown_after_reply = True
            else:
                result = self.adapter.dispatch(request['command'], request['params'])
            if request['command'] in ('open_project', 'save_copy'):
                self.ready_after = time.time() + 2.0
            if request['command'] == 'open_project':
                self.compile_ready_after = max(self.compile_ready_after, time.time() + 30.0)
            if request['command'] == 'capabilities':
                result['runtime_health'] = {'ui_callback_thread_id': Thread.CurrentThread.ManagedThreadId,
                    'script_entry_has_returned': True, 'ide_enabled': bool(self.ide is not None and self.ide.Enabled),
                    'invoke_required': bool(self.ide is not None and self.ide.InvokeRequired),
                    'modal_windows': sum(bool(f.Modal) for f in Application.OpenForms)}
            job['response'] = {'ok': True, 'result': result}
        except Rejected as exc:
            job['response'] = {'ok': False, 'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}}
        except Exception as exc:
            job['response'] = {'ok': False, 'error': {'code': 'OPERATION_OUTCOME_UNKNOWN' if request['command'] in MUTATING else 'HOST_ERROR',
                              'message': text(exc), 'details': {'command': request['command']}}}
        finally:
            if request['command'] in MUTATING:
                atomic_json(job['journal'], {'status': 'completed', 'request_hash': job['request_hash'], 'response': job['response'], 'at': time.time()})
            self.busy = False
            self.update_cache()
            self.last_publish = 0; self.publish()
            job['done'].set()

    def read_exact(self, pipe, size):
        chunks = []
        deadline = time.time() + 30
        while size and self.running:
            buf = Array.CreateInstance(Byte, min(size, 65536))
            op = pipe.BeginRead(buf, 0, len(buf), None, None)
            while not op.AsyncWaitHandle.WaitOne(1000):
                self.publish()
                if not self.running or time.time() > deadline: raise ValueError('Pipe input deadline expired.')
            count = pipe.EndRead(op)
            if not count: raise ValueError('Pipe client disconnected.')
            chunks.append(bytes(bytearray(buf[:count])))
            size -= count
        if size: raise ValueError('Bridge stopped during read.')
        return b''.join(chunks)

    def write_response(self, pipe, response):
        raw = json.dumps(response, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(raw) > MAX_FRAME:
            raw = json.dumps({'ok': False, 'error': {'code': 'RESPONSE_TOO_LARGE', 'message': 'Native response exceeds 16 MiB; reduce project scope.', 'details': {}}}).encode('utf-8')
        data = Array[Byte](bytearray(struct.pack('<I', len(raw)) + raw))
        pipe.Write(data, 0, len(data)); pipe.Flush()

    def request(self, request):
        if not isinstance(request, dict) or not isinstance(request.get('params'), dict):
            raise Rejected('INVALID_ARGUMENT', 'Malformed pipe request.')
        token = request.get('token')
        if not isinstance(token, type('')) or not constant_time_equal(token, self.token):
            raise Rejected('UNAUTHORIZED', 'Bridge token rejected.')
        identifier = request.get('command_id', '')
        if not identifier or len(identifier) > 128 or not all(c.isalnum() or c in '-_' for c in identifier):
            raise Rejected('INVALID_ARGUMENT', 'Invalid command ID.')
        fingerprint = hash_value([request.get('command'), request['params']])
        journal = os.path.join(self.config['state_dir'], 'bridge_commands', self.instance, identifier + '.json')
        if os.path.isfile(journal):
            with io.open(journal, encoding='utf-8') as handle: previous = json.load(handle)
            if previous['request_hash'] != fingerprint: raise Rejected('IDEMPOTENCY_CONFLICT', 'Command ID was reused for different input.')
            if previous['status'] == 'completed': return previous['response']
            raise Rejected('OPERATION_OUTCOME_UNKNOWN', 'Prior execution has no completion record; reconcile before any retry.')
        job = {'request': request, 'done': threading.Event(), 'journal': journal, 'request_hash': fingerprint, 'started': False}
        with self.lock:
            if self.pending is not None: raise Rejected('HOST_BUSY', 'Another command is already queued.')
            self.pending = job
        queued_at = time.time()
        while self.running and not job['done'].wait(1):
            self.publish()
            if not job['started'] and time.time() - queued_at > self.config.get('queue_timeout', 120):
                with self.lock:
                    if not job['started']:
                        job['cancelled'] = True
                        if self.pending is job: self.pending = None
                        raise Rejected('HOST_BUSY', 'IDE did not become available before the queue deadline.')
        return job.get('response', {'ok': False, 'error': {'code': 'OPERATION_OUTCOME_UNKNOWN', 'message': 'Host stopped before responding.', 'details': {}}})

    def serve(self):
        while self.running:
            try:
                security = PipeSecurity()
                security.SetAccessRuleProtection(True, False)
                security.AddAccessRule(PipeAccessRule(WindowsIdentity.GetCurrent().User, PipeAccessRights.FullControl, AccessControlType.Allow))
                self.pipe = NamedPipeServerStream(self.pipe_name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte,
                                                 PipeOptions.Asynchronous, 65536, 65536, security)
                op = self.pipe.BeginWaitForConnection(None, None)
                while self.running and not op.AsyncWaitHandle.WaitOne(1000): self.publish()
                if not self.running: break
                self.pipe.EndWaitForConnection(op)
                size = struct.unpack('<I', self.read_exact(self.pipe, 4))[0]
                if not 0 < size <= MAX_FRAME: raise Rejected('INVALID_ARGUMENT', 'Invalid pipe frame size.')
                request = json.loads(self.read_exact(self.pipe, size).decode('utf-8'))
                try: response = self.request(request)
                except Rejected as exc: response = {'ok': False, 'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}}
                self.write_response(self.pipe, response)
            except Exception as exc:
                if self.running:
                    atomic_json(os.path.join(self.config['state_dir'], 'bridge_last_error.json'), {'at': time.time(), 'error': text(exc), 'traceback': traceback.format_exc()})
            finally:
                try:
                    if self.pipe is not None: self.pipe.Dispose()
                except Exception: pass
                self.pipe = None
            if getattr(self, 'shutdown_after_reply', False):
                # Schedule shutdown on the UI thread only after the pipe response was sent.
                self.timer.Tick -= self.tick
                self.timer.Tick += self.exit_host
                return

    def exit_host(self, sender, event):
        self.timer.Tick -= self.exit_host
        self.stop()
        if self.adapter.project is not None:
            # Test hosts own disposable copies; shutdown does not persist an unrequested edit.
            self.adapter.project.close()
        system.exit()


def main():
    existing = AppDomain.CurrentDomain.GetData(KEY)
    if existing is not None and existing.running: return existing
    config_path = globals().get('INOPROSHOP_MCP_CONFIG', os.path.join(ROOT, 'work', 'mcp_p0', 'bridge.json'))
    with io.open(config_path, encoding='utf-8-sig') as handle: config = json.load(handle)
    bridge = Bridge(config)
    AppDomain.CurrentDomain.SetData(KEY, bridge)
    return bridge


try:
    main()
except Exception:
    error_path = os.path.join(ROOT, 'work', 'mcp_p0', 'bridge_startup_error.json')
    atomic_json(error_path, {'at': time.time(), 'traceback': traceback.format_exc()})
    raise

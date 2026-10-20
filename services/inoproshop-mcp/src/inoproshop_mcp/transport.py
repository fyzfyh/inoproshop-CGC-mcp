"""Authenticated, bounded, deadline-aware local Windows named-pipe transport."""
from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import struct
import time
import uuid

from .common import DomainError

MAX_FRAME = 16 * 1024 * 1024
WRITE_COMMANDS = frozenset({"replace_text", "import_st", "delete_object", "save_copy", "open_project", "shutdown"})


def encode_frame(value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(payload) <= MAX_FRAME:
        raise DomainError("INVALID_ARGUMENT", "Pipe message exceeds the 16 MiB limit.")
    return struct.pack("<I", len(payload)) + payload


def read_frame(read):
    def exact(count):
        chunks = []
        while count:
            chunk = read(count)
            if not chunk:
                raise EOFError("Bridge disconnected before the complete response arrived.")
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)

    size = struct.unpack("<I", exact(4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError("Invalid pipe response length.")
    value = json.loads(exact(size).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Pipe response must be a JSON object.")
    return value


def discover_hosts(state_dir):
    """Return registrations with explicit liveness; stale files are not writable hosts."""
    result = []
    for path in sorted((Path(state_dir) / "hosts").glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8-sig"))
            if not all(item.get(key) for key in ("host_id", "host_instance_id", "pipe_name", "pid")):
                continue
            item["available"] = time.time() - float(item.get("heartbeat_at", 0)) < 15 and not item.get("stopped", False)
            item["registration_path"] = str(path)
            result.append(item)
        except (OSError, ValueError, TypeError):
            continue
    return result


class _WindowsPipe:
    """Overlapped Win32 I/O permits a real timeout without leaving a blocked worker."""

    def __init__(self, name, timeout):
        if os.name != "nt":
            raise OSError("Native named pipes require Windows.")
        from ctypes import wintypes as w

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                        ("Offset", w.DWORD), ("OffsetHigh", w.DWORD), ("hEvent", w.HANDLE)]

        self.OVERLAPPED = OVERLAPPED
        self.w = w
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.deadline = time.monotonic() + timeout
        k = self.kernel
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.WaitNamedPipeW.argtypes = [w.LPCWSTR, w.DWORD]
        k.WaitNamedPipeW.restype = w.BOOL
        k.CreateEventW.argtypes = [w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR]
        k.CreateEventW.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.ReadFile.argtypes = [w.HANDLE, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(OVERLAPPED)]
        k.WriteFile.argtypes = k.ReadFile.argtypes
        k.GetOverlappedResult.argtypes = [w.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(w.DWORD), w.BOOL]
        k.CancelIoEx.argtypes = [w.HANDLE, ctypes.POINTER(OVERLAPPED)]
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        if not name or any(part in name for part in ("/", "\\", ":")):
            raise ValueError("Registration must contain a local pipe name, not a path.")
        path = "\\\\.\\pipe\\" + name
        invalid = ctypes.c_void_p(-1).value
        while True:
            handle = k.CreateFileW(path, 0xC0000000, 0, None, 3, 0x40000000, None)
            if handle != invalid:
                self.handle = handle
                break
            error = ctypes.get_last_error()
            if error not in (2, 231):
                raise ctypes.WinError(error)
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Bridge pipe was not ready before the deadline.")
            k.WaitNamedPipeW(path, min(100, self.remaining()))
            if error == 2:
                time.sleep(min(0.05, max(0, self.deadline - time.monotonic())))

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Bridge request timed out.")
        return max(1, int(left * 1000))

    def _io(self, data, size, writing):
        k, w = self.kernel, self.w
        self.remaining()
        buffer = ctypes.create_string_buffer(data, len(data)) if writing else ctypes.create_string_buffer(size)
        overlapped = self.OVERLAPPED()
        overlapped.hEvent = k.CreateEventW(None, True, False, None)
        if not overlapped.hEvent:
            raise ctypes.WinError(ctypes.get_last_error())
        count = w.DWORD()
        try:
            ok = (k.WriteFile if writing else k.ReadFile)(self.handle, buffer, size, ctypes.byref(count), ctypes.byref(overlapped))
            if not ok:
                error = ctypes.get_last_error()
                if error != 997:
                    raise ctypes.WinError(error)
                try:
                    wait = k.WaitForSingleObject(overlapped.hEvent, self.remaining())
                except TimeoutError:
                    k.CancelIoEx(self.handle, ctypes.byref(overlapped))
                    k.GetOverlappedResult(self.handle, ctypes.byref(overlapped), ctypes.byref(count), True)
                    raise
                if wait != 0:
                    k.CancelIoEx(self.handle, ctypes.byref(overlapped))
                    k.GetOverlappedResult(self.handle, ctypes.byref(overlapped), ctypes.byref(count), True)
                    raise TimeoutError("Bridge request timed out.")
                if not k.GetOverlappedResult(self.handle, ctypes.byref(overlapped), ctypes.byref(count), False):
                    raise ctypes.WinError(ctypes.get_last_error())
            return count.value if writing else buffer.raw[:count.value]
        finally:
            k.CloseHandle(overlapped.hEvent)

    def write(self, data):
        while data:
            size = self._io(data, len(data), True)
            if not size:
                raise EOFError("No bytes written to bridge.")
            data = data[size:]

    def read(self, size):
        return self._io(None, size, False)

    def close(self):
        if getattr(self, "handle", None):
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class PipeClient:
    def __init__(self, host_record, token, timeout=120):
        self.host = host_record
        self.token = token
        self.timeout = timeout

    def call(self, command, params):
        if not isinstance(params, dict):
            raise DomainError("INVALID_ARGUMENT", "Bridge parameters must be an object.")
        payload = encode_frame({"command_id": params.get("command_id") or uuid.uuid4().hex,
                                "token": self.token, "command": command, "params": params})
        pipe, sent = None, False
        try:
            pipe = _WindowsPipe(self.host["pipe_name"], self.timeout)
            # A failed/partial write may have reached the host: never retry automatically.
            sent = True
            pipe.write(payload)
            response = read_frame(pipe.read)
            if response.get("ok") is True and isinstance(response.get("result"), dict):
                return response["result"]
            error = response.get("error") or {}
            if response.get("ok") is False and error.get("code"):
                raise DomainError(error["code"], error.get("message", "Native operation rejected."), error.get("details") or {})
            raise ValueError("Malformed bridge response.")
        except DomainError:
            raise
        except (OSError, TimeoutError, EOFError, ValueError, KeyError) as exc:
            code = "OPERATION_OUTCOME_UNKNOWN" if sent and command in WRITE_COMMANDS else "HOST_UNAVAILABLE"
            raise DomainError(code, str(exc), {"command": command, "host_instance_id": self.host.get("host_instance_id"),
                                             "request_may_have_executed": sent}) from exc
        finally:
            if pipe:
                pipe.close()

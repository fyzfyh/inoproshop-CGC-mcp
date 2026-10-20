"""Exact, previously verified OEM startup dialogs on one owned process only."""
import ctypes
from ctypes import wintypes as w
import json
from pathlib import Path
import threading
import time


class DialogWatcher:
    def __init__(self, pid, report_path):
        self.pid, self.path = pid, Path(report_path)
        self.stop_event = threading.Event()
        self.records = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set(); self.thread.join(timeout=3)

    def run(self):
        user = ctypes.WinDLL('user32', use_last_error=True)
        callback = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        user.EnumWindows.argtypes = [callback, w.LPARAM]
        user.EnumChildWindows.argtypes = [w.HWND, callback, w.LPARAM]
        user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        user.SendMessageTimeoutW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM, w.UINT, w.UINT, ctypes.POINTER(ctypes.c_size_t)]
        def value(handle, class_name=False):
            buffer = ctypes.create_unicode_buffer(4096)
            (user.GetClassNameW if class_name else user.GetWindowTextW)(handle, buffer, len(buffer))
            return buffer.value
        @callback
        def window(handle, _):
            pid = w.DWORD(); user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if pid.value != self.pid or value(handle, True) != '#32770': return True
            children = []
            @callback
            def child(item, _): children.append((item, value(item), value(item, True))); return True
            user.EnumChildWindows(handle, child, 0)
            buttons = [item for item in children if item[2] == 'Button']
            result = ctypes.c_size_t()
            if any('AM600ConfigurationInterface, Version=1.1.0.0' in item[1] for item in children) and len(buttons) == 1:
                user.SendMessageTimeoutW(buttons[0][0], 0x00F5, 0, 0, 2, 1000, ctypes.byref(result))
                self.records.append({'action': 'acknowledge_known_missing_AM600', 'pid': self.pid, 'at': time.time()})
            elif value(handle) in ('File Download', '文件下载') and any('appdata\\local\\temp' in item[1].lower() for item in children) and any(item[1].lower().endswith('.ppa') for item in children):
                user.SendMessageTimeoutW(handle, 0x0010, 0, 0, 2, 1000, ctypes.byref(result))
                self.records.append({'action': 'cancel_known_temp_ppa_download', 'pid': self.pid, 'at': time.time()})
            else: return True
            self.path.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding='utf-8')
            return True
        while not self.stop_event.is_set():
            user.EnumWindows(window, 0)
            self.stop_event.wait(0.5)

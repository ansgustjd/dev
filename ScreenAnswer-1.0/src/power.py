"""Cheap input activity checks and bounded UIA refresh scheduling."""
import ctypes as C
from ctypes import wintypes as W


class LastInputInfo(C.Structure):
    _fields_ = [('cbSize', W.UINT), ('dwTime', W.DWORD)]


def input_stamp():
    info = LastInputInfo(C.sizeof(LastInputInfo), 0)
    user = C.windll.user32
    user.GetForegroundWindow.restype = W.HWND
    if not user.GetLastInputInfo(C.byref(info)):
        return None  # Fall back to frequent reads rather than miss a selection.
    return user.GetForegroundWindow(), info.dwTime


def input_idle_seconds():
    info = LastInputInfo(C.sizeof(LastInputInfo), 0)
    if not C.windll.user32.GetLastInputInfo(C.byref(info)):
        return 0
    C.windll.kernel32.GetTickCount64.restype = C.c_ulonglong
    return ((C.windll.kernel32.GetTickCount64() - info.dwTime) & 0xffffffff) / 1000


def ui_poll_ms(idle_seconds, busy=False, manual=False):
    return 50 if busy or manual or idle_seconds < 2 else 200


class SelectionRefresh:
    def __init__(self):
        self.stamp = object()
        self.last_read = float('-inf')
        self.active_until = 0
        self.selecting = False

    def due(self, stamp, now, selecting, gate=None):
        changed = stamp is None or stamp != self.stamp
        released = self.selecting and not selecting
        self.stamp, self.selecting = stamp, selecting
        if changed:
            self.active_until = now + .2
        if selecting:
            return False  # Intermediate drag fragments are never sent.
        pending = bool(gate and gate.candidate and gate.candidate != gate.sent
                       and now - gate.since >= gate.delay)
        interval = .1 if now < self.active_until else 1.0
        if changed or released or pending or now - self.last_read >= interval:
            self.last_read = now
            return True
        return False

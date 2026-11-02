"""Parse and register Windows hotkeys on a dedicated message thread."""
import ctypes as C
from ctypes import wintypes as W
import queue
import threading

def parse_hotkey(value):
    parts = [x.strip().upper() for x in value.split('+')]
    modifiers = {'ALT': 1, 'CTRL': 2, 'SHIFT': 4, 'WIN': 8}
    if len(parts) < 2 or any(x not in modifiers for x in parts[:-1]):
        raise ValueError('Ctrl+Alt+Q 또는 Ctrl+Shift+F8 형식으로 입력하세요.')
    if len(set(parts[:-1])) != len(parts[:-1]):
        raise ValueError('보조키가 중복되어 있습니다.')
    key = parts[-1]
    if len(key) == 1 and key.isascii() and key.isalnum():
        vk = ord(key)
    elif key == 'SPACE':
        vk = 0x20
    elif key.startswith('F') and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError('키는 영문, 숫자, Space 또는 F1~F24를 사용하세요.')
    return sum(modifiers[x] for x in parts[:-1]) | 0x4000, vk

class CommandQueue(queue.Queue):
    def __init__(self, wake):
        super().__init__()
        self.wake = wake

    def put(self, item, block=True, timeout=None):
        super().put(item, block, timeout)
        self.wake()


class Hotkey:
    def __init__(self, events, event_name='scan'):
        self.events = events
        self.event_name = event_name
        self.thread_id = 0
        self.ready = threading.Event()
        self.commands = CommandQueue(self.wake)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def wake(self):
        if self.thread_id:
            C.windll.user32.PostThreadMessageW(self.thread_id, 0x8001, 0, 0)

    def register(self, value):
        mods, vk = parse_hotkey(value) if value is not None else (None, None)
        result = queue.Queue()
        self.commands.put(('register', mods, vk, result))
        if not result.get(timeout=3):
            raise ValueError('이 단축키를 등록할 수 없습니다. 다른 앱이 사용 중이면 다른 키를 선택하세요.')

    def run(self):
        user = C.windll.user32
        user.PeekMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT, W.UINT]
        user.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
        message = W.MSG()
        user.PeekMessageW(C.byref(message), None, 0, 0, 0)
        self.thread_id = C.windll.kernel32.GetCurrentThreadId()
        self.ready.set()
        current, active = None, 1
        try:
            while True:
                try:
                    while True:
                        cmd = self.commands.get_nowait()
                        if cmd[0] == 'stop':
                            return
                        _, mods, vk, reply = cmd
                        if mods is None:
                            success = not current or bool(user.UnregisterHotKey(None, active))
                            if success:
                                current = None
                            reply.put(success)
                        elif current == (mods, vk):
                            reply.put(True)
                        else:
                            next_id = 2 if active == 1 else 1
                            success = bool(user.RegisterHotKey(None, next_id, mods, vk))
                            if success:
                                if current:
                                    user.UnregisterHotKey(None, active)
                                active, current = next_id, (mods, vk)
                            reply.put(success)
                except queue.Empty:
                    pass
                if user.GetMessageW(C.byref(message), None, 0, 0) <= 0:
                    return
                if message.message == 0x312 and current and message.wParam == active:
                    self.events.put((self.event_name, None))
        finally:
            if current:
                user.UnregisterHotKey(None, active)
            self.thread_id = 0

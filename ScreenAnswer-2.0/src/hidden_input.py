"""A transparent, focused editor for explicit manual input mode, with native IME."""
import ctypes as C
from ctypes import wintypes as W
import tkinter as tk


class GuiThreadInfo(C.Structure):
    _fields_ = [('cbSize', W.DWORD), ('flags', W.DWORD),
                ('hwndActive', W.HWND), ('hwndFocus', W.HWND),
                ('hwndCapture', W.HWND), ('hwndMenuOwner', W.HWND),
                ('hwndMoveSize', W.HWND), ('hwndCaret', W.HWND), ('rcCaret', W.RECT)]


def foreground_window():
    user = C.windll.user32
    user.GetForegroundWindow.restype = W.HWND
    return user.GetForegroundWindow()


def window_handle(window):
    user = C.windll.user32
    user.GetAncestor.argtypes = [W.HWND, W.UINT]
    user.GetAncestor.restype = W.HWND
    return user.GetAncestor(window.winfo_id(), 2)


def commit_composition(widget):
    """Commit the final Korean/Japanese syllable before reading the editor."""
    imm = C.windll.imm32
    imm.ImmGetContext.argtypes = [W.HWND]
    imm.ImmGetContext.restype = W.HANDLE
    imm.ImmNotifyIME.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD]
    imm.ImmReleaseContext.argtypes = [W.HWND, W.HANDLE]
    hwnd = widget.winfo_id()
    context = imm.ImmGetContext(hwnd)
    if context:
        try:
            imm.ImmNotifyIME(context, 0x15, 1, 0)  # NI_COMPOSITIONSTR / CPS_COMPLETE
        finally:
            imm.ImmReleaseContext(hwnd, context)


class HiddenInput:
    def __init__(self, root, app):
        self.app = app
        self.active = False
        self.updating = False
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.title('ScreenAnswer · 입력 모드')
        self.window.overrideredirect(True)
        self.window.attributes('-alpha', 0.0)
        self.window.attributes('-toolwindow', True)
        self.window.geometry('12x12+0+0')
        self.editor = tk.Text(self.window, undo=True, wrap='word')
        self.editor.pack(fill='both', expand=True)
        self.editor.bind('<<Modified>>', self.remember)
        self.editor.bind('<KeyPress>', lambda _: self.app.manual_activity())
        self.editor.bind('<Control-Return>', self.send)
        self.editor.bind('<Control-KP_Enter>', self.send)

    def activate(self):
        value = self.app.manual_draft
        if self.editor.get('1.0', 'end-1c') != value:
            self.updating = True
            self.editor.delete('1.0', 'end')
            self.editor.insert('1.0', value)
            self.editor.edit_modified(False)
            self.updating = False
        self.active = True
        self.window.deiconify()
        self.window.update_idletasks()
        self.ensure_focus()

    def ensure_focus(self):
        if not self.active:
            return
        if self.has_keyboard_focus():
            return
        hwnd = window_handle(self.window)
        if foreground_window() != hwnd:
            info = GuiThreadInfo(cbSize=C.sizeof(GuiThreadInfo))
            if C.windll.user32.GetGUIThreadInfo(0, C.byref(info)) and info.flags & 0x1c:
                return  # Let the tray menu and system menus finish normally.
            if any(C.windll.user32.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 0x12, 0x5b, 0x5c)):
                return
            # The user explicitly routed typing to ScreenAnswer. A real focused
            # editor keeps the system IME working, without translating raw keys.
            user = C.windll.user32
            user.SetForegroundWindow.argtypes = [W.HWND]
            user.ShowWindow.argtypes = [W.HWND, C.c_int]
            user.SetActiveWindow.argtypes = [W.HWND]
            user.SetFocus.argtypes = [W.HWND]
            user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
            user.GetWindowThreadProcessId.restype = W.DWORD
            user.AttachThreadInput.argtypes = [W.DWORD, W.DWORD, W.BOOL]
            current_thread = C.windll.kernel32.GetCurrentThreadId()
            foreground_thread = user.GetWindowThreadProcessId(foreground_window(), None)
            attached = bool(foreground_thread and foreground_thread != current_thread and
                            user.AttachThreadInput(current_thread, foreground_thread, True))
            try:
                # A hidden launcher may supply SW_HIDE in STARTUPINFO. Explicitly
                # map the transparent receiver before assigning keyboard focus.
                user.ShowWindow(hwnd, 5)
                user.SetForegroundWindow(hwnd)
                user.SetActiveWindow(hwnd)
                self.editor.focus_force()
                user.SetFocus(self.editor.winfo_id())
            finally:
                if attached:
                    user.AttachThreadInput(current_thread, foreground_thread, False)
        elif self.window.focus_get() != self.editor:
            self.editor.focus_set()

    def has_keyboard_focus(self):
        if not self.active or self.window.focus_get() != self.editor:
            return False
        user = C.windll.user32
        user.GetFocus.restype = W.HWND
        user.IsChild.argtypes = [W.HWND, W.HWND]
        hwnd, focused = window_handle(self.window), user.GetFocus()
        # A fully transparent foreground window can temporarily be reported as
        # NULL. Require its real native focus as well as Tk's editor focus.
        return (foreground_window() in (None, hwnd) and bool(focused) and
                (focused in (hwnd, self.editor.winfo_id()) or user.IsChild(hwnd, focused)))

    def remember(self, _event=None):
        if self.editor.edit_modified():
            if self.active and not self.updating:
                self.app.manual_draft = self.editor.get('1.0', 'end-1c')
                self.app.manual_activity()
            self.editor.edit_modified(False)

    def suspend(self):
        if self.active:
            commit_composition(self.editor)
            self.remember()
        self.active = False
        self.window.withdraw()

    def send(self, _event=None):
        commit_composition(self.editor)
        self.remember()
        self.app.manual_activity()
        self.app.submit_manual(self.editor.get('1.0', 'end-1c'))
        return 'break'

    def close(self):
        self.suspend()
        self.window.destroy()

"""Windows tray UI coordinating screen input, AI requests and LED feedback."""
import base64
from contextlib import ExitStack
import ctypes as C
from ctypes import wintypes as W
import io
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from PIL import Image, ImageDraw, ImageGrab
import pystray
from selection import SelectionWatcher, parse_delay_ms
from mouse import MouseWatcher, click_number, click_interval
from mathtext import readable_math
from blink import Blinker, blink_count
from references import reference_inputs, validate_reference_paths, validate_vector_store_id
from tasks import PRESETS, task_prompt, feedback as task_feedback, validate_feedback, migrate_task_profiles, default_task_prompt
from led_response import validate_result
from monitors import monitor_workarea, capture_bounds
from diagnostics import record_error
from result_panel import ResultPanel
from power import input_idle_seconds, ui_poll_ms
from hidden_input import HiddenInput, foreground_window, window_handle
from browser_selection import Clipboard, copied_selection, is_browser
from browser_drag import BrowserCopyRequest
from tray_hover import icon_bounds, cursor_position, contains

import api_client
import settings_store
from api_client import response_text, validate_text
from credentials import protect
from hotkeys import Hotkey, parse_hotkey
from settings_store import APP_DIR, CONFIG, DEFAULTS, parse_transparency

VERSION = '2.0'
MANUAL_IDLE_SECONDS = 5


def load_settings(warnings=None):
    return settings_store.load_settings(warnings, config=CONFIG)


def save_settings(data):
    settings_store.save_settings(data, config=CONFIG)


def payload(settings, jpeg=None, text=None):
    return api_client.payload(settings, jpeg, text=text, reference_loader=reference_inputs)


def ask(settings, jpeg, key, text=None, on_progress=None):
    return api_client.ask(settings, jpeg, key, text=text,
                          payload_builder=payload, error_logger=record_error, on_progress=on_progress)


def tooltip(text):
    # The custom tray bubble has no native szTip character limit.
    return readable_math(text).replace('\x00', '').strip()

def make_icon(color):
    icon = Image.new('RGB', (64, 64), '#172b4d')
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((10, 12, 54, 46), 6, outline=color, width=4)
    draw.line((24, 53, 40, 53), fill=color, width=4)
    draw.ellipse((43, 43, 61, 61), fill=color)
    return icon

def near_geometry(anchor, work, width, height):
    left, top, right, bottom = work
    width, height = min(width, right-left-16), min(height, bottom-top-16)
    x, y = anchor[0], anchor[3] + 8
    if y + height > bottom - 8:
        y = anchor[1] - height - 8
    return (max(left+8, min(x, right-width-8)),
            max(top+8, min(y, bottom-height-8)), width, height)

class HomeTrayIcon(pystray.Icon):
    """Open the settings home on a left click; preserve the right-click menu."""
    def __init__(self, *args, home_action, hover_action=None, dismiss_action=None, **kwargs):
        self.home_action = home_action
        self.hover_action = hover_action
        self.dismiss_action = dismiss_action
        self.hover_pending = False
        super().__init__(*args, **kwargs)

    def _message(self, code, flags, **kwargs):
        # Native szTip is limited to 127 UTF-16 units. Render the full title
        # ourselves, including on initial registration and Explorer restart.
        if 'szTip' in kwargs and self.hover_action:
            kwargs['szTip'] = ''
        return super()._message(code, flags, **kwargs)

    def _on_notify(self, wparam, lparam):
        if lparam == 0x200 and getattr(self, 'hover_action', None):
            if not self.hover_pending:
                self.hover_pending = True
                self.hover_action()
            return
        if lparam in (0x201, 0x202, 0x203, 0x204, 0x205):
            if getattr(self, 'dismiss_action', None):
                self.dismiss_action()
        if lparam == 0x203:  # The button-up event handles opening the home.
            return
        if lparam == 0x202:  # WM_LBUTTONUP
            self.home_action()
            return
        return super()._on_notify(wparam, lparam)


class App:
    def __init__(self, demo=False):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title('ScreenAnswer ' + VERSION)
        self.events = queue.Queue()
        recovery_warnings = []
        self.settings = load_settings(recovery_warnings)
        self.busy = False
        self.popup_error = False
        self.blinker = Blinker(self.root)
        self.dialog = None
        self.manual_mode = False
        self.manual_draft = ''
        self.manual_status = ''
        self.manual_request = False
        self.hidden_input = None
        self.manual_last_input = 0
        self.manual_idle = False
        self.manual_previous_window = None
        self.last_auto_selection = None
        self.pending_selection = None
        self.near_popup = None
        self.near_signature = None
        self.selection = SelectionWatcher(self.events, delay_ms=self.settings['selection_delay_ms'],
                                          browser_copy=self.settings.get('browser_selection_copy', True))
        self.mouse = MouseWatcher(self.events)
        self.hotkey = Hotkey(self.events)
        self.manual_hotkey = Hotkey(self.events, event_name='manual_input')
        for handler, setting, flag in [(self.hotkey, 'hotkey', 'screen_enabled'),
                                       (self.manual_hotkey, 'manual_input_hotkey', 'manual_input_enabled')]:
            try:
                handler.register(self.settings[setting] if not demo and self.settings[flag] else None)
            except ValueError as error:
                messagebox.showerror('단축키', self.settings[setting] + '\n' + str(error))
        self.tray = HomeTrayIcon('ScreenAnswer', make_icon('#72d7f3'), 'ScreenAnswer · ' + self.settings['hotkey'],
            pystray.Menu(
                pystray.MenuItem('입력 모드', lambda: self.events.put(('manual_input', None)),
                                checked=lambda _: self.manual_mode,
                                enabled=lambda _: self.settings['manual_input_enabled']),
                pystray.MenuItem('설정', lambda: self.events.put(('settings', None))),
                pystray.MenuItem('최근 원문과 답변', lambda: self.events.put(('result', None))),
                pystray.MenuItem('종료', lambda: self.events.put(('quit', None)))),
                home_action=lambda: self.events.put(('settings', None)),
                hover_action=lambda: self.events.put(('tray_hover', None)),
                dismiss_action=lambda: self.events.put(('tray_dismiss', None)))
        threading.Thread(target=self.tray.run, daemon=True).start()
        self.last = '아직 답변이 없습니다.'
        self.root.after(50, self.poll)
        if not demo:
            self.selection.set_enabled(self.settings['auto_selection'])
            self.mouse.configure(self.settings['mouse_click_enabled'] and self.settings['screen_enabled'], self.settings['mouse_click_count'], self.settings['mouse_click_interval_ms'], circle_enabled=self.settings['mouse_circle_enabled'] and self.settings['screen_enabled'])
        if demo:
            self.root.after(250, lambda: self.publish('ScreenAnswer 데모 · 화면 캡처와 API 호출 없이 실행 중입니다.', '#72d7f3'))
        elif recovery_warnings:
            self.root.after(200, lambda: self.publish(recovery_warnings[0], '#ff8484'))
        elif not self.settings['key'] and not os.environ.get('OPENAI_API_KEY'):
            self.root.after(200, lambda: self.publish('API 키가 없습니다. 트레이 메뉴의 설정에서 등록하세요.', '#ff8484'))

    def hide_near(self):
        window = getattr(self, 'near_popup', None)
        if window and window.winfo_exists():
            window.destroy()
        self.near_popup = None
        self.near_signature = None

    def display_selection_answer(self, text, final=True):
        context = getattr(self, 'pending_selection', None)
        if final:
            self.pending_selection = None
        if getattr(self, 'popup_error', False):
            return
        if not context or not self.settings['bubble_enabled'] or not self.selection.enabled.is_set() or self.settings_visible():
            return
        generation, source, anchor, selected = context
        user = C.windll.user32
        user.GetForegroundWindow.restype = W.HWND
        if generation != self.selection.generation or user.GetForegroundWindow() != source or not anchor or self.selection.current != (source, selected):
            return
        try:
            if not final and self.near_popup and self.near_popup.winfo_exists():
                self.resize_near(text, anchor)
            else:
                self.show_near(text, anchor)
            self.near_signature = (generation, (source, selected))
        except Exception as error:
            record_error('bubble', error)
            self.hide_near()  # The answer preview remains accessible in the tray.

    def show_near(self, text, anchor):
        self.hide_near()
        self.near_popup = self.create_answer_bubble(text, anchor)

    def create_answer_bubble(self, text, anchor):
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        window.attributes('-alpha', 1 - self.settings.get('answer_transparency', 0) / 100)
        window.configure(bg='#f9f9f9', highlightbackground='#e4e4e4', highlightthickness=1)
        work = monitor_workarea(anchor)
        canvas = tk.Canvas(window, bg='#f9f9f9', highlightthickness=0,
                           borderwidth=0, takefocus=False)
        canvas.pack(side='left', fill='both', expand=True)
        label = tk.Label(canvas, text=readable_math(text), bg='#f9f9f9', fg='#202020',
                         font=('Malgun Gothic', 10), wraplength=min(520, work[2]-work[0]-60),
                         justify='left', padx=10, pady=6, takefocus=False)
        canvas.create_window(0, 0, window=label, anchor='nw')
        scrollbar = ttk.Scrollbar(window, orient='vertical', command=canvas.yview,
                                  takefocus=False)
        canvas.configure(yscrollcommand=scrollbar.set)
        window.answer_widgets = (canvas, label, scrollbar)
        def scroll(event):
            canvas.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), 'units')
            return 'break'
        for widget in (label, canvas, scrollbar):
            widget.bind('<MouseWheel>', scroll)
        self.resize_near(text, anchor, window=window)
        return window

    def resize_near(self, text, anchor, window=None):
        window = self.near_popup if window is None else window
        canvas, label, scrollbar = window.answer_widgets
        work = monitor_workarea(anchor)
        label.configure(text=readable_math(text))
        window.update_idletasks()
        content_width, content_height = label.winfo_reqwidth(), label.winfo_reqheight()
        canvas.configure(width=content_width, height=min(content_height, work[3]-work[1]-18),
                         scrollregion=(0, 0, content_width, content_height))
        if content_height > work[3]-work[1]-18:
            scrollbar.pack(side='right', fill='y')
        else:
            scrollbar.pack_forget()
        window.update_idletasks()
        x, y, width, height = near_geometry(anchor, work,
            window.winfo_reqwidth(), window.winfo_reqheight())
        # Tk geometry interprets negative offsets relative to the right edge.
        # Position through Win32 instead to support monitors left of the primary.
        if not getattr(window, 'answer_shown', False):
            window.geometry(f'{width}x{height}+30000+30000')
            window.update_idletasks()
        user = C.windll.user32
        user.GetAncestor.argtypes = [W.HWND, W.UINT]
        user.GetAncestor.restype = W.HWND
        hwnd = user.GetAncestor(window.winfo_id(), 2)
        user.GetWindowLongW.argtypes = [W.HWND, C.c_int]
        user.SetWindowLongW.argtypes = [W.HWND, C.c_int, C.c_long]
        user.SetWindowLongW(hwnd, -20, user.GetWindowLongW(hwnd, -20) | 0x08000000 | 0x80)
        user.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
        # Native nonactivating show avoids Tk's explicit deiconify activation.
        user.ShowWindow.argtypes = [W.HWND, C.c_int]
        user.ShowWindow(hwnd, 4)
        user.SetWindowPos(hwnd, W.HWND(-1), x, y, width, height, 0x10 | 0x40)
        window.answer_shown = True
        # The bubble remains until the selection changes or is cleared.

    def hide_tray_answer(self):
        timer = getattr(self, 'tray_hover_timer', None)
        if timer is not None:
            self.root.after_cancel(timer)
        self.tray_hover_timer = None
        window = getattr(self, 'tray_popup', None)
        if window is not None and window.winfo_exists():
            window.destroy()
        self.tray_popup = None
        self.tray.hover_pending = False

    def begin_tray_hover(self):
        if getattr(self, 'popup_error', False):
            self.hide_tray_answer()
            return
        if getattr(self, 'tray_hover_timer', None) is not None:
            return
        self.tray_hover_started = time.monotonic()
        self.tray_hover_seen = self.tray_hover_started
        self.tray_hover_text = None
        self.tray_hover_timer = self.root.after(100, self.update_tray_hover)

    def update_tray_hover(self):
        self.tray_hover_timer = None
        if getattr(self, 'popup_error', False):
            self.hide_tray_answer()
            return
        try:
            bounds = icon_bounds(self.tray)
            point = cursor_position()
            window = getattr(self, 'tray_popup', None)
            over_answer = False
            if window is not None and window.winfo_exists():
                x, y = window.winfo_rootx(), window.winfo_rooty()
                over_answer = contains((x, y, x + window.winfo_width(),
                                        y + window.winfo_height()), point, margin=8)
            now = time.monotonic()
            if contains(bounds, point) or over_answer:
                self.tray_hover_seen = now
            elif window is None or now - self.tray_hover_seen > 0.5:
                self.hide_tray_answer()
                return
            if now - self.tray_hover_started >= 0.3 and bounds:
                text = self.tray.title
                if window is None:
                    self.tray_popup = self.create_answer_bubble(text, bounds)
                    self.tray_hover_text = text
                elif text != self.tray_hover_text:
                    self.resize_near(text, bounds, window=window)
                    self.tray_hover_text = text
            self.tray_hover_timer = self.root.after(100, self.update_tray_hover)
        except Exception as error:
            record_error('tray_bubble', error)
            self.hide_tray_answer()

    def selection_changed(self, state):
        if state[0] == self.selection.generation and state[1] is None:
            self.last_auto_selection = None
        if getattr(self, 'near_signature', None) and state != self.near_signature:
            self.hide_near()

    def set_tray_color(self, color):
        self.tray_color = color
        shown = '#ffc86b' if getattr(self, 'manual_mode', False) else '#ff8484' if getattr(self, 'manual_idle', False) else color if getattr(self, 'settings', {}).get('tray_icon_changes', True) else '#72d7f3'
        if getattr(self, 'tray_display_color', None) != shown:
            self.tray.icon = make_icon(shown)
            self.tray_display_color = shown

    def publish(self, text, color, *, error=None):
        self.last = text
        self.tray.title = tooltip(text)
        self.popup_error = color == '#ff8484' if error is None else error
        if self.popup_error:
            self.hide_near()
            self.hide_tray_answer()
        self.set_tray_color(color)

    def settings_visible(self):
        dialog = getattr(self, 'dialog', None)
        return bool(dialog and dialog.winfo_exists() and dialog.state() not in ('withdrawn', 'iconic'))

    def refresh_input_pause(self):
        paused = self.busy or self.settings_visible() or getattr(self, 'manual_mode', False)
        (self.selection.suspended.set if paused else self.selection.suspended.clear)()

    def toggle_manual_input(self):
        if not self.settings.get('manual_input_enabled', True):
            return
        self.set_manual_mode(not getattr(self, 'manual_mode', False))

    def set_manual_mode(self, enabled, timed_out=False):
        panel = getattr(self, 'result_panel', None)
        if panel is not None and panel.winfo_exists():
            panel.remember_draft()
        receiver = getattr(self, 'hidden_input', None)
        receiver_foreground = bool(receiver and receiver.active and foreground_window() == window_handle(receiver.window))
        if receiver is not None:
            receiver.suspend()
        if enabled and not getattr(self, 'manual_mode', False):
            self.manual_previous_window = foreground_window()
        self.manual_mode = enabled
        self.manual_idle = timed_out
        if enabled:
            self.manual_activity()
        self.hide_near()
        self.hide_tray_answer()
        if timed_out and getattr(self, 'dialog', None) and self.dialog.winfo_exists():
            self.dialog.withdraw()  # Preserve unsaved settings while resuming inputs.
        self.refresh_input_pause()
        self.set_tray_color(getattr(self, 'tray_color', '#72d7f3'))
        self.tray.update_menu()
        if enabled and self.settings.get('manual_input_show_panel', True):
            self.open_settings()
        panel = getattr(self, 'result_panel', None)
        if panel is not None and panel.winfo_exists():
            panel.sync()
            if enabled and self.settings_visible():
                panel.views[0].focus_force()
        if enabled and not self.settings_visible():
            self.activate_hidden_input()
        elif not enabled and (receiver_foreground or timed_out):
            previous = getattr(self, 'manual_previous_window', None)
            if previous and C.windll.user32.IsWindow(previous):
                C.windll.user32.SetForegroundWindow(previous)

    def manual_activity(self):
        if getattr(self, 'manual_mode', False):
            self.manual_last_input = time.monotonic()

    def activate_hidden_input(self):
        if not getattr(self, 'manual_mode', False) or self.settings_visible():
            return
        if getattr(self, 'hidden_input', None) is None:
            self.hidden_input = HiddenInput(self.root, self)
        self.hidden_input.activate()

    def check_manual_input(self):
        if not getattr(self, 'manual_mode', False):
            return
        if not self.busy and time.monotonic() - self.manual_last_input >= MANUAL_IDLE_SECONDS:
            self.set_manual_mode(False, timed_out=True)
            self.input_status('5초 동안 입력이 없어 입력 모드를 해제했습니다.')
            return
        receiver = getattr(self, 'hidden_input', None)
        if self.settings_visible():
            if receiver is not None and receiver.active:
                receiver.suspend()
        else:
            if receiver is None or not receiver.active:
                self.activate_hidden_input()
            else:
                receiver.ensure_focus()

    def input_status(self, text):
        self.manual_status = text
        panel = getattr(self, 'result_panel', None)
        if panel is not None and panel.winfo_exists():
            panel.sync()

    def submit_manual(self, text):
        if not getattr(self, 'manual_mode', False):
            return
        if self.busy:
            self.input_status('현재 요청이 끝난 뒤 전송해 주세요.')
            return
        try:
            if not text.strip():
                raise ValueError('질문이나 번역할 원문을 입력해 주세요.')
            if len(text.strip()) > 50000:
                raise ValueError('입력은 50,000자 이내로 작성해 주세요.')
            text = validate_text(text)
        except ValueError as error:
            self.input_status(str(error))
            return
        key = self.request_key(manual=True)
        if not key:
            self.input_status(self.last)
            return
        self.busy = True
        self.manual_request = True
        self.pending_selection = None
        self.refresh_input_pause()
        self.input_status(f"{self.settings['task_preset']} · 응답을 기다리는 중입니다.")
        self.set_tray_color('#ffc86b')
        try:
            self.submit(key, text=text)
        except Exception as error:
            record_error('manual_input', error)
            self.handle_error('요청을 시작하지 못했습니다. 다시 시도해 주세요.')

    def request_key(self, manual=False):
        if self.busy:
            return None
        if not manual and self.settings_visible():
            return None
        if not manual and getattr(self, 'manual_mode', False):
            return None
        try:
            key = os.environ.get('OPENAI_API_KEY', '').strip()
            if not key and self.settings['key']:
                key = protect(base64.b64decode(self.settings['key']), True).decode('utf-8')
            if not key:
                self.publish('API 키가 없습니다. 트레이 메뉴의 설정에서 등록하세요.', '#ff8484')
                return None
        except Exception as error:
            record_error('key', error)
            self.publish('저장된 API 키를 읽을 수 없습니다. 설정에서 키를 다시 입력하세요.', '#ff8484')
            return None
        return key

    def scan(self, source=None, point=None):
        if not self.settings.get('screen_enabled', True):
            return
        key = self.request_key()
        if not key:
            return
        try:
            bounds = capture_bounds(source, point)
        except Exception as error:
            record_error('capture', error)
            self.publish('캡처할 모니터를 확인할 수 없습니다. 다시 시도하세요.', '#ff8484')
            return
        self.busy = True
        self.manual_idle = False
        self.pending_selection = None
        self.hide_near()
        self.selection.suspended.set()
        self.tray.title = 'ScreenAnswer · 화면을 캡처하는 중…'
        self.set_tray_color('#ffc86b')
        # Let the desktop repaint after hiding our selection bubble.
        self.root.after(180, lambda: self.capture(key, bounds))

    def answer_selection(self, value):
        generation, sample = value
        _, text = sample[:2]
        copy_requested = isinstance(text, BrowserCopyRequest)
        if not self.selection.enabled.is_set() or generation != self.selection.generation or self.busy:
            return
        if self.settings_visible():
            return
        mouse = getattr(self, 'mouse', None)
        if mouse:
            remaining, serial, just_triggered = mouse.selection_pause()
            if just_triggered:
                return
            if remaining > 0:
                def resume():
                    if mouse.selection_pause()[1] == serial and self.selection.current == sample[:2]:
                        self.answer_selection(value)
                self.root.after(int(remaining * 1000) + 50, resume)
                return
        if sample[:2] == self.last_auto_selection:
            return
        try:
            if copy_requested:
                if not self.settings.get('browser_selection_copy', True) or not is_browser(sample[0]):
                    return
                text = None
            else:
                text = validate_text(text)
        except ValueError:
            return
        key = self.request_key()
        if not key:
            return
        # Recheck at dispatch time: queued or deferred events can outlive the
        # selection or foreground window that produced them.
        user = C.windll.user32
        user.GetForegroundWindow.restype = W.HWND
        if (generation != self.selection.generation or
                not self.selection.enabled.is_set() or
                self.selection.current != sample[:2] or
                user.GetForegroundWindow() != sample[0]):
            return
        self.last_auto_selection = sample[:2]
        self.pending_selection = (generation, sample[0], sample[2] if len(sample) > 2 else None, sample[1])
        self.hide_near()
        self.busy = True
        self.manual_idle = False
        self.selection.suspended.set()
        self.set_tray_color('#ffc86b')
        if copy_requested or (self.settings.get('browser_selection_copy', True) and is_browser(sample[0])):
            self.submit(key, text=text, copy_context=(generation, sample))
        else:
            self.submit(key, text=text)

    def toggle_selection(self):
        self.toggle_feature('auto_selection')

    def apply_settings(self, updated):
        updated = validate_feedback(updated.copy())
        updated = {**updated, 'selection_delay_ms': parse_delay_ms(updated['selection_delay_ms']),
                   'answer_transparency': parse_transparency(updated['answer_transparency']),
                   'mouse_click_count': click_number(updated['mouse_click_count']),
                   'mouse_click_interval_ms': click_interval(updated['mouse_click_interval_ms'])}
        previous = self.settings.copy()
        parse_hotkey(updated['hotkey'])
        manual_combo = parse_hotkey(updated.get('manual_input_hotkey', DEFAULTS['manual_input_hotkey']))
        if (updated['screen_enabled'] and updated.get('manual_input_enabled', True)
                and parse_hotkey(updated['hotkey']) == manual_combo):
            raise ValueError('화면 캡처와 질문 입력에는 서로 다른 단축키를 지정해 주세요.')
        handlers = [(self.hotkey, 'hotkey', 'screen_enabled')]
        if getattr(self, 'manual_hotkey', None) is not None:
            handlers.append((self.manual_hotkey, 'manual_input_hotkey', 'manual_input_enabled'))
        try:
            # Release both first so exchanging the two hotkeys is supported.
            for handler, _, _ in handlers:
                handler.register(None)
            for handler, setting, flag in handlers:
                handler.register(updated[setting] if updated[flag] else None)
            save_settings(updated)
        except Exception:
            for handler, _, _ in handlers:
                handler.register(None)
            for handler, setting, flag in handlers:
                handler.register(previous[setting] if previous[flag] else None)
            raise
        self.settings = updated
        if not updated.get('manual_input_enabled', True) and getattr(self, 'manual_mode', False):
            self.set_manual_mode(False)
        self.set_tray_color(getattr(self, 'tray_color', '#72d7f3'))
        if getattr(self, 'mouse', None):
            self.mouse.configure(updated['mouse_click_enabled'] and updated['screen_enabled'], updated['mouse_click_count'], updated['mouse_click_interval_ms'], circle_enabled=updated['mouse_circle_enabled'] and updated['screen_enabled'])
        window = getattr(self, 'near_popup', None)
        if window and window.winfo_exists():
            window.attributes('-alpha', 1 - updated['answer_transparency'] / 100)
        self.selection.set_delay(updated['selection_delay_ms'])
        self.selection.set_browser_copy(updated.get('browser_selection_copy', True))
        if self.selection.enabled.is_set() != updated['auto_selection']:
            self.selection.set_enabled(updated['auto_selection'])
        if not updated['auto_selection'] or not updated['bubble_enabled']:
            self.hide_near()
        self.tray.update_menu()

    def toggle_feature(self, name):
        updated = self.settings.copy()
        updated[name] = not (self.selection.enabled.is_set() if name == 'auto_selection' else updated[name])
        try:
            self.apply_settings(updated)
        except Exception as error:
            self.publish('기능 설정을 적용하지 못했습니다: ' + str(error), '#ff8484')

    def mouse_scan(self, value):
        generation, source = value[:2]
        feature = 'mouse_circle_enabled' if len(value) > 2 and value[2] == 'circle' else 'mouse_click_enabled'
        if not self.settings[feature] or not self.settings['screen_enabled'] or generation != self.mouse.generation:
            return
        C.windll.user32.GetForegroundWindow.restype = W.HWND
        if feature != 'mouse_circle_enabled' and C.windll.user32.GetForegroundWindow() != source:
            return
        point = value[3] if len(value) > 3 else None
        self.scan(source=source, point=point)

    def capture(self, key, bounds=None):
        if not self.settings.get('screen_enabled', True) or getattr(self, 'manual_mode', False):
            self.busy = False
            self.refresh_input_pause()
            return
        try:
            with ExitStack() as resources:
                captured = ImageGrab.grab(bbox=bounds if bounds is not None else capture_bounds(), all_screens=True)
                resources.callback(captured.close)
                captured.thumbnail((2560, 2560))
                with io.BytesIO() as buffer:
                    if captured.mode == 'RGB':
                        captured.save(buffer, format='JPEG', quality=90)
                    else:
                        screen = captured.convert('RGB')
                        resources.callback(screen.close)
                        screen.save(buffer, format='JPEG', quality=90)
                    jpeg = buffer.getvalue()
        except Exception as error:
            record_error('capture', error)
            self.busy = False
            self.refresh_input_pause()
            self.publish('화면을 캡처할 수 없습니다. 일반 Windows 데스크톱에서 다시 시도하세요.', '#ff8484')
            return
        self.submit(key, jpeg=jpeg)

    def submit(self, key, jpeg=None, text=None, copy_context=None):
        self.request_source = validate_text(text) if text is not None else None
        text = self.request_source
        if copy_context:
            self.request_source = None
        clipboard_hwnd = self.root.winfo_id() if copy_context else None
        self.popup_error = False
        self.result_text = 'AI 응답을 기다리는 중입니다.'
        self.tray.title = 'ScreenAnswer ' + VERSION + ' · ' + ('선택한 텍스트' if text is not None or copy_context else '화면') + '의 AI 응답을 기다리는 중…'
        settings = self.settings.copy()
        inputs = [jpeg]
        progress = self.translation_progress = queue.Queue(maxsize=1)
        def update(answer):
            try:
                progress.get_nowait()
            except queue.Empty:
                pass
            progress.put_nowait(answer)
        def worker():
            try:
                request_text = text
                if copy_context:
                    generation, sample = copy_context
                    def valid():
                        return (self.selection.enabled.is_set() and generation == self.selection.generation and
                                self.selection.current == sample[:2])
                    request_text = validate_text(copied_selection(sample[0], Clipboard(clipboard_hwnd), valid))
                    self.request_source = request_text
                if request_text is not None and settings.get('task_preset') == '번역' and settings.get('bubble_enabled'):
                    result = ask(settings, inputs.pop(), key, text=request_text, on_progress=update)
                else:
                    result = ask(settings, inputs.pop(), key, text=request_text)
                self.events.put(('answer', result))
            except Exception as error:
                record_error('request', error)
                self.events.put(('error', str(error) if isinstance(error, ValueError) else '응답 처리에 실패했습니다. 다시 시도하세요.'))
        threading.Thread(target=worker, daemon=True).start()

    def open_settings(self):
        self.manual_activity()
        receiver = getattr(self, 'hidden_input', None)
        if receiver is not None:
            receiver.suspend()
        if self.dialog and self.dialog.winfo_exists():
            self.dialog.deiconify()
            self.dialog.lift()
            self.refresh_input_pause()
            if getattr(self, 'manual_mode', False):
                self.result_panel.sync()
                self.result_panel.views[0].focus_force()
                self.manual_activity()
            return
        dialog = self.dialog = tk.Toplevel(self.root)
        dialog.bind('<KeyPress>', lambda _: self.manual_activity(), add='+')
        self.selection.suspended.set()
        dialog.withdraw()
        dialog.title('ScreenAnswer ' + VERSION + ' 설정')
        dialog.resizable(True, True)
        dialog.minsize(520, 420)
        def dismissed(event):
            if event.widget == dialog:
                self.dialog = None
                self.refresh_input_pause()
                if getattr(self, 'manual_mode', False):
                    self.root.after_idle(self.activate_hidden_input)
        dialog.bind('<Destroy>', dismissed, add='+')
        def revealed(event):
            if event.widget == dialog and getattr(self, 'manual_mode', False):
                receiver = getattr(self, 'hidden_input', None)
                if receiver is not None:
                    receiver.suspend()
                self.manual_activity()
                self.result_panel.views[0].focus_force()
        dialog.bind('<Map>', revealed, add='+')
        style = ttk.Style(dialog)
        style.configure('Settings.Title.TLabel', font=('Malgun Gothic', 13, 'bold'))
        style.configure('Settings.Hint.TLabel', foreground='#687280')
        style.configure('Settings.TNotebook.Tab', padding=(10, 8))
        footer = ttk.Frame(dialog, padding=(24, 12, 24, 16))
        footer.pack(side='bottom', fill='x')
        ttk.Separator(dialog).pack(side='bottom', fill='x')
        header = ttk.Frame(dialog, padding=(24, 16, 24, 12))
        header.pack(fill='x')
        ttk.Label(header, text='ScreenAnswer ' + VERSION, style='Settings.Title.TLabel').pack(anchor='w')
        ttk.Label(header, text='설정할 기능을 아래 탭에서 선택하세요.', style='Settings.Hint.TLabel').pack(anchor='w', pady=(4, 0))
        frame = ttk.Frame(dialog, padding=(24, 0, 24, 12))
        frame.pack(fill='both', expand=True)
        panes = ttk.Panedwindow(frame, orient='horizontal')
        panes.pack(fill='both', expand=True)
        settings_body = ttk.Frame(panes)
        tabs = ttk.Notebook(settings_body, style='Settings.TNotebook')
        tabs.pack(fill='both', expand=True)
        panes.add(settings_body, weight=3)
        self.result_panel = ResultPanel(panes, self)
        panes.add(self.result_panel, weight=2)
        positioned = [False]
        def position_panes(event):
            if not positioned[0] and event.width > 100:
                positioned[0] = True
                panes.sashpos(0, int(event.width * .58))
        panes.bind('<Configure>', position_panes)
        # Each page scrolls on small screens / high DPI, keeping Save visible.
        def page(title):
            shell = ttk.Frame(tabs)
            tabs.add(shell, text=title)
            canvas = tk.Canvas(shell, highlightthickness=0, borderwidth=0,
                               bg=style.lookup('TFrame', 'background') or '#f0f0f0')
            bar = ttk.Scrollbar(shell, orient='vertical', command=canvas.yview)
            canvas.configure(yscrollcommand=bar.set)
            bar.pack(side='right', fill='y')
            canvas.pack(side='left', fill='both', expand=True)
            body = ttk.Frame(canvas, padding=16)
            item = canvas.create_window(0, 0, window=body, anchor='nw')
            body.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
            canvas.bind('<Configure>', lambda e: canvas.itemconfigure(item, width=e.width))
            def wheel(e):
                if canvas.winfo_ismapped() and body.winfo_reqheight() > canvas.winfo_height():
                    canvas.yview_scroll(-int(e.delta / 120), 'units')
            dialog.bind('<MouseWheel>', wheel, add='+')
            return body
        manual_page, features, selection_page, feedback, api, reference_page = (
            page('직접 입력'), page('화면 캡처'), page('텍스트 선택'), page('결과 알림'), page('AI'), page('참고 자료'))
        entries, fields, flags, checks = [], {}, {}, {}
        def section(parent, title):
            box = ttk.LabelFrame(parent, text=title, padding=(12, 8))
            box.pack(fill='x', pady=(0, 12))
            box.columnconfigure(1, weight=1)
            return box
        def hint(parent, text):
            ttk.Label(parent, text=text, style='Settings.Hint.TLabel', wraplength=420).grid(
                column=0, columnspan=2, sticky='w', pady=(4, 6))
        def subheading(parent, title):
            ttk.Separator(parent).grid(column=0,columnspan=2,sticky='ew',pady=(12,8))
            ttk.Label(parent,text=title,font=('Malgun Gothic',10,'bold')).grid(column=0,columnspan=2,sticky='w',pady=(0,4))
        def check(parent, name, label, command=None):
            variable = tk.BooleanVar(value=self.selection.enabled.is_set() if name == 'auto_selection' else self.settings[name])
            flags[name] = variable
            checks[name] = ttk.Checkbutton(parent, text=label, variable=variable, command=command)
            checks[name].grid(column=0, columnspan=2, sticky='w', pady=5)
            return variable
        def entry(parent, name, label, masked=False):
            row = parent.grid_size()[1]
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 16), pady=6)
            fields[name] = tk.StringVar(value='' if masked else self.settings[name])
            widget = ttk.Entry(parent, textvariable=fields[name], show='●' if masked else '')
            widget.grid(row=row, column=1, sticky='ew', pady=6)
            entries.append(widget)
            return widget
        def spin(parent, name, label, low, high, step=1):
            row = parent.grid_size()[1]
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', padx=(0, 16), pady=6)
            variable = tk.StringVar(value=str(self.settings[name]))
            widget = ttk.Spinbox(parent, from_=low, to=high, increment=step, width=9, textvariable=variable)
            widget.grid(row=row, column=1, sticky='e', pady=6)
            entries.append(widget)
            return variable, widget
        def choice(parent, name, label, values):
            row = parent.grid_size()[1]
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', pady=6)
            fields[name] = tk.StringVar(value=self.settings[name])
            widget = ttk.Combobox(parent, textvariable=fields[name], values=list(values), state='readonly', width=20)
            widget.grid(row=row, column=1, sticky='ew', pady=6)
            return widget
        manual = section(manual_page, '입력 모드')
        check(manual, 'manual_input_enabled', '입력 모드 사용')
        entry(manual, 'manual_input_hotkey', '모드 전환 단축키')
        check(manual, 'manual_input_show_panel', '입력 모드로 전환할 때 기존 창 열기')
        hint(manual, '입력 모드에서는 창이 안 보여도 ScreenAnswer가 키보드 입력을 받습니다.\n오른쪽 전송 원문 칸에서도 작성 내용을 확인·편집합니다.\nCtrl+Enter로 전송하고 Enter로 줄을 바꿉니다.\n주황색은 입력 중이며, 5초 동안 입력이 없으면 빨간색으로 바뀌고 자동 선택·캡처로 돌아갑니다.\n작성 내용은 앱 종료 전까지 유지됩니다.\n같은 단축키를 다시 눌러 즉시 입력 모드를 해제할 수도 있습니다.')
        capture = section(features, '화면 캡처')
        check(capture, 'screen_enabled', '화면 캡처 사용 · 단축키와 마우스 동작')
        entry(capture, 'hotkey', '화면 단축키')
        hint(capture, '단축키를 누르면 현재 화면을 캡처해서 AI에게 질문합니다.')
        selection = section(selection_page, '텍스트 선택')
        check(selection, 'auto_selection', '선택한 문제 자동 전송')
        delay, delay_entry = spin(selection, 'selection_delay_ms', '버튼을 뗀 뒤 대기 (ms)', 0, 30000, 100)
        hint(selection, '1,000ms = 1초 · 선택을 유지하면 전송합니다.')
        check(selection, 'browser_selection_copy', '브라우저·PDF 선택을 복사 경로로 정확히 읽기')
        hint(selection, 'Edge·Chrome에서 선택한 내용만 잠시 복사해 읽고 기존 클립보드를 복원합니다.')
        selection_display = selection
        subheading(selection, '말풍선 표시')
        bubble = check(selection_display, 'bubble_enabled', '선택한 곳 근처에 말풍선 표시')
        transparency, transparency_entry = spin(selection_display, 'answer_transparency', '말풍선 투명도 (%)', 0, 90, 5)
        hint(selection_display, '선택 자동 답변에 적용됩니다.\n선택을 해제하거나 바꾸면 말풍선이 사라집니다.')
        display = section(feedback, '키보드 LED 알림 · Caps Lock')
        check(display, 'led_enabled', '키보드 LED로 알림 받기')
        hint(display, '완료와 요청 실패를 알립니다. 깜빡임 후 Caps Lock 상태를 복원합니다.')
        led_values = {}
        for name, label in [('led_done_count','기본 알림'), ('led_error_count','요청 실패')]:
            led_values[name], _ = spin(display, name, label + ' · 깜빡임 횟수', 1, 9)
        led_advanced = check(display, 'advanced_feedback', '고급 설정 · 깜빡임 속도')
        led_details = ttk.Frame(display)
        led_details.grid(column=0, columnspan=2, sticky='ew')
        led_details.columnconfigure(1, weight=1)
        led_values['led_interval_ms'], _ = spin(led_details, 'led_interval_ms', '깜빡임 간격 (ms)', 100, 1000, 50)
        def test_led():
            try:
                candidate = validate_feedback({**self.settings, **{name:value.get() for name,value in led_values.items()}})
                if self.busy:
                    return
                self.blinker.interval_ms = candidate['led_interval_ms']
                self.blinker.start(candidate['led_done_count'], lambda error: messagebox.showerror('LED 테스트', error, parent=dialog) if error else None)
            except Exception as error:
                messagebox.showerror('LED 테스트', str(error), parent=dialog)
        ttk.Button(display, text='완료 알림 테스트', command=test_led).grid(column=0, columnspan=2, sticky='w', pady=6)
        hint(display, 'LED 횟수는 AI 작업 프롬프트에서 지시하세요.\n예: 참이면 LED 1번, 거짓이면 2번, 판별 불가면 3번.\n지시가 없으면 작업의 기본 알림을 사용합니다.')
        tray_display = section(feedback, '답변 표시')
        check(tray_display, 'tray_icon_changes', '처리 상태에 따라 아이콘 색상 변경')
        hint(tray_display, '끄면 기본 색상으로 고정됩니다.')
        result_box = tray_display
        subheading(result_box, '최근 원문과 답변')
        ttk.Button(result_box, text='전송한 원문과 답변 확인', command=self.open_result).grid(column=0, columnspan=2, sticky='w', pady=6)
        circle_box = capture
        subheading(capture, '원 그리기로 캡처')
        circle_enabled = check(circle_box, 'mouse_circle_enabled', '커서로 원 두 바퀴 → 화면 전송')
        circle_status = ttk.Label(circle_box,style='Settings.Hint.TLabel',wraplength=420)
        circle_status.grid(column=0,columnspan=2,sticky='w',pady=4)
        hint(circle_box, '버튼 없이 같은 창에서 8초 안에 두 바퀴.\n지름 35픽셀 이상 · 양쪽 회전 방향 지원')
        click_box = capture
        subheading(capture, '연속 클릭으로 캡처')
        mouse_enabled = check(click_box, 'mouse_click_enabled', '연속 클릭 → 화면 전송')
        mouse_count, mouse_count_entry = spin(click_box, 'mouse_click_count', '왼쪽 버튼 클릭 횟수', 3, 20)
        mouse_interval, mouse_interval_entry = spin(click_box, 'mouse_click_interval_ms', '클릭 사이 최대 간격 (ms)', 100, 2000, 100)
        hint(click_box, '같은 위치에서 클릭하세요. 드래그는 제외합니다.')
        hint(capture, '원 그리기와 연속 클릭은 화면 캡처 기능이 켜져 있을 때 동작합니다.')
        connection = section(api, 'API 연결')
        entry(connection, 'key', 'OpenAI API 키', masked=True)
        hint(connection, '비워 두면 기존 키 유지 · 입력한 키는 암호화해 저장')
        entry(connection, 'model', '일반 작업 모델')
        request = section(api, 'AI 작업 지시')
        task_choice = choice(request, 'task_preset', '작업 종류', PRESETS)
        task_mode_hint = ttk.Label(request,style='Settings.Hint.TLabel',wraplength=420)
        task_mode_hint.grid(column=0,columnspan=2,sticky='w',pady=6)
        translation = section(api, '번역 모드')
        check(translation, 'translation_fast', '번역 시 별도 모델 사용')
        translation_model = entry(translation, 'translation_model', '번역 모델')
        translation_language_choice = choice(translation, 'translation_language', '번역할 언어', ('한국어','영어','일본어','중국어'))
        check(translation, 'translation_references', '번역에도 참고 자료 사용')
        hint(translation, '작업 종류가 번역일 때 적용합니다. 기본 모델: gpt-4.1-mini.\n문제 풀이 대신 원문을 번역하며 번역문만 출력합니다.\n기본은 참고 자료 검색·첨부를 생략합니다.')
        ttk.Label(request, text='추가 요청 · 언어, 답변 길이 등').grid(column=0, columnspan=2, sticky='w', pady=6)
        extra_request = tk.Text(request, height=3, wrap='word', font=('Malgun Gothic', 10))
        extra_request.insert('1.0', self.settings['extra_request'])
        extra_request.grid(column=0, columnspan=2, sticky='ew')
        advanced_prompt = check(request, 'advanced_prompt', '이 작업의 프롬프트 직접 수정')
        prompt_frame = ttk.Frame(request)
        prompt_frame.grid(column=0, columnspan=2, sticky='ew')
        prompt = tk.Text(prompt_frame, height=6, wrap='word', font=('Malgun Gothic', 10),
                         relief='solid', borderwidth=1, padx=8, pady=6, undo=False)
        prompt.insert('1.0', self.settings['prompt'])
        prompt.pack(fill='x')
        hint(request, 'LED 지시 예: 정답이면 1번, 오답이면 2번 깜빡여.\n작업마다 프롬프트와 추가 요청을 따로 저장합니다.\n직접 수정을 켜면 작성한 프롬프트를 사용합니다.\n저장을 눌러야 변경 사항이 다음 실행에도 유지됩니다.')
        task_profiles = migrate_task_profiles(self.settings.copy())['task_profiles']
        editing_task = [fields['task_preset'].get()]
        def remember_task():
            profile = task_profiles[editing_task[0]]
            if advanced_prompt.get():
                profile['prompt'] = prompt.get('1.0','end-1c').strip()
            profile.update(advanced=advanced_prompt.get(),extra=extra_request.get('1.0','end-1c').strip())
        def load_task_editor():
            name = fields['task_preset'].get()
            editing_task[0] = name
            profile = task_profiles[name]
            advanced_prompt.set(profile.get('advanced',False))
            extra_request.delete('1.0','end')
            extra_request.insert('1.0',profile.get('extra',''))
            prompt.configure(state='normal')
            prompt.delete('1.0','end')
            prompt.insert('1.0',profile['prompt'] if profile.get('advanced',False) else default_task_prompt({**self.settings,'translation_language':fields['translation_language'].get()},name))
            prompt.configure(state='normal' if advanced_prompt.get() else 'disabled')
        load_task_editor()
        def reset_task_prompt():
            name = editing_task[0]
            task_profiles[name] = dict(prompt=default_task_prompt({**self.settings,'translation_language':fields['translation_language'].get()},name),advanced=False,extra='')
            load_task_editor()
            refresh_controls()
        ttk.Button(request,text='이 작업만 기본값으로',command=reset_task_prompt).grid(column=0,columnspan=2,sticky='w',pady=6)
        store_box = section(reference_page, '참고 자료')
        subheading(store_box, '업로드한 자료 검색')
        store_enabled = check(store_box, 'file_search_enabled', '업로드된 저장소를 검색해서 답변')
        entry(store_box, 'vector_store_id', '저장소 ID (vs_…)')
        hint(store_box, '한 번 업로드한 자료에서 질문과 관련된 부분을 검색합니다.\nAPI 키와 같은 프로젝트의 저장소 ID를 입력하세요.\n켜면 아래 로컬 파일 첨부 대신 저장소를 사용합니다.\n저장소 보관·검색에는 API 요금이 적용될 수 있습니다.')
        reference_box = store_box
        subheading(reference_box, '컴퓨터의 파일 첨부')
        reference_enabled = check(reference_box, 'reference_files_enabled', '질문할 때 참고 파일 함께 전송')
        hint(reference_box, '선택한 파일의 내용이 질문할 때마다 OpenAI API로 전송됩니다.\n파일 선택·설정 저장만으로는 전송하지 않습니다.\n자료가 길면 비용과 응답 시간이 늘어날 수 있습니다.')
        reference_paths = list(self.settings.get('reference_files', ()))
        reference_list = tk.Listbox(reference_box, height=7, selectmode='extended', exportselection=False)
        reference_list.grid(column=0, columnspan=2, sticky='ew')
        reference_scroll = ttk.Scrollbar(reference_box, orient='horizontal', command=reference_list.xview)
        reference_scroll.grid(column=0, columnspan=2, sticky='ew')
        reference_list.configure(xscrollcommand=reference_scroll.set)
        def refresh_references():
            reference_list.delete(0, 'end')
            for name in reference_paths:
                reference_list.insert('end', name)
        def add_references():
            selected = filedialog.askopenfilenames(parent=dialog, title='배경지식 참고 파일 선택',
                filetypes=[('참고 문서', '*.pdf *.txt *.md *.docx *.pptx'), ('PDF', '*.pdf'),
                           ('텍스트', '*.txt *.md'), ('Word', '*.docx'), ('PowerPoint', '*.pptx')])
            if not selected:
                return
            try:
                updated_paths = validate_reference_paths(reference_paths + list(selected))
            except ValueError as error:
                messagebox.showerror('참고 파일 추가 실패', str(error), parent=dialog)
                return
            reference_paths[:] = updated_paths
            reference_enabled.set(True)
            refresh_references()
        def remove_references():
            for index in reversed(reference_list.curselection()):
                del reference_paths[index]
            refresh_references()
        reference_buttons = ttk.Frame(reference_box)
        reference_buttons.grid(column=0, columnspan=2, sticky='w', pady=(8, 0))
        ttk.Button(reference_buttons, text='파일 추가…', command=add_references).pack(side='left')
        ttk.Button(reference_buttons, text='선택 항목 제거', command=remove_references).pack(side='left', padx=8)
        hint(reference_box, 'PDF · TXT · MD · DOCX · PPTX 지원\n파일당 50MB 미만, 전체 50MB 이하.\nDOCX·PPTX의 삽입 그림은 읽지 못할 수 있으므로 그림이 중요하면 PDF로 변환하세요.\n원본 파일을 옮기거나 삭제하면 다시 선택해야 합니다.')
        refresh_references()
        privacy_hint = ttk.Label(api, text='화면과 질문·답변 기록은 파일로 저장하지 않습니다.',
                  style='Settings.Hint.TLabel', wraplength=420)
        privacy_hint.pack(anchor='w')
        # Keep dependent controls clear without adding more explanatory text.
        def refresh_controls():
            task_mode_hint.configure(text='전용 모드: AI는 답만 반환합니다. LED는 참 1번·거짓 2번·판별 불가 3번 또는 객관식 정답 번호로 연결합니다.' if fields['task_preset'].get() in ('참·거짓','객관식') else '일반 모드: 프롬프트에서 LED 횟수를 지시할 수 있습니다.')
            circle_status.configure(text='화면 캡처가 꺼져 있습니다. 위의 화면 캡처 사용을 켜세요.' if not flags['screen_enabled'].get() else '마우스 버튼을 누르지 않고 같은 창 위에서 원을 두 바퀴 그리세요.')
            checks['mouse_circle_enabled'].configure(state='normal' if flags['screen_enabled'].get() else 'disabled')
            checks['mouse_click_enabled'].configure(state='normal' if flags['screen_enabled'].get() else 'disabled')
            if fields['task_preset'].get() == '번역':
                translation.pack(fill='x', pady=(0,12), before=privacy_hint)
            else:
                translation.pack_forget()
            prompt.configure(state='normal' if advanced_prompt.get() else 'disabled')
            translation_model.configure(state='normal' if flags['translation_fast'].get() else 'disabled')
            (led_details.grid if led_advanced.get() else led_details.grid_remove)()
            prompt_frame.grid()
            transparency_entry.configure(state='normal' if bubble.get() else 'disabled')
            delay_entry.configure(state='normal' if flags['auto_selection'].get() else 'disabled')
            active = mouse_enabled.get() and flags['screen_enabled'].get()
            for widget in (mouse_count_entry, mouse_interval_entry):
                widget.configure(state='normal' if active else 'disabled')
        for name in ('auto_selection', 'bubble_enabled', 'mouse_click_enabled', 'screen_enabled', 'advanced_feedback', 'advanced_prompt','translation_fast'):
            checks[name].configure(command=refresh_controls)
        def prompt_editing_changed():
            profile = task_profiles[editing_task[0]]
            if not advanced_prompt.get():
                profile['prompt'] = prompt.get('1.0','end-1c').strip()
            profile.update(advanced=advanced_prompt.get(),extra=extra_request.get('1.0','end-1c').strip())
            load_task_editor()
            refresh_controls()
        checks['advanced_prompt'].configure(command=prompt_editing_changed)
        def task_changed(_=None):
            remember_task()
            load_task_editor()
            refresh_controls()
        task_choice.bind('<<ComboboxSelected>>', task_changed)
        def translation_language_changed(_=None):
            if fields['task_preset'].get()=='번역' and not advanced_prompt.get():
                remember_task()
                load_task_editor()
        translation_language_choice.bind('<<ComboboxSelected>>',translation_language_changed)
        refresh_controls()
        def save():
            remember_task()
            updated = self.settings.copy()
            updated['task_profiles'] = {name:dict(profile) for name,profile in task_profiles.items()}
            updated.update({name: variable.get() for name, variable in flags.items()})
            updated.update(hotkey=fields['hotkey'].get().strip(), model=fields['model'].get().strip(), prompt=prompt.get('1.0', 'end-1c').strip())
            updated['manual_input_hotkey'] = fields['manual_input_hotkey'].get().strip()
            try:
                updated.update(task_preset=fields['task_preset'].get(), extra_request=extra_request.get('1.0', 'end-1c').strip(), translation_model=fields['translation_model'].get().strip(),translation_language=fields['translation_language'].get())
                updated.update({name: value.get() for name, value in led_values.items()})
                updated['answer_transparency'] = parse_transparency(transparency.get())
                updated['vector_store_id'] = fields['vector_store_id'].get().strip()
                if store_enabled.get():
                    updated['vector_store_id'] = validate_vector_store_id(updated['vector_store_id'])
                updated['reference_files'] = validate_reference_paths(reference_paths) if reference_enabled.get() and not store_enabled.get() else list(reference_paths)
                updated.update(mouse_circle_enabled=circle_enabled.get(), mouse_click_enabled=mouse_enabled.get(), mouse_click_count=click_number(mouse_count.get()), mouse_click_interval_ms=click_interval(mouse_interval.get()))
                updated['selection_delay_ms'] = parse_delay_ms(delay.get())
                if not updated['model'] or not task_prompt(updated).strip():
                    raise ValueError('모델과 요청을 입력하세요.')
                if fields['key'].get().strip():
                    updated['key'] = base64.b64encode(protect(fields['key'].get().strip().encode('utf-8'))).decode('ascii')
                self.apply_settings(updated)
                self.tray.title = 'ScreenAnswer · 설정 저장됨'
                dialog.destroy()
            except Exception as error:
                messagebox.showerror('설정 저장 실패', str(error), parent=dialog)
        ttk.Button(footer, text='저장 (Enter)', command=save).pack(side='right')
        ttk.Button(footer, text='취소', command=dialog.destroy).pack(side='right', padx=8)
        def save_on_enter(event):
            save()
            return 'break'
        for entry in entries:
            entry.bind('<Return>', save_on_enter)
            entry.bind('<KP_Enter>', save_on_enter)
        dialog.bind('<Escape>', lambda _: dialog.destroy())
        dialog.update_idletasks()
        rect = W.RECT()
        C.windll.user32.SystemParametersInfoW(0x30, 0, C.byref(rect), 0)
        width = min(max(1040, int(630 * float(dialog.tk.call('tk', 'scaling'))), dialog.winfo_reqwidth()), rect.right - rect.left - 24)
        height = min(max(720, int(440 * float(dialog.tk.call('tk', 'scaling')))), rect.bottom - rect.top - 64)
        dialog.geometry(f'{width}x{height}+{rect.left+(rect.right-rect.left-width)//2}+{rect.top+(rect.bottom-rect.top-height)//2}')
        dialog.deiconify()
        if getattr(self, 'manual_mode', False):
            self.result_panel.views[0].focus_force()

    def handle_answer(self, value):
        if isinstance(value,dict):
            try:
                result=validate_result(value)
            except ValueError as error:
                self.handle_error(str(error))
                return
            self.result_text=result['answer']
            self.finish_binary(result['led_count'],self.result_text)
            return
        self.result_text = str(value).strip() or 'AI가 빈 응답을 반환했습니다.'
        if 'task_preset' in getattr(self, 'settings', {}):
            count, label = task_feedback(self.settings, self.result_text)
            self.finish_binary(count, label)
            return
        count = blink_count(value)
        label = {1: 'True · 1번', 2: 'False · 2번', 3: '판별 불가 · 3번'}[count]
        if count == 3:
            if self.result_text.upper() == 'UNKNOWN':
                self.result_text += '\nAI가 이유나 답 없이 UNKNOWN만 반환했습니다. AI 요청에 설명 형식을 지정해 주세요.'
            label += '\n' + self.result_text
        self.finish_binary(count, label)

    def handle_error(self, value):
        self.result_text = '요청 오류\n' + str(value)
        self.finish_binary(getattr(self, 'settings', {}).get('led_error_count', 3), self.result_text, error=True)

    def open_result(self):
        self.open_settings()

    def finish_binary(self, count, label, *, error=False):
        if getattr(self, 'manual_request', False):
            self.manual_request = False
            self.manual_activity()
            self.input_status('요청 실패' if error else '답변 완료')
        self.popup_error = error
        self.hide_near()
        if error:
            self.pending_selection = None
        else:
            self.display_selection_answer(label)
        self.busy = True
        self.selection.suspended.set()
        color = '#ff8484' if error or label.startswith('판별 불가') else '#72d7f3'
        if not error and color == '#ff8484':
            self.publish(label, color, error=False)  # Uncertainty is a valid AI answer.
        else:
            self.publish(label, color)
        def finished(error):
            self.busy = False
            self.refresh_input_pause()
            if error:
                self.publish('LED 알림 실패: ' + error, '#ff8484')
        try:
            settings = getattr(self, 'settings', {})
            self.blinker.interval_ms = settings.get('led_interval_ms', 200)
            if not settings.get('led_enabled', True):
                finished(None)
                return
            self.blinker.start(count, finished)
        except Exception as error:
            finished(str(error))

    def poll(self):
        stopping = False
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == 'quit':
                    self.close()
                    stopping = True
                    return
                if event == 'scan': self.scan()
                elif event == 'manual_input': self.toggle_manual_input()
                elif event == 'mouse_scan': self.mouse_scan(value)
                elif event == 'toggle_circle': self.toggle_feature('mouse_circle_enabled')
                elif event == 'toggle_mouse': self.toggle_feature('mouse_click_enabled')
                elif event == 'mouse_error': self.publish('연속 클릭 감지를 시작하지 못했습니다. 화면 단축키를 사용하세요.', '#ff8484')
                elif event == 'selection': self.answer_selection(value)
                elif event == 'selection_state': self.selection_changed(value)
                elif event == 'toggle_selection': self.toggle_selection()
                elif event == 'toggle_screen': self.toggle_feature('screen_enabled')
                elif event == 'toggle_tray_icon': self.toggle_feature('tray_icon_changes')
                elif event == 'toggle_bubble': self.toggle_feature('bubble_enabled')
                elif event == 'selection_error':
                    self.tray.update_menu()
                    self.publish('자동 선택 읽기를 시작하지 못했습니다. 화면 캡처 단축키를 사용하세요.', '#ff8484')
                elif event == 'settings': self.open_settings()
                elif event == 'tray_hover': self.begin_tray_hover()
                elif event == 'tray_dismiss': self.hide_tray_answer()
                elif event == 'result': self.open_result()
                elif event == 'led_test':
                    if not self.busy:
                        self.manual_idle = False
                        self.finish_binary(value, 'LED 테스트 · ' + str(value) + '번 (AI 호출 없음)')
                elif event == 'answer':
                    self.translation_progress = None
                    self.handle_answer(value)
                elif event == 'error':
                    self.translation_progress = None
                    self.handle_error(value)
        except queue.Empty:
            progress = getattr(self, 'translation_progress', None)
            if progress is not None:
                try:
                    answer = progress.get_nowait()
                except queue.Empty:
                    pass
                else:
                    try:
                        if getattr(self, 'manual_request', False):
                            self.result_text = answer + '\n\n번역 중…'
                        self.display_selection_answer(answer + '\n\n번역 중…', final=False)
                    except Exception as error:
                        record_error('bubble', error)
        except Exception as error:
            record_error('event', error)
            # A failed tray update or dialog must not stop all input handling.
            # Do not expose exception text that could contain request data.
            if event in ('scan', 'selection', 'answer', 'error', 'led_test'):
                self.busy = bool(self.blinker.active)
                self.refresh_input_pause()
            try:
                self.publish('이벤트 처리에 실패했습니다. 다시 시도하세요.', '#ff8484')
            except Exception:
                pass
        finally:
            if not stopping:
                try:
                    self.check_manual_input()
                except Exception as error:
                    record_error('manual_input', error)
                self.root.after(self.poll_delay_ms(), self.poll)

    def poll_delay_ms(self):
        return ui_poll_ms(input_idle_seconds(), self.busy, getattr(self, 'manual_mode', False))

    def close(self):
        self.manual_mode = False
        if getattr(self, 'hidden_input', None) is not None:
            self.hidden_input.close()
        self.hide_tray_answer()
        self.blinker.close()
        self.mouse.close()
        self.selection.close()
        self.hotkey.commands.put(('stop',))
        self.hotkey.thread.join(timeout=1)
        self.manual_hotkey.commands.put(('stop',))
        self.manual_hotkey.thread.join(timeout=1)
        self.tray.stop()
        self.root.destroy()

def main():
    if sys.platform != 'win32':
        raise SystemExit('Windows 10/11에서 실행하세요.')
    if '--mouse-runtime-test' in sys.argv:
        watcher = MouseWatcher(queue.Queue())
        try:
            assert watcher.ready.wait(3) and watcher.installed
        finally:
            watcher.close()
        assert not watcher.thread.is_alive()
        assert not watcher.circle_thread.is_alive()
        return
    if '--selection-runtime-test' in sys.argv:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            assert auto.GetRootControl() is not None
        return
    C.windll.user32.SetProcessDPIAware()
    kernel = C.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [C.c_void_p, W.BOOL, W.LPCWSTR]
    kernel.CreateMutexW.restype = W.HANDLE
    mutex = kernel.CreateMutexW(None, False, 'Local\\ScreenAnswer.TrayApp')
    if not mutex:
        raise C.WinError(C.get_last_error())
    if C.get_last_error() == 183:
        if '--manual-runtime-test' in sys.argv or '--smoke-test' in sys.argv:
            raise SystemExit(2)
        messagebox.showinfo('ScreenAnswer', '이미 실행 중입니다. 시스템 트레이의 아이콘을 확인하세요.')
        return
    try:
        manual_test = '--manual-runtime-test' in sys.argv
        app = App(demo=manual_test or '--demo' in sys.argv or '--smoke-test' in sys.argv)
        if manual_test:
            try:
                app.root.update()
                app.toggle_manual_input()
                app.root.update()
                assert app.manual_mode and app.selection.suspended.is_set()
                app.result_panel.views[0].insert('1.0', '질문 입력 확인')
                app.root.update()
                assert app.manual_draft == '질문 입력 확인'
                app.settings['led_enabled'] = False
                app.request_key = lambda manual=False: 'runtime-test-key'
                def reply(key, text):
                    assert key == 'runtime-test-key' and text == '질문 입력 확인'
                    app.request_source = text
                    app.handle_answer({'answer': '입력 모드 응답 확인', 'led_count': 1})
                app.submit = reply
                app.result_panel.views[0].event_generate('<Control-Return>')
                app.root.update()
                assert app.result_text == '입력 모드 응답 확인'
                assert app.tray.title == '입력 모드 응답 확인'
                assert app.manual_mode and not app.busy
                app.dialog.destroy()
                assert app.manual_mode and app.selection.suspended.is_set()
                app.toggle_manual_input()
                assert not app.manual_mode
                assert not app.selection.suspended.is_set()
                app.toggle_manual_input()
                assert app.result_panel.views[0].get('1.0', 'end-1c') == '질문 입력 확인'
                app.dialog.destroy()
                app.toggle_manual_input()
                app.settings['manual_input_show_panel'] = False
                app.toggle_manual_input()
                assert app.dialog is None and app.manual_mode
                app.toggle_manual_input()
                app.open_settings()
                app.root.update()
                app.dialog.destroy()
                assert not app.selection.suspended.is_set()
                app.manual_hotkey.register('Ctrl+Alt+Space')
                app.manual_hotkey.register(None)
                from runtime_checks import check_hidden_input
                check_hidden_input(app)
                APP_DIR.mkdir(parents=True, exist_ok=True)
                (APP_DIR / 'manual-runtime-status.txt').write_text('PASS', encoding='utf-8')
            except Exception:
                import traceback
                APP_DIR.mkdir(parents=True, exist_ok=True)
                (APP_DIR / 'manual-runtime-status.txt').write_text(traceback.format_exc(), encoding='utf-8')
                raise SystemExit(1)
            finally:
                app.close()
            return
        if '--smoke-test' in sys.argv:
            app.root.after(1500, app.close)
        app.root.mainloop()
    finally:
        kernel.CloseHandle.argtypes = [W.HANDLE]
        kernel.CloseHandle(mutex)

if __name__ == '__main__':
    main()

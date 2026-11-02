"""Opt-in checks against this app's own windows, without an API request."""
import ctypes as C
from ctypes import wintypes as W
import time
from blink import Input
from hidden_input import foreground_window, window_handle


def check_hidden_input(app):
    def pump(seconds=.12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.root.update()
            time.sleep(.005)

    def send(events):
        receiver = app.hidden_input
        deadline = time.monotonic() + 1
        while receiver.active and not receiver.has_keyboard_focus() and time.monotonic() < deadline:
            receiver.ensure_focus()
            pump(.05)
        assert receiver.has_keyboard_focus(), (
            'Input receiver is not foreground', receiver.active, receiver.window.state(),
            foreground_window(), window_handle(receiver.window), str(receiver.window.focus_get()))
        inputs = (Input * len(events))()
        for item, (vk, scan, flags) in zip(inputs, events):
            item.type = 1
            item.keyboard.vk, item.keyboard.scan, item.keyboard.flags = vk, scan, flags
        user = C.windll.user32
        user.SendInput.argtypes = [W.UINT, C.POINTER(Input), C.c_int]
        user.SendInput.restype = W.UINT
        assert user.SendInput(len(inputs), inputs, C.sizeof(Input)) == len(inputs)
        pump()

    def type_text(text):
        send([(0, ord(ch), flag) for ch in text for flag in (4, 6)])

    app.settings['manual_input_show_panel'] = False
    app.settings['led_enabled'] = False
    app.manual_draft = ''
    app.set_manual_mode(True)
    pump()
    assert not app.settings_visible() and app.hidden_input.active
    assert app.hidden_input.window.attributes('-alpha') == 0.0
    type_text('가나다 abc')
    assert app.manual_draft == '가나다 abc', repr(app.manual_draft)
    send([(8, 0, 0), (8, 0, 2)])
    assert app.manual_draft == '가나다 ab'
    send([(13, 0, 0), (13, 0, 2)])
    type_text('둘째 줄')
    expected = '가나다 ab\n둘째 줄'
    assert app.manual_draft == expected
    def reply(key, text):
        assert key == 'runtime-test-key' and text == expected
        app.request_source = text
        app.handle_answer({'answer': '숨긴 입력 확인', 'led_count': 1})
    app.request_key = lambda manual=False: 'runtime-test-key'
    app.submit = reply
    send([(17, 0, 0), (13, 0, 0), (13, 0, 2), (17, 0, 2)])
    assert app.result_text == '숨긴 입력 확인' and not app.busy
    app.open_settings()
    pump()
    assert app.dialog.focus_get() == app.result_panel.views[0]
    assert app.result_panel.views[0].get('1.0', 'end-1c') == expected
    app.dialog.iconify()
    pump()
    assert app.hidden_input.active and not app.settings_visible()
    app.open_settings()
    pump()
    assert not app.hidden_input.active and app.settings_visible(), (
        app.hidden_input.active, app.dialog.state(), app.manual_mode,
        time.monotonic() - app.manual_last_input)
    app.result_panel.views[0].insert('end', ' 편집')
    pump()
    app.dialog.destroy()
    pump()
    assert app.hidden_input.active and app.manual_draft == expected + ' 편집'
    app.open_settings()
    pump()
    app.manual_last_input = time.monotonic() - 5.2
    app.check_manual_input()
    assert not app.manual_mode and app.manual_idle
    assert not app.selection.suspended.is_set()
    assert app.dialog.state() == 'withdrawn' and not app.settings_visible()
    assert not app.hidden_input.active
    assert app.tray.icon.getpixel((30, 12)) == (255, 132, 132)
    app.open_settings()
    assert app.settings_visible()
    app.dialog.destroy()

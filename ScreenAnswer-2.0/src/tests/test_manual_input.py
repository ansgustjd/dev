import ctypes as C
import gc
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, DEFAULTS
from hotkeys import Hotkey
from result_panel import ResultPanel
from hidden_input import HiddenInput
from settings_store import load_settings

class ManualInputTests(unittest.TestCase):
    def make_app(self):
        app = App.__new__(App)
        app.settings = {**DEFAULTS, 'led_enabled': False}
        app.busy, app.dialog, app.manual_request = False, None, False
        app.manual_mode, app.manual_draft, app.manual_status = False, '', ''
        app.activate_hidden_input = Mock()
        app.selection = Mock()
        app.selection.suspended = threading.Event()
        app.hotkey, app.manual_hotkey, app.tray, app.blinker = Mock(), Mock(), Mock(), Mock()
        app.hide_near, app.hide_tray_answer, app.publish = Mock(), Mock(), Mock()
        app.display_selection_answer, app.open_settings = Mock(), Mock()
        return app

    def test_mode_optional_window_and_orange_until_disabled(self):
        app = self.make_app()
        app.settings['tray_icon_changes'] = False
        with patch('app.make_icon') as icon:
            app.toggle_manual_input()
            self.assertTrue(app.manual_mode)
            self.assertTrue(app.selection.suspended.is_set())
            app.open_settings.assert_called_once()
            icon.assert_called_with('#ffc86b')
            app.set_tray_color('#72d7f3')
            icon.assert_called_with('#ffc86b')
            app.toggle_manual_input()
            self.assertFalse(app.manual_mode)
            self.assertFalse(app.selection.suspended.is_set())
            icon.assert_called_with('#72d7f3')
        app.settings['manual_input_show_panel'] = False
        app.open_settings.reset_mock()
        app.toggle_manual_input()
        app.open_settings.assert_not_called()
        self.assertTrue(app.manual_mode)

    def test_existing_panel_edits_and_retains_draft_across_close(self):
        root = tk.Tk()
        root.withdraw()
        app = self.make_app()
        app.request_source, app.result_text = 'old source', 'old answer'
        app.submit_manual = Mock()
        try:
            panel = ResultPanel(root, app)
            self.assertEqual(panel.views[0].cget('state'), 'disabled')
            app.manual_mode = True
            panel.sync()
            editor = panel.views[0]
            draft = '질문입니다.\n둘째 줄입니다.'
            editor.insert('1.0', draft)
            root.update()
            self.assertEqual(app.manual_draft, draft)
            editor.mark_set('insert', '1.2')
            panel.sync()
            self.assertEqual(editor.index('insert'), '1.2')
            panel.send()
            app.submit_manual.assert_called_once_with(draft)
            panel.destroy()
            panel = ResultPanel(root, app)
            self.assertEqual(panel.views[0].get('1.0', 'end-1c'), draft)
            app.manual_mode = False
            panel.sync()
            self.assertEqual(panel.views[0].get('1.0', 'end-1c'), 'old source')
            app.manual_mode = True
            panel.sync()
            self.assertEqual(panel.views[0].get('1.0', 'end-1c'), draft)
            answer = '전체 답변\n' * 200
            app.result_text = answer
            panel.sync()
            self.assertEqual(panel.views[1].get('1.0', 'end-1c'), answer)
        finally:
            root.destroy()
            del editor, panel, root, app
            gc.collect()

    def test_submit_from_existing_settings_uses_shared_output(self):
        app = self.make_app()
        app.manual_mode = True
        app.dialog = Mock()
        app.submit = Mock()
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}):
            app.submit_manual('  질문\n내용  ')
        app.submit.assert_called_once_with('test-key', text='질문\n내용')
        app.submit_manual('다른 질문')
        self.assertEqual(app.submit.call_count, 1)
        app.handle_answer({'answer': '전체 답변', 'led_count': 1})
        app.publish.assert_called_with('전체 답변', '#72d7f3')
        self.assertEqual(app.result_text, '전체 답변')
        self.assertFalse(app.busy)
        self.assertTrue(app.manual_mode)
        self.assertTrue(app.selection.suspended.is_set())

    def test_empty_disabled_and_capture_do_not_send(self):
        app = self.make_app()
        app.submit = Mock()
        app.submit_manual('question')
        app.manual_mode = True
        app.submit_manual('  ')
        self.assertIn('입력', app.manual_status)
        with patch('app.ImageGrab.grab') as capture:
            app.scan()
            app.capture('test-key')
            capture.assert_not_called()
        app.submit.assert_not_called()

    def test_failure_allows_retry_and_keeps_mode(self):
        app = self.make_app()
        app.manual_mode = True
        app.request_key = Mock(return_value='test-key')
        app.submit = Mock(side_effect=RuntimeError('test'))
        with patch('app.record_error'):
            app.submit_manual('question')
        self.assertFalse(app.busy)
        self.assertFalse(app.manual_request)
        self.assertTrue(app.manual_mode)
        self.assertIn('요청 오류', app.result_text)

    def test_shortcuts_rollback_and_collision(self):
        app = self.make_app()
        with patch('app.save_settings', side_effect=OSError('test')):
            with self.assertRaises(OSError):
                app.apply_settings({**app.settings, 'manual_input_hotkey': 'Ctrl+Alt+F10'})
        app.manual_hotkey.register.assert_called_with('Ctrl+Alt+Space')
        app.hotkey.register.assert_called_with('Ctrl+Alt+Q')
        with self.assertRaises(ValueError):
            app.apply_settings({**app.settings, 'manual_input_hotkey': 'Alt+Ctrl+Q'})

    def test_existing_settings_keep_key_and_task(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text(json.dumps({'key': 'encrypted-test', 'task_preset': '번역', 'settings_version': 4}), encoding='utf-8')
            settings = load_settings(config=path)
            self.assertEqual(settings['key'], 'encrypted-test')
            self.assertEqual(settings['task_preset'], '번역')
            self.assertTrue(settings['manual_input_show_panel'])

    def test_restored_settings_stop_hidden_keyboard_receiver(self):
        app = self.make_app()
        app.manual_mode, app.manual_last_input = True, time.monotonic()
        app.dialog = Mock()
        app.dialog.state.return_value = 'normal'
        app.hidden_input = Mock(active=True)
        app.check_manual_input()
        app.hidden_input.suspend.assert_called_once()

    def test_hidden_focus_requires_owned_native_and_tk_editor_focus(self):
        receiver = HiddenInput.__new__(HiddenInput)
        receiver.active, receiver.window, receiver.editor = True, Mock(), Mock()
        receiver.window.focus_get.return_value = receiver.editor
        receiver.editor.winfo_id.return_value = 33
        with patch('hidden_input.window_handle', return_value=11), \
             patch('hidden_input.foreground_window', return_value=None) as foreground, \
             patch('hidden_input.C.windll.user32.GetFocus', return_value=33) as focused, \
             patch('hidden_input.C.windll.user32.IsChild', return_value=False):
            self.assertTrue(receiver.has_keyboard_focus())
            foreground.return_value = 99
            self.assertFalse(receiver.has_keyboard_focus())
            foreground.return_value = None
            focused.return_value = 99
            self.assertFalse(receiver.has_keyboard_focus())

    def test_native_hotkey_dispatch(self):
        events = queue.Queue()
        handler = Hotkey(events, 'manual_input')
        try:
            handler.register('Ctrl+Alt+Shift+F23')
            self.assertTrue(C.windll.user32.PostThreadMessageW(handler.thread.native_id, 0x312, 2, 0))
            self.assertEqual(events.get(timeout=2), ('manual_input', None))
            handler.register(None)
        finally:
            handler.commands.put(('stop',))
            handler.thread.join(3)
        self.assertFalse(handler.thread.is_alive())

    def test_idle_timeout_restores_other_inputs_and_red_icon(self):
        app = self.make_app()
        app.manual_mode = True
        app.manual_last_input = 10
        app.manual_draft = '작성 중인 질문'
        with patch('app.time.monotonic', return_value=15.01), patch('app.make_icon') as icon:
            app.check_manual_input()
            icon.assert_called_with('#ff8484')
        self.assertFalse(app.manual_mode)
        self.assertTrue(app.manual_idle)
        self.assertFalse(app.selection.suspended.is_set())
        self.assertEqual(app.manual_draft, '작성 중인 질문')

    def test_activity_extends_idle_deadline_and_request_is_not_interrupted(self):
        app = self.make_app()
        app.manual_mode = True
        with patch('app.time.monotonic', return_value=20):
            app.manual_activity()
        self.assertEqual(app.manual_last_input, 20)
        with patch('app.time.monotonic', return_value=24.99):
            app.check_manual_input()
        self.assertTrue(app.manual_mode)
        app.busy = True
        with patch('app.time.monotonic', return_value=99):
            app.check_manual_input()
        self.assertTrue(app.manual_mode)

    def test_withdrawn_settings_do_not_block_input_after_idle(self):
        app = self.make_app()
        app.dialog = Mock()
        app.dialog.state.return_value = 'withdrawn'
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'test-key'}):
            self.assertEqual(app.request_key(), 'test-key')
        app.refresh_input_pause()
        self.assertFalse(app.selection.suspended.is_set())

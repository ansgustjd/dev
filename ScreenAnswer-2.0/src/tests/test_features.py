import sys
import queue
import tempfile
import threading
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, DEFAULTS, Hotkey, near_geometry, load_settings, parse_transparency
from selection import selected_text, SelectionWatcher


class FeatureTests(unittest.TestCase):
    def test_previous_display_migrates_to_bubble_toggle(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'settings.json'
            for previous, enabled in [('near',True), ('tray',False)]:
                path.write_text(json.dumps({'selection_display':previous, 'key':'encrypted-example'}), encoding='utf-8')
                with patch('app.CONFIG',path):
                    saved = load_settings()
                self.assertEqual(saved['bubble_enabled'], enabled)
                self.assertNotIn('selection_display', saved)
                self.assertEqual(saved['key'], 'encrypted-example')

    def test_tray_answer_is_always_published_with_or_without_bubble(self):
        for enabled in (False,True):
            app = self.instance()
            app.settings['task_preset'] = '참·거짓'
            app.settings['bubble_enabled'] = enabled
            app.events = queue.Queue()
            app.selection.current = (77,'problem')
            app.pending_selection = (4,77,(10,10,100,40),'problem')
            app.show_near = Mock()
            app.blinker = Mock()
            app.blinker.start.side_effect = lambda count, done: done(None)
            app.events.put(('answer','FALSE'))
            with patch('app.C.windll.user32.GetForegroundWindow', return_value=77):
                app.poll()
            self.assertEqual(app.tray.title,'False · 2번')
            self.assertEqual(app.blinker.start.call_args.args[0], 2)
            if enabled:
                app.show_near.assert_called_once_with('False · 2번', (10,10,100,40))
            else:
                app.show_near.assert_not_called()

    def instance(self):
        app = App.__new__(App)
        app.settings = DEFAULTS.copy()
        app.root = Mock()
        app.hotkey = Mock()
        app.selection = Mock(generation=4)
        app.selection.enabled.is_set.return_value = True
        app.selection.enabled.clear = Mock()
        app.tray = Mock()
        app.busy = False
        app.dialog = app.near_popup = None
        return app

    def test_disabled_features_do_not_read_capture_or_request(self):
        app = self.instance()
        app.settings.update(screen_enabled=False)
        app.request_key = Mock()
        with patch('app.ImageGrab.grab') as capture, patch('app.ask') as ask:
            app.scan()
            app.busy = True
            app.capture('fake-key')  # Disabled while the delayed capture was pending.
            capture.assert_not_called()
            ask.assert_not_called()
            app.root.clipboard_get.assert_not_called()
            app.request_key.assert_not_called()
            self.assertFalse(app.busy)

    def test_transparency_persists_and_updates_selection_bubble(self):
        app = self.instance()
        app.settings['bubble_enabled'] = True
        app.near_popup = Mock()
        with tempfile.TemporaryDirectory() as folder, patch('app.APP_DIR', Path(folder)), patch('app.CONFIG', Path(folder)/'settings.json'):
            app.apply_settings({**app.settings, 'answer_transparency': 50})
            self.assertEqual(load_settings()['answer_transparency'], 50)
        app.near_popup.attributes.assert_called_once_with('-alpha', .5)
        for value in (0, '50', 90):
            self.assertEqual(parse_transparency(value), int(value))
        for value in (-1, 91, '', '1.5', True):
            with self.assertRaises(ValueError): parse_transparency(value)

    def test_disable_persists_and_releases_hotkey(self):
        app = self.instance()
        with tempfile.TemporaryDirectory() as folder, patch('app.APP_DIR', Path(folder)), patch('app.CONFIG', Path(folder)/'settings.json'):
            app.toggle_feature('screen_enabled')
            self.assertFalse(app.settings['screen_enabled'])
            self.assertIsNone(app.hotkey.register.call_args.args[0])
            app.toggle_feature('bubble_enabled')
            self.assertTrue(load_settings()['bubble_enabled'])
            self.assertFalse(load_settings()['screen_enabled'])

    def test_save_failure_restores_registered_hotkeys_and_settings(self):
        app = self.instance()
        with patch('app.save_settings', side_effect=OSError('test failure')):
            with self.assertRaises(OSError):
                app.apply_settings({**app.settings, 'screen_enabled': False})
        self.assertTrue(app.settings['screen_enabled'])
        self.assertEqual(app.hotkey.register.call_args.args[0], DEFAULTS['hotkey'])

    def test_near_answer_only_for_same_foreground_and_generation(self):
        app = self.instance()
        app.settings['bubble_enabled'] = True
        app.show_near = Mock()
        app.selection.current = (77, 'problem')
        anchor = (10, 10, 100, 40)
        with patch('app.C.windll.user32.GetForegroundWindow', return_value=77):
            app.pending_selection = (4, 77, anchor, 'problem')
            app.display_selection_answer('answer')
            app.show_near.assert_called_once_with('answer', anchor)
            for context in [(3, 77, anchor, 'problem'), (4, 88, anchor, 'problem')]:
                app.pending_selection = context
                app.display_selection_answer('stale')
            app.settings['bubble_enabled'] = False
            app.pending_selection = (4, 77, anchor, 'problem')
            app.display_selection_answer('tray')
            self.assertEqual(app.show_near.call_count, 1)

    def test_deselection_hides_bubble_and_pending_answer_stays_hidden(self):
        app = self.instance()
        app.near_popup = Mock()
        app.near_signature = (4, (77, 'problem'))
        app.selection_changed((4, None))
        self.assertIsNone(app.near_popup)
        self.assertIsNone(app.near_signature)
        app.settings['bubble_enabled'] = True
        app.pending_selection = (4, 77, (10,10,100,40), 'problem')
        app.selection.current = None
        app.show_near = Mock()
        with patch('app.C.windll.user32.GetForegroundWindow', return_value=77):
            app.display_selection_answer('late answer')
        app.show_near.assert_not_called()

    def test_translation_progress_keeps_request_context_until_final_answer(self):
        app = self.instance()
        app.settings.update(task_preset='번역', bubble_enabled=True)
        app.events = queue.Queue()
        app.selection.current = (77, 'problem')
        context = (4, 77, (10, 10, 100, 40), 'problem')
        app.pending_selection = context
        app.show_near = Mock()
        app.busy = True
        def translate(*args, on_progress, **kwargs):
            for n in range(100):
                on_progress(f'{n}번째 번역')
            return {'answer': '전체 번역', 'led_count': 2}
        with patch('app.ask', side_effect=translate), patch('app.threading.Thread') as thread:
            thread.return_value.start.side_effect = lambda: thread.call_args.kwargs['target']()
            app.submit('test-key', text='problem')
        self.assertEqual(app.translation_progress.qsize(), 1)
        final_event = app.events.get_nowait()
        with patch('app.C.windll.user32.GetForegroundWindow', return_value=77):
            app.poll()
            app.show_near.assert_called_once_with('99번째 번역\n\n번역 중…', context[2])
            self.assertEqual(app.pending_selection, context)
            self.assertTrue(app.busy)
            app.handle_answer = Mock()
            app.events.put(final_event)
            app.poll()
        app.handle_answer.assert_called_once_with({'answer': '전체 번역', 'led_count': 2})
        self.assertIsNone(app.translation_progress)

    def test_watcher_monitors_deselection_while_api_is_busy(self):
        watcher = SelectionWatcher.__new__(SelectionWatcher)
        watcher.enabled = threading.Event()
        watcher.enabled.set()
        watcher.suspended = threading.Event()
        watcher.suspended.set()
        watcher.stop = Mock()
        watcher.stop.wait.side_effect = [False, False, True]
        watcher.events = queue.Queue()
        watcher.generation, watcher.current = 4, None
        auto = Mock()
        with patch.dict('sys.modules', {'uiautomation': auto}), patch('selection.selected_text', side_effect=[(77, 'problem', (1,2,3,4)), None]), patch('selection.input_stamp', side_effect=[(77, 1), (77, 2)]), patch('selection.ctypes.windll.user32.GetAsyncKeyState', return_value=0):
            watcher.run()
        self.assertEqual(watcher.events.get_nowait(), ('selection_state', (4, (77, 'problem'))))
        self.assertEqual(watcher.events.get_nowait(), ('selection_state', (4, None)))
        self.assertTrue(watcher.events.empty())
        self.assertIsNone(watcher.current)

    def test_geometry_bottom_edge_and_negative_monitor(self):
        for work, anchor in [((0,0,1920,1040), (1850,1000,1890,1020)),
                             ((-1920,0,0,1040), (-1900,900,-1850,940))]:
            x,y,w,h = near_geometry(anchor, work, 400, 200)
            self.assertGreaterEqual(x, work[0])
            self.assertGreaterEqual(y, work[1])
            self.assertLessEqual(x+w, work[2])
            self.assertLessEqual(y+h, work[3])

    def test_selected_text_has_provider_position(self):
        auto = Mock()
        auto.GetForegroundControl.return_value = Mock(ProcessId=222, NativeWindowHandle=77)
        focused = auto.GetFocusedControl.return_value
        focused.ProcessId, focused.IsPassword = 222, False
        focused.GetParentControl.return_value = None
        selected = Mock()
        selected.GetText.return_value = 'problem'
        selected.GetBoundingRectangles.return_value = [Mock(left=-500, top=100, right=-300, bottom=120)]
        focused.GetPattern.return_value.GetSelection.return_value = [selected]
        self.assertEqual(selected_text(auto, own_pid=111, with_position=True), (77, 'problem', (-500,100,-300,120)))

    def test_hotkey_release_and_reregister_thread(self):
        # Mock native API calls; do not register keys on the user's desktop.
        with patch('hotkeys.C.windll.user32.RegisterHotKey', return_value=1) as register, \
             patch('hotkeys.C.windll.user32.UnregisterHotKey', return_value=1) as unregister:
            handler = Hotkey(queue.Queue())
            try:
                handler.register('Ctrl+Shift+F23')
                handler.register(None)
                handler.register('Ctrl+Shift+F23')
                self.assertEqual(register.call_count, 2)
                self.assertEqual(unregister.call_count, 1)
            finally:
                handler.commands.put(('stop',))
                handler.thread.join(2)
            self.assertFalse(handler.thread.is_alive())


if __name__ == '__main__': unittest.main()

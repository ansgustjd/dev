import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, DEFAULTS, load_settings, payload
from tasks import migrate_task_profiles


class RecoveryTests(unittest.TestCase):
    def instance(self):
        app = App.__new__(App)
        app.settings = DEFAULTS.copy()
        app.root = Mock()
        app.poll_delay_ms = Mock(return_value=50)
        app.dialog = None
        app.busy = False
        app.last_auto_selection = None
        app.selection = Mock(generation=1, current=(77, 'problem'))
        app.selection.enabled.is_set.return_value = True
        app.blinker = Mock(active=False)
        app.request_key = Mock(return_value='fake-test-key')
        app.submit = Mock()
        app.hide_near = Mock()
        app.set_tray_color = Mock()
        app.publish = Mock()
        app.events = queue.Queue()
        return app

    def test_stale_selection_and_foreground_are_not_submitted(self):
        for current, foreground in [(None, 77), ((77, 'different'), 77), ((77, 'problem'), 88)]:
            with self.subTest(current=current, foreground=foreground):
                app = self.instance()
                app.selection.current = current
                with patch('app.C.windll.user32.GetForegroundWindow', return_value=foreground):
                    app.answer_selection((1, (77, 'problem')))
                app.submit.assert_not_called()
                self.assertFalse(app.busy)

    def test_selection_changed_while_getting_key_is_rechecked(self):
        app = self.instance()
        def get_key():
            app.selection.generation += 1
            return 'fake-test-key'
        app.request_key.side_effect = get_key
        with patch('app.C.windll.user32.GetForegroundWindow', return_value=77):
            app.answer_selection((1, (77, 'problem')))
        app.submit.assert_not_called()

    def test_same_text_can_be_reselected_after_clearing_while_busy(self):
        app = self.instance()
        app.last_auto_selection = (77, 'problem')
        app.busy = True
        app.selection_changed((1, None))
        app.busy = False
        with patch('app.C.windll.user32.GetForegroundWindow', return_value=77):
            app.answer_selection((1, (77, 'problem')))
        app.submit.assert_called_once_with('fake-test-key', text='problem')

    def test_corrupt_settings_are_preserved_and_defaults_loaded(self):
        for raw in ['{', '[]', '{"screen_enabled": "false"}', '{"selection_delay_ms": -1}',
                    '{"task_profiles": {"요약": {"extra": 3}}}', '{"timeout": 0}',
                    '{"task_preset": "missing"}', '{"reference_files": 5}',
                    '{"task_profiles": {"요약": {"advanced": true, "prompt": ""}}}']:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'settings.json'
                path.write_text(raw, encoding='utf-8')
                warnings = []
                with patch('app.CONFIG', path):
                    settings = load_settings(warnings)
                self.assertEqual(settings['model'], DEFAULTS['model'])
                self.assertEqual(settings['selection_delay_ms'], DEFAULTS['selection_delay_ms'])
                self.assertEqual(path.read_text(encoding='utf-8'), raw)
                backups = list(Path(folder).glob('settings.recovery-*.json'))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), path.read_bytes())
                self.assertEqual(len(warnings), 1)

    def test_backup_failure_does_not_prevent_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text('{', encoding='utf-8')
            warnings = []
            with patch('app.CONFIG', path), patch.object(Path, 'write_bytes', side_effect=PermissionError):
                settings = load_settings(warnings)
            self.assertEqual(settings['hotkey'], DEFAULTS['hotkey'])
            self.assertIn('백업하지 못했습니다', warnings[0])

    def test_valid_settings_preserve_key_and_partial_profiles(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text(json.dumps({'key': 'encrypted-placeholder', 'task_profiles': {'요약': {'extra': 'short'}}}), encoding='utf-8')
            warnings = []
            with patch('app.CONFIG', path):
                settings = load_settings(warnings)
            self.assertEqual(settings['key'], 'encrypted-placeholder')
            self.assertEqual(settings['task_profiles']['요약']['extra'], 'short')
            self.assertTrue(settings['task_profiles']['요약']['prompt'])
            self.assertEqual(warnings, [])
            self.assertEqual(len(list(Path(folder).glob('settings.recovery-*.json'))), 0)

    def test_poll_recovers_even_when_reporting_fails(self):
        app = self.instance()
        app.busy = True
        app.events.put(('answer', {}))
        app.handle_answer = Mock(side_effect=RuntimeError('test failure'))
        app.publish.side_effect = RuntimeError('tray failure')
        app.poll()
        self.assertFalse(app.busy)
        app.selection.suspended.clear.assert_called_once()
        app.root.after.assert_called_once_with(50, app.poll)
        app.publish.side_effect = None
        app.events.put(('settings', None))
        app.open_settings = Mock()
        app.poll()
        app.open_settings.assert_called_once()

    def test_quit_does_not_reschedule_poll(self):
        app = self.instance()
        app.events.put(('quit', None))
        app.close = Mock()
        app.poll()
        app.close.assert_called_once()
        app.root.after.assert_not_called()

    def test_configured_done_count_reaches_regular_and_translation_requests(self):
        for preset in ('자유 질문', '요약', '번역'):
            settings = migrate_task_profiles({**DEFAULTS, 'task_preset': preset, 'led_done_count': 9})
            prompt = payload(settings, text='sample')['input'][0]['content'][0]['text']
            self.assertIn('9번 깜빡이세요', prompt)
            self.assertNotIn('완료 시 1번', prompt)

    def test_custom_led_prompt_is_preserved(self):
        settings = migrate_task_profiles({**DEFAULTS, 'led_done_count': 9})
        settings['task_profiles']['자유 질문'].update(advanced=True, prompt='LED는 2번 깜빡이세요.')
        self.assertEqual(payload(settings, text='sample')['input'][0]['content'][0]['text'], 'LED는 2번 깜빡이세요.')


if __name__ == '__main__':
    unittest.main()

import gc
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import App, DEFAULTS
from hotkeys import Hotkey
from mouse import MouseWatcher
from power import SelectionRefresh, ui_poll_ms
from result_panel import ResultPanel
from selection import SelectionGate, SelectionWatcher


class PowerTests(unittest.TestCase):
    def test_stationary_input_avoids_repeated_provider_reads(self):
        reads = SelectionRefresh()
        count = sum(reads.due((7, 100), i * .05, False) for i in range(100))
        self.assertLessEqual(count, 7)
        self.assertGreaterEqual(count, 4)

    def test_input_window_switch_release_and_provider_failure_refresh_immediately(self):
        reads = SelectionRefresh()
        self.assertTrue(reads.due((7, 1), 0, False))
        self.assertTrue(reads.due((7, 2), .01, False))
        self.assertTrue(reads.due((8, 2), .02, False))
        self.assertFalse(reads.due((8, 3), .03, True))
        self.assertTrue(reads.due((8, 3), .04, False))
        self.assertTrue(reads.due(None, .05, False))

    def test_dispatch_deadline_rechecks_text_before_sending(self):
        reads, gate = SelectionRefresh(), SelectionGate(delay=.05, cooldown=0)
        gate.observe(None, 0)
        gate.observe((7, 'selected'), 1)
        reads.due((7, 1), 1, False, gate)
        self.assertTrue(reads.due((7, 1), 1.051, False, gate))
        gate.observe((7, 'selected'), 1.051)
        self.assertFalse(reads.due((7, 1), 1.06, False, gate))

    def test_disabled_selection_blocks_and_enabling_wakes_it(self):
        calls, observed = [], threading.Event()
        original = SelectionWatcher._wait
        def wait(watcher, delay):
            calls.append(delay)
            return original(watcher, delay)
        with patch.object(SelectionWatcher, '_wait', wait), \
             patch.dict('sys.modules', {'uiautomation': Mock()}), \
             patch('selection.selected_text', side_effect=lambda *a, **k: observed.set()), \
             patch('selection.input_stamp', return_value=(7, 1)):
            watcher = SelectionWatcher(queue.Queue(), delay_ms=50, browser_copy=False)
            try:
                time.sleep(.1)
                self.assertEqual(calls, [None])
                self.assertFalse(observed.is_set())
                watcher.set_enabled(True)
                self.assertTrue(observed.wait(1))
            finally:
                watcher.close()
        self.assertFalse(watcher.thread.is_alive())

    def test_hotkey_waits_for_native_messages_without_periodic_wakes(self):
        native = __import__('ctypes').windll.user32.GetMessageW
        with patch('hotkeys.C.windll.user32.GetMessageW', wraps=native) as get_message:
            handler = Hotkey(queue.Queue())
            try:
                handler.register(None)
                time.sleep(.03)
                calls = get_message.call_count
                self.assertGreater(calls, 0)
                time.sleep(.1)
                self.assertEqual(get_message.call_count, calls)
            finally:
                handler.commands.put(('stop',))
                handler.thread.join(2)
        self.assertFalse(handler.thread.is_alive())

    def test_circle_worker_blocks_until_work_or_shutdown(self):
        class CountingQueue(queue.Queue):
            calls = 0
            def get(self, *args, **kwargs):
                self.calls += 1
                return super().get(*args, **kwargs)
        watcher = MouseWatcher.__new__(MouseWatcher)
        watcher.stop = threading.Event()
        watcher.circle_moves = CountingQueue()
        worker = threading.Thread(target=watcher.run_circles, daemon=True)
        worker.start()
        try:
            time.sleep(.1)
            self.assertEqual(watcher.circle_moves.calls, 1)
        finally:
            watcher.stop.set()
            watcher.circle_moves.put(None)
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_same_icon_is_not_recreated(self):
        app = App.__new__(App)
        app.settings, app.tray = DEFAULTS.copy(), Mock()
        with patch('app.make_icon') as icon:
            for _ in range(100):
                app.set_tray_color('#72d7f3')
            self.assertEqual(icon.call_count, 1)
            app.set_tray_color('#ff8484')
            self.assertEqual(icon.call_count, 2)

    def test_ui_keeps_fast_polling_during_work_and_manual_input(self):
        self.assertEqual(ui_poll_ms(0), 50)
        self.assertEqual(ui_poll_ms(30), 200)
        self.assertEqual(ui_poll_ms(30, busy=True), 50)
        self.assertEqual(ui_poll_ms(30, manual=True), 50)

    def test_hidden_panel_has_no_refresh_timer_and_reopens_with_latest_text(self):
        root = tk.Tk()
        root.withdraw()
        app = SimpleNamespace(result_text='before', settings=DEFAULTS.copy(), busy=False)
        panel = ResultPanel(root, app)
        panel.pack(fill='both', expand=True)
        try:
            self.assertIsNone(panel.job)
            root.deiconify()
            root.update()
            self.assertIsNotNone(panel.job)
            root.withdraw()
            root.update()
            self.assertIsNone(panel.job)
            app.result_text = 'after'
            root.deiconify()
            root.update()
            self.assertEqual(panel.views[1].get('1.0', 'end-1c'), 'after')
        finally:
            root.destroy()
            del root, panel, app
            gc.collect()

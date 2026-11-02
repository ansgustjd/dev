import sys
import time
import queue
import math
import threading
import random
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mouse import ClickGate, CircleGate, MouseWatcher, click_number, click_interval
from app import App, DEFAULTS
from PIL import Image


class MouseTests(unittest.TestCase):
    def test_circles_after_long_pointer_travel_keep_two_turn_detection(self):
        for steps in (100,120,180,240,360):
            for direction in (-1,1):
                with self.subTest(steps=steps,direction=direction):
                    gate = CircleGate()
                    for i in range(400):
                        gate.observe((-1000+i,300),7,i*.008)
                    hits=[]
                    for i in range(steps+1):
                        angle=direction*i/steps*4*math.pi
                        hits.append(gate.observe((300+70*math.cos(angle),300+70*math.sin(angle)),7,(400+i)*.008))
                    self.assertTrue(any(hits))

    def test_ordinary_pointer_wander_does_not_trigger_or_grow_history(self):
        gate=CircleGate()
        generator=random.Random(91)
        x=y=300.0
        for i in range(3000):
            x+=generator.uniform(-8,8)
            y+=generator.uniform(-8,8)
            self.assertFalse(gate.observe((x,y),7,i*.008))
        self.assertLessEqual(len(gate.points),400)

    def test_high_rate_hook_sampling_keeps_every_drag_movement(self):
        watcher=MouseWatcher.__new__(MouseWatcher)
        watcher.gate=ClickGate()
        watcher.circle_enabled=True
        watcher.last_hook_move=float('-inf')
        accepted=sum(watcher.accept_move(i/8000) for i in range(8000))
        self.assertLessEqual(accepted,125)
        self.assertGreater(accepted,115)
        watcher.gate.observe('down',(100,100),7,1)
        self.assertTrue(all(watcher.accept_move(1+i/8000) for i in range(1000)))
        for point in ((109,100),(100,100)):
            now=watcher.last_hook_move+.00001
            self.assertTrue(watcher.accept_move(now))
            watcher.gate.observe('move',point,7,now)
            # Once a drag has been rejected, sampling may resume.
            if watcher.gate.pressed is None:
                break
        self.assertFalse(watcher.gate.observe('up',(100,100),7,1.01))

    def test_imperfect_fast_small_slow_and_drifting_circles(self):
        cases = [('fast',70,70,.6,0,36),('small',22,22,2,0,100),
                 ('ellipse',100,24,3,0,100),('slow',70,70,6,0,120),
                 ('drift',70,70,3,65,100)]
        for name,rx,ry,duration,drift,steps in cases:
            for direction in (-1,1):
                gate, results = CircleGate(), []
                for i in range(steps+1):
                    fraction=i/steps
                    angle=direction*fraction*4*math.pi
                    point=(300+rx*math.cos(angle)+drift*fraction,
                           300+ry*math.sin(angle)+3*math.sin(angle*5))
                    results.append(gate.observe(point,7,fraction*duration))
                self.assertTrue(any(results),(name,direction))

    def test_circle_worker_emits_event_without_native_hook_or_api(self):
        watcher=MouseWatcher.__new__(MouseWatcher)
        watcher.events=queue.Queue()
        watcher.circle_moves=queue.Queue()
        watcher.lock=threading.Lock()
        watcher.stop=threading.Event()
        watcher.generation=4
        watcher.circle_enabled=True
        watcher.last_queued_move=float('-inf')
        watcher.circle=CircleGate()
        watcher.gate=ClickGate()
        worker=threading.Thread(target=watcher.run_circles)
        worker.start()
        try:
            for i in range(121):
                angle=i*math.pi/30
                point=(300+70*math.cos(angle),300+70*math.sin(angle))
                watcher.queue_circle(point,7,i*.03,False)
            event, value = watcher.events.get(timeout=3)
            self.assertEqual((event, value[:3]), ('mouse_scan', (4,7,'circle')))
            self.assertEqual(len(value[3]), 2)
            self.assertEqual(watcher.gate.serial,1)
        finally:
            watcher.stop.set()
            watcher.circle_moves.put(None)
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def circles(self, gate, turns=2, direction=1, buttons=False):
        triggered = []
        for i in range(60*turns+1):
            angle = direction * i * 2*math.pi/60
            point = (300+70*math.cos(angle), 300+70*math.sin(angle))
            triggered.append(gate.observe(point,7,i*.03,buttons))
        return triggered

    def test_circle_two_turns_both_directions(self):
        for direction in (-1,1):
            result = self.circles(CircleGate(),direction=direction)
            self.assertEqual(sum(result),1)
        self.assertFalse(any(self.circles(CircleGate(),turns=1)))

    def test_circles_ignore_buttons_and_tiny_or_linear_motion(self):
        self.assertFalse(any(self.circles(CircleGate(),buttons=True)))
        for shape in ('tiny','line','figure-eight'):
            gate = CircleGate()
            results = []
            for i in range(121):
                angle = i*2*math.pi/60
                if shape == 'tiny':
                    point = (300+10*math.cos(angle),300+10*math.sin(angle))
                elif shape == 'line':
                    point = (300+i,300+i)
                else:
                    point = (300+70*math.sin(angle),300+70*math.sin(2*angle))
                results.append(gate.observe(point,7,i*.03))
            self.assertFalse(any(results),shape)

    def test_circle_requires_same_window(self):
        gate = CircleGate()
        results = []
        for i in range(121):
            angle = i*2*math.pi/60
            point=(300+70*math.cos(angle),300+70*math.sin(angle))
            results.append(gate.observe(point,7 if i<60 else 8,i*.03))
        self.assertFalse(any(results))

    def click(self, gate, now, window=7, point=(100,100)):
        self.assertFalse(gate.observe('down', point, window, now))
        return gate.observe('up', point, window, now+.02)

    def test_three_or_custom_number_complete_clicks(self):
        for count in (3,4,10,20):
            gate = ClickGate(count)
            for i in range(count):
                self.assertEqual(self.click(gate, i*.1), i == count-1)
            self.assertEqual(gate.serial, 1)
            self.assertFalse(self.click(gate, count*.1))

    def test_slow_clicks_different_position_or_window_do_not_trigger(self):
        for cause in ('slow', 'position', 'window'):
            gate = ClickGate(3, 200)
            self.click(gate, 0)
            self.click(gate, .1)
            result = self.click(gate, .5 if cause=='slow' else .2,
                                8 if cause=='window' else 7,
                                (150,100) if cause=='position' else (100,100))
            self.assertFalse(result)

    def test_drag_and_unmatched_release_are_not_clicks(self):
        gate = ClickGate()
        self.click(gate, 0)
        self.click(gate, .1)
        gate.observe('down', (100,100), 7, .2)
        gate.observe('move', (120,100), 7, .21)
        self.assertFalse(gate.observe('up', (100,100), 7, .22))
        self.assertFalse(self.click(gate, .3))
        self.assertFalse(gate.observe('up', (100,100), 7, .4))

    def test_validation(self):
        for value in (2, 0, -1, 21, 'three', '3.5', True):
            with self.assertRaises(ValueError): click_number(value)
        for value in (99, 2001, 'abc'):
            with self.assertRaises(ValueError): click_interval(value)
        self.assertEqual(click_number('4'),4)

    def instance(self):
        app = App.__new__(App)
        app.settings = {**DEFAULTS, 'task_preset':'참·거짓'}
        app.mouse = Mock(generation=4)
        app.scan = Mock()
        return app

    def test_only_current_enabled_gesture_in_foreground_invokes_scan(self):
        app = self.instance()
        with patch('app.C.windll.user32.GetForegroundWindow', return_value=7):
            app.mouse_scan((3,7))
            app.mouse_scan((4,8))
            app.scan.assert_not_called()
            app.mouse_scan((4,7))
            app.scan.assert_called_once()

    def test_circle_can_trigger_when_click_feature_is_off(self):
        app = self.instance()
        app.settings.update(mouse_click_enabled=False,mouse_circle_enabled=True)
        with patch('app.C.windll.user32.GetForegroundWindow',return_value=7):
            app.mouse_scan((4,7,'circle'))
            app.scan.assert_called_once()

    def test_button_free_circle_does_not_require_foreground_activation(self):
        app=self.instance()
        with patch('app.C.windll.user32.GetForegroundWindow',return_value=88):
            app.mouse_scan((4,7,'circle'))
            app.scan.assert_called_once()
            app.settings['mouse_circle_enabled']=False
            app.mouse_scan((4,7,'circle'))
            app.scan.assert_called_once()
            app.settings['screen_enabled'] = False
            app.mouse_scan((4,7))
            app.settings.update(screen_enabled=True, mouse_click_enabled=False)
            app.mouse_scan((4,7))
            app.scan.assert_called_once()

    def test_selected_text_is_deferred_and_dropped_when_gesture_triggers(self):
        app = self.instance()
        app.root = Mock()
        app.busy, app.dialog = False, None
        app.selection = Mock(generation=4, current=(7,'problem'))
        app.selection.enabled.is_set.return_value = True
        app.mouse.selection_pause.return_value = (.2, 0, False)
        app.request_key = Mock()
        app.answer_selection((4,(7,'problem')))
        app.request_key.assert_not_called()
        callback = app.root.after.call_args.args[1]
        app.mouse.selection_pause.return_value = (.2, 1, True)
        callback()
        app.request_key.assert_not_called()

    def test_gesture_uses_screenshot_request_and_tray_answer(self):
        app = App.__new__(App)
        app.blinker = Mock()
        app.blinker.start.side_effect = lambda count, done: done(None)
        app.settings = {**DEFAULTS, 'task_preset':'참·거짓'}
        app.mouse = Mock(generation=4)
        app.root, app.tray, app.selection = Mock(), Mock(), Mock()
        app.events = queue.Queue()
        app.busy, app.dialog, app.near_popup = False, None, None
        app.request_key = Mock(return_value='fake-key')
        with patch('app.C.windll.user32.GetForegroundWindow', return_value=7), patch('app.capture_bounds', return_value=(-1920,0,0,1080)), patch('app.ImageGrab.grab', return_value=Image.new('RGB',(40,40))) as capture, patch('app.ask', return_value='FALSE') as ask:
            app.mouse_scan((4,7))
            capture.assert_not_called()
            self.assertTrue(app.busy)
            app.root.after.call_args.args[1]()
            event = app.events.get(timeout=3)
            app.events.put(event)
            app.poll()
            self.assertEqual(app.last, 'False · 2번')
            self.assertEqual(app.blinker.start.call_args.args[0], 2)
            capture.assert_called_once_with(bbox=(-1920,0,0,1080), all_screens=True)
            self.assertTrue(ask.call_args.args[1].startswith(b'\xff\xd8'))
            self.assertIsNone(ask.call_args.kwargs['text'])


if __name__ == '__main__': unittest.main()

"""Measure idle monitoring against a disposable test window; never call the API."""
import argparse
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import queue
import subprocess
import sys
import time


def fixture():
    import tkinter as tk
    root = tk.Tk()
    root.title('ScreenAnswer · 절전 성능 검사')
    root.geometry('480x160')
    text = tk.Text(root, wrap='word')
    text.pack(fill='both', expand=True)
    text.insert('1.0', 'ScreenAnswer performance fixture.\nThis is synthetic test text, without an API request.')
    text.tag_add('sel', '1.0', '1.12')
    root.update()
    user = C.windll.user32
    user.GetAncestor.argtypes = [W.HWND, W.UINT]
    user.GetAncestor.restype = W.HWND
    hwnd = user.GetAncestor(root.winfo_id(), 2)
    print(json.dumps({'hwnd': hwnd, 'editor': text.winfo_id()}), flush=True)
    root.mainloop()


def measure(source, seconds):
    sys.path.insert(0, str(source.resolve()))
    import selection
    import uiautomation as auto
    from hotkeys import Hotkey
    from mouse import MouseWatcher
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--fixture'],
                             stdout=subprocess.PIPE, text=True,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    watcher = hotkey = mouse = None
    original = selection.selected_text
    counts = {'reads': 0}
    original_keys = C.windll.user32.GetAsyncKeyState
    try:
        handles = json.loads(child.stdout.readline())
        hwnd, editor = handles['hwnd'], handles['editor']
        # Use real UIA calls against only the synthetic fixture. Fix activity
        # inputs so typing in another app cannot invalidate the comparison.
        auto.GetForegroundControl = lambda: auto.ControlFromHandle(hwnd)
        auto.GetFocusedControl = lambda: auto.ControlFromHandle(editor)
        auto.GetCursorPos = lambda: (0, 0)
        auto.ControlFromPoint = lambda *args: auto.ControlFromHandle(editor)
        selection.input_stamp = lambda: (hwnd, 1)
        C.windll.user32.GetAsyncKeyState = lambda key: 0
        def read(*args, **kwargs):
            counts['reads'] += 1
            return original(*args, **kwargs)
        selection.selected_text = read
        events = queue.Queue()
        watcher = selection.SelectionWatcher(events, delay_ms=50, browser_copy=False)
        mouse = MouseWatcher(events)
        hotkey = Hotkey(events)
        hotkey.register(None)
        watcher.set_enabled(True)
        time.sleep(1.5)  # Exclude COM and thread initialization.
        start_reads = counts['reads']
        start_cpu, start = time.process_time(), time.perf_counter()
        time.sleep(seconds)
        wall, cpu = time.perf_counter() - start, time.process_time() - start_cpu
        return {'seconds': round(wall, 3), 'cpu_seconds': round(cpu, 6),
                'cpu_one_core_percent': round(cpu / wall * 100, 3),
                'uia_reads': counts['reads'] - start_reads,
                'fixture_scoped': True, 'fixed_idle_input': True,
                'api_requests': 0}
    finally:
        selection.selected_text = original
        C.windll.user32.GetAsyncKeyState = original_keys
        if watcher is not None:
            watcher.close()
        if mouse is not None:
            mouse.close()
        if hotkey is not None:
            hotkey.commands.put(('stop',))
            hotkey.thread.join(3)
        child.terminate()
        child.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', action='store_true')
    parser.add_argument('--source', type=Path, default=Path(__file__).parent / 'src')
    parser.add_argument('--seconds', type=float, default=8)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.fixture:
        fixture()
    else:
        if not 1 <= args.seconds <= 60:
            parser.error('--seconds must be between 1 and 60')
        result = measure(args.source, args.seconds)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))

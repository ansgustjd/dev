"""Recent input and answer embedded beside the settings pages."""
import tkinter as tk
from tkinter import ttk
from hidden_input import commit_composition


class ResultPanel(ttk.LabelFrame):
    def __init__(self, parent, app):
        super().__init__(parent, text='최근 원문과 답변', padding=12)
        self.app = app
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.rowconfigure(3, weight=2)
        self.views = []
        for row, title in ((0, '전송 원문'), (2, '답변')):
            ttk.Label(self, text=title).grid(row=row, column=0, sticky='w', pady=(8, 4))
            body = ttk.Frame(self)
            body.grid(row=row+1, column=0, sticky='nsew')
            scrollbar = ttk.Scrollbar(body)
            scrollbar.pack(side='right', fill='y')
            text = tk.Text(body, wrap='word', width=30, height=8,
                           font=('Malgun Gothic', 10), padx=8, pady=8,
                           yscrollcommand=scrollbar.set)
            text.pack(fill='both', expand=True)
            scrollbar.configure(command=text.yview)
            self.views.append(text)
        self.displayed = [None, None]
        self.editing = None
        self.updating = False
        self.views[0].bind('<<Modified>>', self.remember_draft)
        self.views[0].bind('<KeyPress>', lambda _: self.app.manual_activity())
        self.views[0].bind('<Control-Return>', self.send)
        self.views[0].bind('<Control-KP_Enter>', self.send)
        self.status = ttk.Label(self, wraplength=340)
        self.status.grid(row=4, column=0, sticky='ew', pady=(8, 4))
        self.send_button = ttk.Button(self, text='전송 (Ctrl+Enter)', command=self.send)
        self.send_button.grid(row=5, column=0, sticky='e')
        self.job = None
        self.bind('<Destroy>', self.stop)
        self.bind('<Map>', self.resume)
        self.bind('<Unmap>', self.pause)
        self.refresh()

    def refresh(self):
        self.sync()
        if self.job is not None:
            self.after_cancel(self.job)
        self.job = self.after(250, self.refresh) if self.winfo_ismapped() else None

    def resume(self, event):
        if event.widget == self and self.job is None:
            self.refresh()

    def pause(self, event):
        if event.widget == self and self.job is not None:
            self.after_cancel(self.job)
            self.job = None

    def remember_draft(self, _event=None):
        source = self.views[0]
        if source.edit_modified():
            if self.editing and not self.updating:
                self.app.manual_draft = source.get('1.0', 'end-1c')
                self.app.manual_activity()
            source.edit_modified(False)

    def send(self, _event=None):
        if getattr(self.app, 'manual_mode', False):
            commit_composition(self.views[0])
            self.app.manual_draft = self.views[0].get('1.0', 'end-1c')
            self.app.submit_manual(self.app.manual_draft)
            self.sync()
        return 'break'

    def sync(self):
        editing = getattr(self.app, 'manual_mode', False)
        changed_mode = editing != self.editing
        self.editing = editing
        values = (getattr(self.app, 'manual_draft', '') if editing else getattr(self.app, 'request_source', None) or '아직 전송한 텍스트가 없습니다.',
                  getattr(self.app, 'last', '요청 오류') if getattr(self.app, 'popup_error', False)
                  else getattr(self.app, 'result_text', '아직 AI 응답이 없습니다.'))
        for index, value in enumerate(values):
            # Never replace the editor during a refresh: typing, selection,
            # undo and Korean IME composition must remain intact.
            remote_update = False
            if index == 0 and editing and not changed_mode:
                source = self.views[0]
                remote_update = source.focus_get() != source and source.get('1.0', 'end-1c') != value
                if not remote_update:
                    continue
            if value != self.displayed[index] or (index == 0 and changed_mode) or remote_update:
                text = self.views[index]
                self.updating = True
                text.configure(state='normal')
                text.delete('1.0', 'end')
                text.insert('1.0', value)
                text.edit_modified(False)
                text.configure(state='normal' if index == 0 and editing else 'disabled')
                self.displayed[index] = value
                self.updating = False
        self.configure(text='입력 모드 · 원문과 답변' if editing else '최근 원문과 답변')
        self.status.configure(text=(getattr(self.app, 'manual_status', '') or
            f"저장된 작업: {self.app.settings['task_preset']} · Ctrl+Enter로 전송") if editing else '')
        if editing:
            self.status.grid()
            self.send_button.grid()
            self.send_button.configure(state='disabled' if self.app.busy else 'normal')
        else:
            self.status.grid_remove()
            self.send_button.grid_remove()

    def stop(self, event):
        if event.widget == self and self.job is not None:
            self.after_cancel(self.job)
            self.job = None

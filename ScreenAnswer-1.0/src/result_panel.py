"""Recent input and answer embedded beside the settings pages."""
import tkinter as tk
from tkinter import ttk


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
        self.job = None
        self.bind('<Destroy>', self.stop)
        self.bind('<Map>', self.resume)
        self.bind('<Unmap>', self.pause)
        self.refresh()

    def refresh(self):
        values = (getattr(self.app, 'request_source', None) or '아직 전송한 텍스트가 없습니다.',
                  getattr(self.app, 'last', '요청 오류') if getattr(self.app, 'popup_error', False)
                  else getattr(self.app, 'result_text', '아직 AI 응답이 없습니다.'))
        for index, value in enumerate(values):
            if value != self.displayed[index]:
                text = self.views[index]
                text.configure(state='normal')
                text.delete('1.0', 'end')
                text.insert('1.0', value)
                text.configure(state='disabled')
                self.displayed[index] = value
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

    def stop(self, event):
        if event.widget == self and self.job is not None:
            self.after_cancel(self.job)
            self.job = None

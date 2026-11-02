"""Optional Windows launcher for existing Strata and Ollama model installations.

Set LOCAL_MODELS_REGISTRY to a JSON registry. No model is loaded by opening the window.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk

import psutil
import requests

REGISTRY_PATH = Path(os.environ.get('LOCAL_MODELS_REGISTRY', Path(__file__).with_name('local-models.json')))
REGISTRY = json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
ROOT = Path(REGISTRY['asset_root'])
STATE_DIR = Path(REGISTRY['state_dir'])
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE = STATE_DIR / 'server-state.json'
ENTRIES = REGISTRY['models']
MODELS = tuple(entry['label'] for entry in ENTRIES)
URLS = tuple(entry.get('url', 'http://127.0.0.1:8080/v1') for entry in ENTRIES)
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def initial_selection():
    try:
        key = json.loads(STATE.read_text())['key']
        return next(i for i, entry in enumerate(ENTRIES) if entry['key'] == key)
    except (OSError, ValueError, KeyError, StopIteration):
        return 0


class Backend:
    context = 32768

    def ensure_proxy(self):
        proxy = REGISTRY.get('proxy')
        if not proxy:
            return
        with socket.socket() as connection:
            connection.settimeout(1)
            if connection.connect_ex((proxy['host'], proxy['port'])) == 0:
                return
        with (STATE_DIR/'proxy.log').open('ab') as log:
            subprocess.Popen([proxy['python'], proxy['script']], cwd=proxy['source_root'],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, creationflags=FLAGS)

    def owned_process(self):
        try:
            state = json.loads(STATE.read_text())
            process = psutil.Process(state['pid'])
            if abs(process.create_time() - state['created']) > 0.5:
                return None
            if state['config'].lower() not in ' '.join(process.cmdline()).lower():
                return None
            return process
        except (OSError, ValueError, KeyError, psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def stop_server(self):
        process = self.owned_process()
        if process is None:
            return
        children = process.children(recursive=True)
        # Capture the owned descendants before terminating the interpreter launcher.
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
        _, alive = psutil.wait_procs([process, *children], timeout=10)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        STATE.unlink(missing_ok=True)

    def ensure_ollama(self, entry):
        try:
            requests.get('http://127.0.0.1:11434/api/tags', timeout=3).raise_for_status()
            return
        except requests.ConnectionError:
            executable = entry.get('executable')
            if not executable or not Path(executable).is_file():
                raise RuntimeError('Ollama is offline. Start the installed Ollama app, then try again.')
        with (STATE_DIR/'ollama.log').open('ab') as log:
            subprocess.Popen([executable, 'serve'], stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, creationflags=FLAGS)
        for _ in range(30):
            time.sleep(1)
            try:
                requests.get('http://127.0.0.1:11434/api/tags', timeout=2).raise_for_status()
                return
            except requests.ConnectionError:
                continue
        raise RuntimeError('Ollama did not start. See ollama.log.')

    def loaded(self):
        try:
            r = requests.get('http://127.0.0.1:11434/api/ps', timeout=3)
            r.raise_for_status()
            return r.json().get('models', [])
        except requests.ConnectionError:
            return []

    def unload(self, name):
        r = requests.post('http://127.0.0.1:11434/api/generate', json={'model':name,'keep_alive':0}, timeout=180)
        r.raise_for_status()

    def status(self, index):
        entry = ENTRIES[index]
        try:
            if entry['kind'] == 'ollama':
                model = next((m for m in self.loaded() if m['name']==entry['model_id']), None)
                return f"Ready · {model.get('context_length', 0):,} context" if model else 'Unloaded'
            if not Path(entry['config']).exists():
                return 'Not installed · model download or preparation is still in progress'
            response = requests.get(entry.get('base','http://127.0.0.1:8080')+'/health', timeout=3)
            response.raise_for_status()
            health = response.json()
            if health.get('model') != entry['model_id']:
                return 'Unloaded · another model is running'
            if health.get('loaded') is not True:
                return 'Loading / not ready'
            return f"Ready · {health['max_context']:,} context"
        except requests.ConnectionError:
            return 'Loading / not ready' if self.owned_process() else 'Offline'
        except (requests.RequestException, ValueError, KeyError):
            return 'API not responding'

    def start(self, index):
        self.ensure_proxy()
        entry = ENTRIES[index]
        if entry['kind'] == 'ollama':
            self.stop_server()
            self.ensure_ollama(entry)
            for model in self.loaded():
                if model['name'] != entry['model_id']:
                    self.unload(model['name'])
            response = requests.post('http://127.0.0.1:11434/api/generate', json={
                'model':entry['model_id'], 'stream':False, 'keep_alive':-1,
                'options':{'num_ctx':self.context}}, timeout=600)
            response.raise_for_status()
            return self.status(index)
        source_config = Path(entry['config'])
        if not source_config.exists():
            raise RuntimeError('The model download or preparation has not finished yet.')
        if self.context not in entry.get('contexts', [262144]):
            raise ValueError('This context size is not configured for the selected model.')
        try:
            active = json.loads(STATE.read_text())
        except (OSError, ValueError):
            active = {}
        if active.get('key') == entry['key'] and active.get('context') == self.context and self.owned_process():
            status = self.status(index)
            if status.startswith('Ready'):
                return status
        self.stop_server()
        base = entry.get('base','http://127.0.0.1:8080')
        try:
            requests.get(base+'/health', timeout=2)
        except requests.ConnectionError:
            pass
        else:
            raise RuntimeError('The API port is occupied by another service. It was preserved.')
        for model in self.loaded():
            self.unload(model['name'])
        config = json.loads(source_config.read_text(encoding='utf-8'))
        arguments = config['args']
        if '--max-context' in arguments:
            arguments[arguments.index('--max-context')+1] = str(self.context)
        config.update(host='127.0.0.1', port=int(base.rsplit(':',1)[1]), open_browser=False,
                      log=str(STATE_DIR/f"{entry['key']}-engine.log"))
        active_config = STATE_DIR/f"{entry['key']}-active.json"
        active_config.write_text(json.dumps(config,indent=2))
        log_path = STATE_DIR/f"{entry['key']}-server.log"
        with log_path.open('ab') as log:
            process = subprocess.Popen([entry['python'],'-u','-m','serve.server','--engine','strata',
                '--config',str(active_config),'--host','127.0.0.1','--port',str(config['port'])],
                cwd=entry['source_root'], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=FLAGS, env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONUTF8':'1'})
        owned = psutil.Process(process.pid)
        temporary = STATE.with_suffix('.new')
        temporary.write_text(json.dumps({'pid':process.pid,'created':owned.create_time(),
            'key':entry['key'],'config':str(active_config),'context':self.context}))
        temporary.replace(STATE)
        deadline = time.monotonic()+entry.get('ready_timeout',600)
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise RuntimeError(f'Model process exited ({process.returncode}). See {log_path}')
            status = self.status(index)
            if status.startswith('Ready'):
                return status
            time.sleep(2)
        raise RuntimeError(f'Model is still loading. It was preserved; see {log_path}')

    def stop(self, index):
        if ENTRIES[index]['kind'] == 'ollama':
            for model in self.loaded():
                if model['name']==ENTRIES[index]['model_id']:
                    self.unload(model['name'])
        else:
            self.stop_server()
        return self.status(index)


class App:
    def __init__(self, root, backend=None):
        self.root, self.backend = root, backend or Backend()
        self.events, self.busy, self.polling = queue.Queue(), False, False
        self.error = False
        root.title('Local Models')
        root.geometry('580x560')
        root.resizable(False, False)
        bg, panel, ink, muted, accent = '#101722', '#1a2433', '#f1f5fb', '#98abc1', '#62e4c5'
        root.configure(bg=bg)
        icon = ROOT / 'local-models.ico'
        if icon.exists():
            root.iconbitmap(str(icon))
        root.option_add('*Font', ('Segoe UI', 10))
        root.option_add('*TCombobox*Listbox.background', panel)
        root.option_add('*TCombobox*Listbox.foreground', ink)
        root.option_add('*TCombobox*Listbox.selectBackground', '#30465c')
        root.option_add('*TCombobox*Listbox.selectForeground', ink)
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('TFrame', background=bg)
        style.configure('Card.TFrame', background=panel)
        style.configure('TLabel', background=bg, foreground=ink)
        style.configure('Muted.TLabel', foreground=muted, font=('Segoe UI', 10))
        style.configure('Card.TLabel', background=panel, foreground=muted, font=('Segoe UI', 9, 'bold'))
        style.configure('TCombobox', fieldbackground='#243246', background='#243246',
                        foreground=ink, arrowcolor=accent, bordercolor='#34465d', padding=12)
        style.map('TCombobox', fieldbackground=[('readonly', '#243246'), ('disabled', panel)],
                  foreground=[('readonly', ink), ('disabled', muted)], selectbackground=[('readonly', '#243246')],
                  selectforeground=[('readonly', ink)])
        style.configure('TButton', background='#2a3a50', foreground=ink, borderwidth=0,
                        padding=(18, 12), font=('Segoe UI', 10, 'bold'), focusthickness=0)
        style.map('TButton', background=[('active', '#374e69'), ('disabled', '#202c3c')],
                  foreground=[('disabled', '#66798f')])
        style.configure('Primary.TButton', background=accent, foreground='#09251e')
        style.map('Primary.TButton', background=[('disabled', '#244e49'), ('active', '#8cefd8')],
                  foreground=[('disabled', '#779995')])
        frame = ttk.Frame(root, padding=28)
        frame.pack(fill='both', expand=True)
        header = ttk.Frame(frame)
        header.pack(fill='x')
        mark = tk.Canvas(header, width=42, height=42, bg=bg, highlightthickness=0)
        mark.pack(side='left', padx=(0, 12))
        mark.create_rectangle(5, 5, 37, 37, fill=accent, outline='')
        mark.create_text(21, 20, text='⏻', fill='#09251e', font=('Segoe UI Symbol', 23))
        if (ROOT / 'local-models.png').exists():
            from PIL import Image, ImageTk
            self.brand_icon = ImageTk.PhotoImage(Image.open(ROOT / 'local-models.png').resize((42, 42), Image.Resampling.LANCZOS))
            mark.delete('all')
            mark.create_image(21, 21, image=self.brand_icon)
        headings = ttk.Frame(header)
        headings.pack(side='left')
        ttk.Label(headings, text='Local Models', font=('Segoe UI', 23, 'bold')).pack(anchor='w')
        ttk.Label(headings, text='Your models. On your machine.', style='Muted.TLabel').pack(anchor='w', pady=(2, 0))
        card = ttk.Frame(frame, style='Card.TFrame', padding=20)
        card.pack(fill='x', pady=(24, 18))
        ttk.Label(card, text='MODEL', style='Card.TLabel').pack(anchor='w', pady=(0, 8))
        self.combo = ttk.Combobox(card, values=MODELS, state='readonly', font=('Segoe UI', 11))
        self.combo.pack(fill='x')
        selected = initial_selection()
        self.combo.current(selected)
        self.combo.bind('<<ComboboxSelected>>', self.changed)
        context_row = ttk.Frame(card, style='Card.TFrame')
        context_row.pack(fill='x', pady=(12, 0))
        ttk.Label(context_row, text='CONTEXT', style='Card.TLabel').pack(side='left')
        self.context = tk.StringVar()
        self.context_combo = ttk.Combobox(context_row, textvariable=self.context, width=12, state='readonly')
        self.context_combo.pack(side='right')
        self.update_context()
        self.status = tk.StringVar(value='Checking…')
        self.status_label = tk.Label(card, textvariable=self.status, bg=panel, fg=muted,
                                     anchor='w', justify='left', wraplength=450, font=('Segoe UI', 10))
        self.status_label.pack(fill='x', pady=(14, 0))
        self.status.trace_add('write', self.style_status)
        ttk.Label(frame, text='API ENDPOINT', style='Muted.TLabel', font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        self.url = tk.StringVar(value=URLS[selected])
        endpoint = ttk.Frame(frame)
        endpoint.pack(fill='x', pady=(8, 18))
        tk.Entry(endpoint, textvariable=self.url, state='readonly', readonlybackground=bg,
                 foreground='#b9cde4', relief='flat', bd=0, font=('Consolas', 11)).pack(side='left', fill='x', expand=True)
        self.copy_button = ttk.Button(endpoint, text='Copy API URL', command=self.copy)
        self.copy_button.pack(side='right')
        row = ttk.Frame(frame)
        row.pack(fill='x')
        self.start_button = ttk.Button(row, text='Start', style='Primary.TButton', command=lambda: self.action('start'))
        self.start_button.pack(side='left', fill='x', expand=True)
        self.stop_button = ttk.Button(row, text='Stop', command=lambda: self.action('stop'))
        self.stop_button.pack(side='left', padx=(12, 0), fill='x', expand=True)
        ttk.Label(frame, text='Start switches models. Closing the app keeps your model running.',
                  style='Muted.TLabel', font=('Segoe UI', 9)).pack(anchor='w', pady=(18, 0))
        root.after(100, self.tick)
        root.after(50, self.dark_titlebar)

    def dark_titlebar(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            enabled = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(enabled), 4)
        except (AttributeError, OSError):
            pass

    def style_status(self, *_):
        value = self.status.get()
        self.status_label.configure(fg='#62e4c5' if value.startswith('Ready') else
                                    '#ff9999' if value.startswith('Error:') else '#a4b5cb')

    def update_context(self):
        entry = ENTRIES[self.combo.current()]
        values = tuple(str(n) for n in entry.get('contexts', [262144]))
        self.context_combo.configure(values=values, state='readonly' if len(values)>1 else 'disabled')
        self.context.set(str(entry.get('default_context', int(values[0]))))

    def changed(self, _=None):
        self.update_context()
        self.error = False
        self.url.set(URLS[self.combo.current()])
        self.status.set('Checking…')

    def copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url.get())
        self.root.update_idletasks()
        self.copy_button.configure(text='Copied!')
        self.root.after(1800, lambda: self.copy_button.configure(text='Copy API URL'))

    def action(self, action):
        if self.busy:
            return
        self.busy = True
        self.error = False
        index = self.combo.current()
        self.backend.context = int(self.context.get())
        self.status.set('Loading… Strata can take several minutes.' if action == 'start' else 'Unloading…')
        for widget in (self.combo, self.context_combo, self.start_button, self.stop_button):
            widget.configure(state='disabled')
        def run():
            try:
                value = getattr(self.backend, action)(index)
            except Exception as exc:
                value = 'Error: ' + str(exc)
            self.events.put(('done', index, value))
        threading.Thread(target=run, daemon=False).start()

    def tick(self):
        while not self.events.empty():
            kind, index, value = self.events.get_nowait()
            if kind == 'done':
                self.busy = False
                self.error = value.startswith('Error:')
                self.combo.configure(state='readonly')
                self.context_combo.configure(state='readonly' if len(ENTRIES[self.combo.current()].get('contexts', []))>1 else 'disabled')
                self.start_button.configure(state='normal')
                self.stop_button.configure(state='normal')
            else:
                self.polling = False
            if index == self.combo.current() and (kind == 'done' or (not self.busy and not self.error)):
                self.status.set(value)
        if not self.busy and not self.polling:
            self.polling = True
            index = self.combo.current()
            def poll():
                self.events.put(('poll', index, self.backend.status(index)))
            threading.Thread(target=poll, daemon=True).start()
        self.root.after(2000, self.tick)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--status',action='store_true')
    parser.add_argument('--start')
    parser.add_argument('--stop',action='store_true')
    parser.add_argument('--context',type=int,default=32768)
    args=parser.parse_args()
    backend=Backend()
    backend.context=args.context
    if args.status:
        print(json.dumps({entry['key']:backend.status(i) for i,entry in enumerate(ENTRIES)}))
    elif args.start:
        index=next(i for i,entry in enumerate(ENTRIES) if entry['key']==args.start)
        print(backend.start(index),flush=True)
    elif args.stop:
        backend.stop_server()
    else:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Maxis.LocalModels')
        kernel=ctypes.windll.kernel32
        kernel.CreateMutexW.restype=ctypes.c_void_p
        mutex=kernel.CreateMutexW(None,False,'Local\\MaxisLocalModelLauncherV2')
        if kernel.GetLastError()==183:
            window=ctypes.windll.user32.FindWindowW(None,'Local Models')
            if window:
                ctypes.windll.user32.ShowWindow(window,9)
                ctypes.windll.user32.SetForegroundWindow(window)
        else:
            backend.ensure_proxy()
            root=tk.Tk()
            App(root,backend)
            root.mainloop()

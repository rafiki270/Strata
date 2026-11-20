"""Optional Windows launcher for existing Strata, Ollama and voice model installations.

Set LOCAL_MODELS_REGISTRY to a JSON registry. No model is loaded by opening the window.
"""
import argparse
from contextlib import contextmanager
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
import webbrowser

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
FLAGS = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
DEFAULT_BASE = 'http://127.0.0.1:8080'
VOICE_PORTS = {'haru': 8998, 'personaplex': 8999}
PERSONAPLEX_SERVER = Path(__file__).with_name('personaplex') / 'server.py'


def voice_port(entry):
    return int(entry.get('port', VOICE_PORTS[entry['kind']]))


def voice_url(entry):
    return f'http://127.0.0.1:{voice_port(entry)}'


def model_url(entry):
    return voice_url(entry) if entry['kind'] in VOICE_PORTS else entry.get('url', DEFAULT_BASE + '/v1')


@contextmanager
def model_change():
    # The desktop and CLI can share one registry and ownership record.
    import msvcrt

    with (STATE_DIR / 'model-change.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError('A model is already starting or stopping. Try again when it finishes.') from exc
        yield


def context_label(tokens):
    return '1M' if tokens == 1048576 else f'{tokens // 1024}K' if tokens % 1024 == 0 else str(tokens)


CONTEXT_VALUES = {context_label(n): n for entry in ENTRIES for n in entry.get('contexts', [262144])}


def served_model(base):
    """Return the model id a Strata API reports, or None when nothing answers."""
    try:
        response = requests.get(base+'/health', timeout=3)
        response.raise_for_status()
        return response.json().get('model')
    except (requests.RequestException, ValueError, AttributeError):
        return None


def initial_selection():
    # Select the model that is actually being served, even when a supervisor started it.
    for base in dict.fromkeys(entry.get('base', DEFAULT_BASE) for entry in ENTRIES if entry['kind'] == 'strata'):
        model = served_model(base)
        for index, entry in enumerate(ENTRIES):
            if entry['kind'] == 'strata' and entry.get('base', DEFAULT_BASE) == base and entry['model_id'] == model:
                return index
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

    def owned_process(self, key=None):
        try:
            state = json.loads(STATE.read_text())
            if key is not None and state['key'] != key:
                return None
            process = psutil.Process(state['pid'])
            if abs(process.create_time() - state['created']) > 0.5:
                return None
            args = process.cmdline()
            option_index = args.index(state.get('option', '--config'))
            if args[option_index + 1].casefold() != state['config'].casefold():
                return None
            return process
        except (OSError, ValueError, IndexError, KeyError, psutil.NoSuchProcess, psutil.AccessDenied):
            return None

    def stop_server(self, key=None):
        process = self.owned_process(key)
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

    def release_supervisor(self, entry):
        """Stop a model that a declared persistent supervisor serves, and keep it stopped."""
        command = entry.get('supervisor_stop')
        base = entry.get('base', DEFAULT_BASE)
        if not command or served_model(base) != entry['model_id']:
            return
        result = subprocess.run(command, cwd=entry.get('source_root'), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, creationflags=FLAGS, timeout=120)
        if result.returncode:
            raise RuntimeError('The supervisor stop command failed: '
                               + (result.stderr or result.stdout).strip()[-500:])
        # The supervisor may apply the request on its next check rather than immediately.
        deadline = time.monotonic() + entry.get('stop_timeout', 90)
        while served_model(base) == entry['model_id']:
            if time.monotonic() >= deadline:
                raise RuntimeError('Stop was requested, but the model is still running. Check the supervisor log.')
            time.sleep(2)

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
            if entry['kind'] in VOICE_PORTS:
                if not self.voice_installed(entry):
                    return 'Not installed · Voice runtime or checkpoint missing'
                if not self.owned_process(entry['key']):
                    return 'Offline'
                try:
                    response = requests.get(voice_url(entry) + '/api/ready', timeout=3)
                    response.raise_for_status()
                except requests.ConnectionError:
                    return 'Loading / not ready'
                readiness = response.json()
                return ('Ready · voice chat' if isinstance(readiness, dict) and readiness.get('ready') is True
                        else 'Loading / not ready')
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
            return 'Loading / not ready' if self.owned_process(entry['key']) else 'Offline'
        except (requests.RequestException, ValueError, KeyError):
            return 'API not responding'

    def require_api_free(self, base):
        try:
            requests.get(base+'/health', timeout=2)
        except requests.ConnectionError:
            return
        raise RuntimeError('The API port is occupied by another service. It was preserved.')

    @staticmethod
    def voice_installed(entry):
        checkpoint = Path(entry['checkpoint'])
        if not Path(entry['python']).is_file():
            return False
        if entry['kind'] == 'haru':
            return all((checkpoint / name).is_file() for name in ('config.json', 'tokenizer.json', 'model.safetensors'))
        model = checkpoint / 'Codes4Fun/personaplex-7b-v1-q4_k-GGUF'
        common = checkpoint / 'Codes4Fun/moshi-common'
        return all(path.is_file() for path in (
            PERSONAPLEX_SERVER, Path(entry['executable']), model / 'model-q4_k.gguf',
            model / 'personaplex-config.json', model / 'voices/NATF2.gguf',
            common / 'mimi-e351c8d8-125.gguf', common / 'tokenizer_spm_32k_3.model',
        ))

    @staticmethod
    def voice_command(entry):
        command = [entry['python'], '-u']
        if entry['kind'] == 'haru':
            option = '--model'
            command += ['-m', 'haru.server', '--device', 'cuda', '--host', '127.0.0.1',
                        '--network-profile', 'local', '--webrtc', 'off']
        else:
            option = '--checkpoints'
            command += [str(PERSONAPLEX_SERVER), '--executable', entry['executable'],
                        '--log', str(STATE_DIR / f"{entry['key']}-engine.log")]
            if 'lan' in entry:
                lan = entry['lan']
                command += ['--lan-ip', lan['ip'], '--lan-client', lan['client'],
                            '--certificate', lan['certificate'], '--private-key', lan['private_key']]
        return command + [option, entry['checkpoint'], '--port', str(voice_port(entry))], option

    def start_voice(self, index):
        entry = ENTRIES[index]
        if not self.voice_installed(entry):
            raise RuntimeError('The voice model needs its runtime and complete checkpoint. See tools/LOCAL_MODELS.md.')
        log_path = STATE_DIR / f"{entry['key']}-server.log"
        process = self.owned_process(entry['key'])
        if process is None:
            port = voice_port(entry)
            with socket.socket() as connection:
                connection.settimeout(1)
                if connection.connect_ex(('127.0.0.1', port)) == 0:
                    raise RuntimeError(f'Voice port {port} is occupied by another service. It was preserved.')
            self.stop_server()
            strata = [model for model in ENTRIES if model['kind'] == 'strata']
            for model in strata:
                self.release_supervisor(model)
            for base in {model.get('base', DEFAULT_BASE) for model in strata}:
                self.require_api_free(base)
            for model in self.loaded():
                self.unload(model['name'])
            command, option = self.voice_command(entry)
            process = self.spawn_server(entry, command, log_path, option, entry['checkpoint'])
        return self.wait_for_model(index, process, log_path)

    def spawn_server(self, entry, command, log_path, option, marker, context=None):
        with log_path.open('ab') as log:
            process = subprocess.Popen(command, cwd=entry.get('source_root'), stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, creationflags=FLAGS,
                                       env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'})
        try:
            owned = psutil.Process(process.pid)
            temporary = STATE.with_suffix('.new')
            temporary.write_text(json.dumps({'pid': process.pid, 'created': owned.create_time(),
                'key': entry['key'], 'config': str(marker), 'option': option, 'context': context}))
            temporary.replace(STATE)
        except (OSError, psutil.Error):
            # The launcher cannot manage a process whose ownership it couldn't record.
            process.terminate()
            raise
        return owned

    def wait_for_model(self, index, process, log_path):
        deadline = time.monotonic() + ENTRIES[index].get('ready_timeout', 600)
        while time.monotonic() < deadline:
            if not process.is_running():
                raise RuntimeError(f'Model process exited. See {log_path}')
            status = self.status(index)
            if status.startswith('Ready'):
                return status
            time.sleep(2)
        raise RuntimeError(f'Model is still loading. It was preserved; see {log_path}')

    def start(self, index):
        entry = ENTRIES[index]
        if entry['kind'] in VOICE_PORTS:
            return self.start_voice(index)
        self.ensure_proxy()
        if entry['kind'] == 'ollama':
            self.stop_server()
            for base in {e.get('base','http://127.0.0.1:8080') for e in ENTRIES if e['kind']=='strata'}:
                self.require_api_free(base)
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
        self.require_api_free(base)
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
        command = [entry['python'], '-u', '-m', 'serve.server', '--engine', 'strata',
                   '--config', str(active_config), '--host', '127.0.0.1', '--port', str(config['port'])]
        process = self.spawn_server(entry, command, log_path, '--config', active_config, self.context)
        return self.wait_for_model(index, process, log_path)

    def stop(self, index):
        entry = ENTRIES[index]
        if entry['kind'] == 'ollama':
            for model in self.loaded():
                if model['name'] == entry['model_id']:
                    self.unload(model['name'])
        else:
            self.stop_server(entry['key'])
            if entry['kind'] == 'strata':
                self.release_supervisor(entry)
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
        self.context_row = ttk.Frame(card, style='Card.TFrame')
        ttk.Label(self.context_row, text='CONTEXT', style='Card.TLabel').pack(side='left')
        self.context = tk.StringVar()
        self.context_combo = ttk.Combobox(self.context_row, textvariable=self.context, width=12, state='readonly')
        self.context_combo.pack(side='right')
        self.status = tk.StringVar(value='Checking…')
        self.status_label = tk.Label(card, textvariable=self.status, bg=panel, fg=muted,
                                     anchor='w', justify='left', wraplength=450, font=('Segoe UI', 10))
        self.status_label.pack(fill='x', pady=(14, 0))
        self.status.trace_add('write', self.style_status)
        self.endpoint_label = ttk.Label(frame, style='Muted.TLabel', font=('Segoe UI', 9, 'bold'))
        self.endpoint_label.pack(anchor='w')
        self.url = tk.StringVar()
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
        self.changed()
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
        if entry['kind'] in VOICE_PORTS:
            self.context_row.pack_forget()
            return
        self.context_row.pack(fill='x', pady=(12, 0), before=self.status_label)
        values = tuple(context_label(n) for n in entry.get('contexts', [262144]))
        self.context_combo.configure(values=values, state='readonly' if len(values)>1 else 'disabled')
        self.context.set(context_label(entry.get('default_context', entry.get('contexts', [262144])[0])))

    def changed(self, _=None):
        self.update_context()
        self.error = False
        entry = ENTRIES[self.combo.current()]
        self.url.set(model_url(entry))
        voice = entry['kind'] in VOICE_PORTS
        self.endpoint_label.configure(text='VOICE CHAT' if voice else 'API ENDPOINT')
        self.copy_button.configure(text='Open Voice Chat' if voice else 'Copy API URL',
                                   command=self.open_voice_chat if voice else self.copy,
                                   state='disabled' if voice else 'normal')
        self.status.set('Checking…')

    def open_voice_chat(self):
        webbrowser.open(self.url.get())

    def copy(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.url.get())
        self.root.update_idletasks()
        self.copy_button.configure(text='Copied!')
        self.root.after(1800, lambda: self.copy_button.configure(text='Copy API URL')
                        if ENTRIES[self.combo.current()]['kind'] not in VOICE_PORTS else None)

    def action(self, action):
        if self.busy:
            return
        self.busy = True
        self.error = False
        index = self.combo.current()
        voice = ENTRIES[index]['kind'] in VOICE_PORTS
        if voice:
            self.copy_button.configure(state='disabled')
        else:
            self.backend.context = CONTEXT_VALUES[self.context.get()]
        loading = f"Starting {ENTRIES[index]['label']}…" if voice else 'Loading… Strata can take several minutes.'
        self.status.set(loading if action == 'start' else 'Unloading…')
        for widget in (self.combo, self.context_combo, self.start_button, self.stop_button):
            widget.configure(state='disabled')
        def run():
            try:
                with model_change():
                    value = getattr(self.backend, action)(index)
            except Exception as exc:
                value = 'Error: ' + str(exc)
            self.events.put((action, index, value))
        threading.Thread(target=run, daemon=False).start()

    def tick(self):
        while not self.events.empty():
            kind, index, value = self.events.get_nowait()
            done = kind != 'poll'
            if done:
                self.busy = False
                self.error = value.startswith('Error:')
                self.combo.configure(state='readonly')
                self.context_combo.configure(state='readonly' if len(ENTRIES[self.combo.current()].get('contexts', []))>1 else 'disabled')
                self.start_button.configure(state='normal')
                self.stop_button.configure(state='normal')
            else:
                self.polling = False
            if index == self.combo.current() and (done or (not self.busy and not self.error)):
                self.status.set(value)
                if ENTRIES[index]['kind'] in VOICE_PORTS:
                    ready = value.startswith('Ready')
                    self.copy_button.configure(state='normal' if ready else 'disabled')
                    if ready and kind == 'start':
                        self.open_voice_chat()
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
        with model_change():
            print(backend.start(index),flush=True)
    elif args.stop:
        with model_change():
            backend.stop_server()
            for entry in ENTRIES:
                if entry['kind'] == 'strata':
                    backend.release_supervisor(entry)
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

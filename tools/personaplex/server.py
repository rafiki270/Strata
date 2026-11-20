"""Serve the Windows moshi.cpp PersonaPlex runtime as a live browser conversation."""

import argparse
import asyncio
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import ipaddress
import logging
import msvcrt
import os
from pathlib import Path
import ssl
import subprocess
import uuid

from aiohttp import WSMsgType, web


FRAME_BYTES = 1920 * 4  # One 80 ms frame of 24 kHz mono float32 audio.
PIPE_ACCESS_DUPLEX = 3  # Switching a pipe to blocking mode requires write access.
PIPE_NOWAIT, PIPE_REJECT_REMOTE_CLIENTS = 1, 8
ERROR_PIPE_CONNECTED, ERROR_PIPE_LISTENING = 535, 536
ASSETS = Path(__file__).with_name('web')
PROMPT = ('You are a helpful conversational assistant. Listen to the question and respond '
          'directly in short, natural sentences. Ask for clarification when needed.')
LOG = logging.getLogger(__name__)

kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, *([wintypes.DWORD] * 6), wintypes.LPVOID]
kernel.CreateNamedPipeW.restype = wintypes.HANDLE
kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
kernel.ConnectNamedPipe.restype = wintypes.BOOL
kernel.SetNamedPipeHandleState.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                           wintypes.LPVOID, wintypes.LPVOID]
kernel.SetNamedPipeHandleState.restype = wintypes.BOOL
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
kernel.CloseHandle.restype = wintypes.BOOL


class AudioPipe:
    def __init__(self, name, writing):
        self.path = rf'\\.\pipe\strata-personaplex-{name}'
        self.writing = writing
        self.handle = kernel.CreateNamedPipeW(self.path, PIPE_ACCESS_DUPLEX, PIPE_NOWAIT | PIPE_REJECT_REMOTE_CLIENTS,
                                              1, FRAME_BYTES * 4, FRAME_BYTES * 4, 0, None)
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    async def open(self, process):
        deadline = asyncio.get_running_loop().time() + 45
        while not kernel.ConnectNamedPipe(self.handle, None):
            error = ctypes.get_last_error()
            if error == ERROR_PIPE_CONNECTED:
                break
            if error != ERROR_PIPE_LISTENING:
                raise ctypes.WinError(error)
            if process.returncode is not None or asyncio.get_running_loop().time() >= deadline:
                raise RuntimeError('PersonaPlex could not open its audio stream. Check the runtime log.')
            await asyncio.sleep(0.05)
        mode = wintypes.DWORD(0)  # Use blocking audio I/O in worker threads once connected.
        if not kernel.SetNamedPipeHandleState(self.handle, ctypes.byref(mode), None, None):
            raise ctypes.WinError(ctypes.get_last_error())
        fd = msvcrt.open_osfhandle(self.handle, os.O_BINARY | (os.O_WRONLY if self.writing else os.O_RDONLY))
        self.handle = None  # The file now owns the handle.
        return os.fdopen(fd, 'wb' if self.writing else 'rb', buffering=FRAME_BYTES)

    def close(self):
        if self.handle is not None:
            kernel.CloseHandle(self.handle)


@contextmanager
def audio_pipe(name, writing):
    pipe = AudioPipe(name, writing)
    try:
        yield pipe
    finally:
        pipe.close()


async def receive_audio(ws, capture):
    def write_frame(data):
        capture.write(data)
        capture.flush()

    async for message in ws:
        if message.type == WSMsgType.BINARY and len(message.data) == FRAME_BYTES:
            await asyncio.to_thread(write_frame, message.data)
        elif message.type == WSMsgType.BINARY:
            await ws.close(code=1003, message=b'Expected 80 ms of 24 kHz float32 audio')
            return


async def send_audio(ws, speaker):
    while len(frame := await asyncio.to_thread(speaker.read, FRAME_BYTES)) == FRAME_BYTES:
        await ws.send_bytes(frame)


async def send_transcript(ws, stdout):
    while line := await stdout.readline():
        if line.rstrip() == b'ready':
            break
    while text := await stdout.read(1024):
        await ws.send_json({'text': text.decode('utf-8', errors='replace')})


async def conversation(ws, options):
    key = uuid.uuid4().hex
    with audio_pipe(f'{key}-in', writing=True) as input_pipe, \
         audio_pipe(f'{key}-out', writing=False) as output_pipe, \
         options.log.open('ab') as log:
        executable = options.executable
        process = await asyncio.create_subprocess_exec(
            str(executable), '-r', str(options.checkpoints), '-d', 'CUDA0', '-c', '1024',
            '-v', 'NATF2', '-p', PROMPT, cwd=executable.parent, stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, 'SDL_AUDIODRIVER': 'disk', 'SDL_DISKAUDIOFILEIN': input_pipe.path,
                 'SDL_DISKAUDIOFILE': output_pipe.path},
        )
        capture = speaker = None
        workers = []
        try:
            capture = await input_pipe.open(process)
            speaker = await output_pipe.open(process)
            await ws.send_json({'status': 'Listening'})
            workers = [asyncio.create_task(receive_audio(ws, capture)),
                       asyncio.create_task(send_audio(ws, speaker)),
                       asyncio.create_task(send_transcript(ws, process.stdout))]
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for worker in done:
                worker.result()
            if not ws.closed and process.returncode is not None:
                raise RuntimeError('PersonaPlex stopped during the conversation. Check the runtime log.')
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            await process.wait()
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for stream in (capture, speaker):
                if stream is not None:
                    stream.close()


def make_app(options):
    active = asyncio.Lock()

    @web.middleware
    async def restrict_lan(request, handler):
        if request.remote not in ('127.0.0.1', options.lan_client):
            raise web.HTTPForbidden()
        return await handler(request)

    async def index(_request):
        return web.FileResponse(ASSETS / 'index.html')

    async def ready(_request):
        return web.json_response({'ready': True})

    async def chat(request):
        origin = request.headers.get('Origin')
        if origin and origin != f'{request.scheme}://{request.host}':
            raise web.HTTPForbidden()
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=FRAME_BYTES * 2)
        await ws.prepare(request)
        if active.locked():
            await ws.send_json({'error': 'PersonaPlex is already in a conversation.'})
            await ws.close()
            return ws
        async with active:
            try:
                await conversation(ws, options)
            except (OSError, RuntimeError) as exc:
                LOG.warning('PersonaPlex conversation ended: %s', exc)
                if not ws.closed:
                    await ws.send_json({'error': 'The voice runtime stopped. Try starting again.'})
        await ws.close()
        return ws

    app = web.Application(middlewares=[restrict_lan])
    app.router.add_get('/', index)
    app.router.add_get('/api/ready', ready)
    app.router.add_get('/api/chat', chat)
    app.router.add_static('/assets', ASSETS)
    return app


async def serve(options):
    runner = web.AppRunner(make_app(options), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, '127.0.0.1', options.port).start()
        if options.lan_ip:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(options.certificate, options.private_key)
            await web.TCPSite(runner, options.lan_ip, options.port, ssl_context=tls).start()
        await asyncio.Future()
    finally:
        await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--checkpoints', type=Path, required=True)
    parser.add_argument('--log', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8999)
    parser.add_argument('--lan-ip', type=lambda value: str(ipaddress.IPv4Address(value)))
    parser.add_argument('--lan-client', type=lambda value: str(ipaddress.IPv4Address(value)))
    parser.add_argument('--certificate', type=Path)
    parser.add_argument('--private-key', type=Path)
    options = parser.parse_args()
    tls_options = (options.lan_ip, options.lan_client, options.certificate, options.private_key)
    if any(tls_options) and not all(tls_options):
        parser.error('LAN access needs both IP addresses, the certificate and its private key.')
    asyncio.run(serve(options))


if __name__ == '__main__':
    main()

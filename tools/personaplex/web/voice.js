const toggle = document.querySelector('#toggle');
const status = document.querySelector('#status');
const transcript = document.querySelector('#transcript');
let active = null;

function close(session, message) {
  if (active !== session) return;
  active = null;
  session.socket?.close();
  session.stream?.getTracks().forEach(track => track.stop());
  session.context.close();
  toggle.disabled = false;
  toggle.textContent = 'Start conversation';
  status.textContent = message;
}

async function start() {
  if (!navigator.mediaDevices?.getUserMedia) {
    status.textContent = 'Open this page over HTTPS to use your microphone.';
    return;
  }
  const session = { context: new AudioContext({ sampleRate: 24000 }), listening: false };
  const { context } = session;
  active = session;
  transcript.textContent = '';
  toggle.textContent = 'End conversation';
  status.textContent = 'Waiting for microphone access…';
  try {
    const resume = context.resume();
    resume.catch(() => {});
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true } });
    if (active !== session) {
      stream.getTracks().forEach(track => track.stop());
      return;
    }
    session.stream = stream;
    status.textContent = 'Starting audio…';
    await resume;
    await context.audioWorklet.addModule('/assets/duplex-audio.js');
    if (active !== session) return;
    const audio = new AudioWorkletNode(context, 'duplex-audio', { outputChannelCount: [1] });
    context.createMediaStreamSource(session.stream).connect(audio).connect(context.destination);
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${location.host}/api/chat`);
    socket.binaryType = 'arraybuffer';
    session.socket = socket;
    status.textContent = 'Loading PersonaPlex…';

    audio.port.onmessage = ({ data }) => {
      if (session.listening && socket.readyState === WebSocket.OPEN && socket.bufferedAmount < 30720) {
        socket.send(data);
      }
    };
    socket.onmessage = ({ data }) => {
      if (typeof data !== 'string') {
        audio.port.postMessage(data, [data]);
        return;
      }
      const update = JSON.parse(data);
      if (update.error) close(session, update.error);
      if (update.status) {
        session.listening = true;
        status.textContent = `${update.status} · Speak naturally`;
      }
      if (update.text) transcript.textContent += update.text;
    };
    socket.onerror = () => close(session, 'Could not connect to PersonaPlex. Try again.');
    socket.onclose = () => close(session, 'Conversation ended.');
  } catch (error) {
    close(session, error.name === 'NotAllowedError'
      ? 'Allow microphone access and try again.' : 'Could not start audio. Check your microphone and try again.');
  }
}

toggle.addEventListener('click', () => {
  if (active) close(active, 'Conversation ended.');
  else start();
});

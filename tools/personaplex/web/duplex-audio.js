const frameSamples = 1920;

class DuplexAudio extends AudioWorkletProcessor {
  constructor() {
    super();
    this.capture = new Float32Array(frameSamples);
    this.captured = 0;
    this.playback = [];
    this.playing = null;
    this.played = 0;
    this.port.onmessage = ({ data }) => {
      if (this.playback.length >= 4) this.playback.shift();
      this.playback.push(new Float32Array(data));
    };
  }

  process(inputs, outputs) {
    const microphone = inputs[0]?.[0];
    const speaker = outputs[0][0];
    if (microphone) {
      for (const sample of microphone) {
        this.capture[this.captured++] = sample;
        if (this.captured === frameSamples) {
          this.port.postMessage(this.capture.buffer, [this.capture.buffer]);
          this.capture = new Float32Array(frameSamples);
          this.captured = 0;
        }
      }
    }
    for (let i = 0; i < speaker.length; i++) {
      if (!this.playing && this.playback.length >= 2) this.playing = this.playback.shift();
      if (!this.playing) break;
      speaker[i] = this.playing[this.played++];
      if (this.played === frameSamples) {
        this.playing = this.playback.shift();
        this.played = 0;
      }
    }
    return true;
  }
}

registerProcessor('duplex-audio', DuplexAudio);

# Local Models launcher and benchmark

`local_model_launcher.py` is an optional Windows launcher for existing Strata configurations, installed Ollama models and Haru voice chat. Opening the window loads no model and selects the Strata model the API is serving, if it is in the registry. Starting a model unloads Ollama models and can stop the server recorded by this launcher after checking its creation time and command. An occupied API port is preserved.

A Strata model kept online by a persistent supervisor is not recorded by this launcher. Give its entry a `supervisor_stop` argument list: the supervisor's own stop command, which must also pause its recovery. It runs from `source_root`. Stop runs it only while the API reports that entry's `model_id`, then waits up to `stop_timeout` seconds (default 90) for the model to unload, and reports an error if it is still running. Entries without `supervisor_stop` keep the earlier behaviour: Stop affects only the launcher's own server.

Set `LOCAL_MODELS_REGISTRY` to a private JSON file (or place `local-models.json` beside the script). It contains `asset_root` for existing icons, `state_dir` for generated configurations/logs, and a `models` array. Each entry has `key`, `label`, `kind` (`strata` or `ollama`), `model_id`, `contexts` (integers), and `default_context`. Strata entries also have `config` (an existing setup-generated JSON), `python`, and `source_root`. Optional fields are `base` (default `http://127.0.0.1:8080`), `url`, and `ready_timeout`. An Ollama entry may set `executable` to start its existing service when offline. An optional `proxy` object (`host`, `port`, `python`, `script`, `source_root`) preserves an already configured API proxy. Omit it for local-only use. Keep all paths local. Do not put credentials in the registry.

To use the same address after switching Strata text models, set `"strata_port": 18080` at the top of the registry. This setting covers new entries and overrides saved Strata addresses. Set the same port in any Strata Terminal profile you also use. Ollama and voice services keep their own addresses.

Point your client at `http://127.0.0.1:18080/v1`. Run `curl.exe -fsS http://127.0.0.1:18080/health` to see the current model ID in `model` and confirm `loaded` is true. If you have configured LAN access, use the serving computer's hostname in place of `127.0.0.1`. After unloading a model for another task, the next model you load uses the same address.

The context selector lists configured sizes. A listed size does not establish that it fits the GPU or retrieves reliably; validate each profile before adding it. The launcher reports the loaded model and context returned by the API. A missing configuration remains unavailable while its download/preparation finishes.

## Haru-4B voice chat

On Windows with an NVIDIA GPU, install [Haru](https://github.com/higurashi-ai/Haru) in its own Python environment. The Maxis installation uses Python 3.13, PyTorch 2.9.1 with CUDA 12.8, `haru-speech==0.1.1`, and `transformers==5.16.0`.

```powershell
python -m venv .venv-haru
$haruPython = Resolve-Path .venv-haru\Scripts\python.exe
& $haruPython -m pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cu128
& $haruPython -m pip install haru-speech==0.1.1 transformers==5.16.0
& .\.venv-haru\Scripts\hf.exe download higurashi-ai/Haru-4B config.json tokenizer.json model.safetensors --revision 8d6e83bcd3701a76fc27a764965665da1df7da78 --local-dir Haru-4B
```

Add a `haru` entry to the registry with `key`, `label`, `kind`, `python` (absolute path to the Haru environment's `python.exe`), and `checkpoint` (absolute path to the downloaded directory). The optional `port` defaults to 8998. Haru listens on `127.0.0.1`. It never uses the launcher API proxy. Start switches away from any launcher-owned model. It also requests a supervised Strata model to stop when its registry entry declares `supervisor_stop`.

Select **Haru-4B** and click **Start**. When Haru is ready, the launcher opens voice chat in the default browser. Choose a character and a reference clip, then click **Start conversation**. To provide a default English reference, place a clean speech clip you have permission to use at `Lib/site-packages/haru/assets/voices/en.flac` inside the Haru environment. The reference determines how Haru sounds. Install `ffmpeg` on `PATH` if you plan to record reference clips in the browser. **Open Voice Chat** reopens a running session's setup page. **Stop** ends chat and releases GPU memory.

Headless commands use the same lifecycle implementation:

```powershell
python tools/local_model_launcher.py --status
python tools/local_model_launcher.py --start deepseek --context 32768
python tools/local_model_launcher.py --stop
python -m unittest discover -s tools -p test_local_model_launcher.py
```

`local-model-benchmark.py` sends fresh text through the real API, counts it with the model tokenizer, requests 512 output tokens with thinking disabled, and saves every response and timing. Use actual `usage.prompt_tokens` and engine `timings`: token-count estimates and chat-template modes can differ. `prompt_ms` and `prompt_per_second` measure newly processed input; `predicted_ms` and `predicted_per_second` measure generation. Time to first token and full request time are measured separately by the client. Cache reuse above 16 tokens marks a run invalid. Verify output separately: throughput does not measure answer quality.

```powershell
python tools/local-model-benchmark.py --url http://127.0.0.1:8080 --label deepseek-2bit --prompt 32768 --output 512 --runs 3 --out build-local/results
```

For context-capacity tests, reserve at least 512 output positions: 261632 input for a 262144 window, 523776 for 524288, and 1048064 for 1048576. Record failed starts, allocation errors, timeouts and actual input counts. An empty context allocation is not a successful full-context benchmark.

For Avalon use `--kind avalon` and its `/apply-template` and `/tokenize` APIs. Record the exact runtime revision, model quantization, draft model, KV precision, context and storage hardware for comparisons.

Add `--seed-base NAME` to use repeatable archives across engines. Every new result stores its seed and exact request payload. Add `--retrieval` to insert random audit codes at the beginning, middle and end and check that the answer repeats all three.

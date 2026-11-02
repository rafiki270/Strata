# Local Models launcher and benchmark

`local_model_launcher.py` is an optional Windows launcher for existing Strata configurations and installed Ollama models. Opening the window loads no model. Starting a model unloads Ollama models and stops only the server recorded by this launcher, after checking its creation time and command. An occupied API port is preserved.

Set `LOCAL_MODELS_REGISTRY` to a private JSON file (or place `local-models.json` beside the script). It contains `asset_root` for existing icons, `state_dir` for generated configurations/logs, and a `models` array. Each entry has `key`, `label`, `kind` (`strata` or `ollama`), `model_id`, `contexts` (integers), and `default_context`. Strata entries also have `config` (an existing setup-generated JSON), `python`, and `source_root`. Optional fields are `base` (default `http://127.0.0.1:8080`), `url`, and `ready_timeout`. Keep all paths local. Do not put credentials in the registry.

The context selector lists configured sizes. A listed size does not establish that it fits the GPU or retrieves reliably; validate each profile before adding it. The launcher reports the loaded model and context returned by the API. A missing configuration remains unavailable while its download/preparation finishes.

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

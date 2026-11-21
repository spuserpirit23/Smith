# Local models: LLM, vision and TTS on the server itself

The default stack has no per-use AI bill. The server runs its own models:

| Stage | Default | Runs where | Swap via |
|---|---|---|---|
| LLM | **Gemma 3 4B** (`gemma3:4b`, Ollama), the same model as vision | `ollama` container, CPU | `LLM_MODEL` |
| Vision ("what do you see?") | **Gemma 3 4B** multimodal (`gemma3:4b`, Ollama) | same `ollama` container | `VISION_MODEL`, or `MITRA_VISION_PROVIDER=none` |
| TTS | **Piper** (`en_US-lessac-medium` + `hi_IN-pratham-medium`) | inside the `api` process, CPU | `PIPER_MODEL`, `PIPER_MODEL_HI` |
| STT | Google Web Speech (free, no key; not local), or local Whisper | Google / `tools/whisper_server.py` | see "STT" below |

Hosted providers (Bedrock, Polly, Gemini, OpenAI...) are still there. They're config only, see
`backend/config/mitra.yaml`.

## How it fits together

* `deploy/docker-compose.yml` adds an `ollama` service (internal network only, never published)
  and a one-shot `ollama-pull` job. On first start the job pulls `LLM_MODEL` and `VISION_MODEL` into
  the `ollama_models` volume, then the API starts. Models stay loaded (`OLLAMA_KEEP_ALIVE=-1`).
* The backend talks to Ollama through the existing `openai_compat` LLM and vision providers
  (`http://ollama:11434/v1`). Any OpenAI-compatible server works the same way: llama.cpp
  `llama-server`, vLLM, LM Studio.
* On startup the backend sends one warm-up request with the real system prompt (`Runtime._warm_up_llm`).
  That loads the model and fills Ollama's prompt cache, so the first visitor doesn't pay a cold
  load (5 s for 1.7B, ~40 s for 4B on CPU, which is longer than the 10 s LLM timeout).
* Piper voices are downloaded into the image at build time (`Dockerfile`, `PIPER_VOICES`). Each
  sentence is routed by script: Devanagari goes to the Hindi voice, everything else to the English one.

## Benchmarks (CPU only)

Measured 9–10 Nov 2026 on an Intel i7-13620H laptop (10 cores / 16 threads, 16 GB, no GPU),
Ollama 0.32.11, Q4_K_M weights. The real Mitra system prompt (~460 tokens) was sent through the
production `OpenAICompatLLM` class. "First sentence" is when the first complete sentence is ready
for TTS, which is what the listener actually waits for. Script:
`python bench_llm.py <models> --effort=none` (kept outside the repo).

| Model | RAM | First sentence, 1st turn | First sentence, later turns | Quality notes |
|---|---|---|---|---|
| `gemma3:1b` | ~0.9 GB | 2.7–6.1 s | 0.85–1.1 s | Weak: forgot the user's name 2 turns later, garbled questions, emojis |
| **`qwen3:1.7b`** (thinking off) | ~1.4 GB | **0.4–1.1 s** | **0.4–0.8 s** | Good tone, kept the name. Some factual slips, weak Hinglish |
| `qwen3:4b` | 2.5 GB | — | — | **Unusable**: thinking-only build, ~30 s of reasoning per turn, read out loud (see below) |
| `qwen3:4b-instruct` | 2.5 GB | _pending_ | _pending_ | _pending_ |
| `gemma3:4b` | ~3.3 GB | _pending_ | _pending_ | _pending_ |
| `lfm3` | — | — | — | Tag exists but needs a newer Ollama than 0.32.11 (registry HTTP 412) |

Piper on the same CPU: first audio 0.04–0.12 s, ~30× faster than real time.

Why Gemma's first turn is slow: generation runs at 30–47 tok/s on all of these, so the cost is
evaluating the ~460-token system prompt (2.5–5 s uncached, 60 ms cached). Ollama reuses the cached
prefix across Qwen3 requests, even for different questions. With Gemma 3 (sliding-window
attention) it only reused the cache within one growing conversation.

## Picking a model

* **Thinking models must have thinking off.** `LLM_REASONING_EFFORT=none` does this for hybrid
  models (Qwen3 1.7B/8B, original release). Non-thinking models accept and ignore it.
* **Never use a thinking-only build** (`qwen3:4b` today, any `*-thinking-2507`). Their chat
  template opens `<think>` itself, so the reasoning streams as ordinary text with only a closing
  `</think>`. The robot would read its reasoning aloud before answering. The backend strips
  `<think>…</think>`, logs an error when it sees a stray `</think>`, and the boot-time warm-up
  triggers that error, so check the logs after changing models. Use `qwen3:4b-instruct` instead.
* Bigger models sound better but every extra billion parameters costs first-turn latency on CPU.
  Keep `conversation.timeouts.llm_s` (10 s to first token) in mind.
* One multimodal model (e.g. `gemma3:4b`) can do both chat and vision. Set `LLM_MODEL` and
  `VISION_MODEL` to the same tag to keep only one model in RAM.

## Server sizing

RAM: `api` + Postgres + Mosquitto + Caddy ≈ 1.3 GB, plus the models (table above) plus ~0.5 GB KV
cache per model at a 4k context. CPU: prompt evaluation scales with cores, and a burstable
(t-series) instance runs out of CPU credits under sustained inference.

| Setup | Fits on |
|---|---|
| gemma3:4b for chat + vision (default, one model in RAM) | 4 vCPU / 8–16 GB, e.g. AWS `m7g.xlarge` (Terraform default) |
| qwen3:1.7b chat + gemma3:4b vision | 4 vCPU / 16 GB, e.g. `m7g.xlarge` |
| qwen3:1.7b, vision off | 4 vCPU / 8 GB, e.g. `c7g.xlarge` |
| Any of the above, a GPU box | First-turn latency drops below 0.5 s; Ollama uses the GPU automatically |

These numbers come from a 16-thread laptop. A 4-vCPU server is slower at prompt evaluation, so
benchmark on the real instance before a demo:

```bash
docker compose exec ollama ollama run gemma3:4b --verbose "Hi"   # prints prompt/eval tokens per second
```

## Running locally (Windows / macOS / Linux)

```powershell
# 1. Install Ollama (https://ollama.com/download), then:
ollama pull gemma3:4b      # chat + vision (qwen3:1.7b is the faster, lighter chat option)
# 2. Piper voices (put the paths in backend/.env as PIPER_MODEL / PIPER_MODEL_HI)
#    https://huggingface.co/rhasspy/piper-voices  en/en_US/lessac/medium, hi/hi_IN/pratham/medium
pip install -e ".[local,speech,pc]"
# 3. backend/.env
#    MITRA_LLM_PROVIDER=openai_compat  LLM_BASE_URL=http://localhost:11434/v1  LLM_MODEL=gemma3:4b
#    LLM_REASONING_EFFORT=none  MITRA_VISION_PROVIDER=openai_compat  VISION_BASE_URL=http://localhost:11434/v1
#    VISION_MODEL=gemma3:4b  MITRA_TTS_PROVIDERS=piper_local
mitra check-providers --image ../pc/check_camera.jpg     # one LLM + one vision call
```

## STT

The default `google` provider is free (Google Web Speech, no key) but it's an online, unofficial
endpoint with no SLA. For offline speech recognition, run a Whisper server with an
OpenAI-compatible `/audio/transcriptions` endpoint and point the `openai_compat` STT provider at
it. No backend code change is needed.

### Local Whisper on a PC (`tools/whisper_server.py`)

A small faster-whisper server (CPU, int8) with the endpoint the backend expects. It needs its own
venv; faster-whisper's CTranslate2 wheels are most reliable on Python 3.11.

```powershell
uv venv --python 3.11 $env:LOCALAPPDATA/mitra/whisper-venv
uv pip install --python $env:LOCALAPPDATA/mitra/whisper-venv/Scripts/python.exe `
    faster-whisper fastapi "uvicorn[standard]" python-multipart
& $env:LOCALAPPDATA/mitra/whisper-venv/Scripts/python.exe tools/whisper_server.py   # port 8000
```

`backend/.env`:

```
MITRA_STT_PROVIDER=openai_compat
STT_BASE_URL=http://localhost:8000/v1
STT_MODEL=small
```

The first start downloads the model from Hugging Face (`small` is ~480 MB; about 3.5 min on a
home connection), then it's cached. Choose another size with `WHISPER_MODEL`
(`tiny|base|small|medium|distil-large-v3`); the backend's `STT_MODEL` is only a label.

Measured 12 Nov 2026 on the i7-13620H laptop, `small`, int8, greedy decoding: a 3.3 s sentence
took 1.4 s (warm) and came back word-perfect except "a" → "the", confidence 0.84. Budget ~1 GB RAM.

Notes:
* The backend sends `language` from `persona.language` (`en-IN` → `en`), so Whisper transcribes as
  English. Hindi speech comes back transliterated or translated, not in Devanagari.
* The server has no auth and binds to `127.0.0.1`. Don't expose it.
* With Docker, [speaches](https://github.com/speaches-ai/speaches) is an alternative with the
  same endpoint.

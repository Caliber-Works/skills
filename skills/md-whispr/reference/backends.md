# Backends

## Why not LM Studio

LM Studio's OpenAI-compatible API supports exactly five endpoints:

| Endpoint | Method |
|---|---|
| `/v1/models` | GET |
| `/v1/responses` | POST |
| `/v1/chat/completions` | POST |
| `/v1/embeddings` | POST |
| `/v1/completions` | POST |

There is no `/v1/audio/speech` and no `/v1/audio/voices`. GGUF TTS models
(Orpheus-3B and friends) will download and load in LM Studio, which makes it look
like TTS should work — but the audio endpoints return errors. This is a tracked
feature request in `lmstudio-ai/lmstudio-bug-tracker`, not a shipped capability.

If that changes, md-whispr needs no code change:

```bash
export MD_WHISPR_TTS_URL=http://localhost:1234/v1/audio/speech
export MD_WHISPR_MODEL=<whatever LM Studio calls the model>
```

## Default: mlx-audio

Apple MLX-native, so it uses the M-series GPU directly rather than going through
PyTorch MPS. Ships an OpenAI-compatible FastAPI server.

```bash
pip install "mlx-audio[server]" misaki
mlx_audio.server --port 8000
```

Optional language packs: `pip install misaki[ja]`, `pip install misaki[zh]`.

Request shape md-whispr sends:

```json
{
  "model": "mlx-community/Kokoro-82M-bf16",
  "input": "...",
  "voice": "af_heart",
  "speed": 1.15,
  "lang_code": "a",
  "response_format": "wav",
  "stream": false
}
```

Useful server flags:

- `--start-ui` — launches the Studio web UI on :3000 alongside the API
- `--tts-max-batch-size 8` — continuous batching for concurrent requests
- `--host 0.0.0.0` — reachable from other devices on the network

Keeping the server running as a background service means zero warm-up on the
first read of the day. Otherwise the first request pays the model-load cost
(a few seconds) once per server start.

## Model choices

| Model | When |
|---|---|
| `mlx-community/Kokoro-82M-bf16` | **Default.** 82M params, ~100x realtime, 54 voices, tiny memory footprint. The right call for long-document narration. |
| Chatterbox | Expressive English + voice cloning, MIT license. Heavier; overkill for docs. |
| Qwen3-TTS | Broad multilingual + cloning. Heavier. |
| CSM / Dia | Conversational, multi-speaker. Not built for long-form narration. |

Kokoro wins for this use case specifically because time-to-first-audio dominates
the experience when you're reading a 30-minute document, and 82M params on MLX is
about as fast as local TTS gets on an M4.

## Fallback: macOS `say`

If the server is unreachable and `--engine auto` is in play, md-whispr falls back
to the built-in `say` command so you're never fully blocked. It is noticeably more
robotic than Kokoro. `doctor` reports which engine is live, and `status` shows it
per-session — check there if a read sounds wrong.

Force one or the other with `--engine mlx` or `--engine say`.

## Environment variables

| Variable | Default |
|---|---|
| `MD_WHISPR_TTS_URL` | `http://localhost:8000/v1/audio/speech` |
| `MD_WHISPR_MODEL` | `mlx-community/Kokoro-82M-bf16` |
| `MD_WHISPR_VOICE` | `af_heart` |
| `MD_WHISPR_SPEED` | `1.15` |
| `MD_WHISPR_LANG` | `a` |
| `MD_WHISPR_HOME` | `~/.md-whispr` |
| `MD_WHISPR_CACHE_MB` | `512` |

State lives in `MD_WHISPR_HOME`: `cache/` (rendered audio), `state.json` (current
session), `bookmarks.json` (per-file resume points), `whispr.log`.

## Other OpenAI-compatible servers

Anything implementing `POST /v1/audio/speech` with the OpenAI request shape works
— Kokoro-FastAPI, a local vLLM audio server, or a hosted endpoint. Point
`MD_WHISPR_TTS_URL` at it and set `MD_WHISPR_MODEL` to whatever that server calls
the model. Auth, if needed, goes in the `Authorization: Bearer` header, which
md-whispr already sends (currently hardcoded to `not-needed`; edit
`_synth_http` in `scripts/md_whispr.py` if you need a real key).

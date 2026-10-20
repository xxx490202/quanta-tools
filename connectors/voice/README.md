# Quanta Voice connector (Rhema)

Streaming speech sidecar for Quanta's voice page.

- `rhema.mjs --serve` — VAD + streaming ASR over stdin/stdout
- `asr.mjs`, `asr-models.mjs`, `tts.mjs` — ASR/TTS helpers
- `mlx_asr.py` — MLX-based ASR worker (Apple Silicon)

## Requirements

- Node.js 18+
- Python 3.10+ with `mlx-audio` and `huggingface_hub` for the MLX ASR path

## Run

```bash
node rhema.mjs --serve
```

Part of the [Quanta](https://github.com/xxx490202/quanta) ecosystem.

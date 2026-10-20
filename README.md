# Quanta Tools

MCP servers, connectors, and skills built for [Quanta](https://github.com/xxx490202/quanta) — the screen-analysis and desktop-agent app.

## MCP servers

| Server | What it does | Folder | Download |
| --- | --- | --- | --- |
| `quanta-ask` | Ask the user a question through Quanta's selection-card UI | [`mcp/quanta-ask`](mcp/quanta-ask) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-ask.zip) |
| `quanta-browser` | Control Quanta's built-in browser | [`mcp/quanta-browser`](mcp/quanta-browser) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-browser.zip) |
| `quanta-memory` | Read and write Quanta's local memory store | [`mcp/quanta-memory`](mcp/quanta-memory) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-memory.zip) |
| `quanta-schedule` | Manage scheduled tasks and recurring runs | [`mcp/quanta-schedule`](mcp/quanta-schedule) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-schedule.zip) |
| `quanta-harness` | Ask Claude Code, Codex, or DeepSeek as consultants | [`mcp/quanta-harness`](mcp/quanta-harness) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-harness.zip) |
| `quanta-cua` | Desktop computer use via `@trycua/cua-driver` | [`mcp/quanta-cua`](mcp/quanta-cua) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-cua.zip) |
| `quanta-screen` | Screenshot-only fallback when the CUA driver is unavailable | [`mcp/quanta-screen`](mcp/quanta-screen) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-screen.zip) |
| `quantification-materials` | List/read project materials and run jev judgments | [`mcp/quanta-materials`](mcp/quanta-materials) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-materials.zip) |
| `quantification-pages` | Read outputs from other Quanta pages | [`mcp/quanta-pages`](mcp/quanta-pages) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-pages.zip) |

## Connectors

| Connector | What it does | Folder | Download |
| --- | --- | --- | --- |
| WeChat | Personal WeChat account ↔ Quanta agent | [`connectors/wechat`](connectors/wechat) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-connector-wechat.zip) |
| Voice (Rhema) | Streaming VAD / ASR / TTS sidecar | [`connectors/voice`](connectors/voice) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/quanta-connector-voice.zip) |

## Skills

| Skill | What it does | Folder | Download |
| --- | --- | --- | --- |
| `agnes-image` | Generate/edit images through the Agnes Image API | [`skills/agnes-image`](skills/agnes-image) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/agnes-image-skill.zip) |
| `agnes-video` | Generate videos through the Agnes Video API | [`skills/agnes-video`](skills/agnes-video) | [zip](https://github.com/xxx490202/quanta-tools/releases/download/v1.0.0/agnes-video-skill.zip) |

## Notes

- **No API keys are included.** The skills ship with `.env.example`; copy it to `.env` and fill in `AGNES_API_KEY`.
- The Python MCP servers import Quanta's `config` module and read the app data directory. They are intended to run alongside a [Quanta](https://github.com/xxx490202/quanta) source checkout; see each folder's README.
- `quanta-cua` requires `@trycua/cua-driver` and the `QUANTA_DATA_DIR` / `QUANTA_CUA_ROOT` environment variables.
- The WeChat connector needs `npm install`; the voice connector's MLX ASR path needs `mlx-audio` and `huggingface_hub` on Apple Silicon.
- Each component is also published as an individual zip in the [v1.0.0 release](https://github.com/xxx490202/quanta-tools/releases/tag/v1.0.0).

## Layout

```text
quanta-tools/
├── mcp/            # 9 MCP servers
├── connectors/     # WeChat and voice connectors
└── skills/         # agnes-image and agnes-video
```

# agnes-video skill

Generates videos through the Agnes Video API, including text-to-video, first/last-frame control, and image/audio reference generation. The skill is defined in `SKILL.md`; `scripts/generate_video.py` is the CLI and `references/api_docs.md` documents the API.

## Setup

```bash
cp .env.example .env
# edit .env and set AGNES_API_KEY
```

The script also accepts `--api-key` or the `AGNES_API_KEY` environment variable.

Part of the [Quanta](https://github.com/xxx490202/quanta) ecosystem.

---
name: agnes-video
description: "This skill should be used when the user asks to generate videos via the Agnes Video API (model `agnes-video-2.5-flash`), including text-to-video, first/last frame (keyframe) control, and image/audio reference generation. Trigger words: 生成视频, 文生视频, 首尾帧, 图生视频, 视频参考, keyframe, Agnes video, make a video."
agent_created: true
---

# Agnes Video

## Overview

This skill generates videos via the Agnes Video 2.5 Flash API using an async
create-task + poll workflow (`POST /v1/videos` then poll until `completed`, then
download the mp4). The bundled CLI script handles task creation, 1–2s polling,
and download, so prefer running the script directly instead of hand-writing
curl sequences.

## Prerequisites

- API key: resolved as `--api-key` flag > `AGNES_API_KEY` environment variable >
  `.env` in this skill directory (copy `.env.example` to `.env` and fill in your key). If none is available, ask
  the user.
- Python 3.9+ (standard library only).
- Media inputs are normally **publicly accessible URLs** that stay valid until the
  task completes. **Local image files also work**: pass the path and the script
  converts it to a Data URI (verified 2026-09-29 — the first frame of the result
  matched the input image). Audio still needs a public URL.

## Rate limits (free tier)

Free tokens are limited to **RPM = 1** — one request per minute, for create *and* for status
queries. The CLI therefore polls once every **61 seconds**, so a normal clip takes a few minutes
and shows only a handful of `status=` lines. That is expected — do not re-launch the task, do not
poll it yourself in between, and if you ever get a rate-limit error, wait about a minute and try
once more.

## Workflow

### 1. Choose the mode

- `text` — user gives only a text description.
- `keyframe` — user provides first and/or last frame images (URL).
- `reference` — user provides reference images (≤5) and/or audios (≤3);
  cite them in the prompt as `<Picture N>` / `<Audio N>`.

### 2. Build the prompt

- Text: subject + motion + camera movement + atmosphere (+ ambient sound).
- Keyframe: describe the transition between the frames.
- Reference: explicitly state what to take from each asset and what to keep
  consistent ("以 <Picture 1> 中的角色和美术风格为参考…保持外观一致").

Full parameter tables, mode rules, and error codes: `references/api_docs.md`.

### 3. Run the script

```bash
python3 <skill-dir>/scripts/generate_video.py \
  --prompt "视频描述" \
  [--mode text|keyframe|reference] \
  [--first-frame URL] [--last-frame URL] \
  [--images URL ...] [--audios URL ...] \
  [--seconds 5] [--aspect-ratio 16:9] \
  [-o out.mp4]
```

Defaults: mode `text`, `--seconds 5`, `--aspect-ratio 16:9`, output
`agnes_video.mp4`. The script validates Flash-specific limits (720P only,
≤5 images, ≤3 audios, no videos) before sending, polls every 2s (up to 20 min),
and downloads the mp4 on completion.

### 4. Deliver

After the script prints `Saved: <path>`, present the mp4 to the user. On failure:
- HTTP 400 `detail` — check the Flash limits table in `references/api_docs.md`.
- Poll timeout — the script prints the manual query URL; ask the user to retry
  the query later or re-run with the same prompt.

## Critical API Rules (encoded in the script, relevant if hand-writing requests)

1. `size` must be the string `"720P"`; anything else → HTTP 400.
2. `seconds` is a **string** `"4"`–`"12"`, not an integer.
3. Query tasks with `video_id` **plus** `model_name=agnes-video-2.5-flash`
   (plain `video_id` only works for `text` mode tasks).
4. Trust `status`/`progress`; ignore `internal_status`/`internal_progress`.
5. `videos` reference input is not supported on Flash.
6. Never expose the API key in frontend code, logs, or public repos.

## Resources

### scripts/
- `generate_video.py` — full-featured async CLI: create → poll → download. No dependencies.

### references/
- `api_docs.md` — endpoints, parameter tables, mode rules, output-size matrix,
  task lifecycle, Flash-specific errors, and billing formula.

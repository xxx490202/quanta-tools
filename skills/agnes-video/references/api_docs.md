# Agnes Video 2.5 Flash API Reference

> Complete docs index: https://wiki.agnes-ai.cn/llms.txt
> Flash reuses Agnes Video 2.5's parameters/validation except the Flash-specific limits below.

## Endpoints

```
Create:  POST https://api.agnes-ai.cn/v1/videos
Query:   GET  https://api.agnes-ai.cn/agnesapi?video_id=<ID>&model_name=agnes-video-2.5-flash
Headers: Authorization: Bearer YOUR_API_KEY, Content-Type: application/json
```

Query rule: always use `video_id` **with** `model_name=agnes-video-2.5-flash`
(the plain `video_id` query without model_name only works for `mode: "text"` tasks).

## Common Parameters

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `model` | string | Yes | `agnes-video-2.5-flash` |
| `prompt` | string | Yes | In reference mode use `<Picture N>` / `<Audio N>` to refer to assets |
| `mode` | string | Yes | `text` / `keyframe` / `reference` |
| `seconds` | string | No | String `"4"`–`"12"`, default `"5"` (string, not int!) |
| `size` | string | No | Flash: **must be `"720P"`**, other values → HTTP 400 |
| `aspect_ratio` | string | No | `21:9` `16:9` `4:3` `1:1` `3:4` `9:16`, default `16:9` |
| `seed` | integer | No | |
| `n` | integer | No | Only `1` |

## Mode Rules

| mode | Purpose | Required media | Forbidden fields |
|---|---|---|---|
| `text` | text-to-video | none | `first_frame` `last_frame` `images` `audios` `videos` |
| `keyframe` | first/last frame control | `first_frame` and/or `last_frame` (≥1) | `images` `audios` `videos` |
| `reference` | image/audio reference | `images` and/or `audios` (≥1) | `first_frame` `last_frame` `videos` |

Mode-specific params: `first_frame` (URL), `last_frame` (URL),
`images` (URL[], max 5), `audios` (URL[], max 3). `videos` is NOT supported on Flash.

All media URLs must be publicly accessible by the Agnes service and remain valid
until task completion. **Exception (measured 2026-09-29): a Data URI works for
`first_frame`/`last_frame`/`images` — the CLI now converts local image paths to Data
URIs automatically.** Audio still needs a public URL.

## Aspect Ratio → Output Pixels

| ratio | pixels |
|---|---|
| 21:9 | 1680x720 |
| 16:9 | 1280x704 |
| 4:3 | 960x720 |
| 1:1 | 720x720 |
| 3:4 | 720x960 |
| 9:16 | 720x1280 |

## Task Lifecycle

1. Create → save `video_id` from the create response (`id`/`task_id`/`video_id`).
2. Poll every 1–2s until `status` is `completed` or `failed`.
3. On completion, download from the top-level `url` field.

Response fields: trust `status` and `progress` — ignore `internal_status` /
`internal_progress` (they may stay `pending`/`0` even when done).

Completed response example (abridged):

```json
{
  "id": "task_...",
  "status": "completed",
  "progress": 100,
  "seconds": "4",
  "size": "720P",
  "url": "https://example.com/generated/video.mp4"
}
```

## Flash-Specific Errors (all HTTP 400)

Checked in order: `size` → `images` → `audios` → `videos`.

| detail | cause |
|---|---|
| `size must be 720P` | size ≠ "720P" |
| `images length must not exceed 5` | >5 reference images |
| `audios length must not exceed 3` | >3 reference audios |
| `videos is not supported` | reference videos passed |

Failed validation = no task created, no charge.

## Pricing

Same billing formula as Agnes Video 2.5; currently free (list price ¥0.15/s for 720P,
first 5 reference images free). Formula:

```
total = output_seconds × resolution_price
      + input_video_seconds × resolution_price
      + max(0, image_count - 5) × excess_image_price
```

## Prompt Tips

- Text mode: describe subject + motion + camera movement + atmosphere (+ ambient sound).
- Keyframe mode: describe the transition between frames ("自然转身走向窗边…平滑过渡到尾帧").
- Reference mode: explicitly cite assets — "以 <Picture 1> 中的角色和美术风格为参考…保持外观一致".

## Rate limits (free tier)

- **RPM = 1** for free tokens: one request per minute (create *and* query share the same limit).
  Exceeding it returns HTTP 429 `rate_limit_exceeded` — and sometimes a misleading HTTP 401
  "无效的令牌" with an empty `code`, which is *also* a rate limit, not a bad key.
- The CLI polls every 61 seconds to stay inside this window; a finished clip therefore takes a
  few minutes. Never retry a create in the same minute.

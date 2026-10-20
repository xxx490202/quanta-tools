# Agnes Image 2.5 Flash API Reference

> Complete docs index: https://wiki.agnes-ai.cn/llms.txt

## Endpoint

```
POST https://api.agnes-ai.cn/v1/images/generations
Headers:
  Authorization: Bearer YOUR_API_KEY
  Content-Type: application/json
```

## Request Parameters

| Parameter | Type | Required | Description |
|---|---|---|---|
| `model` | string | Yes | `agnes-image-2.5-flash` |
| `prompt` | string | Yes | Generation or edit instruction |
| `size` | string | Yes | Recommended: `1K`/`2K`/`3K`/`4K`; legacy exact sizes like `1024x768` accepted (may be normalized) |
| `ratio` | string | No | `1:1`, `3:4`, `4:3`, `16:9`, `9:16`, `2:3`, `3:2`, `21:9`; default `1:1` |
| `image` | string[] | img2img/multi-composition required | In `extra_body.image`; public URL or Data URI Base64 |
| `return_base64` | boolean | No | Top-level; **text-to-image only**, returns Base64 |
| `extra_body.response_format` | string | No | `url` or `b64_json`; **must be inside extra_body, never top-level** |

## Output Sizes (size × ratio)

| Ratio | 1K | 2K | 3K | 4K |
|---|---|---|---|---|
| 1:1 | 1024x1024 | 2048x2048 | 3072x3072 | 4096x4096 |
| 3:4 | 864x1152 | 1728x2304 | 2592x3456 | 3456x4608 |
| 4:3 | 1152x864 | 2304x1728 | 3456x2592 | 4608x3456 |
| 16:9 | 1312x736 | 2624x1472 | 3936x2208 | 5248x2944 |
| 9:16 | 736x1312 | 1472x2624 | 2208x3936 | 2944x5248 |
| 2:3 | 832x1248 | 1664x2496 | 2496x3744 | 3328x4992 |
| 3:2 | 1248x832 | 2496x1664 | 3744x2496 | 4992x3328 |
| 21:9 | 1568x672 | 3136x1344 | 4704x2016 | 6272x2688 |

Note: exact display sizes like 1920x1080 / 2560x1440 are NOT native outputs; they get
normalized (e.g. to 16:9 1K = 1312x736). For such targets request `2K` + `16:9` and
crop/scale downstream.

## Response Format

```json
{
  "created": 1780000000,
  "data": [
    { "url": "https://...", "b64_json": null, "revised_prompt": null }
  ]
}
```

URL path: `data[0].url`; Base64 path: `data[0].b64_json`.

## Critical Pitfalls

1. **Never put `response_format` at top level** — it goes in `extra_body.response_format`.
   Exception: text-to-image Base64 uses top-level `return_base64: true`.
2. **No `tags: ["img2img"]`** — img2img only needs `extra_body.image`.
3. **img2img/multi-composition require `extra_body.image`** — omitting it is an error.
4. **Input image URLs must be publicly accessible** (no login/cookies/private headers);
   otherwise use Data URI Base64: `data:image/png;base64,BASE64_HERE`.
5. **Timeouts**: generation takes seconds to tens of seconds; set client timeout to 60–360s.

## Pricing

All output resolutions and input reference images are currently **free**
(list price was ¥0.07–0.16 per output image, ¥0.02 per reference image from the 4th).

## Prompt Structure Recommendations

- Text-to-image: `[主体] + [场景/环境] + [风格] + [光照] + [构图] + [质量要求]`
- Image-to-image: `[改变要求] + [新风格/场景] + [需添加/移除的元素] + [需保留的元素]`
  — always state what to preserve (composition, camera angle, layout).
- Multi-image: state each reference image's role and how to combine them.
- High-density images: describe visual hierarchy explicitly (main subject, background,
  secondary details, style, lighting, composition constraints).

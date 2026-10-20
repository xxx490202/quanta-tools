---
name: agnes-image
description: "This skill should be used when the user asks to generate, create, edit, or transform images via the Agnes Image API (model `agnes-image-2.5-flash`), including text-to-image, image-to-image editing (style transfer, relighting, background change), multi-image composition, high-density complex scenes, and product/social-media visuals. Trigger words: 生成图片, 画一张, 文生图, 图生图, 改图, 风格迁移, 多图合成, Agnes, generate image."
agent_created: true
---

# Agnes Image

## Overview

This skill generates and edits images via the Agnes Image 2.5 Flash API
(`POST https://api.agnes-ai.cn/v1/images/generations`). A bundled CLI script
handles all workflows (text-to-image, image-to-image, multi-image composition)
and saves the result locally, so prefer running the script directly instead of
hand-writing curl/HTTP code.

## Prerequisites

- API key: resolved as `--api-key` flag > `AGNES_API_KEY` environment variable >
  `.env` in this skill directory (copy `.env.example` to `.env` and fill in your key). If none is available, ask
  the user for their Agnes AI key before proceeding.
- Python 3.9+ (script uses only the standard library, no dependencies).

## Workflow

### 1. Determine the workflow type

- **Text-to-image**: user provides only a prompt → run script without `--image`.
- **Image-to-image**: user provides one reference image + edit instruction →
  pass the image via `--image`.
- **Multi-image composition**: user provides 2+ reference images → pass all via
  `--image` and state each image's role in the prompt.

### 2. Build the prompt

Follow these structures (full guidance in `references/api_docs.md`):

- Text-to-image: `[主体] + [场景/环境] + [风格] + [光照] + [构图] + [质量要求]`
- Image-to-image: `[改变要求] + [新风格/场景] + [添加/移除的元素] + [需保留的元素]`
  — always explicitly state what to preserve (composition, camera angle, layout).
- Multi-image: state each reference image's role and how to combine them.

### 3. Run the script

```bash
python3 <skill-dir>/scripts/generate_image.py \
  --prompt "提示词" \
  [--image 本地路径或https URL ...] \
  [--size 2K] [--ratio 16:9] \
  [-o output.png] [--api-key KEY]
```

Defaults: `--size 2K` (omit `--ratio` for 1:1). Common ratios: `16:9` landscape,
`9:16` portrait, `3:4`/`4:3` for documents/prints.

### 4. Deliver

After the script prints `Saved: <path>`, present the image file to the user.
If generation fails, check: missing API key, unreachable input image URL
(switch to a local file path — the script auto-converts to Data URI Base64),
or timeout (script waits up to 360s; retry once before reporting failure).

## Critical API Rules (encoded in the script, relevant if hand-writing requests)

1. `response_format` must live inside `extra_body`, never at top level.
2. Text-to-image Base64 output uses top-level `return_base64: true` instead.
3. No `tags: ["img2img"]` — img2img only needs `extra_body.image`.
4. Exact display sizes like `1920x1080` are not native; they get normalized.
   Request a tier (`1K`/`2K`/`3K`/`4K`) plus `ratio` for predictable output.
5. Client timeout: 60–360s.

## Resources

### scripts/
- `generate_image.py` — full-featured CLI. Run directly; no dependencies.

### references/
- `api_docs.md` — complete parameter table, output-size matrix, response format,
  pitfalls, and prompt-structure guidance. Load when hand-writing requests,
  choosing size/ratio, or debugging API errors.

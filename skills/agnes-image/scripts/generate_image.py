#!/usr/bin/env python3
"""Agnes Image 2.5 Flash generation CLI.

Supports text-to-image, image-to-image (single or multiple reference images),
and returns/saves the generated image locally.

Usage examples:
  # Text to image
  python3 generate_image.py --prompt "日落雪山湖泊，电影级写实" --size 2K --ratio 16:9 -o out.png

  # Image to image (local file or https URL)
  python3 generate_image.py --prompt "改成赛博朋克夜景，保留构图" --image input.png -o out.png

  # Multi-image composition
  python3 generate_image.py --prompt "第一张是主角，第二张是产品参考，合成海报" --image a.png b.png -o poster.png

API key resolution order:
  --api-key flag > AGNES_API_KEY env var > .env in the skill directory.
"""
import argparse
import base64
import json
import mimetypes
import os
import ssl
import sys
import urllib.request
import urllib.error

API_URL = "https://api.agnes-ai.cn/v1/images/generations"
MODEL = "agnes-image-2.5-flash"
TIMEOUT = 360  # seconds; generation can take tens of seconds

VALID_RATIOS = {"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"}

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(SKILL_DIR, ".env")


def make_ssl_context() -> ssl.SSLContext:
    """TLS 上下文，顺带兜住 python.org 版解释器缺默认 CA 库的情况。"""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0):
        return ctx
    candidates = []
    try:
        import certifi  # 可选：装了就优先用它那份 CA
        candidates.append(certifi.where())
    except Exception:
        pass
    candidates += ["/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"]
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                ctx.load_verify_locations(cafile=path)
                return ctx
            except ssl.SSLError:
                continue
    return ctx


def load_skill_env() -> None:
    """把技能目录下 .env 里的键补进 os.environ（已有环境变量优先，不覆盖）。"""
    if not os.path.isfile(ENV_FILE):
        return
    with open(ENV_FILE, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if key:
                os.environ.setdefault(key, value)


def to_data_uri(path_or_url: str) -> str:
    """Return https URLs as-is; convert local files to Data URI Base64."""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    path = os.path.expanduser(path_or_url)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Input image not found: {path_or_url}")
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


def build_payload(args) -> dict:
    payload = {
        "model": MODEL,
        "prompt": args.prompt,
        "size": args.size,
    }
    if args.ratio:
        if args.ratio not in VALID_RATIOS:
            raise SystemExit(f"Invalid ratio '{args.ratio}'. Valid: {sorted(VALID_RATIOS)}")
        payload["ratio"] = args.ratio

    if args.image:
        payload["extra_body"] = {
            "image": [to_data_uri(p) for p in args.image],
            # img2img base64 output; for URL output change to "url"
            "response_format": "b64_json" if args.return_base64 else "url",
        }
    elif args.return_base64:
        # text-to-image base64 goes at TOP LEVEL (not extra_body)
        payload["return_base64"] = True
    else:
        payload["extra_body"] = {"response_format": "url"}
    return payload



# ── 进度：给界面看的（应用读 <输出>.progress，画进度条）────────────────────
def status_path(out: str) -> str:
    return str(out) + ".progress"


def write_status(out: str, **fields) -> None:
    try:
        import json as _json
        import time as _time
        payload = dict(fields)
        payload.setdefault("at", _time.time())
        tmp = status_path(out) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            _json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, status_path(out))
    except OSError:
        pass


def clear_status(out: str) -> None:
    try:
        os.unlink(status_path(out))
    except OSError:
        pass


def call_api(payload: dict, api_key: str) -> dict:
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=make_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"API error {e.code}: {body[:2000]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Network error (timeout={TIMEOUT}s): {e.reason}")


def extract_image(data: dict) -> tuple[bytes, str]:
    """Return (image_bytes, extension) from the first item in data[]."""
    item = (data.get("data") or [{}])[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"]), "png"
    url = item.get("url")
    if url:
        ext = os.path.splitext(url.split("?")[0])[1].lstrip(".") or "png"
        with urllib.request.urlopen(url, timeout=120, context=make_ssl_context()) as r:
            return r.read(), ext
    raise SystemExit(f"No image in response: {json.dumps(data)[:1000]}")


def main():
    load_skill_env()
    ap = argparse.ArgumentParser(description="Agnes Image 2.5 Flash generator")
    ap.add_argument("--prompt", required=True, help="Text prompt (generation or edit instruction)")
    ap.add_argument("--image", nargs="*", default=[], help="Reference image(s): local path or https URL")
    ap.add_argument("--size", default="2K", help="1K/2K/3K/4K or legacy exact size (default: 2K)")
    ap.add_argument("--ratio", default=None, help="Aspect ratio, e.g. 16:9 (default: 1:1)")
    ap.add_argument("--return-base64", dest="return_base64", action="store_true",
                    help="Request base64 response instead of URL")
    ap.add_argument("-o", "--output", default=None, help="Output file path (default: ./agnes_<ts>.png)")
    ap.add_argument("--api-key", default=None,
                    help="Agnes API key (default: AGNES_API_KEY env var or skill .env)")
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("AGNES_API_KEY")
    if not api_key:
        raise SystemExit("Missing API key: pass --api-key, set AGNES_API_KEY, "
                         f"or add AGNES_API_KEY to {ENV_FILE}")

    payload = build_payload(args)
    # 状态文件要在**已经知道目标路径之后**写：-o 没给时是 None，写出去会变成
    # "None.progress"（而且落在别处，应用扫不到）。扩名要等结果才知道，这里用占位名做信标。
    status_target = args.output or os.path.join(os.getcwd(), "agnes_out.png")
    write_status(status_target, stage="rendering")
    try:
        result = call_api(payload, api_key)
    finally:
        clear_status(status_target)
    img_bytes, ext = extract_image(result)

    out = args.output or f"agnes_{args.size}.{ext}"
    if not os.path.splitext(out)[1]:
        out += f".{ext}"
    with open(out, "wb") as f:
        f.write(img_bytes)
    print(f"Saved: {out} ({len(img_bytes) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()

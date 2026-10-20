#!/usr/bin/env python3
"""Agnes Video 2.5 Flash generation CLI (async task + polling + download).

Supports three modes:
  text      — text-to-video
  keyframe  — first/last frame control (at least one of --first-frame/--last-frame)
  reference — image/audio reference generation (--images up to 5, --audios up to 3)

Usage:
  # Text to video
  python3 generate_video.py --prompt "雨后未来城市街道，霓虹倒映，电影级运镜" -o out.mp4

  # Keyframe control (URLs only)
  python3 generate_video.py --mode keyframe --prompt "人物自然转身走向窗边" \
      --first-frame https://example.com/first.png --last-frame https://example.com/last.png

  # Reference (image)
  python3 generate_video.py --mode reference --prompt "以 <Picture 1> 的角色为参考，在花田奔跑" \
      --images https://example.com/character.png

API key: --api-key flag > AGNES_API_KEY env var > .env in the skill directory.
Media inputs must be publicly accessible URLs (kept valid until task completion).
"""
import argparse
import json
import os
import pathlib
import ssl
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "https://api.agnes-ai.cn/v1"
QUERY_URL = "https://api.agnes-ai.cn/agnesapi"
MODEL = "agnes-video-2.5-flash"
CREATE_TIMEOUT = 120
# 轮询间隔。免费用户是 RPM=1（每分钟 1 次请求），所以必须是 61 秒——60 秒整卡在窗口边界上
# 容易被判超限，多给 1 秒。查一次要等一分钟，看着慢，但这是免费额度的硬限制。
POLL_INTERVAL = 61
MAX_WAIT_SECONDS = 1200    # give up polling after 20 minutes

VALID_RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}

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


def die(msg: str):
    raise SystemExit(f"ERROR: {msg}")


def build_payload(args) -> dict:
    payload = {
        "model": MODEL,
        "prompt": args.prompt,
        "mode": args.mode,
        "seconds": str(args.seconds),  # API expects a string "4"–"12"
        "size": "720P",                # Flash: must be exactly "720P"
        "n": 1,
    }
    if args.aspect_ratio:
        if args.aspect_ratio not in VALID_RATIOS:
            die(f"Invalid aspect_ratio '{args.aspect_ratio}'. Valid: {sorted(VALID_RATIOS)}")
        payload["aspect_ratio"] = args.aspect_ratio
    if args.seed is not None:
        payload["seed"] = args.seed

    mode = args.mode
    if mode == "keyframe":
        if not args.first_frame and not args.last_frame:
            die("keyframe mode requires --first-frame and/or --last-frame")
        if args.images or args.audios:
            die("keyframe mode does not allow images/audios")
        if args.first_frame:
            payload["first_frame"] = as_media_url(args.first_frame)
        if args.last_frame:
            payload["last_frame"] = as_media_url(args.last_frame)
    elif mode == "reference":
        if not args.images and not args.audios:
            die("reference mode requires --images and/or --audios")
        if len(args.images) > 5:
            die("Flash supports at most 5 reference images")
        if len(args.audios) > 3:
            die("Flash supports at most 3 reference audios")
        if args.first_frame or args.last_frame:
            die("reference mode does not allow first_frame/last_frame")
        if args.images:
            payload["images"] = [as_media_url(item) for item in args.images]
        if args.audios:
            payload["audios"] = args.audios
    else:  # text
        if args.images or args.audios or args.first_frame or args.last_frame:
            die("text mode does not allow any media fields")
    return payload


def as_media_url(value: str) -> str:
    """本地图片也能当首尾帧/参考图用：转成 data URI（实测接口收这个）。

    官方文档写着「no Data URI support」，但实测（2026-09-29）拿一张 256×256 的 PNG 转成
    data URI 当 first_frame，任务正常创建、出片的第一帧就是那张图，所以这里认本地路径。
    音频不转（没验过，而且体积大）。
    """
    raw = str(value or "").strip()
    if not raw or raw.startswith(("http://", "https://", "data:")):
        return raw
    path = pathlib.Path(os.path.expanduser(raw))
    if not path.is_file():
        return raw
    import base64
    import mimetypes
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"



# ── 进度：给界面看的（应用读 <输出>.progress，画进度条）────────────────────
def status_path(out: str) -> str:
    return str(out) + ".progress"


def write_status(out: str, **fields) -> None:
    """写一份小的 JSON：谁在读都不影响生成，出错就当没写。"""
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


def api_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def create_task(payload: dict, api_key: str, base_url: str) -> str:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/videos",
        data=json.dumps(payload).encode("utf-8"),
        headers=api_headers(api_key),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=CREATE_TIMEOUT,
                                    context=make_ssl_context()) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        die(f"create task failed HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:2000]}")
    except urllib.error.URLError as e:
        die(f"network error: {e.reason}")

    video_id = data.get("video_id") or data.get("task_id") or data.get("id")
    if not video_id:
        die(f"no task id in create response: {json.dumps(data)[:1000]}")
    print(f"Task created: {video_id}", file=sys.stderr)
    return video_id


def query_task(video_id: str, api_key: str) -> dict:
    url = f"{QUERY_URL}?video_id={video_id}&model_name={MODEL}"
    req = urllib.request.Request(url, headers=api_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=60, context=make_ssl_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        die(f"query failed HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:2000]}")


def wait_for_result(video_id: str, api_key: str) -> dict:
    deadline = time.time() + MAX_WAIT_SECONDS
    last_progress = -1
    wait = POLL_INTERVAL
    while time.time() < deadline:
        try:
            data = query_task(video_id, api_key)
        except SystemExit:                       # query_task 用 die()：限流/网络抖动时别整次放弃
            wait = min(120, max(POLL_INTERVAL, wait + 30))
            print(f"查询失败，{wait}s 后重试", file=sys.stderr)
            time.sleep(wait)
            continue
        wait = POLL_INTERVAL
        status = data.get("status")
        progress = data.get("progress")
        if progress != last_progress:
            print(f"status={status} progress={progress}", file=sys.stderr)
            last_progress = progress
        write_status(args.output, stage="rendering", task=video_id,
                     status=status, progress=progress,
                     elapsed=round(time.time() - (deadline - MAX_WAIT_SECONDS)))
        if status == "completed":
            return data
        if status == "failed":
            die(f"task failed: {json.dumps(data.get('error') or data)[:1000]}")
        time.sleep(POLL_INTERVAL)
    die(f"timeout after {MAX_WAIT_SECONDS}s; query manually: "
        f"{QUERY_URL}?video_id={video_id}&model_name={MODEL}")


def download(url: str, out: str) -> str:
    if not os.path.splitext(out)[1]:
        out += ".mp4"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=300, context=make_ssl_context()) as r, open(out, "wb") as f:
        f.write(r.read())
    return out


def main():
    load_skill_env()
    ap = argparse.ArgumentParser(description="Agnes Video 2.5 Flash generator")
    ap.add_argument("--prompt", required=True, help="Video description (use <Picture N>/<Audio N> in reference mode)")
    ap.add_argument("--mode", default="text", choices=["text", "keyframe", "reference"])
    ap.add_argument("--first-frame", default=None, help="First frame image URL (keyframe mode)")
    ap.add_argument("--last-frame", default=None, help="Last frame image URL (keyframe mode)")
    ap.add_argument("--images", nargs="*", default=[], help="Reference image URLs, max 5 (reference mode)")
    ap.add_argument("--audios", nargs="*", default=[], help="Reference audio URLs, max 3 (reference mode)")
    ap.add_argument("--seconds", type=int, default=5, help="Duration 4-12 (default 5)")
    ap.add_argument("--aspect-ratio", default="16:9", help=f"One of {sorted(VALID_RATIOS)}")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("-o", "--output", default="agnes_video.mp4", help="Output mp4 path")
    ap.add_argument("--api-key", default=None,
                    help="Agnes API key (default: AGNES_API_KEY env var or skill .env)")
    ap.add_argument("--base-url", default=os.environ.get("AGNES_BASE_URL", DEFAULT_BASE))
    args = ap.parse_args()

    api_key = args.api_key or os.environ.get("AGNES_API_KEY")
    if not api_key:
        die("missing API key: pass --api-key, set AGNES_API_KEY, "
            f"or add AGNES_API_KEY to {ENV_FILE}")
    if not (4 <= args.seconds <= 12):
        die("--seconds must be 4-12")

    payload = build_payload(args)
    write_status(args.output, stage="creating")
    video_id = create_task(payload, args.api_key, args.base_url)
    write_status(args.output, stage="queued", task=video_id)
    try:
        result = wait_for_result(video_id, args.api_key)
        url = result.get("url")
        if not url:
            die(f"completed but no url: {json.dumps(result)[:1000]}")
        write_status(args.output, stage="downloading", task=video_id)
        out = download(url, args.output)
    finally:
        clear_status(args.output)          # 结束（成功/失败）都把状态文件收掉
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# Qwen3-ASR（MLX）侧车：Apple GPU 上跑 1.7B 的语音模型。
#
#   python3 mlx_asr.py --status                 模型 + 运行时装好了吗（一行 JSON 到 stdout）
#   python3 mlx_asr.py --download [--reset]    从 hf-mirror 拉模型（约 2.3GB）
#   python3 mlx_asr.py --wav <文件>            转一段 wav，结果按 [voice] {json} 打到 stderr
#   python3 mlx_asr.py --serve                 常驻：stdin 一行一条 {"pcm": "<base64 16k s16le>"}，
#                                              转完回 [voice] {"event":"text","text":…}
#
# 协议和别的识别引擎（connectors/voice/asr.mjs、rhema.mjs）**完全一致**：桥那边不用为它
# 单独写一套。VAD 断句仍在 node 侧（silero），这边只管「一段音频 -> 文字」。
#
# 和 sherpa 那几个的区别：这是 LLM 解码的大模型（1.7B），更准、自带标点，但慢一档
#（这台机器上约 1~1.4 秒/句）。MLX 走 GPU，所以比 CPU 上跑 int8 快得多。
import base64
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import wave

REPO = "mlx-community/Qwen3-ASR-1.7B-8bit"
# --model 指定的话用它（0.6B 那份走这儿）；--repo 同理（下载用的仓库）
MODEL_OVERRIDE = ""


def data_dir() -> pathlib.Path:
    env = (os.environ.get("QUANTA_DATA_DIR") or "").strip()
    if env:
        return pathlib.Path(env)
    return pathlib.Path.home() / "Library" / "Application Support" / "Quantification"


def model_dir() -> pathlib.Path:
    if MODEL_OVERRIDE:
        return pathlib.Path(MODEL_OVERRIDE)
    env = (os.environ.get("QUANTA_TTS_MODELS") or "").strip()
    if env:
        return pathlib.Path(env)
    return data_dir() / "voice" / "qwen3-asr-mlx"


def report(payload: dict) -> None:
    sys.stderr.write("[voice] " + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def require_runner():
    """mlx-audio + mlx 装了吗（用哪个 python 就在哪个环境里找）。"""
    try:
        from mlx_audio.stt import load  # noqa: F401
    except Exception as exc:                                     # noqa: BLE001
        raise RuntimeError(f"没装 mlx-audio：{type(exc).__name__}: {exc}") from exc


def ready() -> dict:
    files = {}
    dir = model_dir()
    for name in ("model.safetensors", "config.json", "vocab.json", "merges.txt",
                 "tokenizer_config.json"):
        files[name] = (dir / name).is_file()
    runner = ""
    try:
        require_runner()
    except Exception as exc:                                     # noqa: BLE001
        runner = str(exc)
    return {"models": str(dir), "error": runner, "files": files,
            "ready": not runner and all(files.values())}


def transcribe(model, wav_path: pathlib.Path) -> str:
    from mlx_audio.stt.generate import generate_transcription
    out = generate_transcription(model=model, audio=str(wav_path))
    # 返回 STTOutput(text=…, segments=[…])：拿 text，没有就拼 segments
    text = getattr(out, "text", None)
    if text is None and isinstance(out, (list, tuple)):
        text = "".join(getattr(seg, "text", "") or "" for seg in out)
    if text is None:
        text = str(out)
    return str(text).strip()


def write_wav(samples: bytes, path: pathlib.Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(samples)


def load_model():
    from mlx_audio.stt import load
    started = time.time()
    model = load(str(model_dir()), lazy=False)
    sys.stderr.write(f"[voice] 模型加载用了 {time.time() - started:.2f}s\n")
    sys.stderr.flush()
    return model


def serve() -> None:
    model = load_model()
    # 标签按**实际加载的那个模型**报（1.7B / 0.6B 走的是同一份脚本，别写死）
    report({"event": "ready", "asr": "qwen3-mlx",
            "label": f"Qwen3-ASR (MLX) · {model_dir().name}"})
    tmp_dir = data_dir() / "voice" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("stop"):
            return
        raw = str(payload.get("pcm") or "")
        if not raw:
            continue
        samples = base64.b64decode(raw)
        if len(samples) < 3200:                                  # 短于 0.1 秒：当噪声丢
            continue
        path = tmp_dir / f"mlx-{int(time.time() * 1000)}.wav"
        try:
            write_wav(samples, path)
            started = time.time()
            text = transcribe(model, path)
            seconds = len(samples) / 2 / 16000
            if text:
                report({"event": "text", "text": text, "seconds": seconds,
                        "took": round(time.time() - started, 2)})
        except Exception as exc:                                 # noqa: BLE001
            report({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
        finally:
            path.unlink(missing_ok=True)


def download(reset: bool) -> None:
    dir = model_dir()
    if reset:
        for name in ("model.safetensors", "config.json", "vocab.json", "merges.txt"):
            (dir / name).unlink(missing_ok=True)
    # HF 新的 Xet 存储走镜像会 401（Unauthorized）：关掉它，走普通 HTTPS 就能下
    env = {**os.environ, "HF_ENDPOINT": os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com",
           "HF_HUB_DISABLE_XET": "1"}
    dir.mkdir(parents=True, exist_ok=True)
    code = (
        "from huggingface_hub import snapshot_download;"
        f"snapshot_download({REPO!r}, local_dir={str(dir)!r},"
        " allow_patterns=['*.json', '*.txt', '*.safetensors'])"
    )
    report({"event": "note", "text": f"从 {env['HF_ENDPOINT']} 拉 {REPO}"})
    proc = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        report({"event": "error", "error": (proc.stderr or "").strip()[-300:] or "下载失败"})
        sys.exit(1)
    report({"event": "note", "text": "下载完成"})


def main() -> None:
    global MODEL_OVERRIDE, REPO
    args = sys.argv[1:]
    if "--model" in args:
        MODEL_OVERRIDE = args[args.index("--model") + 1]
    if "--repo" in args:
        REPO = args[args.index("--repo") + 1]
    if "--status" in args:
        print(json.dumps(ready(), ensure_ascii=False))
        return
    if "--download" in args:
        download("--reset" in args)
        print(json.dumps(ready(), ensure_ascii=False))
        return
    if "--serve" in args:
        serve()
        return
    if "--wav" in args:
        wav_path = pathlib.Path(args[args.index("--wav") + 1])
        info = ready()
        if not info["ready"]:
            report({"event": "error", "error": info["error"] or "Qwen3-ASR（MLX）还没下完"})
            sys.exit(1)
        model = load_model()
        started = time.time()
        try:
            text = transcribe(model, wav_path)
        except Exception as exc:                                 # noqa: BLE001
            report({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
            sys.exit(1)
        with wave.open(str(wav_path), "rb") as handle:
            seconds = handle.getnframes() / max(1, handle.getframerate())
        print(text)                                              # 人看的：纯文字一行
        report({"event": "text", "text": text, "seconds": seconds,
                "took": round(time.time() - started, 2), "asr": "qwen3-mlx"})
        return
    sys.stderr.write("用法：mlx_asr.py --status | --download [--reset] | --wav <文件> | --serve\n")
    sys.exit(2)


if __name__ == "__main__":
    main()

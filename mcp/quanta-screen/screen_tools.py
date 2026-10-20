#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 Claude / Codex 一个「截当前屏幕」的工具（它们没有 dsh 那套 computer use）。

dsh 主力时桌面操作是官方 computer-use 插件包办的（列窗口、无障碍树、点击键盘那一整套）；
那两家没有，所以这里补最小的一个：**截一张屏幕图**（macOS 自带的 screencapture），
它们自己都会看图（Claude 的 Read、Codex 的 view_image）——"能看不能动"。

    screen_capture   截一张当前屏幕，返回文件路径和尺寸

只在「主力不是 deepseek」时挂（见 dsh_agent._mcp_servers）——dsh 有自己的那套，
不用重复给。截图要**屏幕录制权限**，这个权限给的是应用本身（已授权），所以这张图
是应用拿到的真实屏幕内容。

stdout 只能出现 JSON-RPC（多打一行字就把协议弄坏了），调试信息一律走 stderr。
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config              # noqa: E402

PROTOCOL_FALLBACK = "2025-06-18"
SHOT_DIR = config.DATA_DIR / "screenshots"

TOOLS = [
    {
        "name": "screen_capture",
        "description": "截一张**当前屏幕**的图，返回文件路径（再用你的看图能力打开它："
                       "Claude 用 Read、Codex 用 view_image）。用来：用户让你看屏幕上有什么、"
                       "界面排版/图表/验证码这类只能靠看的东西。只能看、不能点——要点哪儿，"
                       "告诉用户，或者走内置浏览器（browser_*）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note": {"type": "string",
                         "description": "可选：这张图是给谁看的（写进文件名，方便事后找）"},
            },
            "required": [],
        },
    },
]


def _safe(text: str, limit: int = 24) -> str:
    keep = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in str(text))
    return keep.strip("-")[:limit]


def screen_capture(arguments) -> str:
    """截一张全屏图（macOS 的 screencapture -x：静音、不发声）。"""
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    note = _safe((arguments or {}).get("note") or "")
    path = SHOT_DIR / f"screen-{stamp}{'-' + note if note else ''}.png"
    try:
        # -x：不播放快门声（用户可能正在开会/录音，别响）
        proc = subprocess.run(["screencapture", "-x", str(path)],
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"截图没跑起来（{type(exc).__name__}：{exc}）")
    if proc.returncode != 0 or not path.exists():
        raise RuntimeError("截图失败："
                           + (proc.stderr or "").strip()[-300:]
                           + "（多半是应用没有「屏幕录制」权限——去 设置 → 通用 → 权限状态 看一眼）")
    size = ""
    try:
        # 尺寸不是必须的，读不到就算了（PIL 在应用那边一定有，这里只是顺手）
        from PIL import Image
        with Image.open(path) as image:
            size = f"{image.width}×{image.height}"
    except Exception:                                            # noqa: BLE001
        pass
    pixels = path.stat().st_size
    return (f"截好了：{path}" + (f"（{size}）" if size else "")
            + f"，{pixels // 1024} KB。用你的看图能力打开它就能看到屏幕上现在是什么。")


def call_tool(name, arguments):
    if name == "screen_capture":
        return screen_capture(arguments)
    raise RuntimeError(f"没有这个工具：{name}")


# --------------------------------------------------------------------------
# MCP 循环（和另外几个 sidecar 一个写法）
# --------------------------------------------------------------------------

def reply(request_id, result):
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def reply_error(request_id, message):
    _write({"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32603, "message": message}})


def _write(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            version = params.get("protocolVersion") or PROTOCOL_FALLBACK
            reply(request_id, {"protocolVersion": version,
                               "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": "quanta-screen", "version": "1.0.0"}})
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue
        elif method == "ping":
            reply(request_id, {})
        elif method == "tools/list":
            reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            try:
                text = call_tool(params.get("name"), params.get("arguments"))
                reply(request_id, {"content": [{"type": "text", "text": text}],
                                   "isError": False})
            except Exception as exc:                    # 工具内部出错当工具结果回，别断连接
                reply(request_id, {"content": [{"type": "text", "text": f"出错了：{exc}"}],
                                   "isError": True})
        elif request_id is not None:
            reply_error(request_id, f"不支持的方法：{method}")


if __name__ == "__main__":
    print(f"截图 MCP server 已启动（存到 {SHOT_DIR}）", file=sys.stderr)
    serve()

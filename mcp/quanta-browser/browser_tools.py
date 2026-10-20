#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「内置浏览器」暴露成 MCP 工具，给 dsh 那边的 agent 用。

浏览器是应用右栏里那个（主进程的 WebContentsView），页面**用户全程看得见**——
这个脚本自己不碰网页，它只是把 agent 的请求转给主进程：

    browser_open        在右栏打开一个网址（没开浏览器就把它开起来），返回标题和正文
    browser_read        看当前这一页：网址、标题、看得见的文字、能点的控件
    browser_click       点一个东西（CSS 选择器，或者控件的文字）
    browser_type        往输入框里打字（可选回车提交）
    browser_eval        在页面里跑一段 JS（拿不准的页面用这个兜底）
    browser_screenshot  截一张图存成 PNG，返回路径（用 read_image 看它）

怎么到主进程：主进程在数据目录里开了一个 unix socket（browser-rpc.sock），
旁边 browser-rpc.json 写着 socket 路径和 token；这个脚本 HTTP POST 过去拿结果。
只在本机、要 token、socket 权限 600——别的进程连不上。

stdout 只能出现 JSON-RPC（多打一行字就会把协议弄坏），所以调试信息一律走 stderr。
"""

import json
import os
import socket
import sys


from http.client import HTTPConnection

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config              # noqa: E402

PROTOCOL_FALLBACK = "2025-06-18"
TIMEOUT = 60


class _UnixHTTPConnection(HTTPConnection):
    """HTTPConnection 默认只会连 TCP，这里改成连 unix socket。"""

    def __init__(self, path, timeout=TIMEOUT):
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._unix_path)
        self.sock = sock


def _rpc(payload):
    """把一次请求发给主进程，拿回结果字典。出任何岔子都给一句人话。"""
    info_path = os.path.join(str(config.DATA_DIR), "browser-rpc.json")
    try:
        with open(info_path, encoding="utf-8") as handle:
            info = json.load(handle)
    except (OSError, ValueError):
        return {"ok": False, "why": "应用那边的浏览器口子还没开（browser-rpc.json 不在）"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = _UnixHTTPConnection(str(info.get("socket") or ""), timeout=TIMEOUT)
    try:
        conn.request("POST", "/rpc", body=body,
                     headers={"content-type": "application/json",
                              "x-quanta-token": str(info.get("token") or "")})
        response = conn.getresponse()
        raw = response.read().decode("utf-8") or "{}"
        if response.status != 200:
            return {"ok": False, "why": f"应用没接这个请求（HTTP {response.status}）"}
        return json.loads(raw)
    except Exception as exc:                                        # noqa: BLE001
        return {"ok": False, "why": f"连不上应用：{type(exc).__name__}: {exc}"}
    finally:
        try:
            conn.close()
        except Exception:                                           # noqa: BLE001
            pass


def _lines(result):
    """把主进程回的那份结果写成人话（模型读的是文字，不是 JSON）"""
    if not result:
        return "（没有结果）"
    if result.get("ok") is False:
        return "没能完成：" + str(result.get("why") or "不知道为啥")
    action = str(result.get("action") or "")
    out = []
    if result.get("url"):
        out.append(f"网址：{result['url']}")
    if result.get("title"):
        out.append(f"标题：{result['title']}")
    if result.get("clicked"):
        out.append("点了：" + str(result["clicked"]))
    if result.get("filled"):
        how = {"form": "（已提交表单）", "enter": "（按了回车）"}.get(str(result.get("submitted")), "")
        out.append("输入了：" + str(result["filled"]) + how)
    if result.get("path"):
        out.append("截图存到：" + str(result["path"]) + "（用 read_image 看它）")
    if result.get("tabs") is not None:
        tabs = result.get("tabs") or []
        out.append("打开的标签：" + (", ".join(
            f"{t.get('title') or t.get('url')}" for t in tabs) if tabs else "（还没有）"))
    if result.get("controls"):
        items = [f"{c.get('tag')}「{c.get('label')}」" + (f" → {c['href']}" if c.get("href") else "")
                 for c in result["controls"][:40]]
        if items:
            out.append("能点的东西：\n" + "\n".join("  · " + line for line in items))
    if result.get("value") is not None:
        out.append("JS 结果：" + json.dumps(result["value"], ensure_ascii=False)[:2000])
    if result.get("text"):
        out.append("页面文字：\n" + str(result["text"])[:6000])
    return "\n".join(out) if out else json.dumps(result, ensure_ascii=False)[:2000]


TOOLS = [
    {
        "name": "browser_open",
        "description": "在内置浏览器（应用右栏那个，用户看得见）打开一个网址，返回标题和页面正文。"
                       "要查资料、看某个页面时从这儿开始；已经开着的页面用 browser_read 看。",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string",
                                   "description": "完整网址，例如 https://example.com/pricing"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_read",
        "description": "读当前页面：网址、标题、看得见的文字、以及能点的控件清单（链接/按钮/输入框）。"
                       "每次点击或提交之后都该读一遍，确认页面变成了什么样。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_click",
        "description": "点页面上的一个东西：给 selector（CSS 选择器）或者 label（控件上写的字）。"
                       "点完用 browser_read 看结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS 选择器，例如 #submit 或 .price a"},
                "label": {"type": "string", "description": "控件上的文字，例如「登录」「Next」"},
            },
            "required": [],
        },
    },
    {
        "name": "browser_type",
        "description": "往输入框里打字：给 selector 或 label（输入框的 placeholder / name 认得的那段字），"
                       "submit=true 表示打完回车提交。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要输入的内容"},
                "selector": {"type": "string", "description": "CSS 选择器（可选）"},
                "label": {"type": "string", "description": "输入框上的提示文字（可选）"},
                "submit": {"type": "boolean", "description": "打完是否提交（回车）"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "把当前页面截成一张图，返回文件路径；用 read_image 打开它来看页面长什么样"
                       "（排版、图表、验证码这种文字读不出来的东西）。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "browser_eval",
        "description": "在当前页面里跑一段 JavaScript 并拿到返回值（兜底手段：页面结构太怪、"
                       "上面几个工具都不好使时用）。要写完整的语句，最后 return 你想要的东西。",
        "inputSchema": {
            "type": "object",
            "properties": {"js": {"type": "string", "description": "一段 JS，例如 "
                                  "`return document.querySelectorAll('h1').length`"}},
            "required": ["js"],
        },
    },
    {
        "name": "browser_status",
        "description": "看内置浏览器现在开着哪些标签（标题和网址）。要接着之前的页面干活时先看一眼。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def call_tool(name, arguments):
    arguments = arguments or {}
    if name == "browser_status":
        return _lines(_rpc({"action": "status"}))
    if name == "browser_open":
        return _lines(_rpc({"action": "open", "url": arguments.get("url")}))
    if name == "browser_read":
        return _lines(_rpc({"action": "read"}))
    if name == "browser_click":
        return _lines(_rpc({"action": "click", "selector": arguments.get("selector"),
                            "label": arguments.get("label")}))
    if name == "browser_type":
        return _lines(_rpc({"action": "type", "text": arguments.get("text"),
                            "selector": arguments.get("selector"),
                            "label": arguments.get("label"),
                            "submit": bool(arguments.get("submit"))}))
    if name == "browser_screenshot":
        return _lines(_rpc({"action": "screenshot"}))
    if name == "browser_eval":
        return _lines(_rpc({"action": "eval", "js": arguments.get("js")}))
    raise ValueError(f"没有这个工具：{name}")


def reply(request_id, result):
    _write({"jsonrpc": "2.0", "id": request_id, "result": result})


def reply_error(request_id, message):
    _write({"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32603, "message": str(message)}})


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
        except ValueError:
            continue
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        try:
            if method == "initialize":
                reply(request_id, {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_FALLBACK,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "quanta-browser", "version": "1.0.0"},
                })
            elif method in ("notifications/initialized", "initialized"):
                continue
            elif method == "tools/list":
                reply(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                text = call_tool(params.get("name"), params.get("arguments"))
                reply(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
            elif request_id is not None:
                reply_error(request_id, f"不认识的方法：{method}")
        except Exception as exc:                                    # noqa: BLE001
            print(f"[browser_tools] {method} 出错：{type(exc).__name__}: {exc}",
                  file=sys.stderr)
            if request_id is not None:
                reply_error(request_id, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    serve()

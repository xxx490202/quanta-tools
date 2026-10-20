#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「其它几页的产出」暴露成 MCP 工具，给 Maestro 页的 agent 用（只读）。

Maestro 上跑的是主对话，用户在别处（Analyse / Crawler / Sentiment / Genesis）
做过的事 Maestro 看不到——这个 server 就是把那些东西递过去：

    list_pages           五个页面各在做什么、目录在哪、有几份材料
    read_page_materials  某一页的材料清单（分析结果、抓取数据、产物）
    read_page_material   读某一页某条材料的全文
    read_page_files      某一页工作目录里的文件（Genesis 出的图/视频就在这儿）
    read_page_chat       某一页最近几轮的问答（这段对话里都聊了什么）

全部只读——它不跑任何东西、不改任何文件。数据就是应用那份 sessions.json
加上各页的工作目录（和 mcp_tools.py 读材料是一个路子）。

stdout 只能出现 JSON-RPC（多打一行字就把协议弄坏了），调试信息一律走 stderr。
"""

import json
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config              # noqa: E402
import harness             # noqa: E402

PROTOCOL_FALLBACK = "2025-06-18"
MAX_RESULT_CHARS = 8000

# 页面的显示名（和界面上那排导航一致）
PAGES = [
    ("project", "Maestro", "主对话：问答、派活、改文件"),
    ("analyse", "Analyse", "材料分析：截图/文件转写、数据表、分析结论"),
    ("crawler", "Crawler", "抓取：跑爬虫、产出数据集"),
    ("sentiment", "Sentiment", "舆情：监控、事件、情绪判断"),
    ("genesis", "Genesis", "生成：出图、出视频"),
    ("colloquy", "Colloquy", "网页版对话（DeepSeek 网页端，只有对话记录）"),
]
PAGE_KEYS = {key for key, _zh, _desc in PAGES}
PAGE_LABELS = {key: zh for key, zh, _desc in PAGES}

TOOLS = [
    {
        "name": "list_pages",
        "description": "看看其它几页（Analyse / Crawler / Sentiment / Genesis）现在在做什么："
                       "各自挂着哪个任务、工作目录在哪、有几份材料、最近什么时候动过。"
                       "要引用别处的产出之前，先调它。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_page_materials",
        "description": "列出某一页的全部材料（编号、类型、标题、字数、时间）。"
                       "材料是那一页的分析结果、抓取数据、生成的产物清单。",
        "inputSchema": {
            "type": "object",
            "properties": {"page": {"type": "string",
                                    "description": "页面：analyse / crawler / sentiment / genesis"}},
            "required": ["page"],
        },
    },
    {
        "name": "read_page_material",
        "description": "读某一页某条材料的完整文本（先用 read_page_materials 拿编号）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "页面，例如 analyse"},
                "artifact_id": {"type": "string", "description": "材料编号，例如 a1"},
            },
            "required": ["page", "artifact_id"],
        },
    },
    {
        "name": "read_page_files",
        "description": "看某一页的工作目录里有哪些文件（Genesis 生成的图片/视频、Crawler 落的"
                       "数据、Analyse 导出的表都在这儿）。给出路径，需要时你自己去读。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "页面，例如 genesis"},
                "limit": {"type": "integer", "description": "最多列多少个文件，默认 40"},
            },
            "required": ["page"],
        },
    },
    {
        "name": "read_page_chat",
        "description": "看某一页最近的几轮问答：用户说了什么、它答了什么（长了会截断）。"
                       "想知道「那边聊到哪儿了」时用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "页面，例如 sentiment"},
                "limit": {"type": "integer", "description": "看最近几轮，默认 6"},
            },
            "required": ["page"],
        },
    },
]


def _books():
    try:
        with open(config.DATA_DIR / "sessions.json", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, ValueError):
        return {}


def _current_session(page: str):
    """这一页现在挂着的那个任务（界面上左栏选中的那条）。"""
    book = _books().get(page) or {}
    sessions = book.get("sessions") or []
    if not sessions:
        return None
    current = book.get("current")
    for item in sessions:
        if item.get("id") == current:
            return item
    return sessions[0]                       # 指针没了就退回最近的那条


def _need(page: str):
    page = str(page or "").strip().lower()
    if page not in PAGE_KEYS:
        raise RuntimeError(f"没有这一页：{page}。可选的：{'、'.join(sorted(PAGE_KEYS))}")
    if page == "project":
        raise RuntimeError("project 就是 Maestro 自己这一页，不用读。"
                           "要看的是 analyse / crawler / sentiment / genesis。")
    item = _current_session(page)
    if item is None:
        raise RuntimeError(f"{PAGE_LABELS[page]} 这一页还没有任务——用户还没在那儿做过事。")
    return page, item


def _status():
    """桥写下的「哪一页正在跑」（没有就算了，只影响 list_pages 里那句提示）。"""
    try:
        with open(config.DATA_DIR / "pages-status.json", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _clip(text, limit=MAX_RESULT_CHARS):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + f"\n...（已截断，全文 {len(text)} 字）"


def _when(item) -> str:
    return str(item.get("updated_at") or item.get("created_at") or "")


def _list_files(folder: str, limit: int):
    root = pathlib.Path(str(folder or "")).expanduser()
    if not folder or not root.is_dir():
        return []
    rows = []
    for path in sorted(root.rglob("*"), key=lambda p: -p.stat().st_mtime if p.is_file() else 0):
        if len(rows) >= limit:
            break
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        rows.append((path, stat.st_size, stat.st_mtime))
    return rows


def call_tool(name, arguments):
    arguments = arguments or {}
    if name == "list_pages":
        books = _books()
        status = _status()
        lines = []
        for key, label, desc in PAGES:
            if key == "project":
                continue
            item = _current_session(key)
            if item is None:
                lines.append(f"· {label}（{key}）：还没有任务。{desc}")
                continue
            store = item.get("store") or {}
            history = store.get("history") or []
            running = "【正在跑一件事】" if status.get(key) else ""
            lines.append(
                f"· {label}（{key}）{running}\n"
                f"    任务：{item.get('name') or '(没名字)'}　最近动过：{_when(item) or '—'}\n"
                f"    工作目录：{item.get('folder') or '—'}\n"
                f"    材料 {len(store.get('artifacts') or [])} 份，对话 {len(history)} 条")
        return "\n".join(lines) or "（还没有任何页面有任务）"
    if name == "read_page_materials":
        page, item = _need(arguments.get("page"))
        store = harness.ContextStore.from_dict(item.get("store") or {})
        # 材料清单直接用存档里那份（ContextStore 的字段是私有的，index_text 只在有材料时好看）
        arts = (item.get("store") or {}).get("artifacts") or []
        if not arts:
            return (f"{PAGE_LABELS[page]} 这一页还没有材料。"
                    "（截图分析、导入文件、爬取结果都会变成材料）")
        head = f"{PAGE_LABELS[page]} 共 {len(arts)} 份材料："
        return head + "\n" + store.index_text()
    if name == "read_page_material":
        page, item = _need(arguments.get("page"))
        store = harness.ContextStore.from_dict(item.get("store") or {})
        artifact = store.get(str(arguments.get("artifact_id", "")).strip())
        if artifact is None:
            return f"{PAGE_LABELS[page]} 里没有这个编号的材料，先调 read_page_materials 看看。"
        return f"【{PAGE_LABELS[page]} · {artifact.title}】\n" + _clip(artifact.text)
    if name == "read_page_files":
        page, item = _need(arguments.get("page"))
        try:
            limit = int(arguments.get("limit") or 40)
        except (TypeError, ValueError):
            limit = 40
        limit = max(1, min(200, limit))
        folder = str(item.get("folder") or "")
        rows = _list_files(folder, limit)
        # 历史里记着的产出（[[output-file]] 那套）也算上：文件可能已经挪走了
        made = []
        for entry in (item.get("store") or {}).get("history") or []:
            for one in entry.get("outputs") or []:
                path = one.get("path") if isinstance(one, dict) else one
                if path and path not in made:
                    made.append(str(path))
        lines = [f"{PAGE_LABELS[page]} 的工作目录：{folder or '—'}"]
        if rows:
            lines.append(f"目录里 {len(rows)} 个文件（最多列 {limit} 个）：")
            for path, size, stamp in rows:
                when = time.strftime('%m-%d %H:%M', time.localtime(stamp))
                lines.append(f"  {path.name}　{size} 字节　{when}　{path}")
        if made:
            lines.append("这一页记录过的产出文件：")
            lines.extend(f"  {path}" for path in made[:limit])
        if not rows and not made:
            lines.append("（目录是空的，也没有产出记录）")
        return "\n".join(lines)
    if name == "read_page_chat":
        page, item = _need(arguments.get("page"))
        try:
            limit = int(arguments.get("limit") or 6)
        except (TypeError, ValueError):
            limit = 6
        limit = max(1, min(40, limit))
        history = (item.get("store") or {}).get("history") or []
        if not history:
            return f"{PAGE_LABELS[page]} 这一页还没有对话记录。"
        lines = [f"{PAGE_LABELS[page]} 最近 {min(limit, len(history))} 条（共 {len(history)} 条）："]
        for entry in history[-limit:]:
            who = "用户" if entry.get("role") == "user" else PAGE_LABELS[page]
            body = str(entry.get("content") or "").strip().replace("\n", " ")
            lines.append(f"[{who}] {_clip(body, 1200)}")
        return "\n".join(lines)
    raise RuntimeError(f"没有这个工具：{name}")


# --------------------------------------------------------------------------
# MCP 循环（和 mcp_tools.py 一模一样的骨架）
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
                               "serverInfo": {"name": "quantification-pages",
                                              "version": "1.0.0"}})
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
            except Exception as exc:                    # 工具内部出错当结果回，别断连接
                reply(request_id, {"content": [{"type": "text", "text": f"出错了：{exc}"}],
                                   "isError": True})
        elif request_id is not None:
            reply_error(request_id, f"不支持的方法：{method}")


if __name__ == "__main__":
    print("页面互通 MCP server 已启动", file=sys.stderr)
    serve()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地记忆库的 MCP 工具，给 dsh 那边的 agent 用。

记忆存在数据目录的 memory/ 下，一条一个 .md（见 memory.py）。这个脚本只是把它
包成几个工具，让模型能自己记、自己查、自己连线：

    memory_save     记一条（标题 + 正文 + 标签），或者改已有的（给 memory_id）
    memory_search   按关键词找记忆（问「我以前记过类似的吗」时用）
    memory_read     读一条的全文
    memory_link     把两条连起来，附一句「为什么相关」
    memory_list     列最近改过的几条（先看看都记过什么）
    memory_import   把一个文件（PDF/Word/Excel/文本）转成一条记忆

「这一轮在哪一页」由应用写在 memory/current-area.json 里（每轮对话前刷新），
所以不给 area 时，记忆就落在用户正看着的那一区。

stdout 只能出现 JSON-RPC（多打一行字就会把协议弄坏），所以调试信息一律走 stderr。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config              # noqa: E402
import memory              # noqa: E402

PROTOCOL_FALLBACK = "2025-06-18"


def current_area() -> str:
    """应用这一轮在哪一页（没写指针就按 Maestro）。"""
    try:
        path = memory.memory_dir() / "current-area.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        area = str(data.get("area") or "")
        return area if area in memory.AREA_KEYS else "maestro"
    except (OSError, ValueError):
        return "maestro"


def area_label(area: str) -> str:
    for key, zh, _en in memory.AREAS:
        if key == area:
            return zh
    return area


TOOLS = [
    {
        "name": "memory_save",
        "description": "往本地记忆库记一条：值得以后还记得的事。**只记用户真说过的话、或者"
                       "真发生过的事实**（见提示词里的记录纪律），你自己的推断不要写进来。"
                       "type 说这条是什么（user 用户是谁 / feedback 我该怎么做事 / "
                       "project 在做什么 / reference 东西在哪）；标题一句话，正文写清楚前因后果，"
                       "标签方便以后找；desc 写一句「什么时候该读它」。改已有的记忆就带上 "
                       "memory_id。默认记在当前这一区（Maestro/Analyse/…），层默认是 concept。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "一句话标题"},
                "body": {"type": "string", "description": "正文（Markdown）"},
                "type": {"type": "string", "enum": ["user", "feedback", "project", "reference"],
                         "description": "这条记的是什么：user 用户是谁（身份、称呼、偏好、习惯）"
                                        " / feedback 我该怎么做事（做法、禁忌、以及为什么）"
                                        " / project 在做什么（目标、决定、进度、谁在等）"
                                        " / reference 东西在哪（文件、链接、外部系统）"},
                "kind": {"type": "string",
                         "enum": ["raw", "summary", "concept", "qa", "decision", "memo", "health"],
                         "description": "记到哪一层：素材 raw（原样收着的东西）/ 摘要 summary"
                                        "（一条素材一条）/ 概念 concept（可复用的规矩、结论，默认）"
                                        "/ qa 问答 / decision 决策 / memo 备忘 / health 体检"},
                "who": {"type": "string", "enum": ["user", "model", "file"],
                        "description": "这话是谁说的：user 用户原话（[stated]）/ model 你自己的"
                                       "结论或总结 / file 从文件里读到的。拿不准就用 model"},
                "desc": {"type": "string",
                         "description": "一句话：这条覆盖什么、什么时候该读它（列表里显示的就是它）"},
                "aliases": {"type": "array", "items": {"type": "string"},
                            "description": "别的叫法（同一个东西的其它名字），检索和连线都认"},
                "tags": {"type": "array", "items": {"type": "string"},
                         "description": "几个标签，例如 [偏好, 表格]"},
                "area": {"type": "string",
                         "description": "记到哪一区：maestro/analyse/crawler/sentiment/genesis（不填＝当前页）"},
                "links": {"type": "array", "items": {"type": "string"},
                          "description": "可选：直接连到哪几条记忆的 id"},
                "importance": {"type": "integer",
                               "description": "1-5，5 最重要。图上越重要越靠近圆心："
                                              "用户的硬性偏好/长期约定给 5，一次性的观察给 1-2，默认 3"},
                "memory_id": {"type": "string", "description": "可选：改这条（不填就是新建）"},
            },
            "required": ["title", "body"],
        },
    },
    {
        "name": "memory_search",
        "description": "按关键词搜记忆库，返回最相关的几条（标题 + 摘要 + id）。"
                       "开始一件事之前、或者用户提到「上次那个」时先搜一下。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要找什么"},
                "area": {"type": "string", "description": "可选：只在某一区里找"},
                "limit": {"type": "integer", "description": "最多几条，默认 8"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_read",
        "description": "读一条记忆的全文（先用 memory_search 拿到 id）。",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
    {
        "name": "memory_link",
        "description": "把两条记忆连起来，并写一句「为什么相关」——图上就是一条线，"
                       "以后顺着这条线能找到相关的东西。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "string", "description": "记忆 id"},
                "b": {"type": "string", "description": "另一条记忆 id"},
                "why": {"type": "string", "description": "为什么相关（一句话）"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "memory_list",
        "description": "列记忆库里最近改过的几条（标题 + 区 + 时间）。想知道都记过什么时用它。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "最多几条，默认 15"}},
            "required": [],
        },
    },
    {
        "name": "memory_import",
        "description": "把一个文件（PDF / Word / Excel / CSV / 文本）转成 Markdown 存进记忆库。"
                       "用户说「把这个文件记下来」时用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件的绝对路径"},
                "title": {"type": "string", "description": "可选：给它起个标题"},
                "area": {"type": "string", "description": "可选：记到哪一区"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["path"],
        },
    },
]


def _brief(item, limit=160):
    body = " ".join((item.get("body") or "").split())
    return body[:limit] + ("…" if len(body) > limit else "")


def call_tool(name, arguments):
    args = arguments or {}
    if name == "memory_save":
        item = memory.save(
            title=args.get("title"), body=args.get("body"),
            area=args.get("area") or current_area(),
            tags=args.get("tags"), links=args.get("links"),
            source="chat", memory_id=args.get("memory_id"),
            importance=args.get("importance"), by="model",
            kind=args.get("kind"), who=args.get("who"), desc=args.get("desc"),
            aliases=args.get("aliases"), mem_type=args.get("type"))
        return (f"记下了：{item['title']}（id {item['id']}，{area_label(item['area'])} 区，"
                f"类型 {item.get('type') or 'feedback'}，层 {item.get('kind') or 'inbox'}，"
                f"谁说的 {item.get('who') or 'model'}，重要度 {item['importance']}/5）"
                + ("\n已连到：" + ", ".join(item["links"]) if item["links"] else ""))
    if name == "memory_search":
        hits = memory.search(args.get("query") or "", area=args.get("area"),
                             limit=args.get("limit") or 8)
        if not hits:
            return "记忆库里没找到相关的。"
        return "\n\n".join(
            f"· [{item.get('type') or 'feedback'}] {item['title']}（id {item['id']}，"
            f"{item.get('kind') or 'inbox'}，"
            f"{area_label(item['area'])} 区"
            + (f"，标签 {', '.join(item['tags'])}" if item["tags"] else "") + "）\n  "
            + _brief(item) for item in hits)
    if name == "memory_read":
        item = memory.get(str(args.get("memory_id") or ""))
        if not item:
            return "没有这条记忆（id 对不对？先用 memory_search 找）"
        return (f"# {item['title']}\n（id {item['id']}，类型 {item.get('type') or 'feedback'}，"
                f"{area_label(item['area'])} 区"
                f"，更新于 {item.get('updated') or item.get('created')}"
                + (f"，标签 {', '.join(item['tags'])}" if item["tags"] else "")
                + (f"，连到 {', '.join(item['links'])}" if item["links"] else "")
                + "）\n\n" + (item.get("body") or ""))
    if name == "memory_link":
        memory.link(str(args.get("a") or ""), str(args.get("b") or ""), str(args.get("why") or ""))
        return "连上了。"
    if name == "memory_list":
        items = memory.load_all()[:max(1, int(args.get("limit") or 15))]
        if not items:
            return "记忆库还是空的。"
        return "\n".join(f"· {item['title']}（id {item['id']}，{area_label(item['area'])} 区，"
                         f"{item.get('updated') or item.get('created')}）" for item in items)
    if name == "memory_import":
        item = memory.import_file(str(args.get("path") or ""),
                                  area=args.get("area") or current_area(),
                                  title=args.get("title"), tags=args.get("tags"))
        return (f"存好了：{item['title']}（id {item['id']}，{area_label(item['area'])} 区，"
                f"{len(item['body'])} 字）")
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
                    "serverInfo": {"name": "quanta-memory", "version": "1.0.0"},
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
            print(f"[memory_tools] {method} 出错：{type(exc).__name__}: {exc}", file=sys.stderr)
            if request_id is not None:
                reply_error(request_id, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    serve()

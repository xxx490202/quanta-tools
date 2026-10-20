#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时任务的 MCP 工具，给 dsh 那边的 agent 用。

任务是这么来的：用户在左栏 Scheduled 那页点「去 Maestro 中创建」，应用就把他带到
Maestro 的对话里（输入框预置一句开头），然后**用聊的**把任务讲清楚 —— agent 问清
时间、要做什么，再调这里的工具落下来。所以不给一堆表单选项。

    schedule_create  建一条（标题、指令、什么时候跑、哪一页、无人值守的权限档）
    schedule_list    看现在有哪些（以及下次什么时候跑）
    schedule_update  改一条（改时间、改指令、暂停/启用都行）
    schedule_delete  删一条

任务存在 <数据目录>/schedules.json（和界面共用同一份，见 schedules.py）。
**注意**：这一轮跑在哪个会话里，由应用那边负责给它建（agent 只写任务本身），
所以建完回话时别说「已经给你开了个会话」——那是应用随后自己补的。

stdout 只能出现 JSON-RPC（多打一行字就把协议弄坏了），调试信息一律走 stderr。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import schedules         # noqa: E402

PROTOCOL_FALLBACK = "2025-06-18"
PAGE_HINT = ("project=Maestro, analyse=Analyse, crawler=Crawler, "
             "sentiment=Sentiment, genesis=Genesis")

TOOLS = [
    {
        "name": "schedule_create",
        "description": ("给用户建一条定时任务（到点自动跑一轮）。调用前先问清楚三件事："
                        "① 要它做什么（prompt 写具体一点）② 什么时候跑 ③ 跑在哪一页；"
                        "用户没说就按对话里的上下文定，别反复追问。"
                        f"页面的取值：{PAGE_HINT}。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "任务名（左栏会显示它）"},
                "prompt": {"type": "string", "description": "到点发给模型的那句话"},
                "repeat": {"type": "string",
                           "enum": ["interval", "daily", "weekday", "weekly"],
                           "description": "interval=每隔 N 分钟；daily=每天；weekday=工作日；weekly=每周几"},
                "minutes": {"type": "number", "description": "repeat=interval 时间隔多少分钟"},
                "at": {"type": "string", "description": "repeat=daily/weekday/weekly 时的时刻，HH:MM（15 分钟一档）"},
                "weekday": {"type": "number", "description": "repeat=weekly 时周几：1=周一 … 7=周日"},
                "page": {"type": "string",
                         "description": f"跑在哪一页（默认就是当前这一页）：{PAGE_HINT}"},
                "permission": {"type": "string", "enum": ["read-only", "workspace-write", "danger-full-access"],
                               "description": "没人看着时越界操作怎么应答（默认 read-only）"},
                "folder": {"type": "string", "description": "工作目录（不填就用这一页的默认目录）"},
            },
            "required": ["title", "prompt", "repeat"],
        },
    },
    {
        "name": "schedule_list",
        "description": "列出现在的定时任务（名字、什么时候跑、下次什么时候、上次结果）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "schedule_update",
        "description": ("改一条定时任务。只传要改的字段；enabled=false 就是暂停它。"
                        "id 从 schedule_list 里拿。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"},
                "prompt": {"type": "string"},
                "enabled": {"type": "boolean"},
                "repeat": {"type": "string", "enum": ["interval", "daily", "weekday", "weekly"]},
                "minutes": {"type": "number"},
                "at": {"type": "string"},
                "weekday": {"type": "number"},
                "page": {"type": "string"},
                "permission": {"type": "string"},
                "folder": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "schedule_delete",
        "description": "删一条定时任务（id 从 schedule_list 里拿）。用户明确说要删才删。",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}},
                        "required": ["id"]},
    },
]


def _when_from(args: dict) -> dict:
    kind = str(args.get("repeat") or "daily")
    when = {"kind": kind if kind in schedules.KINDS else "daily"}
    if when["kind"] == "interval":
        when["minutes"] = int(args.get("minutes") or 30)
    else:
        when["at"] = str(args.get("at") or "09:00")
        if when["kind"] == "weekly":
            when["weekday"] = int(args.get("weekday") or 1)
    return when


def _brief(item: dict) -> str:
    flags = [] if item["enabled"] else ["已暂停"]
    return (f"· {item['title']}（id {item['id']}）\n"
            f"  {schedules.describe(item['when'])} · {item['page']} · {item['permission']}"
            + (" · " + " ".join(flags) if flags else "")
            + (f"\n  下次 {schedules._stamp(item['next_run'])}" if item.get("next_run") else "")
            + (f" · 上一轮 {item['last_status']}" if item.get("last_status") else ""))


def call_tool(name, arguments):
    args = arguments or {}
    if name == "schedule_create":
        data = schedules.load()
        item = schedules.normalize({
            "title": args.get("title"), "prompt": args.get("prompt"),
            "when": _when_from(args), "page": args.get("page") or _current_page(),
            "permission": args.get("permission") or "read-only",
            "folder": args.get("folder") or "", "enabled": True,
        })
        item["next_run"] = schedules.next_after(item["when"])
        data["items"].append(item)
        schedules.save(data)
        return (f"建好了：{item['title']}（id {item['id']}）\n"
                f"· 调度：{schedules.describe(item['when'])}，下次 {schedules._stamp(item['next_run'])}\n"
                f"· 跑在：{item['page']}，无人值守权限 {item['permission']}\n"
                f"· 指令：{item['prompt']}\n"
                "（应用随后会把它的会话备好，左栏那一行点开就是它；到点跑出来的对话也攒在里面。"
                "要改时间或指令随时说。）")
    if name == "schedule_list":
        items = schedules.load()["items"]
        if not items:
            return "现在还没有定时任务。"
        return f"一共 {len(items)} 条：\n" + "\n".join(_brief(i) for i in items)
    if name == "schedule_update":
        data = schedules.load()
        item = schedules.find(data["items"], str(args.get("id") or ""))
        if item is None:
            return "没有这条任务（id 对不对？先用 schedule_list 看一眼）"
        changed = []
        for key in ("title", "prompt", "page", "permission", "folder"):
            if args.get(key) is not None:
                item[key] = str(args[key])
                changed.append(key)
        if args.get("enabled") is not None:
            item["enabled"] = bool(args["enabled"])
            changed.append("enabled")
        if args.get("repeat") is not None or args.get("minutes") is not None or args.get("at") is not None:
            merged = dict(item["when"])
            merged.update({k: v for k, v in _when_from(args).items() if v is not None})
            item["when"] = schedules.normalize({"when": merged})["when"]
            item["next_run"] = schedules.next_after(item["when"])
            changed.append("when")
        data["items"] = [item if x.get("id") == item["id"] else x for x in data["items"]]
        schedules.save(data)
        return f"改好了：{item['title']}\n" + _brief(item) + f"\n（改了：{', '.join(changed) or '什么都没变'}）"
    if name == "schedule_delete":
        data = schedules.load()
        item = schedules.find(data["items"], str(args.get("id") or ""))
        if item is None:
            return "没有这条任务（id 对不对？）"
        data["items"] = [x for x in data["items"] if x.get("id") != item["id"]]
        schedules.save(data)
        return f"删掉了：{item['title']}"
    return f"不认识的工具：{name}"


def _current_page() -> str:
    """这一轮在哪一页：应用每轮会写一份指针（memory/current-area.json 里那份）。"""
    try:
        import memory
        data = json.loads((memory.memory_dir() / "current-area.json").read_text(encoding="utf-8"))
        page = str(data.get("page") or data.get("area") or "")
        return page if page in schedules.PAGES else "project"
    except Exception:                                                   # noqa: BLE001
        return "project"


def reply(request_id, result):
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False),
          flush=True)


def reply_error(request_id, message):
    print(json.dumps({"jsonrpc": "2.0", "id": request_id,
                      "error": {"code": -32000, "message": str(message)}}, ensure_ascii=False),
          flush=True)


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
                    "serverInfo": {"name": "quanta-schedule", "version": "1.0.0"},
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
        except Exception as exc:                                        # noqa: BLE001
            print(f"[schedule_tools] {method} 出错：{type(exc).__name__}: {exc}", file=sys.stderr)
            if request_id is not None:
                reply_error(request_id, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    serve()

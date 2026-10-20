#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 agent 一个「反问你」的工具。

agent 遇到需要用户拍板、自己又猜不准的事（选哪个方案、跑在哪一页、授权范围、
要不要删），别瞎猜也别闷头做 —— 调 ask_user 问一句，界面上会弹一张选择卡，
用户点完答案直接回到它手里。

来回是怎么走的（不用新开端口，全靠文件）：

    agent 调工具 → 写 <数据目录>/ask/<id>.json（问题）
    应用（桥）的守护线程扫到这个文件 → 发事件给界面 → 界面上弹出那张卡
    用户点「提交」→ 界面把答案交给桥 → 桥写 <数据目录>/ask/<id>.answer.json
    这个工具一直在等那个文件 → 读到就返回给 agent（顺手把两个文件删掉）

没人管（超时）或者用户点「忽略」→ 返回「用户跳过了」，agent 自己定或者停下问。

stdout 只能出现 JSON-RPC（多打一行字就把协议弄坏了），调试信息一律走 stderr。
"""

import json
import os
import pathlib
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config            # noqa: E402

PROTOCOL_FALLBACK = "2025-06-18"
MAX_QUESTIONS = 4
MAX_OPTIONS = 4
TIMEOUT_SECONDS = 10 * 60

TOOLS = [
    {
        "name": "ask_user",
        "description": ("需要用户拍板、而你自己猜不准的时候，弹一张选择卡问他。合适："
                        "选哪个方案、跑在哪一页、授权范围、要不要删东西、两个写法挑一个。"
                        "不合适：能从上下文推断的、能自己查清楚的、以及纯粹为了让用户确认你在"
                        "干活的——那些别问。一次最多 4 个问题，每题 2–4 个选项；"
                        "用户还能自己写一句（选项之外的那个「其他」）。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "要问的问题（最多 4 个）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "header": {"type": "string", "description": "短标签，2–4 个字（比如「后半句」「授权范围」）"},
                            "question": {"type": "string", "description": "问题本身，一句话说清"},
                            "options": {
                                "type": "array",
                                "description": "2–4 个选项",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "短标题（1–5 个词）"},
                                        "description": {"type": "string", "description": "一句话说明这个选项什么意思"},
                                    },
                                    "required": ["label"],
                                },
                            },
                            "multi": {"type": "boolean", "description": "允许多选（默认单选）"},
                        },
                        "required": ["question", "options"],
                    },
                },
            },
            "required": ["questions"],
        },
    },
]


def ask_dir() -> pathlib.Path:
    return pathlib.Path(config.DATA_DIR) / "ask"


def _clean(questions) -> list:
    out = []
    for raw in (questions or [])[:MAX_QUESTIONS]:
        if not isinstance(raw, dict):
            continue
        options = []
        for option in (raw.get("options") or [])[:MAX_OPTIONS]:
            if isinstance(option, dict):
                label = str(option.get("label") or "").strip()
                if not label:
                    continue
                options.append({"label": label[:80],
                                "description": str(option.get("description") or "").strip()[:200]})
            elif str(option).strip():
                options.append({"label": str(option).strip()[:80], "description": ""})
        if len(options) < 2:
            continue                       # 少于两个选项就不算选择题了，跳过
        out.append({"header": str(raw.get("header") or "").strip()[:12],
                    "question": str(raw.get("question") or "").strip()[:400],
                    "options": options, "multi": bool(raw.get("multi"))})
    return out


def _format(questions: list, data: dict) -> str:
    """把用户的答案翻成 agent 好读的一段话。"""
    answers = data.get("answers") or []
    if data.get("dismissed") or not answers:
        return "（用户跳过了，没有回答）"
    lines = []
    for index, question in enumerate(questions):
        picked = answers[index] if index < len(answers) else None
        if isinstance(picked, dict):                       # 自己写的那种
            text = str(picked.get("text") or "").strip() or "(空)"
        elif isinstance(picked, list):
            text = "、".join(str(x) for x in picked) or "(没选)"
        else:
            text = str(picked or "(没选)")
        head = question.get("header") or question.get("question", "")[:12]
        lines.append(f"· {head}：{text}")
    return "用户的选择：\n" + "\n".join(lines) + "\n（就按这个来；要改他会自己说。）"


def call_tool(name, arguments):
    if name != "ask_user":
        return f"不认识的工具：{name}"
    questions = _clean((arguments or {}).get("questions"))
    if not questions:
        return "（没问成：至少要一个问题、每题至少两个选项）"
    folder = ask_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = f"ask-{time.time_ns()}"
    question_file = folder / f"{stamp}.json"
    answer_file = folder / f"{stamp}.answer.json"
    # page：这一问是从哪一页问的（桥起的 sidecar 时写进来的）。用户可能在别的页上，
    # 桥靠它把「有人在等你」标到那条会话上（见 bridge 的等待账）
    question_file.write_text(json.dumps({"id": stamp, "at": time.time(), "questions": questions,
                                         "page": str(os.environ.get("QUANTA_PAGE") or "")},
                                        ensure_ascii=False), encoding="utf-8")
    deadline = time.time() + TIMEOUT_SECONDS
    try:
        while time.time() < deadline:
            if answer_file.exists():
                try:
                    data = json.loads(answer_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    time.sleep(0.3)
                    continue
                return _format(questions, data)
            time.sleep(0.4)
        return "（等了十分钟用户也没回答 —— 别干等：要么按你认为最合理的默认先做，要么把这一步停下来告诉他。）"
    finally:
        for path in (question_file, answer_file):
            try:
                path.unlink()
            except OSError:
                pass


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
                    "serverInfo": {"name": "quanta-ask", "version": "1.0.0"},
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
            print(f"[ask_tools] {method} 出错：{type(exc).__name__}: {exc}", file=sys.stderr)
            if request_id is not None:
                reply_error(request_id, f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    serve()

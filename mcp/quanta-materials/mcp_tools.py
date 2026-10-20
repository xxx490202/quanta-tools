#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把应用里的「材料」暴露成 MCP 工具，给 dsh 那边的 agent 用。

这个脚本是个 MCP server（stdio，一行一个 JSON-RPC），由 dsh 按 patch 里的配置拉起来。
它自己不产生材料，只是按只读方式读应用存下的 sessions.json：

    list_artifacts   列出当前项目里的材料
    read_artifact    读某条材料的全文
    ask_jev          对某条材料跑一次 jev 结构化判断

「当前项目是哪一条」由应用写在 <dsh-home>/current-session.json 里——每轮对话前刷新，
所以 agent 看到的始终是用户正看着的那个项目。

stdout 只能出现 JSON-RPC（多打一行字就会把协议弄坏），所以调试信息一律走 stderr。
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config              # noqa: E402
import harness             # noqa: E402
from jev_questions import QUESTIONS  # noqa: E402

PROTOCOL_FALLBACK = "2025-06-18"
POINTER = os.path.join(config.DSH_HOME, "current-session.json")
MAX_RESULT_CHARS = 8000

TOOLS = [
    {
        "name": "list_artifacts",
        "description": "列出当前项目里的全部材料（编号、类型、标题、时间、字数）。"
                       "需要知道有哪些分析结果可读时先调用它。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_artifact",
        "description": "读取某条材料的完整文本内容（截图转写、文件内容、数据分析结果等）。",
        "inputSchema": {
            "type": "object",
            "properties": {"artifact_id": {"type": "string",
                                           "description": "材料编号，例如 a1、a2"}},
            "required": ["artifact_id"],
        },
    },
    {
        "name": "ask_jev",
        "description": "对某条材料调用 jev 模型做结构化判断（情绪、情绪强度、意图、紧急度、"
                       "关系亲密度、是否需要回复、是否话里有话、是否在施压、关系风险、"
                       "建议回复策略），每项带概率或置信度。需要客观判断而不是写一段话时用它。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "description": "材料编号，例如 a1"},
                "dimensions": {
                    "type": "array", "items": {"type": "string"},
                    "description": "可选，只问其中几个维度，可选值：emotion、emotion_intensity、"
                                   "intent、urgency、intimacy、needs_reply、has_subtext、"
                                   "is_pressuring、relationship_risk、reply_strategy。"
                                   "不传则全部问一遍。",
                },
            },
            "required": ["artifact_id"],
        },
    },
]


def current_session():
    """应用正在看的项目：读指针文件，再从 sessions.json 里取出它。"""
    try:
        with open(POINTER, encoding="utf-8") as handle:
            pointer = json.load(handle)
        session_id = pointer.get("session_id")
    except (OSError, ValueError):
        return None
    path = config.DATA_DIR / "sessions.json"
    try:
        with open(path, encoding="utf-8") as handle:
            books = json.load(handle)
    except (OSError, ValueError):
        return None
    for book in (books or {}).values():
        sessions = (book or {}).get("sessions") or []
        for session in sessions:
            if session.get("id") == session_id:
                store = harness.ContextStore.from_dict(session.get("store") or {})
                return session, store
    return None


def _clip(text, limit=MAX_RESULT_CHARS):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n...（已截断，原文共 {len(text)} 字）"


def _need_session():
    found = current_session()
    if found is None:
        raise RuntimeError("应用还没有给出当前项目（或者这个项目里还没有材料）。"
                           "让用户在界面里先做一次分析或选一个项目。")
    return found


def call_tool(name, arguments):
    arguments = arguments or {}
    if name == "list_artifacts":
        _, store = _need_session()
        return store.index_text()
    if name == "read_artifact":
        _, store = _need_session()
        artifact = store.get(str(arguments.get("artifact_id", "")).strip())
        if artifact is None:
            return "没有这个编号的材料，请先用 list_artifacts 看看有哪些。"
        return _clip(artifact.text)
    if name == "ask_jev":
        _, store = _need_session()
        artifact = store.get(str(arguments.get("artifact_id", "")).strip())
        if artifact is None:
            return "没有这个编号的材料，请先用 list_artifacts 看看有哪些。"
        if not (artifact.text or "").strip():
            return f"材料 {artifact.id} 没有文本内容，无法交给 jev。"
        dimensions = arguments.get("dimensions") or None
        if isinstance(dimensions, list) and dimensions:
            questions = {k: QUESTIONS[k] for k in dimensions if k in QUESTIONS} or QUESTIONS
        else:
            questions = QUESTIONS
        import api
        try:
            response = api.jev_ask(_clip(artifact.text, config.JEV_STATE_MAX_CHARS), questions)
        except api.ApiError as exc:
            return f"jev 调用失败：{exc}"
        return json.dumps(harness._compact_answers(response), ensure_ascii=False, indent=2)
    raise RuntimeError(f"没有这个工具：{name}")


# --------------------------------------------------------------------------
# MCP 循环
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
            # 回客户端要的版本：它对不上会自己降级；这里只有工具能力
            version = params.get("protocolVersion") or PROTOCOL_FALLBACK
            reply(request_id, {"protocolVersion": version,
                               "capabilities": {"tools": {"listChanged": False}},
                               "serverInfo": {"name": "quantification-materials",
                                              "version": "1.0.0"}})
        elif method in ("notifications/initialized", "notifications/cancelled"):
            continue                                   # 通知不需要回
        elif method == "ping":
            reply(request_id, {})
        elif method == "tools/list":
            reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            try:
                text = call_tool(params.get("name"), params.get("arguments"))
                reply(request_id, {"content": [{"type": "text", "text": text}],
                                   "isError": False})
            except Exception as exc:                    # 工具内部出错要当成工具结果回，别中断连接
                reply(request_id, {"content": [{"type": "text", "text": f"出错了：{exc}"}],
                                   "isError": True})
        elif request_id is not None:
            reply_error(request_id, f"不支持的方法：{method}")


if __name__ == "__main__":
    print(f"量化材料 MCP server 已启动（项目: "
          f"{(current_session() or [{}])[0].get('name') or '未指定'}）", file=sys.stderr)
    serve()

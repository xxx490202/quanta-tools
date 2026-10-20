#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把「另外两个 agent 运行时」暴露成 MCP 工具（协作：一问一答）。

主力 harness 之外的另外两个，随时能当顾问问一句：

    ask_claude     Claude Code（用户自己那份，登录态在 ~/.claude）
    ask_codex      Codex（登录态和配置在 ~/.codex）
    ask_deepseek   应用自带的运行时（dsh：真 agent 一轮，能自己翻文件；默认只读）

**命名子智能体（续跑）**：派活时带一个 `agent: '名字'`（如 `agent: 'logger-hunter'`）——
第一次是开一条新会话、记下会话 id；**同一个名字再问就是接着它上次的上下文继续**
（claude 走 `--resume`、codex 走 `exec resume`、dsh 用稳定 session id），它记得看过
哪些文件、干过什么。不带名字 = 每次新开。注册表在 <数据目录>/subagents.json，
`subagent_list` 列出来。第一次派活仍要自带全部背景（它看不到这边的对话）。

主力那一个不出现（环境变量 QUANTA_MAIN_HARNESS 说了是谁）——省得它自己问自己。

默认只读（能看不能改），`allow_write=true` 才放开写入——提示词里同样写死了：
只有用户明确让动文件才开。

stdout 只能出现 JSON-RPC（多打一行字就把协议弄坏了），调试信息一律走 stderr。
"""

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config              # noqa: E402
import workflow as workflows   # noqa: E402  （工作流引擎：run_workflow / workflow_status）

PROTOCOL_FALLBACK = "2025-06-18"
MAX_RESULT_CHARS = 6000
CLI_TIMEOUT = 900          # 一个 agent 跑一轮给 15 分钟（它可能真的在翻文件）
DEEPSEEK_TIMEOUT = 180

MAIN = str(os.environ.get("QUANTA_MAIN_HARNESS") or "deepseek").strip().lower()
# 当前项目的工作目录：桥在起这个 server 时写进来的（就是这一轮 agent 的 cwd）
PROJECT_DIR = str(os.environ.get("QUANTA_PROJECT_DIR") or "").strip()


def _tool(name, title, has_tools=True) -> dict:
    hint = ("它自己会翻文件、跑命令" if has_tools else "它是一个纯顾问：只能读你写在问题里的东西")
    return {
        "name": name,
        "description": f"把一件事整段交给 {title} 单独跑一轮，拿回它的结论（{hint}）。"
                       "它看不到当前这段对话：问题要自带全部背景（要什么、什么算好、"
                       "相关资料在哪个文件），像发给一个刚进场的同事。"
                       "默认只读；只有用户明确让动文件时才传 allow_write=true。"
                       "每当二次验证、要另一个模型的角度、或这个活更适合那边跑时用它；"
                       "小事别用（每问一次都是一整轮，慢、而且花用户自己的额度）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "要问的事，自带背景"},
                "cwd": {"type": "string",
                        "description": "可选：在哪个目录里干活，默认当前项目目录"},
                "schema": {"type": "object",
                           "description": "可选：要它只回一个 JSON 就传 JSON Schema"
                                          "（Claude / Codex 走各自的原生结构化输出，回来的是"
                                          "校验过的 JSON 文本）"},
                **({"allow_write": {"type": "boolean",
                                    "description": "可选：允许它改文件（默认 false，只读）"}}
                   if has_tools else {}),
                "agent": {"type": "string",
                          "description": "可选：给这次派活起个**名字**（如 logger-hunter）。"
                                         "同一个名字再问一次 = 接着它上次的上下文继续"
                                         "（它记得上次干过什么、看过哪些文件）；不写名字就"
                                         "每次新开一个。要连续追问同一件事，就用同一个名字。"},
                "fresh": {"type": "boolean",
                          "description": "可选：名字已经有会话时，这个 true 表示不管旧会话、"
                                         "这一轮重新开一个（默认 false = 续跑）"},
            },
            "required": ["prompt"],
        },
    }


ALL_TOOLS = [
    _tool("ask_claude", "Claude Code"),
    _tool("ask_codex", "Codex"),
    _tool("ask_deepseek", "DeepSeek（应用自带的那个运行时）"),
    {
        "name": "subagent_list",
        "description": "看看已经派过哪些**有名字的**子智能体（名字、哪家运行时、跑过几轮、"
                       "最近一次用的目录），以及各自的会话还在不在。用同一个名字再问就是"
                       "接着它继续——拿不准名字拼写或者想知道谁还能追问，先看这个。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "run_workflow",
        "description": "跑一个**确定性工作流脚本**（JS）：把一件大活拆成若干步、交给多个 agent "
                       "并行或流水线地做。适合：要跑十几个独立子任务、或者多阶段（先各自调研、"
                       "再汇总、再验证）的活。脚本里能用：agent(prompt, {agent, name, cwd, schema, "
                       "allowWrite})（name = 给这一步的子 agent 起名，同名的那几步接着彼此"
                       "上次的上下文继续）、parallel([...])、pipeline(items, ...stages)、phase('名字')、"
                       "budget（次数预算，remaining/spent）、log(text) 和 return 结果。"
                       "它在**后台**跑（立刻返回 run_id），进度看右栏「Workflow」那张卡，"
                       "或者用 workflow_status 轮询。跑挂了没关系：同一份脚本重跑会接着上次的"
                       "结果（有 journal 缓存），把 resume_from 传上一次的 run_id 就行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string", "description": "脚本源码（和 script_path 二选一）"},
                "script_path": {"type": "string", "description": "脚本文件路径（.js/.mjs）"},
                "name": {"type": "string", "description": "这个工作流叫什么（显示用）"},
                "cwd": {"type": "string", "description": "子 agent 干活的工作目录，默认当前项目目录"},
                "concurrency": {"type": "integer", "description": "同时最多几个 agent（默认 3，上限 8）"},
                "max_agents": {"type": "integer", "description": "这个工作流最多跑多少个 agent（默认 40）"},
                "resume_from": {"type": "string", "description": "可选：上一次的 run_id，接着它的结果跑"},
            },
            "required": [],
        },
    },
    {
        "name": "workflow_status",
        "description": "看工作流跑到哪儿了：不传 run_id 就列这次会话跑过的所有工作流"
                       "（状态、步数、预算用了多少、每步结果），传了就看那一个的详情。",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "可选：只看这一个"}},
            "required": [],
        },
    },
]
# 主力那个不挂：它问自己没有意义
TOOLS = [item for item in ALL_TOOLS if item["name"] != f"ask_{MAIN}"]


def _clip(text, limit=MAX_RESULT_CHARS):
    text = str(text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"\n...（已截断，全文 {len(text)} 字）"


def _workdir(arguments) -> str:
    """在哪个目录里干活：调用方给的 > 当前项目目录 > 用户主目录。"""
    given = str((arguments or {}).get("cwd") or "").strip()
    for candidate in (given, PROJECT_DIR, os.path.expanduser("~")):
        if candidate and os.path.isdir(candidate):
            return candidate
    return os.getcwd()


def _cli_env(cli: str) -> dict:
    """拉 CLI 时的环境：PATH 里先放它自己那个目录（node 脚本要 node）。"""
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        [os.path.dirname(cli)] + [config.path_with_node()])
    return env


def _run_cli(args, cwd, cli, timeout=CLI_TIMEOUT):
    """跑一个 CLI，返回 (stdout, stderr)；非零退出直接抛（带 stderr 的尾巴）。

    stdin 必须接 /dev/null：不接的话子进程继承的是**这个 MCP server 的 stdin**
    （就是那条 JSON-RPC 管道），`codex exec` 会把管道里的协议行当输入读、等 EOF，
    于是两边互等——实测就是这么卡住的。
    """
    try:
        proc = subprocess.run(args, cwd=cwd, env=_cli_env(cli), text=True,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"它跑了 {timeout // 60} 分钟还没完，先不等了（要么把问题拆小，"
                           f"要么把活拆开分几次问）")
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-600:]
        raise RuntimeError(f"它退出码 {proc.returncode}：{tail or '（没有输出）'}")
    return proc.stdout or "", proc.stderr or ""


def _schema_of(arguments):
    """这一步要结构化返回吗（opts.schema / arguments.schema）——给了就要求它只回一个 JSON。"""
    schema = (arguments or {}).get("schema")
    return schema if isinstance(schema, dict) and schema else None


# --------------------------------------------------------------------------
# 命名子智能体：同一个名字 = 同一条会话，再问就是「接着上次继续」
# --------------------------------------------------------------------------
# 一问一答的顾问有个大缺口：追问就得把背景重讲一遍。这里给它们加上「续跑」：
#   · ask_*(prompt, agent='侦察')  第一次 → 记下这次的会话 id；
#   · ask_*(prompt, agent='侦察')  再来 → 带着那次会话续跑（它记得看过什么、干过什么）；
#   · agent 不写 → 每次新开（老行为）；fresh:true → 有旧会话也重开。
# 三家各自怎么续：claude `--resume <session_id>`；codex `exec resume <thread_id>`；
# dsh 直接拿稳定 session id 再问（它按 id 认会话）。注册表放 <数据目录>/subagents.json。
_SUBAGENT_LOCK = threading.Lock()


def _subagent_path():
    return pathlib.Path(config.DATA_DIR) / "subagents.json"


def _load_subagents() -> dict:
    try:
        data = json.loads(_subagent_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_subagents(data: dict):
    path = _subagent_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _agent_name(arguments) -> str:
    """这次派活有没有名字（名字里的控制字符和斜杠去掉，留个像样的键）。"""
    raw = str((arguments or {}).get("agent") or "").strip()
    raw = re.sub(r"[\x00-\x1f\x7f/\\]+", "", raw)
    return raw[:40].strip()


def _agent_slot(harness: str, name: str) -> dict:
    if not name:
        return {}
    with _SUBAGENT_LOCK:
        return dict((_load_subagents().get(harness) or {}).get(name) or {})


def _agent_remember(harness: str, name: str, session: str, cwd="", note="") -> dict:
    """记下/更新这个命名子智能体的会话 id。返回更新后的那份。"""
    if not name or not session:
        return {}
    with _SUBAGENT_LOCK:
        data = _load_subagents()
        bucket = data.setdefault(harness, {})
        slot = dict(bucket.get(name) or {})
        slot["session"] = str(session)
        slot["turns"] = int(slot.get("turns") or 0) + 1
        slot["at"] = time.strftime("%m-%d %H:%M")
        if cwd:
            slot["cwd"] = str(cwd)
        if note:
            slot["note"] = str(note)[:160]
        bucket[name] = slot
        _save_subagents(data)
        return slot


def _banner_note(name: str, slot: dict, resumed: bool, extra: str = "") -> str:
    """答复抬头后面那半句：第几轮、是不是接着上次的（让主力知道它记得什么）。"""
    bits = []
    if name:
        bits.append(f"{name} · 第 {int(slot.get('turns') or 0)} 轮")
        bits.append("接着上次的上下文" if resumed else "新开的会话")
    if extra:
        bits.append(extra)
    return ("（" + "，".join(bits) + "）") if bits else ""


def subagent_list(arguments=None) -> str:
    """列出所有命名过的子智能体（给主力看：谁还能追问、谁已经跑了几轮）。"""
    data = _load_subagents()
    lines = []
    for harness, bucket in sorted(data.items()):
        for name, slot in sorted((bucket or {}).items()):
            bits = [f"- **{name}**（{harness}）：跑过 {int(slot.get('turns') or 0)} 轮",
                    f"最近 {slot.get('at') or '—'}",
                    f"目录 {slot.get('cwd') or '—'}"]
            if slot.get("session"):
                bits.append("会话在（同一个名字再问 = 接着它继续）")
            if slot.get("note"):
                bits.append(str(slot["note"]))
            lines.append("，".join(bits))
    if not lines:
        return ("还没有命名过的子智能体。派活时带上 `agent: '名字'`（如 agent: 'logger-hunter'）"
                "就会记住它——下次用同一个名字再问，它接着上次的上下文继续。")
    return ("命名子智能体（用同一个名字再派一次 = 接着它继续；`fresh: true` 是重开）：\n"
            + "\n".join(lines))


def ask_claude(arguments) -> str:
    cli = config.harness_cli_path("claude")
    if not cli:
        raise RuntimeError("找不到 claude 命令行（用户没装 Claude Code？）。"
                           "装法：npm install -g @anthropic-ai/claude-code")
    name = _agent_name(arguments)
    slot = _agent_slot("claude", name)
    resumed = bool(slot.get("session")) and not arguments.get("fresh")
    schema = _schema_of(arguments)

    def build(resume_id):
        args = [cli, "-p", str(arguments.get("prompt") or ""), "--output-format", "json"]
        # 给了 schema 就走它自己的结构化输出（--json-schema）：答复在 structured_output 里，
        # 是**已经校验过**的对象——不用再去正文里抠 JSON、也不用为格式重问一轮
        if schema:
            args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
        if arguments.get("allow_write"):
            args += ["--permission-mode", "acceptEdits"]
        if resume_id:
            args += ["--resume", str(resume_id)]
        return args

    note = ""
    try:
        stdout, _ = _run_cli(build(slot["session"] if resumed else ""),
                             _workdir(arguments), cli)
    except RuntimeError:
        if not resumed:
            raise
        # 那条会话找不到了（被清过、或换了账号）：这轮新开，别把工具弄坏
        resumed = False
        note = "旧会话找不到了，这轮新开"
        stdout, _ = _run_cli(build(""), _workdir(arguments), cli)
    text = stdout
    try:
        parsed = json.loads(stdout)
        if isinstance(parsed, dict):
            if parsed.get("is_error"):
                raise RuntimeError(f"它自己报了错：{_clip(parsed.get('result'), 800)}")
            structured = parsed.get("structured_output")
            if schema and structured is not None:
                text = json.dumps(structured, ensure_ascii=False)
            else:
                text = str(parsed.get("result") or "")
            if name and parsed.get("session_id"):
                slot = _agent_remember("claude", name, parsed["session_id"],
                                       _workdir(arguments),
                                       note=str(arguments.get("prompt") or "")[:100])
    except ValueError:
        pass                                   # 不是 JSON 就按纯文本用（老版本行为）
    return ("【Claude Code 的答复】" + _banner_note(name, slot, resumed, note) + "\n"
            + _clip(text))


def ask_codex(arguments) -> str:
    cli = config.harness_cli_path("codex")
    if not cli:
        raise RuntimeError("找不到 codex 命令行（用户没装 Codex？）。"
                           "装法：npm install -g @openai/codex")
    name = _agent_name(arguments)
    slot = _agent_slot("codex", name)
    resumed = bool(slot.get("session")) and not arguments.get("fresh")
    # 给了 schema 就用它的 --output-schema（要一个文件）：最终那条 agent_message 就是
    # 校验过的 JSON 本体
    schema = _schema_of(arguments)
    temp = None
    if schema:
        import tempfile
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                             encoding="utf-8", prefix="quanta-schema-")
        json.dump(schema, handle, ensure_ascii=False)
        handle.close()
        temp = handle.name
    prompt = str(arguments.get("prompt") or "")
    if schema:
        # 实测（2026-10-10）：codex 的 --output-schema 这版**只是建议**——只给标志、
        # 问法里没提形状时它会照样回裸值（"54"）。明说一句才稳
        prompt += ("\n\n按上面那个 JSON Schema 输出**一个 JSON**：不要解释、不要代码围栏、"
                   "不要前后缀。")

    sandbox = "workspace-write" if arguments.get("allow_write") else "read-only"

    def build(resume_id):
        # 选项都给在 `resume` 后面、SESSION_ID 前面（clap 的用法行就是
        # `codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]`）。
        # 注意：`exec resume` 这版**没有** --sandbox 开关（实测会报 usage 错），
        # 但收 -c：用 `-c sandbox_mode="…"` 传同一个设置
        args = [cli, "exec"] + (["resume"] if resume_id else [])
        args += ["--skip-git-repo-check", "--json"]
        if resume_id:
            args += ["-c", f'sandbox_mode="{sandbox}"']
        else:
            args += ["--sandbox", sandbox]
        if temp:
            args += ["--output-schema", temp]
        if resume_id:
            args.append(str(resume_id))
        args.append(prompt)
        return args

    def run(resume_id):
        stdout, _ = _run_cli(build(resume_id), _workdir(arguments), cli)
        # --json 是一行一个事件：最后一条 agent_message 是答复；thread.started 里的
        # thread_id 就是这条会话（续跑要用它）
        parts, thread = [], ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "thread.started":
                thread = str(event.get("thread_id") or "")
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                if item.get("text"):
                    parts.append(str(item["text"]))
        return parts, thread, stdout

    note = ""
    try:
        parts, thread, stdout = run(slot["session"] if resumed else "")
    except RuntimeError:
        if not resumed:
            raise
        resumed = False
        note = "旧会话找不到了，这轮新开"
        parts, thread, stdout = run("")
    finally:
        if temp:
            try:
                os.unlink(temp)
            except OSError:
                pass
    if not parts:
        raise RuntimeError("它这轮没有给出答复（只跑了过程就结束了）。"
                           f"原始输出尾巴：{_clip(stdout[-400:], 400)}")
    # 续跑时它不一定再报一次 thread.started：那就沿用旧的会话 id（不能把名字弄丢）
    session = thread or (str(slot.get("session")) if resumed else "")
    if name and session:
        slot = _agent_remember("codex", name, session, _workdir(arguments),
                               note=str(arguments.get("prompt") or "")[:100])
    return ("【Codex 的答复】" + _banner_note(name, slot, resumed, note) + "\n"
            + _clip("\n".join(parts)))


def ask_deepseek(arguments) -> str:
    """应用自带的运行时（dsh）跑一轮：和另外两个顾问一样，能自己在项目目录里翻文件。

    起的是一个独立模式（consult / consult_write，见 dsh_agent.MODES）：提示词就一段
    「你是顾问」，不挂本应用那套 MCP 工具，权限只读（allow_write 才用可写那档）。
    真跑不起来（dsh 没装）就退回直连接口那一条路——答得浅一点，但别让工具直接坏掉。
    """
    prompt = str(arguments.get("prompt") or "")
    schema = _schema_of(arguments)
    if schema:
        # dsh 没有对应的开关，就在问题末尾把要求写清楚（调用方那边还会再校验一遍）
        prompt += ("\n\n---\n只输出**一个 JSON**（不要解释、不要代码围栏、不要前后缀），"
                   "满足这个 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False))
    cwd = _workdir(arguments)
    mode = "consult_write" if arguments.get("allow_write") else "consult"
    # 有名字就用**稳定的会话 id**：dsh 按 id 认会话，同一个 id 再问就是接着上次继续
    name = _agent_name(arguments)
    slot = _agent_slot("deepseek", name)
    resumed = bool(name) and bool(slot.get("session")) and not arguments.get("fresh")
    session = (slot.get("session") if resumed
               else (f"consult-{name}" if name else f"consult-{int(time.time())}"))
    try:
        import contextlib
        import dsh_agent                      # 应用自己的那份代码（同一目录）
        if not dsh_agent.agent.available():
            raise RuntimeError("dsh 没装")
        # ⚠️ 这个进程的 stdout 是 MCP 协议：dsh_agent 里那些 print 必须改道到 stderr，
        # 不然一行日志就把协议弄坏了（别的 sidecar 也是这个规矩）
        with contextlib.redirect_stdout(sys.stderr):
            reply = dsh_agent.agent.ask(
                session, prompt, mode=mode, cwd=cwd, page="project",
                on_event=None, on_permission=lambda _params: None, timeout=CLI_TIMEOUT,
                harness="deepseek")     # 钉死在 dsh 上：主力换成 Claude 也不能跟着变
    except Exception as exc:                                    # noqa: BLE001
        print(f"[harness] dsh 顾问跑不起来（退回直连接口）：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        return _deepseek_direct(prompt)
    text = str(reply or "").strip()
    if not text:
        text = "（它这轮没有给出答复）"
    if name:
        slot = _agent_remember("deepseek", name, session, cwd,
                               note=str(arguments.get("prompt") or "")[:100])
    return ("【DeepSeek 的答复】" + _banner_note(name, slot, resumed) + "\n" + _clip(text))


def _deepseek_direct(prompt: str) -> str:
    """兜底那条路：直连接口问一句（没有工具、答得浅，但至少能用）。"""
    import api
    messages = [
        {"role": "system",
         "content": "你是被另一个 agent 咨询的顾问。只回答它问的那件事，"
                    "结论先行、简短、说清把握有多大；不确定就直说不确定，别编。"},
        {"role": "user", "content": prompt},
    ]
    try:
        message = api.deepseek_chat(messages, thinking=True, max_tokens=4000)
    except api.ApiError as exc:
        raise RuntimeError(f"接口调用失败：{exc}")
    content = str((message or {}).get("content") or "").strip()
    if not content:
        content = "（它这轮只输出了思考过程、没有正文）"
    return "【DeepSeek 的答复】\n" + _clip(content)


ASKERS = {"claude": ask_claude, "codex": ask_codex, "deepseek": ask_deepseek}


def call_agent(harness: str, arguments) -> str:
    """按名字跑一个 agent 一轮（工作流引擎也用这条，别各写各的）。

    harness：claude / codex / deepseek；arguments 和那三个工具的参数一样
    （prompt 必填，cwd / allow_write / note 可选）。
    """
    fn = ASKERS.get(str(harness or "").strip().lower())
    if fn is None:
        raise RuntimeError(f"没有这个 agent：{harness}（可选：{'、'.join(ASKERS)}）")
    return fn(arguments or {})


def call_tool(name, arguments):
    arguments = arguments or {}
    if name in ("ask_claude", "ask_codex", "ask_deepseek"):
        return call_agent(name[len("ask_"):], arguments)
    if name == "subagent_list":
        return subagent_list(arguments)
    if name == "run_workflow":
        return workflows.run_workflow(arguments)
    if name == "workflow_status":
        return workflows.workflow_status(arguments)
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
                               "serverInfo": {"name": "quanta-harness", "version": "1.0.0"}})
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
            except Exception as exc:                    # 工具内部出错当工具结果回，别断连接
                reply(request_id, {"content": [{"type": "text", "text": f"出错了：{exc}"}],
                                   "isError": True})
        elif request_id is not None:
            reply_error(request_id, f"不支持的方法：{method}")


if __name__ == "__main__":
    print(f"协作 MCP server 已启动（主力：{MAIN}，可用："
          f"{'、'.join(t['name'] for t in TOOLS) or '无'}）", file=sys.stderr)
    serve()

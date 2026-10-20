"""QQ 连接器：官方 QQ 机器人的长连接（WebSocket 网关）+ 被动回复。

走的是 QQ 开放平台（q.qq.com）那套正式接口：
  1. 用 AppID / AppSecret 换 access_token（bots.qq.com，两小时有效，这里自己续）
  2. 拿网关地址（api.sgroup.qq.com/gateway），连上去、发 Identify、按心跳包续着
  3. 收到 C2C_MESSAGE_CREATE（单聊）/ GROUP_AT_MESSAGE_CREATE（群里 @ 我）/
     AT_MESSAGE_CREATE（频道）就把文字交给 on_message
  4. 回消息是**被动回复**：带着对方那条的 msg_id 发回去（群和单聊不允许主动推），
     同一个 msg_id 多条回复要用不同的 msg_seq

不用公网 IP（这是长连接）；但开放平台那边要配 IP 白名单（常见报错 11298）。
底层只用 websocket-client + requests 两个库，没装的话 available() 会说清楚。
"""

import json
import threading
import time

import im_common             # 三家共用的那一层：去重、分片、附件落盘、安全下载

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"
GATEWAY_PATHS = ("/gateway", "/websocket/")     # v1 是 /gateway，v2 有的部署是 /websocket/
# 事件意图：群聊和单聊的事件（含 C2C 和群 @）。频道那几档一起打开，一个数而已
INTENT_GROUP_C2C = 1 << 25
INTENT_PUBLIC_GUILD = 1 << 30
INTENTS = INTENT_GROUP_C2C | INTENT_PUBLIC_GUILD
TEXT_LIMIT = 1800                                # QQ 单条正文上限（约 2000 字，留点余量）
_SEEN = im_common.Seen()                         # msg_id 去重（重连后台会重推）


def available():
    """QQ 这条能不能起：两个基础库在不在。"""
    try:
        import websocket  # noqa: F401
        import requests  # noqa: F401
    except Exception as exc:                                        # noqa: BLE001
        return False, f"缺依赖（{exc}）：pip3 install websocket-client requests"
    return True, ""


def _cut(text, limit=TEXT_LIMIT):
    """老接口留着（别处还在用）：超长截断。**回复不再走它**——现在分片发（见 reply）。"""
    one = str(text or "").strip()
    if len(one) <= limit:
        return one
    return one[:limit] + "\n…（这条太长，后面截掉了）"


def _plain(text):
    """QQ 的群/单聊只吃纯文本（markdown 要单独申请）：把 ``` 围栏和内联记号抹掉。"""
    lines = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            continue                                  # 围栏整行去掉，代码照留
        lines.append(line)
    one = "\n".join(lines)
    for mark in ("**", "__", "~~", "`"):
        one = one.replace(mark, "")
    return one.strip()


class QqBot:
    """一条 QQ 机器人长连接。

    start() 起后台线程跑网关循环（自己重连、自己续 token）；
    stop() 把 stopped 立起来，循环下一圈就退出（并把 socket 关掉）。
    """

    def __init__(self, app_id, app_secret, on_message, log=print):
        self.app_id = str(app_id or "")
        self.app_secret = str(app_secret or "")
        self.on_message = on_message            # (text, meta) -> 要回的文字（空 = 不回）
        self.log = log
        self.stopped = False
        self.thread = None
        self.ws = None
        self.token = ""
        self.token_at = 0.0                     # 拿到 token 的时间（两小时有效，临期自己换）
        self.seq = 0                            # 心跳要带最近一次的 s（事件序号）
        self.session_id = ""

    # ── token / 网关地址 ──────────────────────────────────────────────
    def _http(self, method, path, **kwargs):
        import requests
        url = path if path.startswith("http") else API_BASE + path
        headers = {"Authorization": f"QQBot {self.token}",
                   "Content-Type": "application/json"}
        headers.update(kwargs.pop("headers", {}) or {})
        return requests.request(method, url, headers=headers, timeout=20, **kwargs)

    def _ensure_token(self):
        """换（或续）access_token。"""
        if self.token and time.time() - self.token_at < 100 * 60:
            return self.token
        import requests
        response = requests.post(TOKEN_URL, json={"appId": self.app_id,
                                                  "clientSecret": self.app_secret},
                                 timeout=20)
        data = response.json() if response.content else {}
        token = str(data.get("access_token") or "")
        if not token:
            raise RuntimeError(f"拿不到 access_token：{data.get('code')} {data.get('message') or data}")
        self.token = token
        self.token_at = time.time()
        return token

    def _gateway(self):
        last = None
        for path in GATEWAY_PATHS:
            try:
                response = self._http("GET", path)
                data = response.json() if response.content else {}
                url = str(data.get("url") or "")
                if url:
                    return url
                last = data
            except Exception as exc:                                # noqa: BLE001
                last = exc
        raise RuntimeError(f"拿不到网关地址：{last}")

    # ── 主循环 ────────────────────────────────────────────────────────
    def start(self, on_status=None):
        self.thread = threading.Thread(target=self._run, args=(on_status,), daemon=True)
        self.thread.start()

    def _run(self, on_status):
        import websocket
        backoff = 3
        while not self.stopped:
            try:
                self._ensure_token()
                url = self._gateway()
                self.ws = websocket.create_connection(url, timeout=30)
                if on_status:
                    on_status("connected", "")
                self.log("QQ 网关连上了")
                self._loop(websocket)
                backoff = 3
            except Exception as exc:                                # noqa: BLE001
                if self.stopped:
                    break
                if on_status:
                    on_status("error", f"{type(exc).__name__}: {exc}")
                self.log(f"QQ 网关掉了：{type(exc).__name__}: {exc}（{backoff} 秒后重连）")
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
        if on_status and not self.stopped:
            on_status("closed", "")

    def _send(self, payload):
        if self.ws is None:
            return
        self.ws.send(json.dumps(payload, ensure_ascii=False))

    def _loop(self, websocket):
        """连着之后：先等 Hello（op 10）→ 发 Identify → 心跳 → 处理事件。"""
        heartbeat = None
        while not self.stopped:
            self.ws.settimeout(1.0 if heartbeat else 10.0)
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                if heartbeat:
                    self._heartbeat()
                continue
            except Exception:
                return                                    # 断了：外面那层负责重连
            if not raw:
                continue
            try:
                frame = json.loads(raw)
            except ValueError:
                continue
            op = frame.get("op")
            data = frame.get("d") or {}
            if isinstance(data, dict) and data.get("s") is not None:
                self.seq = data["s"]
            if op == 10:                                  # Hello：告诉我心跳间隔
                heartbeat = float(data.get("heartbeat_interval") or 30) / 1000.0
                self._identify()
            elif op == 0:                                 # 事件
                self._event(frame.get("t") or "", data)
            elif op in (7,):                              # 要我重连
                return
            elif op == 9:                                 # 会话失效：重新 Identify
                self.session_id = ""
                self._identify()
            elif op == 11:                                # 心跳回执
                pass

    def _identify(self):
        self._send({"op": 2, "d": {
            "token": f"QQBot {self.token}",
            "intents": INTENTS,
            "shard": [0, 1],
            "properties": {"$os": "macOS", "$browser": "Quanta", "$device": "Quanta"},
        }})

    def _heartbeat(self):
        self._send({"op": 1, "d": self.seq or None})

    # ── 事件 ──────────────────────────────────────────────────────────
    def _event(self, kind, data):
        try:
            if kind == "READY":
                self.session_id = str(data.get("session_id") or "")
                return
            if kind == "C2C_MESSAGE_CREATE":
                text = str(data.get("content") or "")
                author = data.get("author") or {}
                meta = {"scene": "c2c", "chat_id": f"c2c:{author.get('user_openid') or ''}",
                        "target": str(author.get("user_openid") or ""),
                        "open_id": str(author.get("user_openid") or ""),
                        "message_id": str(data.get("id") or ""),
                        "at": bool(data.get("mentioned")), "text": text}
            elif kind == "GROUP_AT_MESSAGE_CREATE":
                text = str(data.get("content") or "")
                author = data.get("author") or {}
                meta = {"scene": "group", "chat_id": f"group:{data.get('group_openid') or ''}",
                        "target": str(data.get("group_openid") or ""),
                        "open_id": str(author.get("member_openid") or ""),
                        "message_id": str(data.get("id") or ""),
                        "at": True, "text": text}
            elif kind == "AT_MESSAGE_CREATE":
                text = str(data.get("content") or "")
                author = data.get("author") or {}
                meta = {"scene": "channel", "chat_id": f"channel:{data.get('channel_id') or ''}",
                        "target": str(data.get("channel_id") or ""),
                        "open_id": str(author.get("id") or ""),
                        "message_id": str(data.get("id") or ""),
                        "at": True, "text": text}
            else:
                return
            # 群里 @ 我的那条，正文里带着 <@!xxx> 这串：去掉
            text = meta["text"]
            while "<@" in text:
                head = text.index("<@")
                tail = text.find(">", head)
                if tail < 0:
                    break
                text = (text[:head] + " " + text[tail + 1:]).strip()
            meta["text"] = text
            # 附件：QQ 的 payload 里是 attachments[]（有 url / filename / content_type），
            # 这里**只挑引用**，字节等 load_attachments 真被调用时才下（晚于白名单门）
            pending = []
            for one in (data.get("attachments") or []):
                url = str((one or {}).get("url") or "")
                if not url:
                    continue
                mime = str((one or {}).get("content_type") or "")
                pending.append({"kind": "image" if mime.startswith("image/") else "file",
                                "url": url,
                                "name": str((one or {}).get("filename") or "qq-file")})
            if pending:
                meta["pending"] = pending
                meta["kind"] = pending[0]["kind"]
                meta["load_attachments"] = (lambda m=dict(meta): self._load(m))
            if self.stopped or not meta["message_id"]:
                return
            if _SEEN.seen(meta["message_id"]):
                self.log(f"QQ 这条消息重复投递，跳过：{meta['message_id']}")
                return
            if not text.strip() and not pending:
                self.reply(meta, "（这条我还没法读：语音、卡片这些先发文字吧）")
                return
            reply = self.on_message(text.strip(), meta)
            if reply:
                self.reply(meta, reply)
        except Exception as exc:                                    # noqa: BLE001
            self.log(f"QQ 消息处理出错：{type(exc).__name__}: {exc}")

    def _load(self, meta) -> list:
        """把这条消息里的附件真下下来（调用方保证已经过了白名单门）。

        QQ 的附件是 CDN 上的 https URL：走 im_common.fetch_remote（只 https、
        解析出的地址必须公网、连接钉在 IP 上、大小封顶）。
        """
        import config
        out = []
        for one in meta.get("pending") or []:
            limit = im_common.IMAGE_MAX_BYTES if one["kind"] == "image" \
                else im_common.FILE_MAX_BYTES
            try:
                data = im_common.fetch_remote(one["url"], limit)
                why = im_common.check_limit(one["kind"], len(data))
                if why:
                    out.append(f"{one['name']}：{why}")
                    continue
                path = im_common.save_attachment("qq", meta.get("chat_id") or "",
                                                 one["name"], data, config.DATA_DIR)
                out.append({"path": path, "kind": one["kind"], "name": one["name"]})
            except ValueError as exc:
                out.append(f"{one['name']}：{exc}")
            except Exception as exc:                                # noqa: BLE001
                out.append(f"{one['name']}：{type(exc).__name__}: {exc}")
        return out

    # ── 回复（被动回复：带 msg_id）────────────────────────────────────
    def send_reply(self, meta, text):
        """统一的回法（和 feishu.py 的 FeishuBot.send_reply 一个签名）。"""
        return self.reply(meta, text)

    def reply(self, meta, text):
        """把回答发回去。群/单聊必须带对方的 msg_id（被动回复），msg_seq 递增。

        太长的回答**分片**发（im_common.chunk，每条不超过 TEXT_LIMIT），不再截断。
        """
        if meta.get("scene") == "c2c":
            path = f"/v2/users/{meta['target']}/messages"
        elif meta.get("scene") == "group":
            path = f"/v2/groups/{meta['target']}/messages"
        else:
            path = f"/channels/{meta['target']}/messages"
        ok = True
        for piece in im_common.chunk(_plain(text), TEXT_LIMIT):
            body = {"content": piece, "msg_type": 0,
                    "msg_id": meta.get("message_id"), "msg_seq": self._next_seq(meta)}
            try:
                response = self._http("POST", path, json=body)
                data = response.json() if response.content else {}
                if response.status_code >= 300 or data.get("code"):
                    self.log(f"QQ 回复失败：{response.status_code} {data.get('code')} {data.get('message')}")
                    ok = False
                    break
            except Exception as exc:                                # noqa: BLE001
                self.log(f"QQ 回复出错：{type(exc).__name__}: {exc}")
                ok = False
                break
        return ok

    _seqs = None

    def _next_seq(self, meta):
        """同一个 msg_id 的多条回复要用不同的 msg_seq，这里从 1 往上加。"""
        if QqBot._seqs is None:
            QqBot._seqs = {}
        key = str(meta.get("message_id") or "")
        QqBot._seqs[key] = int(QqBot._seqs.get(key, 0)) + 1
        if len(QqBot._seqs) > 500:                        # 别越攒越多
            for one in list(QqBot._seqs)[:200]:
                QqBot._seqs.pop(one, None)
        return QqBot._seqs[key]

    def stop(self):
        self.stopped = True
        try:
            if self.ws is not None:
                self.ws.close()
        except Exception:                                           # noqa: BLE001
            pass

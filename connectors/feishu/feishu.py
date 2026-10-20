"""飞书连接器：一条长连接把飞书里的消息接进这台应用，回答再发回飞书。

为什么走得通：飞书开放平台给「企业自建应用」提供了**长连接**（WebSocket）方式接收
事件订阅——本地电脑不需要公网 IP、不需要域名，也不用内网穿透，SDK 自己维护这条
连接（断线会重连）。发消息走普通的 HTTP OpenAPI，用的还是同一个 app_id / app_secret。

这一层只干两件事：收（长连接 → on_message 回调）和发（reply / create）。
「消息怎么变成一轮对话」是应用那边的事（见 bridge.py 的 feishu_incoming / feishu_turn）。

没有装 lark-oapi 也能跑这个应用：available() 会说明为什么起不来，设置页照常显示。
"""

import asyncio
import json
import threading
import time

import im_common             # 三家共用的那一层：去重、分片、附件落盘、安全下载

# 长连接收到一条消息后，飞书要求 3 秒内返回，否则会重推；耗时的活儿一律丢给应用那边
# 的线程去干（on_message 自己负责别在这儿阻塞太久——bridge 那边是起线程的）
TEXT_LIMIT = 3000          # 单条消息最长多少字（飞书单条上限约 8000 字节，留点余量）
_SEEN = im_common.Seen()   # 同一条 message_id 只处理一次（重连后飞书会重推）


def available():
    """飞书这条能不能起：SDK 在不在。返回 (能不能, 不能的原因)。"""
    try:
        import lark_oapi  # noqa: F401
    except Exception as exc:                                        # noqa: BLE001
        return False, f"没装 lark-oapi（{exc}）：pip3 install lark-oapi"
    return True, ""


def _cut(text, limit=TEXT_LIMIT):
    """太长的回答切一下（飞书单条装不下），末尾说明被截断了。"""
    one = str(text or "").strip()
    if len(one) <= limit:
        return one
    return one[:limit] + "\n…（这条太长，后面截掉了）"


class FeishuBot:
    """一条飞书长连接。

    start() 起一个后台线程跑 SDK 的 ws.Client.start()（它是阻塞的）。

    ⚠️ SDK 的长连接用的是**它模块级那一个 event loop**（lark_oapi/ws/client.py
    文件开头 `loop = asyncio.get_event_loop()`），`start()` 就是在这个 loop 上
    `run_until_complete`。所以：**一个进程里同时只能有一条**——已经跑着的时候
    再 start 一条，第二次会抛 RuntimeError: This event loop is already running。
    桥那边因此给飞书这条做了单例（见 bridge.py 的 _feishu_live）：同一对密钥不重起，
    换密钥才真停旧的那条（stop(hard=True)：关 ws + 把那个 loop 停掉，放它出院）。
    """

    def __init__(self, app_id, app_secret, on_message, log=print):
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_message = on_message            # (text, meta) -> 要回的文字（空 = 不回）
        self.log = log
        self.stopped = False                    # 软停：还连着，但不再处理消息
        self.connected = False                  # SDK 真连上了（_conn 有值）才算
        self.thread = None
        self.client = None
        self.http = None

    # ── 收 ────────────────────────────────────────────────────────────
    def _handle_message(self, data):
        """im.message.receive_v1 —— 收到一条消息。

        图片 / 文件 / 语音这些：**只解析出引用**（key + 文件名），真实的字节等
        `meta["load_attachments"]` 被调用时才去下（见 im_common 头上那段：
        下载必须晚于白名单门，不然没通过白名单的人就能让这台机器落盘）。
        """
        try:
            event = getattr(data, "event", None)
            msg = getattr(event, "message", None)
            if msg is None:
                return
            sender = getattr(event, "sender", None)
            sender_id = getattr(sender, "sender_id", None) if sender else None
            kind = str(getattr(msg, "message_type", "") or "")
            try:
                content = json.loads(getattr(msg, "content", "") or "{}")
            except (ValueError, TypeError):
                content = {}
            text = ""
            pending = []
            if kind == "text":
                text = str(content.get("text") or "")
            elif kind == "image":
                pending.append({"kind": "image", "key": str(content.get("image_key") or ""),
                                "name": f"feishu-image-{int(time.time())}.png"})
            elif kind == "file":
                pending.append({"kind": "file", "key": str(content.get("file_key") or ""),
                                "name": str(content.get("file_name") or "file")})
            elif kind == "audio":
                pending.append({"kind": "file", "key": str(content.get("file_key") or ""),
                                "name": "feishu-audio"})
            elif kind == "media":
                pending.append({"kind": "file", "key": str(content.get("file_key") or ""),
                                "name": str(content.get("file_name") or "feishu-media")})
            meta = {
                "chat_id": str(getattr(msg, "chat_id", "") or ""),
                "message_id": str(getattr(msg, "message_id", "") or ""),
                "chat_type": str(getattr(msg, "chat_type", "") or ""),
                "open_id": str(getattr(sender_id, "open_id", "") or "") if sender_id else "",
                "text": text.strip(),
                "pending": [one for one in pending if one.get("key")],
            }
            if meta["pending"]:
                meta["kind"] = meta["pending"][0]["kind"]
                meta["load_attachments"] = (lambda m=dict(meta): self._load(m))
            if self.stopped or not meta["message_id"]:
                return
            if _SEEN.seen(meta["message_id"]):
                self.log(f"飞书这条消息重复投递，跳过：{meta['message_id']}")
                return
            if not meta["text"] and not meta["pending"]:
                self.reply(meta["message_id"],
                           "（这条我还没法读：飞书的卡片 / 位置这类先发文字吧）")
                return
            reply = self.on_message(meta["text"], meta)
            if reply and meta["message_id"]:
                self.reply(meta["message_id"], reply)
        except Exception as exc:                                    # noqa: BLE001
            self.log(f"飞书消息处理出错：{type(exc).__name__}: {exc}")

    def _load(self, meta) -> list:
        """把这条消息里的附件**真下下来**（调用方保证已经过了白名单门）。

        飞书是 SDK 自己的资源接口（im.v1.image.get / file.get）——数据不从公网 URL 来，
        跟着 app_secret 走，所以不走 im_common.fetch_remote 那套 SSRF 校验；
        但大小上限和落盘规矩是同一套（im_common.save_attachment）。
        """
        import config
        out = []
        for one in meta.get("pending") or []:
            try:
                from lark_oapi.api.im.v1 import GetImageRequest, GetFileRequest
                if one["kind"] == "image":
                    request = GetImageRequest.builder().image_key(one["key"]).build()
                    response = self._client().im.v1.image.get(request)
                else:
                    request = GetFileRequest.builder().file_key(one["key"]).build()
                    response = self._client().im.v1.file.get(request)
                if not response.success():
                    out.append(f"{one['name']}：飞书取不了（{response.code} {response.msg}）")
                    continue
                data = response.file.read()
                why = im_common.check_limit(one["kind"], len(data))
                if why:
                    out.append(f"{one['name']}：{why}")
                    continue
                path = im_common.save_attachment("feishu", meta.get("chat_id") or "",
                                                 one["name"], data, config.DATA_DIR)
                out.append({"path": path, "kind": one["kind"], "name": one["name"]})
            except Exception as exc:                                # noqa: BLE001
                out.append(f"{one['name']}：{type(exc).__name__}: {exc}")
        return out

    # ── 发 ────────────────────────────────────────────────────────────
    def _client(self):
        if self.http is None:
            import lark_oapi as lark
            self.http = (lark.Client.builder()
                         .app_id(self.app_id).app_secret(self.app_secret).build())
        return self.http

    def send_reply(self, meta, text):
        """统一的回法（和 qq.py 的 QqBot.send_reply 一个签名）：飞书回在对方那条下面。"""
        return self.reply(str((meta or {}).get("message_id") or ""), text)

    def reply(self, message_id, text):
        """回在对方那条消息下面（被动回复，不占主动推送的额度）。

        太长的回答**分片**发（im_common.chunk），不再截断——以前对方只能收到半句话。
        """
        ok = True
        for piece in im_common.chunk(text, TEXT_LIMIT):
            try:
                from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
                body = (ReplyMessageRequestBody.builder()
                        .content(json.dumps({"text": piece}, ensure_ascii=False))
                        .msg_type("text").build())
                request = (ReplyMessageRequest.builder().message_id(message_id)
                           .request_body(body).build())
                response = self._client().im.v1.message.reply(request)
                if not response.success():
                    self.log(f"飞书回复失败：{response.code} {response.msg}")
                    ok = False
                    break
            except Exception as exc:                                # noqa: BLE001
                self.log(f"飞书回复出错：{type(exc).__name__}: {exc}")
                ok = False
                break
        return ok

    def send(self, chat_id, text):
        """主动发一条到这个会话（提示「在处理」这类用得上）。太长同样分片。"""
        ok = True
        for piece in im_common.chunk(text, TEXT_LIMIT):
            try:
                from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
                body = (CreateMessageRequestBody.builder()
                        .receive_id(chat_id).msg_type("text")
                        .content(json.dumps({"text": piece}, ensure_ascii=False)).build())
                request = (CreateMessageRequest.builder()
                           .receive_id_type("chat_id").request_body(body).build())
                response = self._client().im.v1.message.create(request)
                if not response.success():
                    self.log(f"飞书发送失败：{response.code} {response.msg}")
                    ok = False
                    break
            except Exception as exc:                                # noqa: BLE001
                self.log(f"飞书发送出错：{type(exc).__name__}: {exc}")
                ok = False
                break
        return ok

    # ── 生命周期 ──────────────────────────────────────────────────────
    def start(self, on_status=None):
        self.thread = threading.Thread(target=self._run, args=(on_status,), daemon=True)
        self.thread.start()

    def _run(self, on_status):
        try:
            import lark_oapi as lark
            handler = (lark.EventDispatcherHandler.builder("", "")
                       .register_p2_im_message_receive_v1(self._handle_message)
                       .build())
            self.client = lark.ws.Client(self.app_id, self.app_secret,
                                         event_handler=handler,
                                         log_level=lark.LogLevel.INFO)
            # 连上之前一律报「正在连」：以前是建完 client 就报 connected，
            # 界面会显示「已连接」但其实还没连上（把真实错误盖住了）
            if on_status:
                on_status("starting", "")
            threading.Thread(target=self._watch, args=(on_status,), daemon=True).start()
            self.client.start()                 # 阻塞，直到连接结束
            if on_status and not self.stopped:
                on_status("closed", "")
        except Exception as exc:                                    # noqa: BLE001
            if self.stopped:
                return                          # 我们自己叫停的（换密钥那条路），不算错
            if "event loop is already running" in str(exc):
                self.log("飞书这条长连接已经在跑了：SDK 一个进程只允许一条（别重复开）")
            else:
                self.log(f"飞书长连接没起来：{type(exc).__name__}: {exc}")
            if on_status:
                on_status("error", f"{type(exc).__name__}: {exc}")

    def _watch(self, on_status):
        """等 SDK 真连上（client._conn 有值）再报 connected，最多等 20 秒。"""
        for _ in range(40):
            if self.stopped:
                return
            client = self.client
            if client is not None and getattr(client, "_conn", None) is not None:
                self.connected = True
                if on_status:
                    on_status("connected", "")
                return
            if self.thread is not None and not self.thread.is_alive():
                return                          # 线程都没了（_run 那边已经报过错）
            time.sleep(0.5)

    def alive(self):
        """这条连接还活着吗（线程还在跑 = SDK 的 loop 还在转）。"""
        return bool(self.thread is not None and self.thread.is_alive())

    def stop(self, hard=False):
        """停这条。

        hard=False（默认）：软停——连接留着，只是不再处理消息（开关一关就走这个，
        再打开立刻能收，不用重连）。
        hard=True：真停——关掉 ws 再把 SDK 那个模块级 loop 停掉，好让下一条能起来
        （换密钥时才需要；停不掉就返回 False，让调用方别硬起第二条）。
        """
        self.stopped = True
        if not hard:
            return True
        client = self.client
        if client is None:
            return True
        try:
            from lark_oapi.ws.client import loop as lark_loop
        except Exception as exc:                                    # noqa: BLE001
            self.log(f"飞书停不下来（拿不到 SDK 的 loop）：{type(exc).__name__}: {exc}")
            return False
        try:
            client._auto_reconnect = False      # 别让它自己又连回来
            fut = asyncio.run_coroutine_threadsafe(client._disconnect(), lark_loop)
            fut.result(timeout=5)
            lark_loop.call_soon_threadsafe(lark_loop.stop)
            if self.thread is not None:
                self.thread.join(timeout=5)
            self.connected = False
            return True
        except Exception as exc:                                    # noqa: BLE001
            self.log(f"飞书停不下来：{type(exc).__name__}: {exc}")
            return False

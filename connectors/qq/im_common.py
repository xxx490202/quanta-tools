"""IM 三家（飞书 / QQ / 微信）共用的那一层：去重、分片、附件落盘、安全下载。

为什么要这一层：三家连接器各写各的，同一个坑要踩三遍——飞书和 QQ 原来都是
「太长就截断」（对方只收到半句话）、收到图片/文件直接回「还不会读」、同一条消息
在重连后被平台重推时会再答一遍。这些逻辑和平台无关，收到这里：

  · `Seen`        按消息 id 去重（TTL + 容量上限；重连重推不再重复应答）
  · `chunk`       长回答分片（按字符 / 按 UTF-8 字节两档，优先断在段落、行、空白处）
  · `save_attachment` / `gc`  附件落盘：原子写（tmp→rename）、文件名清洗、24 小时 GC
  · `fetch_remote`  远端下载（附件 URL）：只 http/https、不带凭证、**每个跳转重新
    校验**、DNS 解析出来的地址必须全是公网、把连接钉在解析到的 IP 上（防 rebinding）、
    大小与超时都封顶

规矩（照 cc-haha 那套，也是踩过的）：**下载要晚于白名单门**——没通过白名单的人
不能让这台机器去落盘任何字节（见 bridge._bot_worker 的调用顺序）。
"""

import http.client
import ipaddress
import json
import os
import pathlib
import re
import socket
import ssl
import threading
import time
import urllib.parse

# ── 去重 ────────────────────────────────────────────────────────────
DEDUPE_TTL = 15 * 60          # 同一条消息 15 分钟内只处理一次
DEDUPE_MAX = 5000             # 最多记这么多条（超了先扔最老的）


class Seen:
    """按 key（一般就是平台的 message_id）去重。线程安全。"""

    def __init__(self, ttl: int = DEDUPE_TTL, capacity: int = DEDUPE_MAX):
        self.ttl = ttl
        self.capacity = capacity
        self._lock = threading.Lock()
        self._items = {}

    def seen(self, key: str) -> bool:
        """见过就 True（并刷新时间）；没见过记下并返回 False。key 空 = 不判（False）。"""
        key = str(key or "").strip()
        if not key:
            return False
        now = time.time()
        with self._lock:
            self._sweep(now)
            hit = key in self._items
            self._items[key] = now
            return hit

    def _sweep(self, now: float):
        if len(self._items) <= self.capacity:
            dead = [k for k, at in self._items.items() if now - at > self.ttl]
            for key in dead:
                self._items.pop(key, None)
            return
        # 超容量：按时间扔到剩下 capacity 的 80%
        for key, _ in sorted(self._items.items(), key=lambda kv: kv[1])[:len(self._items) - int(self.capacity * 0.8) + 1]:
            self._items.pop(key, None)


# ── 分片 ────────────────────────────────────────────────────────────
def chunk(text: str, limit: int = 3000) -> list:
    """把长回答切成每片不超过 limit 个**字符**的几片（块数=1 时原样返回）。

    优先断在空行、换行、空格，实在找不到才硬切——别把一句话从中间劈开。
    """
    one = str(text or "").strip()
    if not one:
        return []
    if len(one) <= limit:
        return [one]
    parts = []
    rest = one
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for sep in ("\n\n", "\n", "。"):
            index = window.rfind(sep)
            if index > limit * 0.5:
                cut = index + len(sep)
                break
        if cut < 0:
            index = window.rfind(" ")
            cut = index + 1 if index > limit * 0.5 else limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].lstrip()
    if rest:
        parts.append(rest)
    return [part for part in parts if part]


def chunk_bytes(text: str, limit_bytes: int = 3500) -> list:
    """按 **UTF-8 字节**分片（中文一个字 3 字节，按字符算会在平台那边超）。

    不会把字符切成两半（Python 字符串按码点切），但会算准每一片的字节数。
    """
    one = str(text or "").strip()
    if not one:
        return []
    if len(one.encode("utf-8")) <= limit_bytes:
        return [one]
    parts, buf, size = [], [], 0
    for char in one:
        width = len(char.encode("utf-8"))
        if size + width > limit_bytes and buf:
            parts.append("".join(buf))
            buf, size = [], 0
        buf.append(char)
        size += width
    if buf:
        parts.append("".join(buf))
    return parts


# ── 附件落盘 ────────────────────────────────────────────────────────
ATTACH_ROOT_NAME = "im-downloads"
RETENTION_SECONDS = 24 * 3600        # 普通附件留 24 小时
PART_GRACE_SECONDS = 10 * 60         # 写了一半的 .part 留 10 分钟（崩溃的写者留下的）
IMAGE_MAX_BYTES = 10 * 1024 * 1024   # 单张图上限（飞书/Telegram 都是 10MB 这一档）
FILE_MAX_BYTES = 30 * 1024 * 1024    # 单个文件上限（飞书 30MB 这一档）
IMAGE_MIMES = ("image/jpeg", "image/png", "image/gif", "image/webp")


def attachment_root(data_dir: str) -> pathlib.Path:
    return pathlib.Path(data_dir) / ATTACH_ROOT_NAME


def safe_name(name: str) -> str:
    """文件名清洗：去掉目录部分、控制字符、路径分隔符和 ..，空的话叫 unnamed。"""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    base = base.replace("/", "_").replace("..", "_").strip()
    return base[:120] or "unnamed"


def ensure_dir(platform: str, chat_id: str, data_dir: str) -> pathlib.Path:
    """这一条会话的附件目录：<数据目录>/im-downloads/<平台>/<会话>/。"""
    folder = attachment_root(data_dir) / safe_name(platform) / safe_name(chat_id or "unknown")
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def save_attachment(platform: str, chat_id: str, name: str, data: bytes,
                    data_dir: str) -> str:
    """原子写一个附件（tmp → rename），重名加随机后缀。返回落盘路径。"""
    folder = ensure_dir(platform, chat_id, data_dir)
    target = folder / safe_name(name)
    if target.exists():
        stem, suffix = target.stem, target.suffix
        target = folder / f"{stem}-{int(time.time())}-{os.urandom(3).hex()}{suffix}"
    tmp = target.with_name(target.name + f".{os.getpid()}.{int(time.time())}.part")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, target)
    return str(target)


def gc(data_dir: str, retention: int = RETENTION_SECONDS,
       part_grace: int = PART_GRACE_SECONDS) -> dict:
    """清超龄附件（普通文件按 retention，.part 按 part_grace）。返回 {removed, bytes}。"""
    root = attachment_root(data_dir)
    removed, freed = 0, 0
    if not root.is_dir():
        return {"removed": 0, "bytes": 0}
    now = time.time()
    for path in root.rglob("*"):
        try:
            if path.is_dir():
                continue
            stat = path.stat()
            age = now - stat.st_mtime
            if path.name.endswith(".part"):
                if age <= part_grace:
                    continue
            elif age <= retention:
                continue
            freed += stat.st_size
            path.unlink()
            removed += 1
        except OSError:
            continue                     # 并发删掉了/读不到：跳过就是
    for path in sorted(root.rglob("*"), key=lambda p: len(str(p)), reverse=True):
        try:
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        except OSError:
            continue
    return {"removed": removed, "bytes": freed}


def check_limit(kind: str, size: int, mime: str = "") -> str:
    """超限返回一句中文说明；没超返回空。kind: image / file。"""
    mime = str(mime or "").split(";")[0].strip().lower()
    if kind == "image":
        if mime and mime not in IMAGE_MIMES:
            return f"这个图片格式（{mime}）看不了，跳过"
        if size > IMAGE_MAX_BYTES:
            return f"图片太大（{size // 1024 // 1024}MB），上限 {IMAGE_MAX_BYTES // 1024 // 1024}MB"
        return ""
    if size > FILE_MAX_BYTES:
        return f"文件太大（{size // 1024 // 1024}MB），上限 {FILE_MAX_BYTES // 1024 // 1024}MB"
    return ""


# ── 安全下载（SSRF 防护）──────────────────────────────────────────────
MAX_REDIRECTS = 5
REMOTE_TIMEOUT = 20                  # 总超时（连接 + 读）


def is_public_ip(ip: str) -> bool:
    """这个地址是「公网」吗。私网 / 回环 / 链路本地 / 组播 / 保留段一律 False。

    IPv6 走白名单：只认 2000::/3（其余本地、翻译、6to4、保留段全不放）。
    """
    try:
        address = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return False
    if address.version == 6:
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            return is_public_ip(str(mapped))              # ::ffff:127.0.0.1 这种回 v4 判
        # v6 走**白名单**：只认 2000::/3（全球单播）。别的（本地、6to4、翻译段、
        # 保留段）全不放——黑名单在 v6 上很难列全
        return address.is_global and str(address) < "4000:"
    # v4 也以 is_global 为准：手写黑名单会漏（比如 100.64/10 运营商级 NAT，
    # is_private 是 False，实测漏过一次），is_global 把 TEST-NET、CGNAT、保留段都算进去了
    if not address.is_global:
        return False
    return not (address.is_private or address.is_loopback or address.is_link_local
                or address.is_multicast or address.is_reserved or address.is_unspecified)


def validate_url(url: str):
    """URL 本体校验（还没解析 DNS）。返回 (parsed, 出错说明)。"""
    try:
        parsed = urllib.parse.urlsplit(str(url or "").strip())
    except ValueError:
        return None, "链接格式不对"
    if parsed.scheme not in ("http", "https"):
        return None, "只支持 http/https 链接"
    if not parsed.hostname:
        return None, "链接里没有主机名"
    if parsed.username or parsed.password:
        return None, "链接里带着账号密码，不下载"
    return parsed, ""


def resolve_public(host: str) -> list:
    """解析主机名，**所有**地址都必须是公网；否则抛 ValueError。返回地址列表。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise ValueError(f"域名解析不了：{exc}") from exc
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise ValueError("域名没有解析到任何地址")
    bad = [one for one in addresses if not is_public_ip(one)]
    if bad:
        raise ValueError(f"域名解析到了非公网地址（{bad[0]}），不下载")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """把连接**钉在**已经校验过的 IP 上，SNI/Host 仍然是原域名（防 DNS rebinding）。"""

    def __init__(self, host, ip, port, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def fetch_remote(url: str, max_bytes: int, timeout: int = REMOTE_TIMEOUT,
                 allowed_mimes=(), allow_host=None) -> bytes:
    """下载一个远端资源（附件用）。到不了就抛 ValueError（带人话原因）。

    安全规矩，一条都不能少：
      · 只 http/https、不带凭证；
      · 每一次跳转都重新解析 + 重新判公网（第二跳指向 169.254.169.254 这种直接拒）；
      · DNS 解析出的**所有**地址必须公网（allow_host 可注入，测试用）；
      · 连接钉在解析到的那个 IP 上（Host/SNI 还是原域名）——关掉 rebinding 那个窗口；
      · 流式读，超过 max_bytes 立刻断；
      · 声明了 content-encoding（压缩）的不收（防解压炸弹）。
    """
    hops = 0
    current = str(url or "").strip()
    while True:
        parsed, why = validate_url(current)
        if parsed is None:
            raise ValueError(why)
        host = parsed.hostname
        if allow_host is not None:
            if not allow_host(host, parsed):
                raise ValueError(f"这个主机不允许下载：{host}")
            addresses = ["127.0.0.1"] if parsed.scheme == "http" else ["127.0.0.1"]
        else:
            addresses = resolve_public(host)
        ip = addresses[0]
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            conn = _PinnedHTTPSConnection(host, ip, port, timeout, context)
        else:
            conn = http.client.HTTPConnection(ip, port, timeout=timeout)
        try:
            conn.request("GET", path, headers={"Host": host, "User-Agent": "Quanta/1.0",
                                               "Accept": "*/*", "Connection": "close"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                conn.close()
                if not location:
                    raise ValueError("跳转没给目标地址")
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise ValueError("跳转次数太多，不跟了")
                current = urllib.parse.urljoin(current, location)
                continue                       # 每一跳都重新走校验
            if response.status != 200:
                raise ValueError(f"下载失败（HTTP {response.status}）")
            encoding = str(response.getheader("Content-Encoding") or "identity").lower()
            if encoding not in ("", "identity"):
                raise ValueError("这个资源是压缩传输的，不收")
            mime = str(response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if allowed_mimes and mime and mime not in allowed_mimes:
                raise ValueError(f"这个类型（{mime}）不在允许清单里")
            declared = response.getheader("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise ValueError(f"对方说这个文件有 {int(declared) // 1024 // 1024}MB，超过上限")
            data = bytearray()
            while True:
                block = response.read(64 * 1024)
                if not block:
                    break
                data += block
                if len(data) > max_bytes:
                    raise ValueError(f"文件超过上限（{max_bytes // 1024 // 1024}MB），断了")
            return bytes(data)
        except (OSError, http.client.HTTPException) as exc:
            raise ValueError(f"下载出错：{exc}") from exc
        finally:
            try:
                conn.close()
            except Exception:                                   # noqa: BLE001
                pass


def summary(platform: str, paths: list, failed: list) -> str:
    """给主力看的那段「用户还发来了这些附件」——路径写清楚，它自己去读。"""
    lines = []
    if paths:
        lines.append(f"（对方在 {platform} 里还发来了 {len(paths)} 个附件，已经存到本机：")
        lines.extend(f"  {one}" for one in paths)
        lines.append("图片可以用看图工具打开；文件直接读。答复时把文件里的事实用上。）")
    if failed:
        lines.append("（有 " + str(len(failed)) + " 个附件没取到：" + "；".join(failed[:3]) + "）")
    return ("\n".join(lines) + "\n\n") if lines else ""

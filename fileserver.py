# -*- coding: utf-8 -*-
"""局域网文件分享 —— 基于 Python 标准库 http.server 的文件分享服务。

这个模块只做一件事：把本机任意位置的文件 / 文件夹，变成一条可以直接发给
别人的 HTTP 下载地址，并如实记录谁正在下载、下到哪儿了。

它建立在标准库 http.server 之上（就是 ``python -m http.server`` 用的那套
ThreadingHTTPServer + SimpleHTTPRequestHandler），并补上标准库没有的三件事：

1. 虚拟挂载点 —— 把散落在不同盘符的文件，映射成整齐的 URL 路径
2. HTTP Range —— 断点续传 / 视频拖动进度条 / 多线程下载器
3. 传输追踪   —— 实时掌握每个下载的客户端、进度与速度

本模块不依赖任何 GUI，可以单独当命令行工具用::

    python fileserver.py "D:/报告.pdf" "E:/照片" -p 8000
"""

from __future__ import annotations

import base64
import binascii
import html
import hmac
import ipaddress
import os
import posixpath
import secrets
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

__all__ = [
    "ShareRegistry",
    "TransferTracker",
    "SecurityPolicy",
    "ShareHandler",
    "ShareServer",
    "get_lan_ips",
    "human_size",
    "human_speed",
]

# 网页上显示的站点名。只能用在 HTML 里：HTTP 响应头由标准库按 latin-1 严格编码，
# 中文放进去会直接抛 UnicodeEncodeError，所以 server_version 与 Basic realm 保持 ASCII
SITE_NAME = "局域网文件分享"

# 预览模式下发这些类型时，浏览器会就地打开而不是下载
_PREVIEW_PREFIXES = ("image/", "video/", "audio/", "text/")
_PREVIEW_EXACT = ("application/pdf",)

# 这些格式本来就已经压过了，再 deflate 一遍纯属白烧 CPU
_ALREADY_COMPRESSED = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv",
    ".mp3", ".flac", ".aac", ".m4a", ".ogg", ".opus",
    ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz", ".zst",
    ".pdf", ".docx", ".xlsx", ".pptx", ".apk", ".jar",
}


def _compress_type(filename: str) -> int:
    ext = os.path.splitext(filename)[1].lower()
    return zipfile.ZIP_STORED if ext in _ALREADY_COMPRESSED else zipfile.ZIP_DEFLATED


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def human_size(n) -> str:
    """1234567 -> '1.2 MB'"""
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_speed(bps) -> str:
    """字节/秒 -> '3.2 MB/s'"""
    if not bps or bps <= 0:
        return "—"
    return human_size(bps) + "/s"


def new_share_token() -> str:
    """生成分享路径的随机前缀。

    用密码学安全的随机源：猜中前缀就等于拿到了访问权，所以不能用 random。
    16 个十六进制字符 = 64 位熵，暴力枚举不可行。
    """
    return secrets.token_hex(8)


def parse_block_entry(text: str) -> str | None:
    """校验并规范一条黑名单条目；不合法返回 None。

    接受单个 IP（192.168.1.100），也接受网段（192.168.1.0/24）。
    """
    text = (text or "").strip()
    if not text:
        return None
    if "/" in text:
        try:
            return str(ipaddress.ip_network(text, strict=False))
        except ValueError:
            return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def get_lan_ips() -> list[str]:
    """列出本机可用于局域网访问的 IPv4 地址，最可能的那个排在最前。

    优先用「向外连一个 UDP 地址」的办法，取出系统实际用于上网的那块网卡
    （UDP connect 不会真的发包，只是让内核选路由）。
    """
    found: list[str] = []

    def push(ip: str) -> None:
        if ip and ip not in found and not ip.startswith("127.") and not ip.startswith("169.254."):
            found.append(ip)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            push(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass

    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            push(ip)
    except (OSError, socket.gaierror):
        pass

    return found


# --------------------------------------------------------------------------
# 虚拟挂载点
# --------------------------------------------------------------------------

class _Mount:
    """一条分享记录：URL 上的虚拟路径 -> 磁盘上的真实路径。"""

    __slots__ = ("vpath", "realpath", "is_dir", "size", "count")

    def __init__(self, vpath: str, realpath: str):
        self.vpath = vpath
        self.realpath = realpath
        self.is_dir = os.path.isdir(realpath)
        self.size = None
        self.count = 0
        self._measure()

    def _measure(self) -> None:
        try:
            if self.is_dir:
                with os.scandir(self.realpath) as it:
                    self.count = sum(1 for _ in it)
            else:
                self.size = os.path.getsize(self.realpath)
        except OSError:
            pass


class ShareRegistry:
    """管理所有分享项。线程安全 —— HTTP 线程和 GUI 线程都会读它。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._mounts: dict[str, _Mount] = {}

    # -- 增删 ---------------------------------------------------------------

    def add(self, realpath: str):
        """加入一个文件或文件夹。返回 (虚拟路径, 是否新增)。

        已分享过的路径不会重复加入；重名时自动加 ``(2)``、``(3)`` 后缀。
        """
        realpath = os.path.realpath(os.path.abspath(os.path.expanduser(realpath)))
        if not os.path.exists(realpath):
            raise FileNotFoundError(realpath)

        with self._lock:
            for m in self._mounts.values():
                if m.realpath == realpath:
                    return m.vpath, False

            base = os.path.basename(realpath.rstrip("\\/")) or realpath.replace(":", "")
            vpath, n = "/" + base, 1
            while vpath in self._mounts:
                n += 1
                vpath = f"/{base} ({n})"

            self._mounts[vpath] = _Mount(vpath, realpath)
            return vpath, True

    def remove(self, vpath: str) -> bool:
        with self._lock:
            return self._mounts.pop(vpath, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._mounts.clear()

    # -- 查询 ---------------------------------------------------------------

    def resolve(self, url_path: str) -> str | None:
        """URL 路径 -> 真实文件系统路径。未挂载、或越出挂载点范围时返回 None。"""
        with self._lock:
            mounts = sorted(self._mounts.values(), key=lambda m: len(m.vpath), reverse=True)

        for m in mounts:
            if m.is_dir:
                if url_path == m.vpath:
                    return m.realpath
                if not url_path.startswith(m.vpath + "/"):
                    continue
                parts = url_path[len(m.vpath) + 1:].split("/")
                # 任何一段是 . 或 .. 都直接拒绝，不给路径穿越留余地
                if any(p in ("", ".", "..") for p in parts):
                    return None
                cand = os.path.realpath(os.path.join(m.realpath, *parts))
                base = os.path.realpath(m.realpath)
                # 再用 realpath 兜一次底，防符号链接指到挂载点外面
                if cand == base or cand.startswith(base + os.sep):
                    return cand
                return None
            elif url_path == m.vpath:
                return m.realpath

        return None

    def mount_of(self, url_path: str):
        """返回 url_path 落在哪个挂载点上，没有则 None。"""
        with self._lock:
            mounts = sorted(self._mounts.values(), key=lambda m: len(m.vpath), reverse=True)
        for m in mounts:
            if url_path == m.vpath or (m.is_dir and url_path.startswith(m.vpath + "/")):
                return m
        return None

    def snapshot(self) -> list[dict]:
        """当前所有分享项的快照，按加入顺序 —— 新加的排在后面，
        界面上就能"往下滚看到最新"。"""
        with self._lock:
            mounts = list(self._mounts.values())
        return [
            {
                "vpath": m.vpath,
                "realpath": m.realpath,
                "name": os.path.basename(m.realpath.rstrip("\\/")) or m.realpath,
                "is_dir": m.is_dir,
                "size": m.size,
                "count": m.count,
            }
            for m in mounts
        ]

    def __len__(self) -> int:
        with self._lock:
            return len(self._mounts)


# --------------------------------------------------------------------------
# 传输追踪
# --------------------------------------------------------------------------

class DownloadTask:
    """一次「下载」—— 某个来源下载某个文件。

    注意它和 HTTP 连接不是一回事：一个任务可能横跨多条连接（下载工具分段拉、
    断线后重连续传），所以状态是从「当前还有没有活跃连接」推出来的，而不是
    显式维护的状态机：

        conns > 0                  -> 进行中
        conns == 0 且已传够整个文件 -> 已完成
        conns == 0 且没传够         -> 已中断（之后重连续传会自动变回进行中）
    """

    __slots__ = ("ip", "name", "url_path", "file_size", "transferred", "conns",
                 "started", "created", "updated", "completed",
                 "_speed", "_peak", "_peak_conns", "_last_t", "_last_bytes")

    def __init__(self, client_ip: str, name: str, url_path: str, file_size: int):
        now = time.monotonic()
        self.ip = client_ip
        self.name = name
        self.url_path = url_path
        self.file_size = max(0, file_size)
        self.transferred = 0
        self.conns = 0
        self.started = now
        self.created = time.time()      # 墙钟时间，界面按它排序（位置固定不跳动）
        self.updated = time.time()      # 墙钟时间，显示"什么时候结束的"
        self.completed = False
        self._speed = 0.0
        self._peak = 0.0
        self._peak_conns = 0
        self._last_t = now
        self._last_bytes = 0

    @property
    def state(self) -> str:
        if self.conns > 0:
            return "active"
        return "done" if self.completed else "cancelled"

    @property
    def progress(self) -> float:
        if self.file_size <= 0:
            return 1.0 if self.completed else 0.0
        return min(1.0, self.transferred / self.file_size)

    def speed(self, now: float) -> float:
        """瞬时速度，做了一点指数平滑，避免数字乱跳。"""
        dt = now - self._last_t
        if dt >= 0.3:
            inst = (self.transferred - self._last_bytes) / dt
            self._speed = inst if self._speed <= 0 else self._speed * 0.55 + inst * 0.45
            self._last_t = now
            self._last_bytes = self.transferred
            if self._speed > self._peak:
                self._peak = self._speed
        return self._speed

    @property
    def peak(self) -> float:
        """本次下载中出现过的最高速度（平滑后的值，与界面显示同源）。"""
        return self._peak

    @property
    def peak_conns(self) -> int:
        """本次下载中出现过的最大并发连接数。

        任务结束后 conns 归零，那一刻的实时值没有留存意义；峰值能说明
        对方是用浏览器单连接下的，还是下载工具分段拉的。
        """
        return self._peak_conns


class _Conn:
    """一条活跃的 HTTP 连接，挂在某个 DownloadTask 上。"""

    __slots__ = ("task", "sent")

    def __init__(self, task: DownloadTask):
        self.task = task
        self.sent = 0


class TransferTracker:
    """记录每一次下载，含进行中与已结束的。线程安全。"""

    # 结束后仍保留在界面上的任务条数上限
    MAX_TASKS = 400

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[int, _Conn] = {}
        self._tasks: dict[tuple, DownloadTask] = {}
        self._seq = 0
        self.total_completed = 0
        self.total_bytes = 0

    def begin(self, client_ip: str, name: str, url_path: str,
              length: int, file_size: int | None = None) -> int:
        """开一条连接。length 是本次要传的字节数，file_size 是整个文件的大小
        （Range 请求时两者不同，进度要按整个文件算）。"""
        if file_size is None:
            file_size = length

        with self._lock:
            key = (client_ip, url_path)
            task = self._tasks.get(key)
            if task is None:
                task = DownloadTask(client_ip, name, url_path, file_size)
                self._tasks[key] = task
                self._trim()
            elif file_size and file_size != task.file_size:
                task.file_size = file_size      # 文件被替换过，更新一下基准

            task.conns += 1
            if task.conns > task._peak_conns:
                task._peak_conns = task.conns
            task.updated = time.time()
            if task.conns == 1:
                task._last_t = time.monotonic()
                task._last_bytes = task.transferred

            self._seq += 1
            self._active[self._seq] = _Conn(task)
            return self._seq

    def advance(self, tid: int, nbytes: int) -> None:
        with self._lock:
            conn = self._active.get(tid)
            if conn is not None:
                conn.sent += nbytes
                conn.task.transferred += nbytes

    def end(self, tid: int, ok: bool = True) -> None:
        with self._lock:
            conn = self._active.pop(tid, None)
            if conn is None:
                return

            task = conn.task
            task.conns = max(0, task.conns - 1)
            task.updated = time.time()
            if conn.sent > 0:
                self.total_bytes += conn.sent

            # 传够整个文件才算完成 —— 分段下载要等最后一段补齐，中途断掉的不算
            if not task.completed and task.file_size > 0 and task.transferred >= task.file_size:
                task.completed = True
                self.total_completed += 1

    def clear_finished(self) -> int:
        """清掉所有已结束的记录，返回清掉的条数。进行中的不动。"""
        with self._lock:
            gone = [k for k, t in self._tasks.items() if t.conns == 0]
            for k in gone:
                self._tasks.pop(k, None)
            return len(gone)

    def _trim(self) -> None:
        """调用方需持有锁。记录超过上限时，从最旧的已结束任务开始丢。"""
        if len(self._tasks) <= self.MAX_TASKS:
            return
        finished = sorted((t.updated, k) for k, t in self._tasks.items() if t.conns == 0)
        for _, k in finished[:len(self._tasks) - self.MAX_TASKS]:
            self._tasks.pop(k, None)

    def snapshot(self) -> dict:
        """给 GUI 轮询用的状态快照：正在下的，以及已经结束的。

        `active` 是还有活跃连接的任务，`recent` 是已结束的（完成或中断），
        按最后活动时间倒序 —— 后者就是"下载记录"。
        """
        now = time.monotonic()
        with self._lock:
            def pack(t: DownloadTask) -> dict:
                total = t.file_size
                sent = t.transferred if total <= 0 else min(t.transferred, total)
                return {
                    "ip": t.ip,
                    "name": t.name,
                    "url_path": t.url_path,
                    "total": total,
                    "sent": sent,
                    "progress": t.progress,
                    "speed": t.speed(now) if t.conns else 0.0,
                    "peak": t.peak,
                    "peak_conns": t.peak_conns,
                    "conns": t.conns,
                    "state": t.state,
                    "created": t.created,
                    "updated": t.updated,
                }

            active, recent = [], []
            for t in self._tasks.values():
                (active if t.conns else recent).append(pack(t))

            recent.sort(key=lambda x: x["updated"], reverse=True)
            return {
                "active": active,
                "recent": recent,
                "task_count": len(active),
                "conn_count": sum(x["conns"] for x in active),
                "completed": self.total_completed,
                "bytes_served": self.total_bytes,
            }


# --------------------------------------------------------------------------
# 下载安全策略
# --------------------------------------------------------------------------

class SecurityPolicy:
    """下载密码 + 每个分享项的下载次数上限。两者默认都不启用。

    次数怎么算：按「一个客户端把一整个文件下完」算一次。中间断线重来、下载器
    开八个线程分段拉，都只算一次 —— 因为按传输次数算的话，多线程下载会一次
    就吃掉八个额度，这显然不是用户设「最多下 3 次」时想要的意思。
    """

    # 同一次下载的累计记录闲置这么久就丢掉，避免 _pending 无限长大
    _PENDING_TTL = 1800.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._password = ""
        self._limit = 0                                   # 0 = 不限次数
        self._used: dict[str, int] = {}                   # vpath -> 已完成次数
        self._pending: dict[tuple, list] = {}             # (ip, vpath) -> 累计状态
        self._blocked_raw: list[str] = []                 # 原样保留，界面要显示
        self._blocked: set[str] = set()                   # 精确 IP
        self._blocked_nets: list = []                     # 预解析的网段

    # -- 密码 ---------------------------------------------------------------

    @property
    def password(self) -> str:
        with self._lock:
            return self._password

    def set_password(self, value: str) -> None:
        with self._lock:
            self._password = value or ""

    @property
    def limit(self) -> int:
        with self._lock:
            return self._limit

    def set_limit(self, value: int) -> None:
        with self._lock:
            self._limit = max(0, int(value))
            self._pending.clear()

    def check_password(self, supplied) -> bool:
        with self._lock:
            expected = self._password
        if not expected:
            return True                                   # 没设密码就是全开
        if not supplied:
            return False
        return hmac.compare_digest(str(supplied), expected)

    # -- 次数 ---------------------------------------------------------------

    def used(self, vpath: str) -> int:
        with self._lock:
            return self._used.get(vpath, 0)

    def remaining(self, vpath: str):
        """还能下几次；None 表示不限。"""
        with self._lock:
            if not self._limit:
                return None
            return max(0, self._limit - self._used.get(vpath, 0))

    def allowed(self, vpath: str) -> bool:
        left = self.remaining(vpath)
        return left is None or left > 0

    def record(self, vpath: str, client_ip: str, sent: int, file_size: int) -> bool:
        """累计一次传输的数据量；凑够一整个文件就记一次下载。返回是否刚扣减。"""
        if file_size <= 0 or sent <= 0:
            return False

        now = time.monotonic()
        with self._lock:
            if not self._limit:
                return False                              # 没限次数就不用记账

            key = (client_ip, vpath)
            st = self._pending.get(key)
            if st is None or st[1] != file_size or now - st[2] > self._PENDING_TTL:
                st = [0, file_size, now]                  # 重新开一次下载
                self._pending[key] = st
            st[0] += sent
            st[2] = now

            if st[0] < file_size:
                return False                              # 还没下完，先不扣

            self._pending.pop(key, None)
            self._used[vpath] = self._used.get(vpath, 0) + 1
            self._gc(now)
            return True

    def _gc(self, now: float) -> None:
        """调用方需持有锁。"""
        stale = [k for k, v in self._pending.items() if now - v[2] > self._PENDING_TTL]
        for k in stale:
            self._pending.pop(k, None)

    # -- 黑名单 -------------------------------------------------------------

    @property
    def blocked(self) -> list[str]:
        with self._lock:
            return list(self._blocked_raw)

    def set_blocked(self, entries) -> int:
        """整批替换黑名单，返回其中格式合法的条数。

        网段在这里预解析好，免得每个请求都去解析一遍。
        """
        raw, exact, nets = [], set(), []
        for e in entries:
            norm = parse_block_entry(e)
            if norm is None:
                continue
            raw.append(norm)
            if "/" in norm:
                nets.append(ipaddress.ip_network(norm, strict=False))
            else:
                exact.add(norm)
        with self._lock:
            self._blocked_raw = raw
            self._blocked = exact
            self._blocked_nets = nets
        return len(raw)

    def is_blocked(self, client_ip: str) -> bool:
        with self._lock:
            exact = self._blocked
            nets = self._blocked_nets
        if not exact and not nets:
            return False
        if client_ip in exact:
            return True
        if nets:
            try:
                addr = ipaddress.ip_address(client_ip)
            except ValueError:
                return False
            for net in nets:
                if addr in net:
                    return True
        return False

    def snapshot(self) -> dict:
        with self._lock:
            return {"limit": self._limit, "used": dict(self._used),
                    "has_password": bool(self._password),
                    "blocked": list(self._blocked_raw)}

    def reset_usage(self) -> None:
        with self._lock:
            self._used.clear()
            self._pending.clear()


# --------------------------------------------------------------------------
# 网页模板（目录页 / 索引页 / 错误页共用一套样式）
# --------------------------------------------------------------------------

_PAGE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f3f3f3;--card:#fff;--fg:#1b1b1b;--dim:#5c5c5c;--line:#e5e5e5;
  --accent:#0067c0;--accent-fg:#fff;--hover:#f0f6fc;
  --shadow:0 1px 2px rgba(0,0,0,.06),0 2px 8px rgba(0,0,0,.05);
}
@media (prefers-color-scheme:dark){
  :root{--bg:#202020;--card:#2b2b2b;--fg:#f0f0f0;--dim:#a0a0a0;--line:#3d3d3d;
        --accent:#60cdff;--accent-fg:#0a2b40;--hover:#333;
        --shadow:0 1px 2px rgba(0,0,0,.3),0 2px 8px rgba(0,0,0,.25)}
}
body{background:var(--bg);color:var(--fg);padding:32px 16px 64px;
  font:14px/1.6 "Segoe UI","Microsoft YaHei UI","PingFang SC",system-ui,sans-serif}
.wrap{max-width:880px;margin:0 auto}
h1{font-size:22px;font-weight:600;margin-bottom:6px;word-break:break-all}
.sub{color:var(--dim);font-size:13px}
.head{display:flex;align-items:flex-start;gap:16px;margin-bottom:18px}
.head .grow{flex:1;min-width:0}
.btn{display:inline-flex;align-items:center;gap:6px;flex:none;white-space:nowrap;
  padding:9px 16px;border-radius:6px;font-size:13px;font-weight:500;
  background:var(--accent);color:var(--accent-fg);text-decoration:none}
.btn:hover{filter:brightness(1.08)}
.crumbs{font-size:13px;margin-bottom:16px;color:var(--dim);word-break:break-all}
.crumbs a{color:var(--accent);text-decoration:none}
.crumbs a:hover{text-decoration:underline}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
  box-shadow:var(--shadow);overflow:hidden}
.row{display:flex;align-items:center;gap:14px;padding:11px 16px;
  border-bottom:1px solid var(--line);text-decoration:none;color:inherit}
.row:last-child{border-bottom:none}
.row:hover{background:var(--hover)}
.ic{width:22px;text-align:center;font-size:17px;flex:none}
.nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sz{color:var(--dim);font-size:12.5px;font-variant-numeric:tabular-nums;flex:none;min-width:74px;text-align:right}
.tm{color:var(--dim);font-size:12.5px;flex:none;min-width:118px;text-align:right}
.empty{padding:48px 16px;text-align:center;color:var(--dim)}
.err{font-size:52px;font-weight:200;color:var(--dim);margin-bottom:8px}
.foot{margin-top:22px;text-align:center;color:var(--dim);font-size:12px}
.foot code{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:1px 6px}
"""


def _render_page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_PAGE_CSS}</style>\n</head>\n<body>\n<div class=\"wrap\">\n"
        f"{body}\n"
        f'<div class="foot">由 {SITE_NAME} 提供 · 基于 Python http.server</div>\n'
        "</div>\n</body>\n</html>\n"
    )


def _fmt_mtime(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (ValueError, OSError):
        return "—"


# --------------------------------------------------------------------------
# HTTP 处理器
# --------------------------------------------------------------------------

class ShareHandler(SimpleHTTPRequestHandler):
    """把 http.server 的默认行为，改造成「只服务已挂载的分享项」。"""

    server_version = "ShareFiles/1.0"
    # HTTP/1.1 才有 keep-alive，也让 Range 语义完整。代价是每个响应都必须
    # 带正确的 Content-Length，本文件里所有分支都保证了这一点。
    protocol_version = "HTTP/1.1"
    bufsize = 64 * 1024

    # 注意：socketserver 的 __init__ 内部会立刻开始处理请求（setup/handle/
    # finish），所以这些状态必须作为类属性给出默认值 —— 写在 __init__ 里
    # 的赋值要等请求处理完才执行，那时早就用到了。
    _tid: int | None = None
    _remaining = 0
    _quota_vpath: str | None = None
    _quota_size = 0

    # -- 基础 ---------------------------------------------------------------

    def __init__(self, *args, **kwargs):
        kwargs.pop("directory", None)
        super().__init__(*args, directory=None, **kwargs)

    @property
    def registry(self) -> ShareRegistry:
        return self.server.registry              # type: ignore[attr-defined]

    @property
    def tracker(self) -> TransferTracker:
        return self.server.tracker               # type: ignore[attr-defined]

    @property
    def policy(self) -> SecurityPolicy:
        return self.server.policy                # type: ignore[attr-defined]

    @property
    def url_prefix(self) -> str:
        """站内链接都要带上的随机前缀，如 '/a1b2c3d4e5f60718'。"""
        token = getattr(self.server, "token", "")
        return "/" + token if token else ""

    # -- 访问密码 -----------------------------------------------------------

    def do_GET(self):
        if not self._rejected_by_blacklist() and self._authenticated():
            super().do_GET()

    def do_HEAD(self):
        if not self._rejected_by_blacklist() and self._authenticated():
            super().do_HEAD()

    def _rejected_by_blacklist(self) -> bool:
        """黑名单检查放在最前面 —— 被拉黑的连密码框都不该看到。"""
        ip = self.client_address[0]
        if not self.policy.is_blocked(ip):
            return False

        body = _render_page("无法访问", (
            '<div class="err">403</div>'
            '<h1>无法访问</h1>'
            f'<div class="sub">这个地址（{html.escape(ip)}）已被分享者加入黑名单。</div>'
            '<div class="card"><div class="empty">如需访问，请联系分享者解除。</div></div>'
        )).encode("utf-8")

        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except OSError:
                pass
        return True

    def _authenticated(self) -> bool:
        """没设密码就直接放行；设了就要求 HTTP Basic Auth。"""
        if not self.policy.password:
            return True

        supplied = None
        header = self.headers.get("Authorization", "")
        if header[:6].lower() == "basic ":
            try:
                raw = base64.b64decode(header[6:].strip(), validate=True)
                text = raw.decode("utf-8", "replace")
                supplied = text.partition(":")[2]     # 用户名随便填，只认密码
            except (binascii.Error, ValueError):
                supplied = None

        if self.policy.check_password(supplied):
            return True

        body = _render_page("需要身份验证", (
            '<div class="err">401</div>'
            '<h1>需要身份验证</h1>'
            '<div class="sub">该分享已启用访问密码，请输入后重试。</div>'
            '<div class="card"><div class="empty">用户名可任意填写，密码请向分享者索取。</div></div>'
        )).encode("utf-8")

        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ShareFiles", charset="UTF-8"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except OSError:
                pass
        return False

    def log_message(self, fmt, *args):           # noqa: A003
        """GUI 模式下没有控制台，静音；命令行模式由 ShareServer 决定是否输出。"""
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def handle(self):
        """连接被对方直接掐断（取消下载、连完即关）是常态，不是故障。

        keep-alive 下服务端会回去读下一个请求行，此时对方已经走了，标准库
        在这里不捕异常，会让 socketserver 打一整段回溯。这里兜住。
        """
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass

    @staticmethod
    def _normalize(path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        path = urllib.parse.unquote(path, errors="surrogatepass")
        path = posixpath.normpath(path)
        if not path.startswith("/"):
            path = "/" + path
        if len(path) > 1:
            path = path.rstrip("/")
        return path or "/"

    def _strip_token(self, url_path: str):
        """URL 形如 /<token>/…，剥掉前缀后的才是真正的分享路径。

        前缀对不上就返回 None —— 对外一律当作 404，不区分"路径不存在"和
        "前缀不对"，免得给人摸索的机会。
        """
        token = getattr(self.server, "token", "")
        if not token:
            return url_path
        prefix = "/" + token
        if url_path == prefix:
            return "/"
        if url_path.startswith(prefix + "/"):
            return url_path[len(prefix):] or "/"
        return None

    def translate_path(self, path: str) -> str:
        stripped = self._strip_token(self._normalize(path))
        if stripped is None:
            return ""
        return self.registry.resolve(stripped) or ""

    # -- 路由 ---------------------------------------------------------------

    def send_head(self):
        # 页面右上角那个"下载全部"就是带 ?zip=1 打开同一地址
        want_zip = "zip" in urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query)

        url_path = self._strip_token(self._normalize(self.path))
        if url_path is None:
            return self._send_error_page(404, "文件不存在", "该文件可能已被移除分享列表。")

        if url_path == "/":
            if want_zip:
                return self._send_zip("/", "全部内容.zip", self._collect_all())
            return self._send_index()

        real = self.registry.resolve(url_path)
        if not real or not os.path.exists(real):
            return self._send_error_page(404, "文件不存在", "该文件可能已被移除分享列表。")

        if os.path.isdir(real):
            if want_zip:
                entries = self._collect_dir(real, url_path)
                if not entries:
                    return self._send_error_page(
                        404, "这个文件夹是空的", "没有可打包的内容。")
                name = (os.path.basename(real.rstrip("\\/")) or "下载") + ".zip"
                return self._send_zip(url_path, name, entries)
            return self._send_directory(real, url_path)
        return self._send_file(real, url_path)

    # -- 打包下载 -----------------------------------------------------------

    @staticmethod
    def _walk(root: str, arc_base: str, quota_vpath: str) -> list:
        """把一个文件 / 目录摊开成 [(zip 内路径, 磁盘路径, 归属的分享项)]。"""
        if os.path.isfile(root):
            return [(arc_base, root, quota_vpath)]
        out = []
        for dirpath, _dirs, filenames in os.walk(root):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                out.append((os.path.join(arc_base, rel), full, quota_vpath))
        return out

    def _collect_all(self) -> list:
        """根索引页的打包清单。额度用尽的分享项直接跳过 —— 打包不能变成
        绕开次数限制的后门。"""
        out = []
        for it in self.registry.snapshot():
            if not self.policy.allowed(it["vpath"]):
                continue
            # 用 vpath 而不是纯文件名做包内路径：不同目录下的同名文件
            # （报告.pdf 和 报告 (2).pdf）用 name 会在包里撞车、互相覆盖
            arc = it["vpath"].lstrip("/") or it["name"]
            out.extend(self._walk(it["realpath"], arc, it["vpath"]))
        return out

    def _collect_dir(self, real_dir: str, url_path: str) -> list:
        mount = self.registry.mount_of(url_path)
        quota_vpath = mount.vpath if mount else url_path
        base = os.path.basename(real_dir.rstrip("\\/")) or "下载"
        return self._walk(real_dir, base, quota_vpath)

    def _send_zip(self, url_path: str, zip_name: str, entries: list):
        if not entries:
            return self._send_error_page(404, "没有可下载的内容", "分享列表是空的。")

        # 先打包到临时文件再发：Content-Length 必须提前知道，而且整包可能很大，
        # 全塞内存会爆。SpooledTemporaryFile 在 8MB 以内用内存，超了自动落盘。
        tmp = tempfile.SpooledTemporaryFile(max_size=8 << 20)
        try:
            try:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                    for arc, full, _ in entries:
                        try:
                            zf.write(full, arc, compress_type=_compress_type(arc))
                        except OSError:
                            continue      # 单个文件读不了就跳过，别让整包失败
            except OSError:
                return self._send_error_page(500, "打包失败", "读取文件时出错。")

            size = tmp.tell()
            tmp.seek(0)

            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             self._attachment_header(zip_name))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            if self.command == "HEAD":
                return None

            tid = self.tracker.begin(self.client_address[0], zip_name,
                                     url_path, size, size)
            sent = 0
            try:
                while sent < size:
                    chunk = tmp.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    self.tracker.advance(tid, len(chunk))
            except (BrokenPipeError, ConnectionResetError):
                self.tracker.end(tid, ok=False)
                return None

            self.tracker.end(tid, ok=sent >= size)

            # 整包发完才算数：参与的每个分享项各记一次下载 —— 否则点一下
            # "下载全部"就等于把每个文件的额度都白送了一次
            if sent >= size:
                for vpath in {v for _, _, v in entries}:
                    self.policy.record(vpath, self.client_address[0], 1, 1)
            return None
        finally:
            tmp.close()

    def list_directory(self, path):              # 父类入口，统一转到我们的实现
        return self._send_directory(path, self._normalize(self.path))

    # -- 虚拟根索引页 --------------------------------------------------------

    def _send_index(self):
        items = self.registry.snapshot()
        if not items:
            body = ('<h1>尚未分享任何内容</h1>'
                    '<div class="card"><div class="empty">分享者尚未添加文件。</div></div>')
            return self._write_html(200, _render_page(SITE_NAME, body))

        rows = []
        for it in items:
            href = (self.url_prefix + urllib.parse.quote(it["vpath"])
                    + ("/" if it["is_dir"] else ""))
            icon = "📁" if it["is_dir"] else "📄"
            size = f'{it["count"]} 项' if it["is_dir"] else human_size(it["size"])
            rows.append(
                f'<a class="row" href="{href}">'
                f'<span class="ic">{icon}</span>'
                f'<span class="nm">{html.escape(it["name"])}</span>'
                f'<span class="sz">{size}</span></a>'
            )

        body = (
            '<div class="head"><div class="grow">'
            '<h1>共享内容</h1>'
            f'<div class="sub">共 {len(items)} 项，点击下载</div>'
            '</div>'
            '<a class="btn" href="?zip=1">⬇&nbsp;下载全部</a>'
            '</div>'
            f'<div class="card">{"".join(rows)}</div>'
        )
        return self._write_html(200, _render_page("共享内容", body))

    # -- 目录页 -------------------------------------------------------------

    def _send_directory(self, real_dir: str, url_path: str):
        raw_path = self.path.split("?", 1)[0]
        if not raw_path.endswith("/"):
            # 补上结尾斜杠，否则页面里的相对链接会算错
            self.send_response(301)
            self.send_header("Location", raw_path + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        try:
            entries = []
            with os.scandir(real_dir) as it:
                for e in it:
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    is_dir = e.is_dir()
                    entries.append((e.name, is_dir, None if is_dir else st.st_size, st.st_mtime))
        except OSError as exc:
            return self._send_error_page(403, "无法读取这个文件夹", str(exc))

        entries.sort(key=lambda t: (not t[1], t[0].lower()))

        mount = self.registry.mount_of(url_path)
        root_vpath = mount.vpath if mount else ""
        rel = url_path[len(root_vpath):].strip("/") if root_vpath else url_path.strip("/")

        pre = self.url_prefix
        crumbs = [f'<a href="{pre}{urllib.parse.quote(root_vpath)}/">'
                  f'{html.escape(os.path.basename(root_vpath) or "根目录")}</a>']
        acc = root_vpath
        for part in [p for p in rel.split("/") if p]:
            acc += "/" + part
            crumbs.append(
                f'<a href="{pre}{urllib.parse.quote(acc)}/">{html.escape(part)}</a>')

        head = os.path.basename(real_dir) or "根目录"
        rows = []
        if rel:                                   # 有上级时给一条返回链接
            rows.append('<a class="row" href="../"><span class="ic">↩</span>'
                        '<span class="nm">返回上级</span>'
                        '<span class="sz">—</span><span class="tm"></span></a>')

        for name, is_dir, size, mtime in entries:
            href = urllib.parse.quote(name) + ("/" if is_dir else "")
            icon = "📁" if is_dir else "📄"
            size_txt = "—" if is_dir else human_size(size)
            rows.append(
                f'<a class="row" href="{href}">'
                f'<span class="ic">{icon}</span>'
                f'<span class="nm">{html.escape(name)}</span>'
                f'<span class="sz">{size_txt}</span>'
                f'<span class="tm">{_fmt_mtime(mtime)}</span></a>'
            )

        listing = "".join(rows) or '<div class="empty">此文件夹为空。</div>'
        zip_btn = ('<a class="btn" href="?zip=1">⬇&nbsp;下载全部</a>'
                   if any(not d for _, d, _, _ in entries) else "")
        body = (
            f'<div class="head"><div class="grow">'
            f'<h1>{html.escape(head)}</h1>'
            f'<div class="crumbs">{" / ".join(crumbs)}</div>'
            f'</div>{zip_btn}</div>'
            f'<div class="card">{listing}</div>'
        )
        return self._write_html(200, _render_page(head, body))

    # -- 文件下载 -----------------------------------------------------------

    def _send_file(self, real_path: str, url_path: str):
        # 次数按「分享项」算：文件夹里每个文件都属于同一个额度
        mount = self.registry.mount_of(url_path)
        quota_vpath = mount.vpath if mount else url_path
        if not self.policy.allowed(quota_vpath):
            limit = self.policy.limit
            return self._send_error_page(
                403, "下载次数已用完",
                f"该分享项最多允许下载 {limit} 次，当前额度已用尽。"
                "如需继续下载，请联系分享者重置。")

        try:
            f = open(real_path, "rb")
        except OSError:
            return self._send_error_page(404, "文件不存在", "该文件可能已被移除分享列表。")

        try:
            st = os.fstat(f.fileno())
            total = st.st_size
            ctype = self.guess_type(real_path)
            if ctype.startswith("text/") or ctype in (
                "application/json", "application/xml", "application/javascript"
            ):
                ctype += "; charset=utf-8"

            rng = self._parse_range(total)
            if rng is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                f.close()
                return None

            start, end = rng
            length = max(0, end - start + 1)
            partial = start != 0 or length != total

            self.send_response(206 if partial else 200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", self.date_time_string(st.st_mtime))
            # 必须禁止缓存：限次数时，浏览器第二次点同一个链接会直接拿缓存，
            # 请求根本不到服务端，于是既计不了数也拦不住 —— 用户那边看起来
            # 就是"限制没生效"。分享出去的文件也不该留在对方缓存里。
            self.send_header("Cache-Control", "no-store")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            if not getattr(self.server, "inline_preview", False):
                self.send_header("Content-Disposition", self._disposition(real_path))
            self.end_headers()

            if self.command == "HEAD":
                f.close()
                return None

            if start:
                f.seek(start)

            if length == 0:
                f.close()
                return None

            self._tid = self.tracker.begin(
                self.client_address[0], os.path.basename(real_path),
                url_path, length, total,       # total 是整个文件，Range 时和 length 不同
            )
            self._remaining = length
            # 记的是整个文件的大小，不是本次分段的大小 —— 这样多线程下载器
            # 把八段加起来才凑满一次，不会被算成八次
            self._quota_vpath = quota_vpath
            self._quota_size = total
            return f
        except Exception:
            f.close()
            raise

    @staticmethod
    def _attachment_header(name: str) -> str:
        """生成 Content-Disposition，中文名走 RFC 5987 编码。

        响应头只能用 latin-1 编码，所以纯中文名必须配一个 ASCII 回退名，
        否则 send_header 会直接抛 UnicodeEncodeError。
        """
        ascii_name = (name.encode("ascii", "ignore").decode("ascii")
                      .replace('"', "").strip())
        if not ascii_name or ascii_name.startswith("."):
            ascii_name = "download" + os.path.splitext(name)[1]
        return (f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{urllib.parse.quote(name)}")

    @staticmethod
    def _disposition(path: str) -> str:
        return ShareHandler._attachment_header(os.path.basename(path))

    def _parse_range(self, total: int):
        """解析 Range 头，返回闭区间 (start, end)；无 Range 时返回整段；非法返回 None。"""
        header = self.headers.get("Range")
        if not header:
            return (0, max(0, total - 1))
        header = header.strip()
        if not header.lower().startswith("bytes="):
            return (0, max(0, total - 1))       # 不认识的范围单位，忽略

        whole = (0, max(0, total - 1))

        spec = header[6:].split(",")[0].strip()  # 多段只取第一段
        if "-" not in spec:
            return whole                         # 看不懂的语法就当没提 Range
        left, _, right = spec.partition("-")
        left, right = left.strip(), right.strip()
        try:
            if not left:                         # "-500"：最后 500 字节
                n = int(right)
                if n <= 0:
                    return None                  # 语法合法但取不到任何字节
                start, end = max(0, total - n), total - 1
            elif not right:                      # "500-"：从 500 到结尾
                start, end = int(left), total - 1
            else:
                start, end = int(left), int(right)
        except ValueError:
            # RFC 7233：解析不了的 Range 要忽略掉、回整段，
            # 416 只留给"语法没问题、但范围满足不了"的情况
            return whole

        if start < 0 or start > end or start >= total:
            return None
        return (start, min(end, total - 1))

    def copyfile(self, source, outputfile):
        """分块发送，顺便把进度喂给 tracker —— 下载状态就是这么来的。"""
        tid = self._tid
        if tid is None:
            return super().copyfile(source, outputfile)

        tracker = self.tracker
        remaining = self._remaining
        sent = 0
        try:
            while remaining > 0:
                chunk = source.read(min(self.bufsize, remaining))
                if not chunk:
                    break
                outputfile.write(chunk)         # wfile 无缓冲，write 返回即已发出
                remaining -= len(chunk)
                sent += len(chunk)
                tracker.advance(tid, len(chunk))
        except (BrokenPipeError, ConnectionResetError):
            # 对方取消了下载或直接关了页面。这是常态，不是故障，
            # 静默收尾即可 —— 抛上去只会让 socketserver 打一堆无谓的回溯
            tracker.end(tid, ok=False)
            self._tid = None
            return
        except OSError:
            tracker.end(tid, ok=False)
            self._tid = None
            raise

        done = remaining <= 0
        tracker.end(tid, ok=done)               # 没发完就不算完成
        self._tid = None

        if done and self._quota_vpath:
            self.policy.record(self._quota_vpath, self.client_address[0],
                               sent, self._quota_size)

    def finish(self):
        # 兜底：连接在中途断掉时，确保不会在界面上留下一条永远"下载中"的记录
        if self._tid is not None:
            self.tracker.end(self._tid, ok=False)
            self._tid = None
        super().finish()

    # -- 响应工具 -----------------------------------------------------------

    def _write_html(self, code: int, markup: str):
        data = markup.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(data)
            except OSError:
                pass
        return None

    def _send_error_page(self, code: int, title: str, detail: str = ""):
        body = (
            f'<div class="err">{code}</div>'
            f'<h1>{html.escape(title)}</h1>'
            f'<div class="sub">{html.escape(detail)}</div>'
            f'<div class="card"><div class="empty">'
            f'<a href="{self.url_prefix}/">← 返回共享内容列表</a></div></div>'
        )
        return self._write_html(code, _render_page(title, body))


# --------------------------------------------------------------------------
# 服务器
# --------------------------------------------------------------------------

class ShareServer(ThreadingHTTPServer):
    """带分享注册表和传输追踪的 HTTP 服务。"""

    daemon_threads = True

    # Windows 的 SO_REUSEADDR 语义和 Unix 不同：它允许两个进程绑定同一个端口，
    # 内核把新连接随机分给其中一个。于是"上一个实例没退干净时又开了一个"会
    # 表现为链接时灵时不灵、时不时 404 —— 对分享工具来说这是最坏的一种故障，
    # 因为发起者以为分享成功了。宁可绑定失败让 create_server 换个端口。
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, addr, registry: ShareRegistry, tracker: TransferTracker,
                 policy: SecurityPolicy | None = None):
        super().__init__(addr, ShareHandler)
        self.registry = registry
        self.tracker = tracker
        self.policy = policy if policy is not None else SecurityPolicy()
        # 所有地址都长这样：http://ip:port/<token>/…，只有拿到完整链接才进得来。
        # 光知道 IP 和端口是没用的。
        self.token = new_share_token()
        self.inline_preview = False
        self.verbose = False
        self._thread: threading.Thread | None = None

    def refresh_token(self) -> str:
        """换一个随机前缀，旧链接立刻失效（已经建立的下载不受影响）。"""
        self.token = new_share_token()
        return self.token

    @property
    def port(self) -> int:
        return self.server_address[1]

    def start(self) -> None:
        """在后台线程里开始服务，GUI 主线程不会被阻塞。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self.serve_forever, kwargs={"poll_interval": 0.2},
            name="sharefiles-http", daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self.shutdown()
        self.server_close()
        self._thread = None


def create_server(registry, tracker, port: int = 8000, host: str = "",
                  max_tries: int = 20, policy: SecurityPolicy | None = None):
    """起服务；端口被占用时自动往后试，返回 (server, 实际端口)。"""
    last: Exception | None = None
    for p in range(port, port + max_tries):
        if p > 65535:
            break
        try:
            return ShareServer((host, p), registry, tracker, policy), p
        except OSError as exc:
            last = exc
            continue
    raise OSError(f"从 {port} 起连续 {max_tries} 个端口都无法使用") from last


# --------------------------------------------------------------------------
# 命令行入口（方便不开界面时直接用，也方便调试）
# --------------------------------------------------------------------------

def _main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="fileserver",
        description="基于标准库 http.server 的局域网文件分享",
    )
    ap.add_argument("paths", nargs="*", help="要分享的文件或文件夹")
    ap.add_argument("-p", "--port", type=int, default=8000, help="起始端口，默认 8000")
    ap.add_argument("--host", default="", help="绑定地址，默认所有网卡")
    args = ap.parse_args(argv)

    registry, tracker = ShareRegistry(), TransferTracker()
    for path in args.paths:
        try:
            vpath, added = registry.add(path)
            print(f"  {'+' if added else '='} {vpath}  <-  {path}")
        except FileNotFoundError:
            print(f"  ! 路径不存在，已跳过：{path}", file=sys.stderr)

    if not len(registry):
        print("没有任何可分享的内容。用法：python fileserver.py <文件或文件夹>...")
        return 1

    try:
        server, port = create_server(registry, tracker, args.port, args.host)
    except OSError as exc:
        print(f"无法启动服务：{exc}", file=sys.stderr)
        return 1

    server.verbose = True
    server.start()
    ips = get_lan_ips() or ["127.0.0.1"]
    print(f"\n  共享中，端口 {port}：")
    for ip in ips:
        print(f"    http://{ip}:{port}/")
    print("\n  Ctrl+C 停止\n")

    try:
        while True:
            time.sleep(0.5)
            snap = tracker.snapshot()
            if snap["task_count"]:
                for it in snap["active"]:
                    print(f"    ↓ {it['ip']}  {it['name']}  "
                          f"{it['progress'] * 100:.0f}%  {human_speed(it['speed'])}")
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

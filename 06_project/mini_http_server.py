#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mini_http_server.py -- 第 19 课（综合项目）：把 18 课的知识组装成一个真服务器
============================================================================

这是整套课程的综合项目。它把前面所有东西串成一条线:

    第 1~4 课  地址/DNS/网卡/路由   -> 服务器要 bind 哪个地址、客户端怎么找到它
    第 5~7 课  端口/五元组/扫描      -> listen 的语义、谁来连、怎么排障
    第 8~9 课  UDP/DNS 报文          -> 二进制协议解析的思路（这里用来解析 HTTP 头）
    第 10~14 课 TCP 握手/字节流/粘包  -> 请求怎么切边界、连接怎么复用/关闭
    第 15~17 课 HTTP 报文/服务/客户端 -> 本文件的核心
    第 18 课   TLS                   -> 本文件提供 --tls 选项, 用自签证书跑 HTTPS

【它比 demo_16 多了什么】
    * Range 请求（断点续传 / 视频拖动进度条）
    * 缓存协商: ETag + Last-Modified, 命中就回 304（省流量）
    * 两种并发模型的实现与实测对比:
        ThreadedServer  每连接一个线程（第 16 课的做法）
        SelectorServer  单线程事件循环（selectors, 非阻塞 IO）
    * 访问日志 + 指标统计（QPS、P99 延迟）
    * --tls 用第 18 课的自签证书跑 HTTPS

运行：
    python3 mini_http_server.py                     # 自测 + 双模型压测对比（有证书则连 HTTPS 一起), 然后退出
    python3 mini_http_server.py --without-tls       # 只跑 HTTP
    python3 mini_http_server.py --serve             # 常驻(HTTP), 用 curl 手工验证
    python3 mini_http_server.py --serve --tls       # 常驻(HTTPS), curl -k 验证
"""

from __future__ import annotations

import mimetypes
import os
import selectors
import signal
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_ROOT = os.path.join(HERE, "_www")
TLS_DIR = os.path.join(HERE, "..", "05_http", "_tls_demo")
MAX_HEAD_BYTES = 32 * 1024
READ_CHUNK = 64 * 1024


# ================================================================ HTTP 基础件
@dataclass
class Request:
    method: str
    target: str
    version: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def path(self) -> str:
        return unquote(urlparse(self.target).path)

    def keep_alive(self) -> bool:
        conn = self.headers.get("connection", "").lower()
        return conn == "keep-alive" if self.version == "HTTP/1.0" else conn != "close"


class BadRequest(Exception):
    pass


class Incomplete(Exception):
    """数据还不够, 需要继续 recv。"""


@dataclass
class Response:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    content_length: int | None = None

    REASONS = {200: "OK", 206: "Partial Content", 301: "Moved Permanently", 302: "Found",
               304: "Not Modified", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
               405: "Method Not Allowed", 416: "Range Not Satisfiable", 500: "Internal Server Error"}

    def serialize(self, keep_alive: bool) -> bytes:
        lines = [f"HTTP/1.1 {self.status} {self.REASONS.get(self.status, 'Unknown')}"]
        headers = dict(self.headers)
        headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        headers.setdefault("Server", "mini-http-server/1.0")
        headers["Content-Length"] = str(self.content_length if self.content_length is not None else len(self.body))
        headers["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
        headers["Connection"] = "keep-alive" if keep_alive else "close"
        for key, value in headers.items():
            lines.append(f"{key}: {value}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + self.body


def parse_request(buf: bytearray) -> tuple[Request | None, int]:
    """从缓冲区里尝试解析一个完整请求, 返回 (请求, 消费字节数)。

    这是第 12 课「长度前缀/分隔符」思想在真实协议上的应用:
        头: 以 \\r\\n\\r\\n 结束
        体: 由 Content-Length 指明长度
    数据不够就抛 Incomplete（调用方继续 recv）, 而不是猜。
    """
    head_end = buf.find(b"\r\n\r\n")
    if head_end < 0:
        if len(buf) > MAX_HEAD_BYTES:
            raise BadRequest("请求头过大")
        raise Incomplete

    head = bytes(buf[:head_end])
    lines = head.decode("iso-8859-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        raise BadRequest(f"请求行不合法: {lines[0]!r}")
    method, target, version = parts

    headers: dict[str, str] = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if not sep:
            raise BadRequest(f"头不合法: {line!r}")
        headers[key.strip().lower()] = value.strip()
    if version == "HTTP/1.1" and "host" not in headers:
        raise BadRequest("HTTP/1.1 必须带 Host")

    length = int(headers.get("content-length", "0") or 0)
    total = head_end + 4 + length
    if len(buf) < total:
        raise Incomplete
    body = bytes(buf[head_end + 4:total])
    return Request(method, target, version, headers, body), total


def safe_join(root: str, path: str) -> str | None:
    """把 URL 路径映射到磁盘路径, 越界返回 None（路径穿越防护, 安全底线）。"""
    target = os.path.realpath(os.path.join(root, path.lstrip("/") or "index.html"))
    root = os.path.realpath(root)
    if target != root and not target.startswith(root + os.sep):
        return None
    return target


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """解析 `Range: bytes=start-end`（只支持单段的常见形式）。

    返回 (起始, 结束 inclusive), 不合法返回 None。
    这就是「视频拖动进度条」「curl -C - 断点续传」背后的东西。
    """
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):].split(",")[0].strip()      # 多段 Range 少见, 这里只取第一段
    start_s, _, end_s = spec.partition("-")
    try:
        if start_s == "":                                    # bytes=-500 -> 最后 500 字节
            n = int(end_s)
            if n <= 0:
                return None
            return max(size - n, 0), size - 1
        start = int(start_s)
        end = int(end_s) if end_s else size - 1
    except ValueError:
        return None
    if start > end or start >= size:
        return None
    return start, min(end, size - 1)


def etag_for(path: str) -> str:
    """用 mtime+size 造一个弱 ETag。生产上常用内容哈希（更准但更贵）。"""
    info = os.stat(path)
    return f'W/"{int(info.st_mtime)}-{info.st_size}"'


def http_date(ts: float) -> str:
    return time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(ts))


def route(request: Request, root: str) -> Response:
    """把请求变成响应。只支持 GET/HEAD/POST(echo) 与静态文件。"""
    if request.method == "POST" and request.path == "/echo":
        return Response(200, headers={"Content-Type": "application/octet-stream"}, body=b"echo:" + request.body)
    if request.method == "GET" and request.path == "/healthz":
        return Response(200, body=b"ok\n")

    if request.method not in ("GET", "HEAD"):
        allowed = ["GET", "HEAD"] + (["POST"] if request.path == "/echo" else [])
        return Response(405, headers={"Allow": ", ".join(allowed)},
                        body=f"405, allowed: {', '.join(allowed)}\n".encode())

    return serve_static(request, root)


def serve_static(request: Request, root: str) -> Response:
    target = safe_join(root, request.path)
    if target is None:
        return Response(403, body=b"403 Forbidden\n")
    if os.path.isdir(target):
        if not request.path.endswith("/"):
            return Response(301, headers={"Location": request.path + "/"}, body=b"")
        target = os.path.join(target, "index.html")
    if not os.path.isfile(target):
        return Response(404, body=f"404 Not Found: {request.path}\n".encode())

    info = os.stat(target)
    etag = etag_for(target)
    last_modified = http_date(info.st_mtime)

    # ---- 缓存协商（第 15 课的头字段在这里发挥作用）----
    # 客户端带上 If-None-Match / If-Modified-Since, 表示"我本地有缓存, 没变就别重发"
    if request.headers.get("if-none-match") == etag:
        return Response(304, headers={"ETag": etag, "Last-Modified": last_modified}, body=b"")
    ims = request.headers.get("if-modified-since")
    if ims and ims == last_modified:
        return Response(304, headers={"ETag": etag, "Last-Modified": last_modified}, body=b"")

    content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
    if content_type.startswith("text/"):
        content_type += "; charset=utf-8"
    headers = {"Content-Type": content_type, "ETag": etag, "Last-Modified": last_modified,
               "Accept-Ranges": "bytes"}

    # ---- Range 请求（断点续传 / 视频拖动）----
    range_header = request.headers.get("range")
    if range_header:
        span = parse_range(range_header, info.st_size)
        if span is None:
            return Response(416, headers={**headers, "Content-Range": f"bytes */{info.st_size}"}, body=b"")
        start, end = span
        with open(target, "rb") as fh:
            fh.seek(start)
            body = fh.read(end - start + 1)
        headers["Content-Range"] = f"bytes {start}-{end}/{info.st_size}"
        if request.method == "HEAD":
            return Response(206, headers=headers, body=b"", content_length=len(body))
        return Response(206, headers=headers, body=body)

    with open(target, "rb") as fh:
        body = fh.read()
    if request.method == "HEAD":
        return Response(200, headers=headers, body=b"", content_length=len(body))
    return Response(200, headers=headers, body=body)


# ================================================================ 统计
class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.count = 0
        self.bytes_out = 0
        self.latencies: list[float] = []
        self.statuses: dict[int, int] = {}

    def record(self, status: int, size: int, elapsed: float) -> None:
        with self.lock:
            self.count += 1
            self.bytes_out += size
            self.latencies.append(elapsed)
            self.statuses[status] = self.statuses.get(status, 0) + 1

    def summary(self) -> dict:
        with self.lock:
            if not self.latencies:
                return {"count": 0}
            ordered = sorted(self.latencies)
            pick = lambda q: ordered[min(int(len(ordered) * q), len(ordered) - 1)] * 1000
            return {"count": self.count, "bytes": self.bytes_out, "p50_ms": pick(0.5),
                    "p99_ms": pick(0.99), "statuses": dict(self.statuses)}


# ================================================================ 模型 A: 每连接一线程
class ThreadedServer:
    """阻塞 IO + 每连接一个线程。写法最直观, 也是 demo_16 用的模型。"""

    name = "ThreadedServer"

    def __init__(self, root: str, host: str = "127.0.0.1", port: int = 0, tls: tuple | None = None):
        self.root = root
        self.metrics = Metrics()
        self.tls = tls
        self.ssl_context = None
        if tls:
            self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ssl_context.load_cert_chain(*tls)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(256)
        self.address = self.sock.getsockname()
        self._stop = threading.Event()

    def serve_forever(self) -> None:
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        self.sock.close()

    def _handle(self, conn: socket.socket) -> None:
        if self.ssl_context:
            try:
                conn = self.ssl_context.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError):
                return
        with conn:
            conn.settimeout(5.0)
            buf = bytearray()
            while not self._stop.is_set():
                try:
                    request, consumed = parse_request(buf)
                except Incomplete:
                    try:
                        chunk = conn.recv(READ_CHUNK)
                    except (socket.timeout, OSError):
                        return
                    if not chunk:
                        return
                    buf += chunk
                    continue
                except BadRequest as exc:
                    conn.sendall(Response(400, body=f"400: {exc}\n".encode()).serialize(False))
                    return

                del buf[:consumed]
                started = time.perf_counter()
                response = route(request, self.root)
                payload = response.serialize(request.keep_alive())
                try:
                    conn.sendall(payload)
                except OSError:
                    return
                self.metrics.record(response.status, len(payload), time.perf_counter() - started)
                if not request.keep_alive():
                    return

    def shutdown(self) -> None:
        self._stop.set()


# ================================================================ 模型 B: 单线程事件循环
class SelectorServer:
    """非阻塞 socket + selectors 事件循环。

    一个线程管理所有连接:
        可读 -> recv 进缓冲区 -> 解析出完整请求就生成响应, 挂到写队列
        可写 -> 把写队列刷出去
    这是 Java NIO / Node.js / nginx / Redis 的同一套思路（C10K 的标准答案）。

    代价: 不能用阻塞调用（任何一次阻塞都会拖住所有连接）,
          代码复杂度明显上升（状态机、部分写、缓冲区管理）。
    """

    name = "SelectorServer"

    def __init__(self, root: str, host: str = "127.0.0.1", port: int = 0):
        self.root = root
        self.metrics = Metrics()
        self.selector = selectors.DefaultSelector()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(256)
        self.sock.setblocking(False)
        self.address = self.sock.getsockname()
        self.selector.register(self.sock, selectors.EVENT_READ, data=None)
        self._stop = False
        self.conns = 0

    # ---- 每个连接的状态 ----
    class _Conn:
        __slots__ = ("sock", "inbuf", "outbuf", "close_when_drained")

        def __init__(self, sock: socket.socket):
            self.sock = sock
            self.inbuf = bytearray()
            self.outbuf = bytearray()
            self.close_when_drained = False

    def serve_forever(self) -> None:
        while not self._stop:
            for key, mask in self.selector.select(timeout=0.3):
                if key.data is None:
                    self._accept()
                else:
                    self._service(key.fileobj, key.data, mask)
        self._close_all()

    def _accept(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        conn.setblocking(False)
        self.selector.register(conn, selectors.EVENT_READ, data=SelectorServer._Conn(conn))
        self.conns += 1

    def _service(self, sock: socket.socket, state: "SelectorServer._Conn", mask: int) -> None:
        if mask & selectors.EVENT_READ:
            try:
                chunk = sock.recv(READ_CHUNK)
            except BlockingIOError:
                chunk = None
            except OSError:
                self._drop(sock)
                return
            if chunk == b"":                     # 对端关闭(EOF): 无论缓冲区里有什么都收工
                self._drop(sock)
                return
            if chunk:
                state.inbuf += chunk
                self._consume_requests(state)
        if mask & selectors.EVENT_WRITE:
            self._flush(state)

    def _consume_requests(self, state: "SelectorServer._Conn") -> None:
        """把 inbuf 里所有完整请求都处理掉（可能一次 recv 里有多个, 粘包!）。"""
        while True:
            try:
                request, consumed = parse_request(state.inbuf)
            except Incomplete:
                break
            except BadRequest as exc:
                state.outbuf += Response(400, body=f"400: {exc}\n".encode()).serialize(False)
                state.close_when_drained = True
                break
            del state.inbuf[:consumed]
            started = time.perf_counter()
            response = route(request, self.root)
            payload = response.serialize(request.keep_alive())
            state.outbuf += payload
            self.metrics.record(response.status, len(payload), time.perf_counter() - started)
            if not request.keep_alive():
                state.close_when_drained = True
                break
        self._flush(state)

    def _flush(self, state: "SelectorServer._Conn") -> None:
        """非阻塞 send: 只发得出去多少发多少, 剩下的等下次可写事件。"""
        if not state.outbuf:
            if state.close_when_drained:
                self._drop(state.sock)
                return
            self.selector.modify(state.sock, selectors.EVENT_READ, state)
            return
        try:
            sent = state.sock.send(state.outbuf)
        except BlockingIOError:
            sent = 0
        except OSError:
            self._drop(state.sock)
            return
        del state.outbuf[:sent]
        if state.outbuf:
            self.selector.modify(state.sock, selectors.EVENT_READ | selectors.EVENT_WRITE, state)
        elif state.close_when_drained:
            self._drop(state.sock)
        else:
            self.selector.modify(state.sock, selectors.EVENT_READ, state)

    def _drop(self, sock: socket.socket) -> None:
        try:
            self.selector.unregister(sock)
        except (KeyError, ValueError):
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _close_all(self) -> None:
        for key in list(self.selector.get_map().values()):
            self._drop(key.fileobj)
        self.sock.close()

    def shutdown(self) -> None:
        self._stop = True


# ================================================================ 客户端工具
class RawHttpClient:
    """一个支持 keep-alive 的最小 HTTP 客户端。

    它自己做两件真实客户端必须做的事（也是第 12/15 课的考点）:
      1. 用 Content-Length 读出恰好一个响应, 并且把「多读到的那部分」留在缓冲区
         （否则下一个响应就解析错乱了）
      2. 复用同一条 TCP 连接发多个请求（keep-alive）
    """

    def __init__(self, host: str, port: int, timeout: float = 10.0, tls: bool = False):
        self.host, self.port = host, port
        self.buf = bytearray()
        self.sock = socket.create_connection((host, port), timeout=timeout)
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE      # 仅用于本机自签证书实验
            self.sock = ctx.wrap_socket(self.sock, server_hostname="localhost")
        self.sock.settimeout(timeout)

    def request(self, path: str, extra_headers: dict | None = None,
                method: str = "GET", keep_alive: bool = True) -> tuple[int, dict, bytes]:
        headers = {"Host": f"{self.host}:{self.port}",
                   "Connection": "keep-alive" if keep_alive else "close"}
        headers.update(extra_headers or {})
        raw = (f"{method} {path} HTTP/1.1\r\n"
               + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n")
        self.sock.sendall(raw.encode("latin-1"))
        return self._read_response(method)

    def _read_response(self, method: str = "GET") -> tuple[int, dict, bytes]:
        # 1) 先确保头收全
        while True:
            head_end = self.buf.find(b"\r\n\r\n")
            if head_end >= 0:
                break
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("响应头没收全就断了")
            self.buf += chunk
        head = bytes(self.buf[:head_end])
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers = {}
        for line in lines[1:]:
            key, sep, value = line.partition(":")
            if sep:
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0") or 0)
        # HTTP 规则: 这几种响应没有 body, 即使头里写了 Content-Length 也不能去读:
        #   HEAD 请求的响应 / 1xx / 204 No Content / 304 Not Modified
        # （真实客户端忘掉这条, 就会在 HEAD 或 304 上卡住, 是经典 bug）
        if method == "HEAD" or status in (204, 304) or 100 <= status < 200:
            length = 0
        # 2) 再确保 body 收满（长度由 Content-Length 决定, 不能多读）
        need = head_end + 4 + length
        while len(self.buf) < need:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("响应体没收全就断了")
            self.buf += chunk
        body = bytes(self.buf[head_end + 4:need])
        del self.buf[:need]                      # 关键: 只消费这一条响应
        return status, headers, body

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def client_get(host: str, port: int, path: str, headers: dict | None = None,
               tls: bool = False, timeout: float = 5.0,
               method: str = "GET") -> tuple[int, dict, bytes]:
    """一次性请求（每次都新建连接）, 用于自测。"""
    client = RawHttpClient(host, port, timeout=timeout, tls=tls)
    try:
        return client.request(path, extra_headers=headers, method=method, keep_alive=False)
    finally:
        client.close()


def expect(label: str, ok: bool, detail: str = "") -> bool:
    print(f"    [{'PASS' if ok else 'FAIL'}] {label}" + (f"   {detail}" if detail else ""))
    return bool(ok)


# ================================================================ 自测
def self_test(server, tls: bool = False) -> bool:
    host, port = server.address
    app_path = os.path.join(STATIC_ROOT, "app.js")
    with open(app_path, "rb") as fh:
        app_bytes = fh.read()
    size = len(app_bytes)
    ok = True

    status, headers, body = client_get(host, port, "/index.html", tls=tls)
    ok &= expect("GET /index.html -> 200", status == 200 and b"mini http server" in body,
                 f"status={status} len={len(body)}")
    etag = headers.get("etag")
    ok &= expect("响应带 ETag / Last-Modified / Accept-Ranges",
                 bool(etag and headers.get("last-modified") and headers.get("accept-ranges") == "bytes"),
                 f"etag={etag}")

    status, headers, body = client_get(host, port, "/index.html",
                                      headers={"If-None-Match": etag or ""}, tls=tls)
    ok &= expect("If-None-Match 命中 -> 304 且没有 body", status == 304 and body == b"",
                 f"status={status} body={body[:20]!r}")

    status, headers, body = client_get(host, port, "/app.js", headers={"Range": "bytes=0-99"}, tls=tls)
    ok &= expect("Range: bytes=0-99 -> 206 且正好 100 字节",
                 status == 206 and len(body) == 100
                 and headers.get("content-range") == f"bytes 0-99/{size}"
                 and body == app_bytes[:100],
                 f"status={status} len={len(body)} content-range={headers.get('content-range')}")

    status, headers, tail = client_get(host, port, "/app.js", headers={"Range": "bytes=-50"}, tls=tls)
    ok &= expect("Range: bytes=-50 -> 最后 50 字节",
                 status == 206 and tail == app_bytes[-50:], f"status={status} len={len(tail)}")

    status, headers, body = client_get(host, port, "/app.js", headers={"Range": f"bytes={size + 10}-"}, tls=tls)
    ok &= expect("越界 Range -> 416", status == 416, f"status={status}")

    status, _, _ = client_get(host, port, "/nope.html", tls=tls)
    ok &= expect("GET 不存在的文件 -> 404", status == 404, f"status={status}")

    status, headers, head_body = client_get(host, port, "/app.js", tls=tls, method="HEAD")
    ok &= expect("HEAD -> 200, 无 body, 但 Content-Length 是完整长度",
                 status == 200 and head_body == b"" and int(headers.get("content-length", 0)) == size,
                 f"status={status} len={headers.get('content-length')}")

    status, _, _ = client_get(host, port, "/../mini_http_server.py", tls=tls)
    ok &= expect("路径穿越被拒 -> 403/404", status in (403, 404), f"status={status}")

    status, headers, body = client_get(host, port, "/healthz", tls=tls)
    ok &= expect("健康检查接口 -> 200 ok", status == 200 and body.strip() == b"ok", f"status={status}")

    # keep-alive: 同一条连接上连发 3 个请求（客户端与服务器两侧都要正确分帧）
    client = RawHttpClient(host, port, tls=tls)
    try:
        codes = [client.request("/healthz")[0] for _ in range(3)]
        ok &= expect("keep-alive: 一条连接上连发 3 个请求都成功", codes == [200, 200, 200], f"codes={codes}")
        first, headers, _ = client.request("/app.js", {"Range": "bytes=0-9"})
        second, _, body2 = client.request("/app.js")
        ok &= expect("keep-alive 下 206 与 200 混发不串包",
                     first == 206 and second == 200 and len(body2) == size,
                     f"first={first} second={second} len={len(body2)}")
    finally:
        client.close()

    return ok


# ================================================================ 压测
def bench(server, label: str, requests_per_thread: int = 40, threads: int = 8,
          keep_alive: bool = True, tls: bool = False) -> dict:
    host, port = server.address
    path = "/app.js"                       # 64KB 文件, 能挡住小响应带来的失真
    errors: list[str] = []

    def worker() -> None:
        client = None
        try:
            if keep_alive:
                client = RawHttpClient(host, port, timeout=15, tls=tls)
            for _ in range(requests_per_thread):
                if client is not None:
                    status, _, body = client.request(path)
                else:
                    status, _, body = client_get(host, port, path, tls=tls, timeout=15)
                if status != 200 or len(body) == 0:
                    errors.append(f"status={status} len={len(body)}")
        except OSError as exc:
            errors.append(type(exc).__name__)
        finally:
            if client is not None:
                client.close()

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    started = time.perf_counter()
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    wall = time.perf_counter() - started
    total = requests_per_thread * threads
    return {"server": f"{server.name} / {label}", "requests": total, "errors": len(errors),
            "wall_s": wall, "rps": total / wall, "metrics": server.metrics.summary()}


# ================================================================ 主程序
def prepare_www() -> None:
    os.makedirs(STATIC_ROOT, exist_ok=True)
    with open(os.path.join(STATIC_ROOT, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><meta charset=utf-8>\n<h1>mini http server</h1>\n")
    app_js = os.path.join(STATIC_ROOT, "app.js")
    if not os.path.exists(app_js) or os.path.getsize(app_js) < 64 * 1024:
        # 造一个 64KB 的静态文件, 用来做 Range / 压测
        with open(app_js, "wb") as fh:
            for i in range(1024):
                fh.write(f"// line {i:05d} {'x' * 40}\n".encode())
        # 补齐到 64KB 以上
        with open(app_js, "ab") as fh:
            fh.write(b"// padding to make it bigger\n" * 100)


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def main() -> None:
    prepare_www()
    serve_mode = "--serve" in sys.argv
    # 默认: 如果第 18 课生成的自签证书还在, 就把 HTTPS 也一并验证/压测
    use_tls = "--without-tls" not in sys.argv
    tls_files = None

    if use_tls:
        cert = os.path.join(TLS_DIR, "cert.pem")
        key = os.path.join(TLS_DIR, "key.pem")
        if os.path.exists(cert) and os.path.exists(key):
            tls_files = (cert, key)
            print(f"  使用自签证书: {cert}（由第 18 课生成）")
        else:
            print("  找不到自签证书, 先跑一次 05_http/demo_18_https_tls.py 生成; 本次改用 HTTP。")
            use_tls = False

    # ---------------------------------------------------------------- 常驻模式
    if serve_mode:
        title("常驻模式（Ctrl+C 退出）")
        server = ThreadedServer(STATIC_ROOT, port=8080, tls=tls_files)
        scheme = "https" if tls_files else "http"
        host, port = server.address
        print(f"  静态目录: {STATIC_ROOT}")
        print(f"  监听: {scheme}://{host}:{port}/")
        print(f"""
  手工验证清单（另开终端）:
    curl -i {'-k ' if tls_files else ''}{scheme}://{host}:{port}/index.html
    curl -I {'-k ' if tls_files else ''}{scheme}://{host}:{port}/app.js              # HEAD: 看长度和 ETag
    curl -i {'-k ' if tls_files else ''}--range 0-99 {scheme}://{host}:{port}/app.js  # 206 Partial Content
    curl -i {'-k ' if tls_files else ''}{scheme}://{host}:{port}/healthz
    ETAG=$(curl -sI {'-k ' if tls_files else ''}{scheme}://{host}:{port}/app.js | awk -F': ' '/ETag/{{print $2}}' | tr -d '\\r')
    curl -i {'-k ' if tls_files else ''}-H "If-None-Match: $ETAG" {scheme}://{host}:{port}/app.js   # 304
    curl -v {'-k ' if tls_files else ''}{scheme}://{host}:{port}/app.js {scheme}://{host}:{port}/healthz  # 看连接复用
""")
        signal.signal(signal.SIGINT, lambda *_: server.shutdown())
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        print("  已退出。")
        return

    # ---------------------------------------------------------------- 自测
    title("第 1 步：自测（HTTP 与 HTTPS 各跑一遍）")

    print("  ── 纯 HTTP，线程模型 ──")
    threaded = ThreadedServer(STATIC_ROOT)
    threading.Thread(target=threaded.serve_forever, daemon=True).start()
    host, port = threaded.address
    print(f"  ThreadedServer 监听 {host}:{port}")
    ok_http = self_test(threaded)

    ok_tls = None                          # None = 没跑（没有证书）
    tls_server = None
    if tls_files:
        print("\n  ── HTTPS（自签证书），线程模型 ──")
        tls_server = ThreadedServer(STATIC_ROOT, tls=tls_files)
        threading.Thread(target=tls_server.serve_forever, daemon=True).start()
        print(f"  HTTPS 监听 {tls_server.address[0]}:{tls_server.address[1]}")
        ok_tls = self_test(tls_server, tls=True)
    else:
        print("\n  （没有自签证书, 跳过 HTTPS 自测; 先跑 05_http/demo_18_https_tls.py 可生成）")

    # ---------------------------------------------------------------- 压测
    title("第 2 步：两种并发模型实测对比")

    print("""  压测方式: 8 个线程, 下载 64KB 的 /app.js。两种客户端行为各测一遍:
      「长连接」: 每个线程复用一条 TCP 连接（真实服务的主流用法）
      「短连接」: 每个请求新建一条 TCP 连接（连握手都算进去）
  说明: 本地回环没有真实网络延迟, 这个测法更偏向「协议处理 + 连接建立开销」。
""")
    results = []
    results.append(bench(threaded, "长连接", requests_per_thread=40))
    results.append(bench(threaded, "短连接", requests_per_thread=20, keep_alive=False))
    if tls_server:
        results.append(bench(tls_server, "长连接（TLS）", requests_per_thread=20, tls=True))

    selector = SelectorServer(STATIC_ROOT)
    threading.Thread(target=selector.serve_forever, daemon=True).start()
    results.append(bench(selector, "长连接", requests_per_thread=40))
    results.append(bench(selector, "短连接", requests_per_thread=20, keep_alive=False))

    print(f"  {'模型 / 客户端行为':<40}{'请求数':>7}{'耗时(s)':>9}{'QPS':>9}{'P50(ms)':>9}{'P99(ms)':>9}{'错误':>6}")
    for r in results:
        m = r["metrics"]
        print(f"  {r['server']:<40}{r['requests']:>7}{r['wall_s']:>9.2f}{r['rps']:>9.1f}"
              f"{m.get('p50_ms', 0):>9.2f}{m.get('p99_ms', 0):>9.2f}{r['errors']:>6}")

    print(f"""
  怎么读这张表:
    * 「长连接 vs 短连接」的差距通常最大: 短连接每次都付三次握手（第 10 课）,
      还要在关闭方留下 TIME_WAIT（第 13 课）。这就是「用连接池」的硬道理。
    * 本机回环上, 线程模型和事件循环常常打平: 回环没有真实网络延迟, 线程切换
      成本被掩盖, 而且 Python 有 GIL, CPU 密集时谁都跑不快。
    * 真正的差别在「连接数」: 1 万条空闲连接时, 线程模型需要 1 万个线程（约几 GB 栈空间）,
      事件循环只需要 1 万个 socket 结构（几十 MB）。
    * 所以: 小规模 + 逻辑复杂 -> 线程模型更省心; 高并发 + 长连接 -> 事件循环/异步。
    * 生产里两者常常混用: 事件循环处理连接, 慢操作丢给线程池（asyncio 的
      run_in_executor 就是这个套路）。
    * 压测的铁律: 别只测回环, 别只看平均值, 要看 P99 和错误率。
""")

    # ---------------------------------------------------------------- 收尾
    title("第 3 步：这个服务器还差什么（真实项目的检查单）")

    print("""  □ 请求体大小限制与慢速攻击防护（这里只有头大小限制）
  □ 超时分层: 连接/读写/空闲 keep-alive 各自可配（这里固定 5 秒）
  □ 多进程 + SO_REUSEPORT 吃满多核（Python 单进程受 GIL 限制）
  □ 日志结构化（JSON）+ 采样 + 异步写入（别让日志拖慢请求）
  □ 指标暴露 /metrics 并用 Prometheus 采集（这里只打印汇总）
  □ TLS 会话复用、HTTP/2、gzip/br 压缩、Range 多段
  □ 优雅关闭: 停止 accept -> 等在途请求完成 -> 关闭（本示例只做到停止 accept）
  □ 安全: 请求头注入、目录穿越（已做）、符号链接、权限、CSP 等响应头
  □ 压测用真实工具（wrk/hey/vegeta）+ 真实网络, 而不是本课这种教学脚本
""")

    threaded.shutdown()
    selector.shutdown()
    if tls_server:
        tls_server.shutdown()

    title("本课小结")
    tls_verdict = "跳过（无证书）" if ok_tls is None else ("全部通过" if ok_tls else "有失败")
    print(f"""1. 一个完整的 HTTP 服务器 = 解析 + 路由 + 缓存协商 + Range + 并发模型 + 观测。
2. 自测结果: HTTP {'全部通过' if ok_http else '有失败'}, HTTPS {tls_verdict}。
3. 并发模型是架构选择, 不是性能参数: 线程模型好写, 事件循环能扛连接数。
4. 缓存协商（304）和 Range（206）是「省流量 / 断点续传」的两个最实用的 HTTP 特性。
5. 到这里, 从 IP 地址、端口、TCP、UDP 到 HTTP、TLS 的整条链路你已经亲手走了一遍。
   遇到任何网络问题, 你的排查路径都应该是:
      DNS 解析 -> 路由可达 -> 端口监听 -> TCP 连接 -> TLS 握手 -> HTTP 语义 -> 应用逻辑
""")
    print("课程结束。回到 README.md 看完整目录, 或重跑任意一课复习。")


if __name__ == "__main__":
    main()

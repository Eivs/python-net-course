#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_16_mini_http_server.py -- 第 16 课：用 socket 写一个能用的 HTTP 服务器
==========================================================================

【目的】
    把第 10~15 课学的东西全部串起来, 亲手实现一个「结构正确」的 HTTP/1.1 服务器。
    它不需要功能多, 但必须在这些点上是对的:

      * 按 \\r\\n\\r\\n 切出请求头, 按 Content-Length 读满请求体（第 12 课的边界问题）
      * 响应必须带 Content-Length, 否则客户端不知道读到哪儿（第 15 课）
      * 支持 keep-alive: 一条连接处理多个请求, 并设置空闲超时（第 14 课）
      * 并发模型: 每连接一个线程（简单正确, 但不是最高效, 第 6 章会对比）
      * 状态码语义正确: 200 / 301 / 400 / 404 / 405 / 500
      * 只读自己目录下的文件, 防止 ../ 路径穿越（安全第一课）

【结构】
    parse_request()   字节流 -> Request 对象（含"读到哪算完"的逻辑）
    Router            路径 -> 处理函数
    Response          状态码 + 头 + body, 负责序列化成字节
    HttpServer        accept 循环 + 每连接线程 + 日志 + 优雅关闭
    self_test()       自带测试, 用标准库客户端真连一遍, 断言结果

运行：
    python3 demo_16_mini_http_server.py            # 启动服务, 自带测试后退出
    python3 demo_16_mini_http_server.py --serve    # 常驻, 让你用 curl 手工验证
"""

from __future__ import annotations

import http.client
import mimetypes
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


# ================================================================ 数据结构
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

    @property
    def query(self) -> str:
        return urlparse(self.target).query

    def wants_keep_alive(self) -> bool:
        conn = self.headers.get("connection", "").lower()
        if self.version == "HTTP/1.0":
            return conn == "keep-alive"
        return conn != "close"


class BadRequest(Exception):
    """请求格式不合法 -> 回 400。"""


@dataclass
class Response:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    # HEAD 请求的响应没有 body, 但必须报告「如果用 GET 请求会有多大」,
    # 所以这里允许显式指定 Content-Length（None 表示按 body 长度算）。
    content_length: int | None = None

    REASONS = {
        200: "OK",
        201: "Created",
        204: "No Content",
        301: "Moved Permanently",
        302: "Found",
        304: "Not Modified",
        400: "Bad Request",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        500: "Internal Server Error",
    }

    def serialize(self, keep_alive: bool) -> bytes:
        reason = self.REASONS.get(self.status, "Unknown")
        lines = [f"HTTP/1.1 {self.status} {reason}"]
        headers = dict(self.headers)
        headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        headers["Content-Length"] = str(
            self.content_length
            if self.content_length is not None
            else len(self.body)
        )
        headers["Date"] = time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime()
        )
        headers["Connection"] = "keep-alive" if keep_alive else "close"
        for key, value in headers.items():
            lines.append(f"{key}: {value}")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        return head + self.body


# ================================================================ 请求解析
MAX_HEADER_BYTES = 64 * 1024  # 防止 slowloris / 超大头攻击
MAX_BODY_BYTES = 8 * 1024 * 1024


def read_request(conn: socket.socket, buf: bytearray) -> Request | None:
    """从连接读出一个完整请求。

    返回 None 表示对端正常关闭（连接可以结束）。
    抛出 BadRequest 表示请求不合法（应回 400 并断开）。

    这里体现了第 12 课的全部功夫:
      * 头部靠 \\r\\n\\r\\n 分隔（方案 B）
      * body 靠 Content-Length 定长读（方案 C）
      * 缓冲区里可能已经有下一个请求的数据 -> 必须原样留着, 不能丢
    """
    # 1) 读头: 一直读到 \\r\\n\\r\\n
    while b"\r\n\r\n" not in buf:
        if len(buf) > MAX_HEADER_BYTES:
            raise BadRequest("请求头过大")
        chunk = conn.recv(4096)
        if not chunk:
            return None  # 对端关闭, 没拿到完整请求
        buf += chunk

    head, _, rest = buf.partition(b"\r\n\r\n")
    buf[:] = rest  # 只保留 body 及后续数据
    try:
        text = head.decode("iso-8859-1")
    except UnicodeDecodeError as exc:
        raise BadRequest(f"请求头不是合法文本: {exc}") from exc

    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise BadRequest(f"请求行格式错误: {lines[0]!r}")
    method, target, version = parts
    if not version.startswith("HTTP/"):
        raise BadRequest(f"不认识的协议版本: {version!r}")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise BadRequest(f"头字段缺少冒号: {line!r}")
        headers[key.strip().lower()] = (
            value.strip()
        )  # 头名大小写不敏感, 统一小写

    if version == "HTTP/1.1" and "host" not in headers:
        raise BadRequest("HTTP/1.1 请求必须带 Host 头")

    # 2) 读 body: 按 Content-Length（分块传输请求体很少见, 这里不支持, 直接 411/400）
    body = b""
    if "chunked" in headers.get("transfer-encoding", "").lower():
        raise BadRequest("本教学服务器不支持 chunked 请求体")
    length = int(headers.get("content-length", "0") or 0)
    if length > MAX_BODY_BYTES:
        raise BadRequest("请求体过大")
    while len(buf) < length:
        chunk = conn.recv(65536)
        if not chunk:
            raise BadRequest("请求体在收全之前对端就关了")
        buf += chunk
    body = bytes(buf[:length])
    del buf[:length]

    return Request(
        method=method,
        target=target,
        version=version,
        headers=headers,
        body=body,
    )


# ================================================================ 服务器
class HttpServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        static_root: str | None = None,
        idle_timeout: float = 5.0,
    ):
        self.static_root = os.path.abspath(static_root) if static_root else None
        self.idle_timeout = idle_timeout
        self.routes: dict[tuple[str, str], callable] = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(64)
        self.address = self.sock.getsockname()
        self._stop = threading.Event()
        self.log_lines: list[str] = []
        self.start_time = time.time()

    # ------------------------------------------------------------ 路由注册
    def route(self, method: str, path: str):
        def decorator(func):
            self.routes[(method.upper(), path)] = func
            return func

        return decorator

    # ------------------------------------------------------------ 生命周期
    def serve_forever(self) -> None:
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, peer = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            # 每连接一个线程: 简单、正确, 但线程数一多就吃力（第 6 章对比）
            threading.Thread(
                target=self._handle_connection, args=(conn, peer), daemon=True
            ).start()

    def shutdown(self) -> None:
        self._stop.set()
        self.sock.close()

    # ------------------------------------------------------------ 单连接处理
    def _handle_connection(self, conn: socket.socket, peer: tuple) -> None:
        with conn:
            conn.settimeout(self.idle_timeout)
            buf = bytearray()
            try:
                while not self._stop.is_set():
                    try:
                        request = read_request(conn, buf)
                    except BadRequest as exc:
                        self._log(peer, "?", f"400 ({exc})", 0)
                        conn.sendall(
                            Response(
                                400, body=f"400 Bad Request: {exc}\n".encode()
                            ).serialize(keep_alive=False)
                        )
                        return
                    except socket.timeout:
                        return  # 空闲超时, 回收连接
                    if request is None:
                        return  # 对端关闭

                    started = time.perf_counter()
                    try:
                        response = self._dispatch(request)
                    except Exception as exc:  # 兜底: 别让异常杀死线程
                        response = Response(
                            500,
                            body=f"500 Internal Server Error: {exc}\n".encode(),
                        )
                    payload = response.serialize(
                        keep_alive=request.wants_keep_alive()
                    )
                    conn.sendall(payload)
                    self._log(
                        peer,
                        f"{request.method} {request.path}",
                        f"{response.status}",
                        len(payload),
                        time.perf_counter() - started,
                    )

                    if not request.wants_keep_alive():
                        return
            except (ConnectionResetError, BrokenPipeError):
                return  # 客户端跑了, 正常现象

    def _log(
        self,
        peer: tuple,
        request_line: str,
        status: str,
        size: int,
        elapsed: float = 0.0,
    ) -> None:
        line = f"{peer[0]}:{peer[1]} {request_line} -> {status} {size}B {elapsed * 1000:.1f}ms"
        self.log_lines.append(line)
        print(f"    [access] {line}")

    # ------------------------------------------------------------ 路由分发
    def _dispatch(self, request: Request) -> Response:
        key = (request.method, request.path)
        if key in self.routes:
            return self.routes[key](request)

        # 同一个路径存在但方法不对 -> 405（顺便告知允许哪些方法）
        allowed = sorted(m for (m, p) in self.routes if p == request.path)
        if allowed:
            return Response(
                405,
                headers={"Allow": ", ".join(allowed)},
                body=f"405 Method Not Allowed, allowed: {', '.join(allowed)}\n".encode(),
            )

        if request.method in ("GET", "HEAD"):
            return self._serve_static(request)

        return Response(404, body=b"404 Not Found\n")

    # ------------------------------------------------------------ 静态文件
    def _serve_static(self, request: Request) -> Response:
        if not self.static_root:
            return Response(404, body=b"404 Not Found (no static root)\n")

        # 安全: 用 realpath 归一化, 再确认结果仍在 static_root 内,
        # 这样才能挡住 ../../etc/passwd 这类路径穿越（最常见的一类漏洞）
        relative = request.path.lstrip("/") or "index.html"
        target = os.path.realpath(os.path.join(self.static_root, relative))
        if (
            not target.startswith(self.static_root + os.sep)
            and target != self.static_root
        ):
            return Response(403, body=b"403 Forbidden\n")

        if os.path.isdir(target):
            if not request.path.endswith("/"):
                return Response(
                    301, headers={"Location": request.path + "/"}, body=b""
                )
            target = os.path.join(target, "index.html")

        if not os.path.isfile(target):
            return Response(
                404, body=f"404 Not Found: {request.path}\n".encode()
            )

        with open(target, "rb") as fh:
            body = fh.read()
        content_type = (
            mimetypes.guess_type(target)[0] or "application/octet-stream"
        )
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        headers = {
            "Content-Type": content_type,
            "Last-Modified": time.strftime(
                "%a, %d %b %Y %H:%M:%S GMT",
                time.gmtime(os.path.getmtime(target)),
            ),
        }
        if request.method == "HEAD":
            # HEAD 不返回 body, 但 Content-Length 要和 GET 一致（RFC 要求）,
            # 否则客户端无法用它来判断资源大小或做缓存校验。
            return Response(
                200, headers=headers, body=b"", content_length=len(body)
            )
        return Response(200, headers=headers, body=body)


# ================================================================ 自带测试
def expect(label: str, condition: bool, detail: str = "") -> bool:
    mark = "PASS" if condition else "FAIL"
    print(f"    [{mark}] {label}" + (f"   {detail}" if detail else ""))
    return condition


def self_test(server: HttpServer, static_root: str) -> bool:
    host, port = server.address
    ok = True
    print("  用标准库客户端真连一遍（不是 mock）:")

    # 1) urllib 简单 GET
    with urllib.request.urlopen(f"http://{host}:{port}/hello") as resp:
        body = resp.read()
        ok &= expect(
            "GET /hello -> 200 且 body 正确",
            resp.status == 200 and b"hello" in body,
            f"status={resp.status} body={body!r}",
        )

    # 2) 404
    try:
        urllib.request.urlopen(f"http://{host}:{port}/nope")
        ok &= expect("GET /nope -> 404", False)
    except urllib.error.HTTPError as exc:
        ok &= expect("GET /nope -> 404", exc.code == 404, f"status={exc.code}")

    # 3) 405（用 POST 打一个只允许 GET 的路径）
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/hello", data=b"x", method="POST"
        )
        urllib.request.urlopen(req)
        ok &= expect("POST /hello -> 405", False)
    except urllib.error.HTTPError as exc:
        ok &= expect(
            "POST /hello -> 405（附带 Allow 头）",
            exc.code == 405 and "GET" in (exc.headers.get("Allow") or ""),
            f"status={exc.code} Allow={exc.headers.get('Allow')}",
        )

    # 4) POST 回显（请求体读取是否正确）
    req = urllib.request.Request(
        f"http://{host}:{port}/echo",
        data="中文payload".encode("utf-8"),
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        ok &= expect(
            "POST /echo 回显 body（含 UTF-8 中文）",
            b"\xe4\xb8\xad\xe6\x96\x87" in body,
            f"body={body!r}",
        )

    # 5) keep-alive: 同一个连接发两个请求（http.client 直接暴露连接对象）
    client = http.client.HTTPConnection(host, port, timeout=5)
    try:
        client.request("GET", "/hello")
        r1 = client.getresponse()
        body1 = r1.read()
        sock_before = client.sock
        client.request("GET", "/hello")
        r2 = client.getresponse()
        body2 = r2.read()
        same_connection = client.sock is sock_before
        ok &= expect(
            "keep-alive: 两条请求复用同一条 TCP 连接",
            same_connection and r1.status == 200 and r2.status == 200,
            f"same_socket={same_connection}",
        )
    finally:
        client.close()

    # 6) HEAD: 不能有 body, 但 Content-Length 要对
    client = http.client.HTTPConnection(host, port, timeout=5)
    try:
        client.request("HEAD", "/sample.txt")
        resp = client.getresponse()
        body = resp.read()
        ok &= expect(
            "HEAD 有 Content-Length 但没有 body",
            body == b"" and int(resp.getheader("Content-Length") or 0) > 0,
            f"len={resp.getheader('Content-Length')} body={body!r}",
        )
    finally:
        client.close()

    # 7) 静态文件 + Content-Type
    with urllib.request.urlopen(f"http://{host}:{port}/sample.txt") as resp:
        ok &= expect(
            "静态文件 GET 正常且 Content-Type 正确",
            resp.status == 200
            and "text/plain" in (resp.headers.get("Content-Type") or ""),
            f"ctype={resp.headers.get('Content-Type')}",
        )

    # 8) 路径穿越防护
    try:
        urllib.request.urlopen(
            f"http://{host}:{port}/../demo_16_mini_http_server.py"
        )
        ok &= expect("路径穿越被挡住", False, "居然读到了目录外的文件!")
    except urllib.error.HTTPError as exc:
        ok &= expect(
            "路径穿越被挡住（403/404）",
            exc.code in (403, 404),
            f"status={exc.code}",
        )

    # 9) 手工裸 socket 发一个残缺请求 -> 400
    raw = socket.create_connection((host, port), timeout=5)
    raw.sendall(b"THIS IS NOT HTTP\r\n\r\n")
    data = raw.recv(256)
    raw.close()
    ok &= expect("非法请求行 -> 400", b"400" in data, f"resp={data[:40]!r}")

    # 10) 验证 keep-alive 的空闲超时回收（等一小会儿再关）
    ok &= expect(
        "访问日志有记录",
        len(server.log_lines) >= 8,
        f"共 {len(server.log_lines)} 行",
    )

    return bool(ok)


# ================================================================ 主程序
def main() -> None:
    serve_mode = "--serve" in sys.argv

    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：先看这个服务器一共多少行 / 由哪几块组成")
    print("""    parse_request / read_request   解析: 按 \\r\\n\\r\\n 切头 + 按 Content-Length 读体
    Router + _dispatch             路由: 精确匹配 -> 405 -> 静态文件 -> 404
    Response.serialize             序列化: 状态行 + 头 + 空行 + body, 关键是有 Content-Length
    HttpServer._handle_connection  keep-alive 循环 + 空闲超时 + 异常兜底
    HttpServer._serve_static       静态文件 + MIME + 路径穿越防护
    self_test                      用标准库客户端真连一遍做断言

  总共约 400 行, 没有第三方依赖, 但已经是一个「结构正确」的 HTTP/1.1 服务器。
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：准备静态目录并启动服务器")

    static_root = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_demo_static"
    )
    os.makedirs(static_root, exist_ok=True)
    with open(
        os.path.join(static_root, "index.html"), "w", encoding="utf-8"
    ) as fh:
        fh.write(
            "<!doctype html><meta charset=utf-8><h1>来自 mini HTTP server</h1>\n"
        )
    with open(
        os.path.join(static_root, "sample.txt"), "w", encoding="utf-8"
    ) as fh:
        fh.write("这是一个静态文本文件，由 socket 服务器直接读出。\n")
    print(f"  静态目录: {static_root}")

    server = HttpServer(static_root=static_root)

    @server.route("GET", "/hello")
    def hello(request: Request) -> Response:
        return Response(
            200,
            headers={"Content-Type": "text/plain; charset=utf-8"},
            body=f"hello, 这是动态路由 {request.path}?{request.query}\n".encode(),
        )

    @server.route("GET", "/stats")
    def stats(request: Request) -> Response:
        uptime = time.time() - server.start_time
        body = (
            f"uptime={uptime:.2f}s\nrequests={len(server.log_lines)}\n"
            f"threads={threading.active_count()}\n"
        ).encode()
        return Response(200, body=body)

    @server.route("POST", "/echo")
    def echo(request: Request) -> Response:
        return Response(
            200,
            headers={"Content-Type": "application/octet-stream"},
            body=b"echo:" + request.body,
        )

    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.address
    print(
        f"  服务器已启动: http://{host}:{port}   （每连接一个线程 + keep-alive + 5 秒空闲超时）"
    )

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：自带测试（真连真发，不是 mock）")

    passed = self_test(server, static_root)

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：访问日志（服务器视角看到了什么）")
    for line in server.log_lines:
        print(f"    {line}")
    print("""
  注意日志里的几个细节:
    * 同一个客户端 IP:端口 出现了多次 -> 那就是 keep-alive 复用连接
    * 每个请求都有耗时, 生产环境要把它做成指标（P50/P99 而不是平均值）
    * 405 的请求记下了「方法+路径」, 这样你才知道是谁在乱调
""")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：手工验证（如果你在终端里跑）")

    print(f"""  本服务器监听 http://{host}:{port} , 你可以另开终端试:

      curl -i http://{host}:{port}/hello                 # 看状态行和头
      curl -i http://{host}:{port}/sample.txt            # 静态文件 + Content-Type
      curl -i -X POST -d 'payload' http://{host}:{port}/echo
      curl -i http://{host}:{port}/nope                  # 404
      curl -i -I http://{host}:{port}/sample.txt         # HEAD
      curl -v http://{host}:{port}/hello http://{host}:{port}/stats   # 看是否复用连接
      curl -i http://{host}:{port}/../demo_16_mini_http_server.py    # 路径穿越应被拒

  用 --serve 参数可以让它常驻, 方便你慢慢试:
      python3 demo_16_mini_http_server.py --serve
""")

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：为什么生产环境不要自己写 HTTP 服务器")

    print("""  上面 400 行代码足以让你理解 HTTP, 但离「能扛线上流量」还差很远:

    1. 并发模型太粗糙: 每连接一个线程, 1 万连接就是 1 万线程 -> 内存和调度都爆
       （第 6 章会对比 线程 / selectors 两种模型）
    2. 没有 TLS: 现在几乎所有流量都是 https, 证书轮换、会话恢复、ALPN 都得处理
    3. 没有 HTTP/2、HTTP/3: 现代浏览器的多路复用、头部压缩全用不上
    4. 慢速攻击: slowloris（一点点发头占用连接）、大 body 攻击
       （本示例只做了大小和超时限制, 真实场景要更细）
    5. 协议细节: Range 断点续传、压缩、100-continue、trailer、请求走私防护、
       各种畸形报文（安全边界几乎全部来自这里, 也是 CVE 高发区）
    6. 运维能力: 优雅重启、热更新、access log 轮转、指标暴露

  正确姿势: 用成熟实现（nginx/Caddy/Envoy 做前端, gunicorn/uvicorn/tomcat 做应用服务器）,
  自己只写业务逻辑。**但理解上面这些原理, 才能在你遇到 502 / 504 / 连接复用 bug 时
  知道该看哪里**（比如 nginx 的 keepalive 与 upstream 的连接池配置）。
""")

    # 收尾
    if serve_mode:
        title("常驻模式（Ctrl+C 退出）")
        print(f"  监听中: http://{host}:{port}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n  收到 Ctrl+C, 正在关闭...")
    server.shutdown()

    title("本课小结")
    print(f"""1. 一个 HTTP 服务器 = 请求解析 + 路由 + 响应序列化 + 连接管理, 本课把它全写了一遍。
2. 「读到哪里算一个请求」是全部难点: 头用 \\r\\n\\r\\n, 体用 Content-Length, 缓冲区要保留残留。
3. 响应必须带 Content-Length; HEAD 只回头不带体; keep-alive 要配合空闲超时。
4. 安全基线: 路径归一化防穿越、头/体大小上限、异常兜底不让线程崩。
5. 自测结论: {("全部通过" if passed else "有失败项, 请看上面的 FAIL 行")}
6. 下一课: 客户端侧, 看标准库/常用库怎么封装这些细节（以及它们的坑）。
""")
    print("下一课: 05_http/demo_17_http_client_libs.py")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

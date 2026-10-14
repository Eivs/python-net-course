#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_15_http_raw.py -- 第 15 课：HTTP 不过是一段格式化的文本
============================================================

【核心认识】
    HTTP 是「应用层协议」, 它的全部内容就是: 双方约定好用什么格式打字。
    跑在 TCP 之上（第 10~13 课那一堆东西全是它的底座）, 所以它继承了
    「字节流、没有消息边界」这个特性 —— HTTP 必须自己解决「响应到哪里结束」的问题。

【请求报文格式】
    GET /index.html HTTP/1.1\r\n        <- 请求行: 方法 路径 版本
    Host: example.com\r\n               <- 头字段（区分大小写不敏感, 但约定这么写）
    User-Agent: python-net-course\r\n
    Accept: */*\r\n
    \r\n                                <- 空行! 头结束的标志（绝对不能漏）
    （GET 通常没有 body; POST 的 body 在这里）

【响应报文格式】
    HTTP/1.1 200 OK\r\n                 <- 状态行: 版本 状态码 原因短语
    Content-Type: text/html\r\n         <- 头字段
    Content-Length: 1256\r\n            <- 长度! 这是「响应体到哪儿结束」的答案之一
    \r\n
    <!DOCTYPE html>...                  <- body

【响应体长度怎么判断（HTTP 里最核心的工程问题）】
    1. Content-Length: N        -> 读 N 字节, 最常见
    2. Transfer-Encoding: chunked -> 分块传输, 每块前面是十六进制长度, 以 0 块结束
                                   （适合边生成边发, 长度事先未知）
    3. Connection: close        -> 读到 EOF 为止（HTTP/1.0 的老办法）
    4. 都没有 -> 按上面的顺序兜底, 兜不住就是协议错误
    这就是 HTTP 版的「长度前缀 / 分隔符 / EOF」三件套, 和第 12 课一模一样。

【本课路线】
    1. 用 socket 手写一个 GET 请求, 发给真实的 example.com:80, 看原始字节
    2. 亲手解析状态行、头、body
    3. 起一个本地教学服务器, 演示 Content-Length / chunked / keep-alive / POST 回显
    4. 头部字段、状态码、方法速查表
    5. HTTP/1.0 -> 1.1 -> 2 -> 3 的演进动机

运行：python3 demo_15_http_raw.py
"""

from __future__ import annotations

import socket
import threading
import time


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def hexdump(data: bytes, prefix: str = "    ", limit: int = 512) -> None:
    shown = data[:limit]
    for offset in range(0, len(shown), 16):
        chunk = shown[offset:offset + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{offset:04x}  {hex_part:<47}  {ascii_part}")
    if len(data) > limit:
        print(f"{prefix}...（共 {len(data)} 字节, 只显示前 {limit} 字节）")


def parse_http_response(raw: bytes) -> dict:
    """一个最小的 HTTP 响应解析器（只看 Content-Length, chunked 在下一节单独处理）。"""
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1").split("\r\n")
    status_line = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return {"status_line": status_line, "headers": headers, "body": body}


# ================================================================ 教学用服务器
class TeachingServer:
    """一个能把 HTTP 各种边界情况演给你看的迷你服务器（纯 socket, 不用 http.server）。

    支持:
        GET /fixed      固定 Content-Length 响应
        GET /chunked    分块传输（Transfer-Encoding: chunked）
        GET /close      不带长度, 靠关连接表示结束
        POST /echo      回显请求体（演示 Content-Length 请求）
        GET /redirect   302 重定向
        其它路径        404
    """

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.address = self.sock.getsockname()
        self.requests_seen: list[str] = []
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        """支持 keep-alive: 一条连接上可以处理多个请求（HTTP/1.1 默认行为）。"""
        with conn:
            conn.settimeout(5)
            buf = b""
            while True:
                # 1) 先读到「头结束」为止: \r\n\r\n 就是分隔符（第 12 课的方案 B）
                while b"\r\n\r\n" not in buf:
                    try:
                        chunk = conn.recv(4096)
                    except (socket.timeout, OSError):
                        return
                    if not chunk:
                        return                    # 客户端关了
                    buf += chunk
                head, _, rest = buf.partition(b"\r\n\r\n")
                lines = head.decode("iso-8859-1").split("\r\n")
                method, path, version = (lines[0].split(" ") + ["", ""])[:3]
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, _, v = line.partition(":")
                        headers[k.strip().lower()] = v.strip()
                self.requests_seen.append(f"{method} {path} {version}")

                # 2) 有 body 的话按 Content-Length 读满（这也是长度前缀的思路）
                length = int(headers.get("content-length", 0))
                while len(rest) < length:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    rest += chunk
                body, buf = rest[:length], rest[length:]

                # 3) 路由
                if method == "POST" and path == "/echo":
                    payload = b"echo:" + body
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                                 + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                                 + payload)
                elif path == "/fixed":
                    payload = b"fixed-length-body"
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                                 + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                                 + payload)
                elif path == "/chunked":
                    # 分块传输: 每块前是十六进制长度 + \r\n, 最后用 0 块结束
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                                 b"Transfer-Encoding: chunked\r\n\r\n")
                    for piece in [b"chunk-1;", b"chunk-2;", b"chunk-3"]:
                        conn.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                        time.sleep(0.05)          # 故意慢一点, 让你看到"流式"
                    conn.sendall(b"0\r\n\r\n")
                elif path == "/close":
                    # HTTP/1.0 风格: 没有长度信息, 靠关闭连接表示结束
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                                 b"Connection: close\r\n\r\n")
                    conn.sendall(b"body-ends-when-connection-closes")
                    conn.shutdown(socket.SHUT_WR)
                    return
                elif path == "/redirect":
                    conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /fixed\r\n"
                                 b"Content-Length: 0\r\n\r\n")
                else:
                    payload = b"not found"
                    conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n"
                                 + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                                 + payload)

                # 4) keep-alive 决定要不要继续复用这条连接
                if version == "HTTP/1.0" or headers.get("connection", "").lower() == "close":
                    return

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def main() -> None:
    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：手写一个 GET 请求，发给真实的 example.com:80")

    print("""  要发的原始请求（注意每行都以 \\r\\n 结束, 最后是一个空行）:
      GET / HTTP/1.1
      Host: example.com
      User-Agent: python-net-course/1.0
      Accept: */*
      Connection: close
      <空行>
""")

    request = (
        "GET / HTTP/1.1\r\n"
        "Host: example.com\r\n"
        "User-Agent: python-net-course/1.0\r\n"
        "Accept: */*\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")

    raw_response = b""
    try:
        sock = socket.create_connection(("example.com", 80), timeout=6)
        t0 = time.perf_counter()
        sock.sendall(request)
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            raw_response += chunk
        elapsed = (time.perf_counter() - t0) * 1000
        sock.close()
        print(f"  服务器响应: {len(raw_response)} 字节, 耗时 {elapsed:.0f} ms")
        print("\n  原始字节（hexdump, 左边是偏移, 右边是 ASCII）:")
        hexdump(raw_response, limit=320)
    except OSError as exc:
        print(f"  连接 example.com:80 失败（可能没网/被墙）: {exc}")
        print("  没关系, 后面的本地实验完全离线可跑。")

    if raw_response:
        parsed = parse_http_response(raw_response)
        print("\n  解析结果:")
        print(f"    状态行: {parsed['status_line']}")
        print("    头字段:")
        for key, value in parsed["headers"].items():
            note = {
                "content-length": "body 的字节数, 读到这么多就结束",
                "content-type": "body 的媒体类型 + 字符集",
                "date": "服务器时间（GMT）",
                "connection": "close 表示这条连接用完就关",
                "server": "服务器软件与版本（信息泄露要小心）",
                "etag": "内容指纹, 用于缓存校验（后端 304 的来源）",
                "cache-control": "缓存策略（max-age 等）",
                "age": "这份响应在中间缓存里待了多久",
            }.get(key, "")
            print(f"      {key}: {value}" + (f"      <- {note}" if note else ""))
        print(f"    body 长度: {len(parsed['body'])} 字节")
        length_header = parsed["headers"].get("content-length")
        if length_header is None:
            transfer = parsed["headers"].get("transfer-encoding", "(无)")
            print(f"    注意: 这条响应没有 Content-Length（Transfer-Encoding: {transfer}）,")
            print("          所以 body 的边界是靠 chunked 分块标记确定的（见下面第 2 步的手写解码）。")
        else:
            print(f"    与 Content-Length 对比: 声明 {length_header}, 实收 {len(parsed['body'])} "
                  f"-> 一致: {int(length_header) == len(parsed['body'])}")
        print("""
  到这里你已经看完了 HTTP 的全貌: 一行状态 + 若干头 + 空行 + body。
  浏览器地址栏敲回车、curl、requests 库, 底层做的都是这件事。
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：起一个本地教学服务器，逐个演示边界情况")

    server = TeachingServer()
    host, port = server.address
    print(f"  教学服务器: http://{host}:{port}")
    print("""  可访问的路径:
      /fixed     有 Content-Length 的普通响应
      /chunked   Transfer-Encoding: chunked 分块响应
      /close     没有长度, 靠关连接表示结束
      POST /echo 回显请求体
      /redirect  302 跳转
      其它       404
""")


    def raw_request(payload: str | bytes) -> socket.socket:
        s = socket.create_connection((host, port), timeout=5)
        s.sendall(payload if isinstance(payload, bytes) else payload.encode("ascii"))
        return s

    # (a) Content-Length
    print("  (a) GET /fixed —— 用 Content-Length 判断 body 结束")
    s = raw_request(f"GET /fixed HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n")
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    hexdump(data, limit=256)
    parsed = parse_http_response(data)
    print(f"      状态行: {parsed['status_line']}")
    print(f"      Content-Length = {parsed['headers'].get('content-length')}, "
          f"实际 body = {len(parsed['body'])} 字节, 内容 = {parsed['body']!r}")
    print("      关键: 收到那么长的字节后, 后面即使还有数据也不属于这个响应 -> 可以开始下一个请求。")

    # (b) chunked
    print("\n  (b) GET /chunked —— Transfer-Encoding: chunked")
    s = raw_request(f"GET /chunked HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n")
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    hexdump(data, limit=256)

    # 手写 chunked 解码（这才是重点: 看清格式）
    head, _, encoded = data.partition(b"\r\n\r\n")
    print("      手写解码分块体:")
    decoded = b""
    rest = encoded
    while True:
        line, _, rest = rest.partition(b"\r\n")
        size = int(line.strip() or b"0", 16)         # 块长度是十六进制!
        if size == 0:
            print(f"        遇到 0 块 -> 传输结束（后面可能还有 trailer 头, 通常忽略）")
            break
        piece, _, rest = rest.partition(b"\r\n")
        decoded += piece[:size]
        print(f"        块大小 {size} (0x{size:x}) -> 内容 {piece[:size]!r}  <- 每块后跟 CRLF")
    print(f"      解码后的 body = {decoded!r}")
    print(f"      与 Content-Length 方案的差别: 长度事先未知也能发, 适合流式生成/压缩/代理转发")

    # (c) close 界定
    print("\n  (c) GET /close —— 没有长度信息, 读到 EOF 为止")
    s = raw_request(f"GET /close HTTP/1.1\r\nHost: {host}\r\n\r\n")
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            print("      recv 返回 b'' -> 连接关闭, 说明 body 结束了（HTTP/1.0 的做法）")
            break
        data += chunk
    s.close()
    parsed = parse_http_response(data)
    print(f"      body = {parsed['body']!r}")
    print("      代价: 每条响应都要新开一条 TCP 连接, 没法复用 -> HTTP/1.1 才引入 keep-alive 和长度")

    # (d) POST + 请求体
    print("\n  (d) POST /echo —— 请求体也要长度（客户端侧同样的问题）")
    payload = "你好，HTTP".encode("utf-8")
    req = (f"POST /echo HTTP/1.1\r\nHost: {host}\r\n"
           f"Content-Type: text/plain; charset=utf-8\r\n"
           f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode("ascii") + payload
    s = raw_request(req)
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    parsed = parse_http_response(data)
    print(f"      发送: Content-Length: {len(payload)}（按字节数, 不是字符数！）")
    print(f"      响应 body: {parsed['body']!r}")
    print("""      注意: 中文一个字符占 3 个 UTF-8 字节, 用 len(str) 算 Content-Length 是经典 bug,
      必须用 len(s.encode('utf-8'))。

  (e) keep-alive —— 一条 TCP 连接发两个请求
""")
    s = socket.create_connection((host, port), timeout=5)
    s.sendall(f"GET /fixed HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    first = s.recv(4096)
    s.sendall(f"GET /fixed HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    second = s.recv(4096)
    s.close()
    print(f"      第一次响应 {len(first)} 字节, 第二次响应 {len(second)} 字节, "
          f"服务端看到的请求序列: {server.requests_seen[-2:]}")
    print("""      同一连接上复用了两次请求（HTTP/1.1 默认 keep-alive）。
      好处: 省掉三次握手和慢启动, 延迟大幅下降。
      代价: 服务端要为每条空闲连接保留资源 -> 需要超时回收（"idle timeout"）。

  (f) 302 重定向 —— 客户端要自己跟着跳（curl -L / requests 默认跟随）
""")
    s = raw_request(f"GET /redirect HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n")
    data = b""
    while True:
        chunk = s.recv(4096)
        if not chunk:
            break
        data += chunk
    s.close()
    parsed = parse_http_response(data)
    print(f"      {parsed['status_line']}   Location: {parsed['headers'].get('location')}")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：速查表")

    print("""  【请求方法】
    GET     取资源, 无副作用, 可缓存, 参数在 URL（会被日志记录, 别放密码）
    POST    提交数据, 有副作用, 不可随意重试           <- 创建资源
    PUT     整体替换, 幂等（重试安全）                  <- 更新资源
    PATCH   局部更新, 不保证幂等
    DELETE  删除, 幂等
    HEAD    只要头不要 body（检查是否存在/拿 Content-Length）
    OPTIONS 问「支持哪些方法」, CORS 预检会用

  【状态码分五类】记住首位即可
    1xx 信息（100 Continue, 101 Switching Protocols）
    2xx 成功（200 OK, 201 Created, 204 No Content）
    3xx 重定向（301 永久, 302/307 临时, 304 Not Modified 用缓存）
    4xx 客户端错（400 参数错, 401 未认证, 403 无权限, 404 不存在, 429 太频繁）
    5xx 服务端错（500 内部错误, 502 网关收到坏响应, 503 过载/维护, 504 网关超时）
    排障口诀: 4xx 改客户端, 5xx 查服务端, 3xx 看 Location, 1xx 一般不用管

  【必须认识的头】
    请求侧: Host(HTTP/1.1 必需!) User-Agent Accept Accept-Encoding Cookie
            Authorization Content-Type Content-Length Referer Origin
    响应侧: Content-Type Content-Length Transfer-Encoding Set-Cookie Cache-Control
            ETag/Last-Modified Location Server Date Connection
    跨域相关: Origin Access-Control-Allow-Origin Access-Control-Allow-Methods（CORS）
    缓存相关: Cache-Control: max-age / no-store, ETag + If-None-Match -> 304
""")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：HTTP 版本演进（每一代都在解决上一代的痛点）")

    print("""  HTTP/0.9 (1991)  只有 GET, 没有头, 没有状态码。就是「给我一个 HTML」。
  HTTP/1.0 (1996)  引入头、状态码、Content-Type; 每个请求一条连接 -> 慢。
  HTTP/1.1 (1997)  主角登场, 直到今天仍是地球上最广泛使用的版本:
                     * 默认 keep-alive（连接复用）
                     * Host 头 -> 一台服务器托管多个域名（虚拟主机, 省钱）
                     * chunked 传输 -> 长度未知也能流式发
                     * 断点续传（Range）、更好的缓存语义（ETag/Cache-Control）
                   痛点: 一条连接同一时刻只能处理一个请求 -> 浏览器只能开 6 条并行连接;
                        队头阻塞（一个慢响应卡住后面所有请求）。
  HTTP/2   (2015)  仍然是 TCP, 但是二进制分帧:
                     * 多路复用: 一条连接上并发跑几百个请求（靠 stream id 区分）
                     * 头部压缩（HPACK）: 重复的头不用反复传
                     * 服务端推送（后来被弃用）
                   痛点: TCP 层依然有队头阻塞（丢一个包, 所有流一起等重传）
  HTTP/3   (2022)  直接换掉 TCP, 跑在 QUIC/UDP 上:
                     * 每个流独立, 丢包只影响自己（真·解决队头阻塞）
                     * 0-RTT 建连（第二次访问可省掉握手）
                     * 连接迁移: 手机从 Wi-Fi 切到 5G, 连接不断（靠 connection id 而不是四元组）
                   你每天都在用（Chrome 访问支持 H3 的站点时自动升级）。

  这条演进路线的启示:
      协议不是圣经, 是工程折中。每一代都是「在当时的物理约束下能接受的最优解」。
      HTTP/3 用不可靠的 UDP, 却在应用层实现了可靠性 —— 说明「可靠」不是 TCP 的专利,
      而是你在哪一层实现它的选择（回到第 8 课的结论）。
""")

    server.close()

    title("本课小结")
    print("""1. HTTP 报文 = 起始行 + 头 + 空行 + body, 全是可读文本（HTTP/2 之前）。
2. 响应体长度的三种界定方式: Content-Length / chunked / 连接关闭。这是协议的核心工程问题。
3. 头结束靠 \\r\\n\\r\\n, body 结束靠长度或 EOF —— 都是第 12 课那三套方案的翻版。
4. keep-alive 用连接复用换延迟, 代价是服务端要管理空闲连接。
5. 状态码首位决定故障方向; Host 头让一台机器托管无数站点。
6. HTTP/2 解决连接内并发, HTTP/3 换 UDP 解决 TCP 层队头阻塞。
7. 下一课: 自己动手写一个 HTTP 服务器（能跑 curl 的那种）。
""")
    print("下一课: 05_http/demo_16_mini_http_server.py")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_17_http_client_libs.py -- 第 17 课：客户端三件套与它们的坑
==============================================================

【为什么要专门讲客户端】
    服务端要你写对一次; 客户端你要用一辈子。而客户端有四个永恒的问题:
      1. 超时     : 不设超时的 HTTP 请求会让整个服务卡死（最常见的事故）
      2. 连接复用 : 每次请求都新建 TCP 连接, 延迟高、端口耗尽（第 13 课的 TIME_WAIT）
      3. 重定向   : 跟不跟? 跟几次? 跟了会不会把 Authorization 头泄漏到别的域?
      4. 错误处理 : HTTP 4xx/5xx 不是异常（在某些库里是, 在某些库里不是）

【四个层次的选择】
    socket           手写报文, 只用来理解原理（第 15 课）
    http.client      标准库低层: 完全控制头/连接/流式读取
    urllib.request   标准库高层: 自动重定向、cookie、代理
    requests         第三方事实标准: API 最舒服（本课会检测是否安装）

【本课路线】
    1. 起一个本地「功能齐全」的测试服务器（复用第 16 课的 HttpServer）
    2. http.client: 连接复用、流式读取、超时
    3. urllib.request: 重定向、自定义头、错误码、cookie
    4. requests 风格: 会演示连接池 / timeout 元组 / raise_for_status / 流式下载
    5. 生产实践清单（重试、超时预算、DNS、连接池大小、观测）
    6. 真实网络小实验: 观察 http://github.com 的 301 跳转链

运行：python3 demo_17_http_client_libs.py
"""

from __future__ import annotations

import gzip
import http.client
import http.cookiejar
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:                                    # 复用第 16 课写的服务器, 顺便说明「组件化」的价值
    from demo_16_mini_http_server import HttpServer, Request, Response
    HAVE_MINI_SERVER = True
except ImportError as exc:              # pragma: no cover
    print(f"  [提示] 无法导入 demo_16 的 HttpServer（{exc}）, 本课将跳过本地实验。")
    HAVE_MINI_SERVER = False


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def build_server() -> tuple[HttpServer, str]:
    """构造一个带各种「坑位」的测试服务器。"""
    server = HttpServer()

    @server.route("GET", "/json")
    def json_route(request: Request) -> Response:
        payload = json.dumps({"ok": True, "path": request.path, "query": request.query},
                             ensure_ascii=False).encode()
        return Response(200, headers={"Content-Type": "application/json; charset=utf-8"}, body=payload)

    @server.route("GET", "/redirect")
    def redirect_route(request: Request) -> Response:
        return Response(302, headers={"Location": "/json"}, body=b"")

    @server.route("GET", "/slow")
    def slow_route(request: Request) -> Response:
        time.sleep(1.0)                      # 故意慢 1 秒, 用来演示超时
        return Response(200, body=b"slow-response\n")

    @server.route("GET", "/big")
    def big_route(request: Request) -> Response:
        return Response(200, headers={"Content-Type": "application/octet-stream"},
                        body=b"0123456789" * 200_000)      # 2 MB

    @server.route("GET", "/gzip")
    def gzip_route(request: Request) -> Response:
        raw = ("压缩内容 " * 2000).encode("utf-8")
        if "gzip" in request.headers.get("accept-encoding", ""):
            return Response(200, headers={"Content-Type": "text/plain; charset=utf-8",
                                          "Content-Encoding": "gzip"},
                            body=gzip.compress(raw))
        return Response(200, headers={"Content-Type": "text/plain; charset=utf-8"}, body=raw)

    @server.route("POST", "/api")
    def api_route(request: Request) -> Response:
        try:
            data = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response(400, body=b"bad json\n")
        return Response(200, headers={"Content-Type": "application/json"},
                        body=json.dumps({"echo": data, "len": len(request.body)}).encode())

    @server.route("GET", "/cookie")
    def cookie_route(request: Request) -> Response:
        seen = request.headers.get("cookie", "(没有 cookie)")
        return Response(200, headers={"Set-Cookie": "session=abc123; Path=/"},
                        body=f"server saw: {seen}\n".encode())

    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.address
    return server, f"http://{host}:{port}"


def main() -> None:
    if not HAVE_MINI_SERVER:
        return
    server, base = build_server()
    host, port = server.address
    print(f"  本地测试服务器: {base}")

    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：http.client —— 标准库低层，控制力最强")

    conn = http.client.HTTPConnection(host, port, timeout=3)
    conn.request("GET", "/json")
    resp = conn.getresponse()
    print(f"  状态: {resp.status} {resp.reason}")
    print(f"  版本: {resp.version}（11 = HTTP/1.1）")
    print(f"  头: Content-Type={resp.getheader('Content-Type')}")
    body = resp.read()                      # 必须读掉 body, 否则连接不能复用
    print(f"  body: {body.decode()}")

    # 复用同一条连接再发一次（响应必须读干净）
    conn.request("GET", "/json?x=1")
    resp2 = conn.getresponse()
    print(f"  第二次请求复用连接: {resp2.status}, 服务端看到的路径 {json.loads(resp2.read())['query']}")
    conn.close()

    print("""
  http.client 的要点:
    * 它是「一条连接」的抽象, 所以天然支持 keep-alive（第 15 课说过）
    * resp.read() 必须消费掉, 否则下个请求会读到上一个响应的残留 -> 典型 bug
    * 想流式处理就用 resp.read(n) 循环, 或者 resp.readline()
    * POST 时要自己设 Content-Length（http.client 会算, 但 body 是文件时要注意）
""")

    # 流式下载: 不要一次 read() 到内存
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/big")
    resp = conn.getresponse()
    total = int(resp.getheader("Content-Length") or 0)
    read_bytes = 0
    chunks = 0
    started = time.perf_counter()
    while True:
        chunk = resp.read(64 * 1024)         # 64KB 一块, 内存占用恒定
        if not chunk:
            break
        read_bytes += len(chunk)
        chunks += 1
    elapsed = time.perf_counter() - started
    conn.close()
    print(f"  流式下载 2MB: 收到 {read_bytes} 字节/{chunks} 块, 耗时 {elapsed * 1000:.0f} ms")
    print(f"    Content-Length 声明 {total}, 实收 {read_bytes} -> 一致: {total == read_bytes}")
    print("""    生产代码必须这样写: 一次 read() 一个大文件会直接把内存吃满（OOM 的常见来源）。
    注意「Content-Length 声明值」和「实收字节数」一定要核对, 这是防截断/防中间人篡改的基本动作。
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：超时 —— 不设超时是最常见的生产事故")

    print("""  http.client / urllib 的默认超时是 socket 的全局默认（通常是「永不超时」）。
  下面连一个「不会回应」的地址, 用 1 秒超时演示正确姿势:
""")
    started = time.perf_counter()
    try:
        # 192.0.2.0/24 是保留的测试网段, 不会有人应答
        bad = http.client.HTTPConnection("192.0.2.1", 80, timeout=1.0)
        bad.request("GET", "/")
        bad.getresponse()
    except (socket.timeout, OSError) as exc:
        print(f"  1 秒超时生效: {type(exc).__name__}: {exc}（等了 {time.perf_counter() - started:.2f}s）")

    # 服务端故意慢 1 秒, 客户端设 0.3 秒 -> 读超时
    started = time.perf_counter()
    conn = http.client.HTTPConnection(host, port, timeout=0.3)
    try:
        conn.request("GET", "/slow")
        conn.getresponse()
    except (socket.timeout, OSError) as exc:
        print(f"  读超时生效: {type(exc).__name__}（等了 {time.perf_counter() - started:.2f}s, "
              f"服务端要 1 秒才回）")
    finally:
        conn.close()

    print("""
  超时的三个层次（都要设, 而且要遵守「超时预算」）:
    连接超时 connect timeout: 建立 TCP 连接最多等多久（1~3 秒, 第 10 课讲过 SYN 重传）
    读超时     read timeout   : 连上之后等响应最多多久（按业务 P99 定, 别拍脑袋）
    总超时     total timeout  : 整个请求（含重试）最多多久 —— 这一层最容易被忽略,
                结果就是「上游超时 3 秒, 我重试 3 次, 用户等了 12 秒才看到错误」
  铁律: 下游超时 < 上游超时。你的服务给上游的承诺是 1 秒,
        那么调用别人的每一项都要在 1 秒内结束（包含重试）。
""")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：urllib.request —— 标准库高层，自动重定向 + cookie")

    # GET 带自定义头
    req = urllib.request.Request(f"{base}/json", headers={"X-Course": "networking", "User-Agent": "demo_17/1.0"})
    with urllib.request.urlopen(req, timeout=3) as resp:
        print(f"  GET /json -> {resp.status}, body={resp.read().decode()}")

    # 自动跟随重定向（urllib 默认跟随 301/302/303/307）
    with urllib.request.urlopen(f"{base}/redirect", timeout=3) as resp:
        final_url = resp.geturl()
        print(f"  GET /redirect -> 最终 URL = {final_url}, 状态 {resp.status}")
        print("""
    urllib 默认会跟随重定向。注意它的语义:
      * 302/303 -> 后续请求会变成 GET（丢掉原方法和 body!）这是规范要求
      * 307/308 -> 保持方法和 body（需要显式支持）
      * 跨域重定向时, 不要把 Authorization 头带过去（curl 和很多库都会剥离,
        但自己手写时必须显式处理, 否则就是把凭据送给了攻击者的域名 -> 账号被盗的经典路径）
""")

    # POST JSON
    payload = json.dumps({"name": "张三", "n": 3}).encode("utf-8")
    req = urllib.request.Request(f"{base}/api", data=payload, method="POST",
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=3) as resp:
        print(f"  POST /api -> {resp.status}, 返回 {resp.read().decode()}")
    print("    注意 Content-Length 是按字节算的（中文 3 字节/字）, 第 15 课踩过这个坑。")

    # 错误码: urllib 对 4xx/5xx 抛 HTTPError
    try:
        urllib.request.urlopen(f"{base}/nope", timeout=3)
    except urllib.error.HTTPError as exc:
        print(f"  GET /nope -> HTTPError {exc.code}（urllib 把 4xx/5xx 当异常抛, 这是它的设计选择）")
    except urllib.error.URLError as exc:
        print(f"  GET /nope -> URLError {exc}")

    # cookie: urlopen 不带 cookie 会话, 要用 opener
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    with opener.open(f"{base}/cookie", timeout=3) as resp:
        print(f"  第一次 /cookie -> {resp.read().decode().strip()}  （服务端下发了 cookie）")
    with opener.open(f"{base}/cookie", timeout=3) as resp:
        print(f"  第二次 /cookie -> {resp.read().decode().strip()}  （cookie 被自动带上）")
    print(f"    本地 cookie 罐: {[c.name + '=' + c.value for c in jar]}")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：gzip —— 一个「白送」的带宽优化（也有坑）")

    plain = urllib.request.Request(f"{base}/gzip")
    with urllib.request.urlopen(plain, timeout=3) as resp:
        body_plain = resp.read()
        print(f"  未请求压缩: 收到 {len(body_plain)} 字节, Content-Encoding={resp.getheader('Content-Encoding')}")

    gz = urllib.request.Request(f"{base}/gzip", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(gz, timeout=3) as resp:
        body_gz = resp.read()
        encoding = resp.getheader("Content-Encoding")
        print(f"  请求 gzip : 收到 {len(body_gz)} 字节, Content-Encoding={encoding}")
        if encoding == "gzip":
            print(f"    手动解压后 {len(gzip.decompress(body_gz))} 字节, 与原文一致: "
                  f"{gzip.decompress(body_gz) == body_plain}")
    print(f"""
  压缩率: {len(body_gz)} / {len(body_plain)} = {len(body_gz) / max(len(body_plain), 1):.1%}
    * urllib 不会自动解压, 你得自己处理 Content-Encoding（requests 会自动解）
    * 服务器必须先看 Accept-Encoding 再决定压不压（本示例就是这么做的）
    * 别压缩已压缩的格式（图片/视频/zip）, 白费 CPU 还变大; 也永远不要压缩敏感内容
      再配合「压缩后长度」做加密（BREACH 攻击的原理）
""")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：requests 风格的最佳实践")

    try:
        import requests                                     # type: ignore
        HAVE_REQUESTS = True
    except ImportError:
        HAVE_REQUESTS = False

    if HAVE_REQUESTS:
        import requests                                     # type: ignore
        print(f"  检测到 requests {requests.__version__}, 真跑一遍:")
        with requests.Session() as session:                  # Session = 连接池 + cookie 罐
            resp = session.get(f"{base}/json", timeout=(1, 2))     # (连接超时, 读超时)
            resp.raise_for_status()                                # 4xx/5xx 主动抛异常
            print(f"    session.get /json -> {resp.status_code}, json={resp.json()}")
            resp = session.post(f"{base}/api", json={"hello": "世界"}, timeout=2)
            print(f"    session.post /api -> {resp.status_code}, {resp.json()}")
            print(f"    同一个 Session 复用连接: 底层走了连接池（默认 10 个连接）")
    else:
        print("  未安装 requests（pip install requests 可体验）, 下面用 urllib 演示等价代码:")
    print("""    # requests 的等价写法注释版（本课重点在这里, 而不是库的 API 细节）

    import requests
    session = requests.Session()                 # ① 复用连接池, 别每次 requests.get 新建

    resp = session.get(
        url,
        timeout=(3.05, 10),                      # ② (连接超时, 读超时), 元组比单个数字安全
        headers={"User-Agent": "my-service/1.0"},
        params={"q": "x"},                       # 自动 urlencode
        allow_redirects=True,                    # ③ 想禁止重定向就设 False（防 SSRF 常见手段）
        verify=True,                             # ④ 千万别为了"省事"设成 False
    )
    resp.raise_for_status()                      # ⑤ 4xx/5xx 一定要显式处理
    data = resp.json()

    # ⑥ 大文件用流式, 别让整个文件进内存
    with session.get(url, stream=True) as r:
        r.raise_for_status()
        with open("out.bin", "wb") as fh:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                fh.write(chunk)

    # ⑦ 重试: 只重试「幂等 + 可恢复」的错误, 且有上限和退避
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    session.mount("https://", HTTPAdapter(max_retries=Retry(
        total=3, backoff_factor=0.3,                      # 0.3, 0.6, 1.2 秒退避
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD", "PUT", "DELETE"],  # POST 默认不重试!（可能重复下单）
        respect_retry_after_header=True)))
""")

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：真实网络小实验（观察重定向链）")

    print("  用 urllib 请求 http://github.com（会 301 跳到 https）:")
    try:
        with urllib.request.urlopen("http://github.com", timeout=6) as resp:
            print(f"    {resp.status}  最终地址 {resp.geturl()}")
            print(f"    Server 头: {resp.getheader('Server')}")
    except urllib.error.HTTPError as exc:
        print(f"    HTTPError {exc.code}, Location={exc.headers.get('Location')}")
    except (urllib.error.URLError, OSError) as exc:
        print(f"    网络不可用, 跳过: {exc}")

    print("""
  真实世界的 HTTP 客户端还要处理这些（每条都能写一篇文章）:
    1. DNS 解析慢/失败 -> 客户端要缓存与超时（第 2 课）
    2. 连接池大小 -> 太小排队, 太大把对端打爆; 要按下游容量配置
    3. 代理环境变量 HTTP_PROXY/NO_PROXY -> 容器里踩坑重灾区（localhost 别走代理）
    4. 幂等性与重试 -> 只有 GET/PUT/DELETE/HEAD 可安全重试, POST 要用幂等键
    5. 观测 -> 每个请求都要有 trace id、耗时直方图、状态码计数、重试次数
    6. 熔断与隔离 -> 下游挂了别把自己拖死（超时 + 熔断 + 舱壁）
    7. TLS -> 下一课专门讲
""")

    server.shutdown()

    title("本课小结")
    print("""1. socket < http.client < urllib < requests: 层次越高越省事, 但可控性越差。
2. 超时必须设, 而且要分「连接 / 读 / 总」三层, 并遵守「下游超时 < 上游超时」。
3. 连接复用（Session / 连接池）是性能的第一杠杆, 直连的代价是握手 + TIME_WAIT。
4. 4xx/5xx 一定要显式处理; 重定向要考虑方法和凭据是否要跟着走。
5. 大响应必须流式读取并核对 Content-Length; gzip 白送带宽但有安全边界。
6. 下一课: HTTPS, 亲手做一次 TLS 握手和证书校验, 看清「加密」到底加在哪一层。
""")
    print("下一课: 05_http/demo_18_https_tls.py")


if __name__ == "__main__":
    main()

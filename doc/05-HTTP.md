# 第 5 章：HTTP（第 15~17 课）

本章解决的问题：**「怎么把字节流组织成人能读、机器能解析的协议，并让双方对边界达成一致」**。
三课分别是：看清报文 → 自己写服务器 → 用好客户端库。

代码：`05_http/demo_15_http_raw.py`、`demo_16_mini_http_server.py`、`demo_17_http_client_libs.py`

---

## 第 15 课：HTTP 不过是一段格式化的文本

**一句话**：HTTP 报文 = 起始行 + 头 + 空行 + body；而它的核心工程问题是
**「body 到哪里结束」**，答案只有三种：`Content-Length` / `chunked` / 连接关闭。

### 【原理】报文形状

```
请求:  方法 SP 路径 SP 版本 CRLF  *(头 CRLF)  CRLF  [body]
响应:  版本 SP 状态码 SP 原因 CRLF  *(头 CRLF)  CRLF  [body]

GET /index.html HTTP/1.1\r\n        ← 请求行
Host: example.com\r\n               ← 头（大小写不敏感）
Accept: */*\r\n
\r\n                                ← 空行！头结束的标志（丢了它就解析不了）
```

**body 长度的三种界定方式**（和第 12 课的三套分帧方案一一对应）：

| 方式 | 对应第 12 课 | 典型场景 |
|---|---|---|
| `Content-Length: N` | 长度前缀 | 绝大多数响应 |
| `Transfer-Encoding: chunked` | 分隔符（十六进制块长 + CRLF，0 块结束） | 长度未知、流式生成、代理转发 |
| `Connection: close` | EOF | HTTP/1.0 的老办法，读到连接关闭 |

### 【实测】`05_http/demo_15_http_raw.py`

**① 手写请求发给真实站点**（不用任何 HTTP 库，只有 `socket`）：

```text
  要发的原始请求（注意每行都以 \r\n 结束, 最后是一个空行）:
      GET / HTTP/1.1
      Host: example.com
      User-Agent: python-net-course/1.0
      Accept: */*
      Connection: close
      <空行>
  服务器响应: 928 字节, 耗时 267 ms
    状态行: HTTP/1.1 200 OK
      date: Wed, 14 Oct 2026 07:48:11 GMT      <- 服务器时间（GMT）
      content-type: text/html; charset=utf-8      <- body 的媒体类型 + 字符集
      transfer-encoding: chunked
      connection: close      <- close 表示这条连接用完就关
      server: cloudflare      <- 服务器软件（信息泄露要小心）
      last-modified: Tue, 13 Oct 2026 22:05:48 GMT
      allow: GET, HEAD
      accept-ranges: bytes
      age: 0      <- 这份响应在中间缓存里待了多久
    body 长度: 589 字节
    注意: 这条响应没有 Content-Length（Transfer-Encoding: chunked）,
          所以 body 的边界是靠 chunked 分块标记确定的（见下面第 2 步的手写解码）。
```

**注意这里**：真实站点的响应经常**没有 `Content-Length`**（用了 chunked）。
如果你只按 `Content-Length` 解析，就会在真实环境里翻车 —— 这就是本课要逐条演示三种方式的原因。

**② 三种边界，同一个本地教学服务器上各跑一遍**（完全离线可复现）：

```text
  (a) GET /fixed —— 用 Content-Length 判断 body 结束
      状态行: HTTP/1.1 200 OK
      Content-Length = 17, 实际 body = 17 字节, 内容 = b'fixed-length-body'
      关键: 收到那么长的字节后, 后面即使还有数据也不属于这个响应 -> 可以开始下一个请求。

  (b) GET /chunked —— Transfer-Encoding: chunked
      手写解码分块体:
        块大小 8 (0x8) -> 内容 b'chunk-1;'  <- 每块后跟 CRLF
        块大小 8 (0x8) -> 内容 b'chunk-2;'  <- 每块后跟 CRLF
        块大小 7 (0x7) -> 内容 b'chunk-3'  <- 每块后跟 CRLF
        遇到 0 块 -> 传输结束（后面可能还有 trailer 头, 通常忽略）
      解码后的 body = b'chunk-1;chunk-2;chunk-3'

  (c) GET /close —— 没有长度信息, 读到 EOF 为止
      recv 返回 b'' -> 连接关闭, 说明 body 结束了（HTTP/1.0 的做法）
      body = b'body-ends-when-connection-closes'
```

**③ POST 的 Content-Length 是按字节算的**（中文的经典坑）：

```text
  (d) POST /echo —— 请求体也要长度
      发送: Content-Length: 13（按字节数, 不是字符数！）
      响应 body: b'echo:\xe4\xbd\xa0\xe5\xa5\xbd\xef\xbc\x8cHTTP'
```

「你好，HTTP」是 5 个字符，UTF-8 下 **13 字节**。用 `len(str)` 算就是 bug。

**④ keep-alive：一条 TCP 连接发两个请求**：

```text
      第一次响应 82 字节, 第二次响应 82 字节, 服务端看到的请求序列: ['GET /fixed HTTP/1.1', 'GET /fixed HTTP/1.1']
```

好处：省掉三次握手与慢启动；代价：服务端要为空闲连接保留资源，必须有 idle timeout。

**⑤ 302 重定向**：`HTTP/1.1 302 Found   Location: /fixed` —— 客户端要自己决定跟不跟。

### 【版本演进】每一代都在解决上一代的痛点

| 版本 | 关键改进 | 遗留痛点 |
|---|---|---|
| 1.0 (1996) | 头、状态码、Content-Type | 每个请求一条连接 |
| **1.1 (1997)** | keep-alive、Host 虚拟主机、chunked、Range | 一条连接同时只能一个请求（队头阻塞） |
| 2 (2015) | 二进制分帧、多路复用、HPACK 头压缩 | TCP 层队头阻塞（丢包拖累所有流） |
| 3 (2022) | QUIC/UDP、每流独立、0-RTT、连接迁移 | 部署复杂（UDP 常被限速/拦截） |

**这条演进线的启示**：协议不是圣经，是工程折中。HTTP/3 用「不可靠的 UDP」实现了可靠性 ——
回到第 8 课的结论：可靠不是 TCP 的专利，而是你选择在哪一层实现它。

### 【坑】

- 手写请求时漏掉最后的空行 → 服务端一直等（表现为挂住，不是报错）。
- 只看 `Content-Length`，遇到 chunked 就解析错位。
- 忘了 `Host` 头（HTTP/1.1 必需）→ 很多服务器直接 400。
- 把 `Connection: close` 当成「礼貌」到处加 → 白白丢掉连接复用带来的性能。

### 【练习】

把 `/fixed` 请求里的 `Connection: close` 去掉，连发两次请求，
观察两次响应都在同一条连接上（日志里客户端端口相同）。

---

## 第 16 课：用 socket 写一个能用的 HTTP 服务器

**一句话**：一个 HTTP 服务器 = 请求解析 + 路由 + 响应序列化 + 连接管理；
难点全部集中在「读到哪儿算一个请求」。

### 【代码要点】四块职责

```python
# 1) 解析：头用 \r\n\r\n 切，体用 Content-Length 读满
head, _, rest = buf.partition(b"\r\n\r\n")
length = int(headers.get("content-length", "0") or 0)
while len(rest) < length:              # 关键: 体可能还没到齐
    rest += conn.recv(4096)
body, buf = rest[:length], rest[length:]   # 多余的留给下一个请求（keep-alive！）

# 2) 响应序列化：永远给出 Content-Length
headers["Content-Length"] = str(len(body))

# 3) keep-alive：默认复用，遇到 Connection: close 才关
if not request.keep_alive():
    return

# 4) 安全：路径归一化后必须仍在根目录内
target = os.path.realpath(os.path.join(root, relative))
if not target.startswith(root + os.sep):
    return Response(403, body=b"403 Forbidden\n")
```

### 【实测】`05_http/demo_16_mini_http_server.py` 的 10 项自测

```text
    [PASS] GET /hello -> 200 且 body 正确   status=200
    [PASS] GET /nope -> 404   status=404
    [PASS] POST /hello -> 405（附带 Allow 头）   status=405 Allow=GET
    [PASS] POST /echo 回显 body（含 UTF-8 中文）   body=b'echo:\xe4\xb8\xad\xe6\x96\x87payload'
    [PASS] keep-alive: 两条请求复用同一条 TCP 连接   same_socket=True
    [PASS] HEAD 有 Content-Length 但没有 body   len=69 body=b''
    [PASS] 静态文件 GET 正常且 Content-Type 正确   ctype=text/plain; charset=utf-8
    [PASS] 路径穿越被挡住（403/404）   status=403
    [PASS] 非法请求行 -> 400   resp=b'HTTP/1.1 400 Bad Request\r\nContent-Type: '
    [PASS] 访问日志有记录   共 10 行
```

**访问日志**（服务器视角）：

```text
    127.0.0.1:61932 GET /hello -> 200 170B 0.4ms
    127.0.0.1:61933 GET /nope -> 404 164B 0.1ms
    127.0.0.1:61934 POST /hello -> 405 201B 0.0ms
    127.0.0.1:61935 POST /echo -> 200 153B 0.0ms
    127.0.0.1:61936 GET /hello -> 200 175B 0.0ms
    127.0.0.1:61936 GET /hello -> 200 175B 0.0ms      ← 同一个客户端端口 = keep-alive 复用
    127.0.0.1:61937 HEAD /sample.txt -> 200 187B 2.1ms
    127.0.0.1:61938 GET /sample.txt -> 200 251B 0.1ms
    127.0.0.1:61939 GET /../demo_16_mini_http_server.py -> 403 157B 0.0ms
    127.0.0.1:61940 ? -> 400 (请求行格式错误: 'THIS IS NOT HTTP') 0B 0.0ms
```

日志里的 `61936` 出现两次，就是 keep-alive 的铁证；最后一行的 400 说明非法请求被兜住了。

`HEAD` 那一条还藏着一个容易被忽略的规范要求：**HEAD 响应不能有 body，
但 `Content-Length` 必须和 GET 一致**（本课代码一开始就写错了，自测抓出来后修正 ——
这正是「自测的价值」）。

### 【为什么生产不要自己写】

| 缺口 | 真实世界要处理的事 |
|---|---|
| 并发模型 | 每连接一线程，1 万连接就 1 万线程（第 19 课对比另一种模型） |
| TLS | 证书轮换、会话恢复、ALPN |
| 协议 | HTTP/2、HTTP/3、Range、压缩、100-continue、trailer |
| 安全 | 慢速攻击（slowloris）、请求走私、畸形报文（CVE 高发区） |
| 运维 | 优雅重启、日志轮转、指标暴露 |

正确姿势：**用成熟实现（nginx/Caddy/Envoy + gunicorn/uvicorn），自己只写业务逻辑**。
但理解上面这些原理，才能在你遇到 502/504/连接复用 bug 时知道该看哪里。

### 【坑】

- 只 `recv` 一次就当作完整请求 → 大请求体被截断（本课用循环读满）。
- 响应不带 `Content-Length` → 客户端只能靠"等连接关闭"，keep-alive 直接失效。
- 不做路径归一化 → `GET /../../etc/passwd` 直接读到系统文件（本课自测里专门验证被 403 挡住）。
- 请求头无限增长不设上限 → 内存被打满（本课有 `MAX_HEADER_BYTES`）。

### 【练习】

给 `demo_16` 加一个 `GET /time` 返回当前时间，并用 `curl -i` 验证；
再把 `MAX_HEADER_BYTES` 改成 64，用超长头触发 400。

---

## 第 17 课：客户端三件套与它们的坑

**一句话**：客户端要做的四件事——设超时、复用连接、处理重定向、处理错误码——
比服务端更容易出错，因为它们是每天都在跑的代码。

### 【四层选择】

| 层次 | 优点 | 代价 |
|---|---|---|
| `socket` 手写 | 完全理解原理 | 什么都要自己写（第 15 课） |
| `http.client` | 标准库低层，控连接/流式读取 | 要自己管 `read()` 的消费 |
| `urllib.request` | 自动重定向、cookie、代理 | 对 4xx/5xx 抛异常，语义绕 |
| `requests` | API 最舒服，事实标准 | 第三方依赖 |

### 【实测】`05_http/demo_17_http_client_libs.py`

**① `http.client` 的连接复用与流式下载**：

```text
  [access] 127.0.0.1:61942 GET /json -> 200 189B 0.2ms
  [access] 127.0.0.1:61942 GET /json -> 200 192B 0.0ms      ← 同一连接（端口相同）复用
  流式下载 2MB: 收到 2000000 字节/31 块, 耗时 1 ms
    Content-Length 声明 2000000, 实收 2000000 -> 一致: True
```

**要点**：`resp.read(64*1024)` 循环 → 内存占用恒定；
**并且一定要核对「声明长度 vs 实收长度」**，这是发现截断/篡改的基本动作。

**② 超时：三层都要设**：

```text
  1 秒超时生效: TimeoutError: timed out（等了 1.00s）
  读超时生效: TimeoutError（等了 0.30s, 服务端要 1 秒才回）
```

```
连接超时 connect timeout: 1~3 秒（对应第 10 课的内核重传）
读超时   read timeout   : 按业务 P99 定
总超时   total timeout  : 含重试！最容易被忽略
铁律: 下游超时 < 上游超时
```

顺带一个 Python 细节：3.10 之后 `socket.timeout` 就是 `TimeoutError` 的别名，
所以你会看到两种写法指向同一个异常。

**③ 重定向与 cookie**：

```text
  GET /redirect -> 最终 URL = http://127.0.0.1:61941/json, 状态 200
  GET /nope -> HTTPError 404（urllib 把 4xx/5xx 当异常抛, 这是它的设计选择）
  第一次 /cookie -> server saw: (没有 cookie)  （服务端下发了 cookie）
  第二次 /cookie -> server saw: session=abc123  （cookie 被自动带上）
```

**跨域重定向时要小心**：不要把 `Authorization` 带过去（这是凭据泄漏的经典路径）。

**④ gzip：白送 60 倍带宽，但有安全边界**：

```text
  未请求压缩: 收到 26000 字节, Content-Encoding=None
  请求 gzip : 收到 104 字节, Content-Encoding=gzip
  压缩率: 104 / 26000 = 0.4%
```

（这段是高度重复的文本，所以压缩率夸张；真实网页通常在 20%~30%。）
**注意**：不要压缩敏感内容再配合压缩长度暴露信息（BREACH 攻击的原理）。

**⑤ `requests` 风格的最佳实践**（即使你没装 requests，这段也是要背下来的）：

```python
session = requests.Session()                      # ① 复用连接池
resp = session.get(url, timeout=(3.05, 10))       # ② (连接超时, 读超时)
resp.raise_for_status()                           # ③ 4xx/5xx 主动抛
with session.get(url, stream=True) as r:          # ④ 大文件流式，别进内存
    for chunk in r.iter_content(64 * 1024):
        fh.write(chunk)
session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=0.3,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "HEAD", "PUT", "DELETE"])))   # ⑤ POST 默认不重试
```

**⑥ 真实世界的重定向链**：

```text
  用 urllib 请求 http://github.com（会 301 跳到 https）:
    200  最终地址 https://github.com/
```

（本段随网络波动：能联外网时如上；被墙或超时时脚本会打印
`网络不可用, 跳过: <urlopen error timed out>` 并继续往下跑（不会崩）。）

### 【坑】

- **不设超时**：这是生产事故第一名（线程/连接被永久占住）。
- 每次 `requests.get()` 都新建连接：QPS 上不去，还制造 TIME_WAIT（第 13 课）。
- 把 POST 也纳入自动重试：可能重复下单/重复扣款，要用幂等键。
- 忘了 `NO_PROXY`：容器里 `localhost` 走了代理，调试一小时才发现。

### 【练习】

把 demo_17 里的超时从 `timeout=3` 改成 `timeout=0.05`，观察大面积超时；
再把 `timeout=(1, 2)` 元组形式用上，理解连接/读超时的区别。

---

## 本章小结

| 结论 | 验证方式 |
|---|---|
| HTTP 报文是「起始行 + 头 + 空行 + body」 | 手写 socket 请求真实站点，打印原始字节 |
| body 边界有三种定法 | 同一个服务器上分别用 Content-Length / chunked / EOF 跑通 |
| 真实响应常常没有 Content-Length | example.com 的 589 字节 body 是 chunked 的 |
| `Content-Length` 按字节算 | 「你好，HTTP」→ 13 字节（不是 5） |
| keep-alive 靠同一连接发多个请求 | 响应头/访问日志里客户端端口相同 |
| 服务器难点在「读到哪算完」+ 安全 | 10 项自测（含路径穿越 403、非法请求 400、HEAD 语义） |
| 客户端必须设三层超时 | 连接超时 1.00s、读超时 0.30s 各自实测触发 |
| 流式读取要核对长度 | 2MB 分 31 块读，声明长度 == 实收长度 |
| gzip 能省大量带宽 | 26000 → 104 字节（0.4%，高度重复文本） |

下一章：[06-HTTPS与TLS.md](06-HTTPS与TLS.md) —— 把这一层套进加密隧道。

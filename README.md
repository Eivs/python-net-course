# Python 网络原理实践课：从 IP 地址到 HTTPS

用 19 个**可运行**的 Python 示例，把计算机网络通讯的原理从最底层一路演示到 HTTPS：

```
IP 地址 -> DNS -> 网卡/路由 -> 端口 -> UDP -> TCP 握手 -> 字节流/粘包
        -> 挥手/TIME_WAIT -> socket 选项 -> HTTP 报文 -> 自己写 HTTP 服务器
        -> HTTP 客户端库 -> HTTPS/TLS 证书 -> 综合项目(两种并发模型)
```

每一课都是**一个能直接跑出真实数据的脚本**（不是伪代码，不是示意图）：
DNS 报文是手写二进制拼出来的、丢包是在你自己机器上真的丢的、
TIME_WAIT/CLOSE_WAIT/206/304 都是从内核和真实响应里读出来的。

- **只用标准库**，不需要 `pip install` 任何东西
- 需要 Python **3.10+**：代码里用到 `X | None` 类型标注（配 `from __future__ import annotations`），
  未使用 `match`/`except*`/`type` 语句等 3.11+ 专有语法；**实测环境是 Python 3.13**
- 每课都会**自动降级**：没网、没 `dig`、没 `openssl`、没 `sysctl` 时不会崩，会跳过对应实验并说明
  （`DEGRADED=1 bash run_all.sh` 实测 19/19 通过）

---

## 快速开始

```bash
cd python-net-course

# 全部跑一遍（推荐第一次这么做，输出存到 .logs/ 下，逐个看）
bash run_all.sh

# 只跑某一章
bash run_all.sh 04_tcp

# 降级模式：把子进程的 PATH 指向空目录, 验证「没有 dig/openssl/sysctl/lsof 时」也能跑
DEGRADED=1 bash run_all.sh

# 单独跑某一课
python3 04_tcp/demo_12_tcp_sticky_packet.py
```

所有输出都会写到 `.logs/<脚本名>.log`，方便你边看代码边对照输出。

---

## 课程目录

| # | 文件 | 你会亲手看到的东西 | 需要公网 |
|---|------|-------------------|---------|
| 1 | `01_address_dns/demo_01_ip_address.py` | IP 就是 32 位整数、网络字节序、掩码/子网、回环与私网地址、UDP connect 探测本机 IP | 否 |
| 2 | `01_address_dns/demo_02_dns_lookup.py` | `getaddrinfo` 全部返回、一个域名多个 IP（CDN）、hosts 文件、缓存让第二次解析快 30 倍量级（实测 51.9ms → 1.6ms）、本机 DNS 服务器 | 是 |
| 3 | `01_address_dns/demo_03_local_interfaces.py` | 本机所有网卡、MAC、MTU 1500 怎么推出 MSS 1460、ARP 表 | 否 |
| 4 | `01_address_dns/demo_04_routing_table.py` | 真实路由表逐列解释、最长前缀匹配、源地址怎么选、`traceroute` 看到真实每一跳 | 是 |
| 5 | `02_port_socket/demo_05_port_basics.py` | 端口 16 位上限、端口 0 让内核分配、`0.0.0.0` vs `127.0.0.1` 谁连得进来、真实五元组、`netstat` 对照 | 否 |
| 6 | `02_port_socket/demo_06_port_observer.py` | 用 `lsof` 反查「谁占了我的端口」、TIME_WAIT/CLOSE_WAIT 状态分布、按对端 IP 聚合连接 | 否 |
| 7 | `02_port_socket/demo_07_port_scan.py` | 端口开/关/被丢弃三种结果、并发扫描本机 1-1024、banner 识别 | 否 |
| 8 | `03_udp/demo_08_udp_echo.py` | UDP 一次发送=一个数据报、**真实丢包 489/500**、UDP 的 connect 不握手、65507 上限与分片、ICMP 端口不可达 | 否 |
| 9 | `03_udp/demo_09_dns_query_packet.py` | **手写 DNS 报文**（hexdump 逐字节对照）、压缩指针解析、事务 ID、TTL | 是 |
| 10 | `04_tcp/demo_10_tcp_handshake.py` | LISTEN/SYN_SENT/ESTABLISHED 真实抓取、同一连接两个视角、连接拒绝 vs 超时、backlog 实测 1/6 成功 | 是(仅一角) |
| 11 | `04_tcp/demo_11_tcp_echo.py` | TCP 是字节流：1MB 被切成多少次 recv、`send` 只发一半、`sendall`、half-close、顺序保证 | 否 |
| 12 | `04_tcp/demo_12_tcp_sticky_packet.py` | **粘包实证**（3 次 send 1 次 recv 拿到 21 字节）、定长/分隔符/长度前缀三种方案、逐字节喂解码器测试 | 否 |
| 13 | `04_tcp/demo_13_tcp_teardown.py` | TIME_WAIT 数量增长、**复现 CLOSE_WAIT**、`SO_REUSEADDR` 救活服务重启、`SO_LINGER=0` 发 RST、写完关闭的连接报错 | 否 |
| 14 | `04_tcp/demo_14_tcp_socket_options.py` | MSS 实测、Nagle/`TCP_NODELAY` 实测、缓冲区与 32MB 吞吐、`SO_RCVTIMEO` 的坑、keepalive 默认 2 小时、`SO_ERROR` | 否 |
| 15 | `05_http/demo_15_http_raw.py` | 手写 GET 到真实站点看原始字节、Content-Length/chunked/关闭 三种边界、keep-alive 复用、302、版本演进 | 是 |
| 16 | `05_http/demo_16_mini_http_server.py` | 用 socket 写一个 HTTP 服务器（路由/405/HEAD/keep-alive/路径穿越防护）+ 10 项自测 | 否 |
| 17 | `05_http/demo_17_http_client_libs.py` | `http.client`/`urllib`/requests 对比、流式下载、三层超时、cookie、gzip、重定向陷阱 | 部分 |
| 18 | `05_http/demo_18_https_tls.py` | TLS 1.3 握手结果、证书链 4 张、**SNI 实验**、自签证书四种校验结果、ALPN、HTTPS 请求 | 是 |
| 19 | `06_project/mini_http_server.py` | 综合项目：Range(206)/缓存协商(304)/HEAD、**线程 vs 事件循环两种并发模型实测对比**、HTTPS | 否 |

辅助文件：

- **`doc/` — 教学文档（推荐从这里开始）**：把「原理 → 代码 → 实测 → 坑 → 练习」串成一条可读的线

  | 文档 | 对应课程 |
  |---|---|
  | [`doc/README.md`](doc/README.md) | 文档索引、阅读路线、环境说明 |
  | [`doc/01-地址与DNS.md`](doc/01-地址与DNS.md) | 第 1~4 课 |
  | [`doc/02-端口与socket.md`](doc/02-端口与socket.md) | 第 5~7 课 |
  | [`doc/03-UDP与二进制协议.md`](doc/03-UDP与二进制协议.md) | 第 8~9 课 |
  | [`doc/04-TCP.md`](doc/04-TCP.md) | 第 10~14 课（最重的一章） |
  | [`doc/05-HTTP.md`](doc/05-HTTP.md) | 第 15~17 课 |
  | [`doc/06-HTTPS与TLS.md`](doc/06-HTTPS与TLS.md) | 第 18 课 |
  | [`doc/07-综合项目与并发模型.md`](doc/07-综合项目与并发模型.md) | 第 19 课 |
  | [`doc/08-实战排障手册.md`](doc/08-实战排障手册.md) | 12 个症状→命令→实测案例 |
  | [`doc/09-进阶路线.md`](doc/09-进阶路线.md) | 自评表 + 十个方向 + 三个练手项目 |

- `NOTES.md` — 速查手册（分层模型、握手/挥手/TLS 时序图、状态机、端口表、排障决策树）
- `tools/check_lesson_chain.py` — 校验课程链（课号连续、每个「下一课」都指向第 N+1 课、doc 链接存在）
- `tools/check_doc_quotes.py` — 校验 `doc/` 里的实测引用都有 `.logs/` 出处
- `run_all.sh` — 顺序跑完所有示例、跑两个检查器、最后汇总（任何一项失败都会非 0 退出）
- `.logs/` — 运行输出（不纳入版本管理）

---

## 建议的学习方式

**先读 `doc/` 里对应的那一章，再跑代码对照。** 每一章的结构是
「一句话结论 → 原理 → 代码要点 → 本机实测输出 → 坑 → 动手练习」，
实测输出都直接取自 `.logs/`，方便你逐行对照自己跑出来的结果。

**不要只读代码**，按这三步走每一课：

1. **先看输出**：`cat .logs/demo_XX.log`，看真实数字（比如「收到 11/500 个包」）。
2. **再读注释**：每个脚本的 docstring 是「原理」，代码里 `# 第 N 步` 分段是「怎么验证原理」。
3. **改一个数字再跑**：这是最有效的部分。下面每课给一个「动手改一改」。

### 动手改一改（每课一个练习）

| 课 | 改什么 | 你会看到什么 |
|----|--------|-------------|
| 1 | 把 `demo_01` 里的网段换成你自己 `ifconfig` 看到的地址 | 广播地址、可用主机数跟着变 |
| 2 | 给 `demo_02` 传参数：`python3 demo_02_dns_lookup.py github.com` | 换域名观察 A 记录数量 |
| 3 | 找一张 MTU ≠ 1500 的网卡（如 utun 隧道） | MSS 变小，理解隧道为什么要限 MSS |
| 4 | 把 `traceroute` 目标换成 `8.8.8.8` | 路径完全不同（可能走 VPN） |
| 5 | 把 `bind(("127.0.0.1", 0))` 改成 `("0.0.0.0", 0)` | 从内网 IP 也能连上了 |
| 6 | 跑起来后另开终端 `lsof -nP -iTCP:<端口>` | 亲眼看内核和工具说的是同一件事 |
| 7 | 把 `TARGET` 改成自己家路由器的内网 IP（你有授权的） | 真实端口扫描结果 |
| 8 | 把 `SO_RCVBUF` 从 2048 改成 65536，或者 `total` 改成 50 | 丢包率剧烈变化 |
| 9 | 把 `qtype` 改成 28（AAAA） | 解析出 IPv6 地址 |
| 10 | 把 `listen(1)` 改成 `listen(64)` | 6 次连接全成功，理解 backlog |
| 11 | 把 1MB 改成 64MB，并把 `recv(4096)` 改成 `recv(1024)` | 观察 recv 次数和耗时 |
| 12 | 把 `LengthPrefixedDecoder` 的 `max_message_size` 改成 100 | 触发防内存攻击的保护 |
| 13 | 加 `--wait` 参数运行 | 亲眼看到 TIME_WAIT 从 25 条变 0（实测 t+30s 时全部消失，正好 2\*MSL） |
| 14 | 把 32MiB 改成 512MiB | 缓冲区大小对吞吐的影响被放大 |
| 15 | 把请求里的 `Connection: close` 去掉，连发两次请求 | 一条连接两个响应（keep-alive） |
| 16 | 加一个 `@server.route("GET", "/time")` 返回当前时间 | 体会路由注册的写法 |
| 17 | 把超时从 `timeout=3` 改成 `timeout=0.05` | 大量超时异常，理解超时预算 |
| 18 | 注释掉 `ctx.set_alpn_protocols(...)` | ALPN 协商结果变成 None |
| 19 | 改 `bench(..., threads=64)` | 两种模型的差距在高并发下拉开 |

---

## 环境与依赖

**必需**：Python 3.10+，仅标准库。

**可选**（有则用，无则自动跳过）：

| 工具 | 用在 | 用途 |
|------|------|------|
| `openssl` | 第 18/19 课 | 生成自签证书做 HTTPS 实验 |
| `dig` | 第 2 课 | 和手写 DNS 解析结果对照 |
| `lsof` / `netstat` | 第 5/6/10/13/14 课 | 观察内核里的真实端口与状态 |
| `traceroute` | 第 4 课 | 观察 TTL 逐跳 |
| `curl` | 第 16/19 课 | 手工验证自己写的 HTTP 服务器 |

**需要公网**的课（离线时不会崩，只是少几段真实数据）：2、4（仅 traceroute 部分）、9、15、17（部分）、18。

---

## 本套课的验证方式（为什么你可以相信里面的数字）

所有结论都来自**运行时的真实输出**，不存在「据说」「通常」。具体做了五件事：

0. **隐私底线**：示例文案与文档正文里**不出现跑课那台机器的私有信息**
   （内网 IP、MAC、主机名、用户名、家目录、hosts 条目、私有 DNS）——
   这些值在 `doc/` 里一律泛化/脱敏成通用示例（如 `192.168.1.10`、`00:1a:2b:3c:4d:5e`、`<用户名>`），
   并在引文块旁标注；`tools/check_no_local_data.py` 会拦住漏网的。
   运行时输出本身（`.logs/`）是你自己机器的，不入版本管理。

1. **全量回归**：`bash run_all.sh` 一次跑完全部 19 个示例 + 三个一致性检查器 → 全绿；
   每个脚本的完整输出都留在 `.logs/`。
2. **关键数字直接读系统**：MTU/MSS 来自 `getsockopt(TCP_MAXSEG)` 与 `ifconfig`；
   TIME_WAIT 时长来自 `sysctl net.inet.tcp.msl`；连接建立超时来自 `net.inet.tcp.keepinit`；
   临时端口范围来自 `net.inet.ip.portrange.*`；端口占用来自 `lsof`/`netstat`。
3. **可观察的现象真的被观察到**：UDP 丢包 489/500（缓冲区溢出）、粘包 3 次 send → 1 次 recv 拿到 21 字节、
   CLOSE_WAIT 复现、`listen(1)` 下 6 次连接只成功 1 次、TIME_WAIT 在 t+30s 归零、
   TLS 证书链 4 张、自签证书四种校验结果各不相同。
4. **降级路径也要测**：`DEGRADED=1 bash run_all.sh` 把子进程的 `PATH` 指向空目录
  （等于 dig/openssl/sysctl/lsof 全不存在），确认 19/19 仍然通过、只跳过实验而不报错崩溃。

---

## 安全声明

- 第 7 课的端口扫描**默认只扫 127.0.0.1**。扫描你不拥有的主机在多数司法辖区是违法行为，也会触发告警。改 `TARGET` 前请确认你有授权。
- 第 13/14 课的 `SO_LINGER=0`、RST 相关实验只在本机回环上做。
- 第 18/19 课使用**自签证书**，客户端校验是显式关闭的（仅用于演示）。生产代码里永远不要 `verify=False`。

---

## 这套课没讲的（但你应该知道它们存在）

- **HTTP/2、HTTP/3(QUIC) 的协议细节**：第 15 课讲了演进动机，没讲帧格式
- **防火墙/NAT 打洞/端口映射**：内网穿透、P2P 的核心
- **更深的拥塞控制**（CUBIC/BBR）、**多路径**（MPTCP）
- **IPv6 的邻居发现、SLAAC、双栈策略**
- **可编程数据面**（eBPF/XDP）、DPDK
- **协议测试工具**：`tcpdump`/`wireshark`（需要 root，本课用 netstat/lsof 替代）、`wrk`/`hey`

每一课的最后都有「还差什么」清单，指向这些方向。

# 第 3 章：UDP 与二进制协议（第 8~9 课）

本章解决的问题：**「不可靠的传输层协议有什么用，以及真实协议长什么样」**。
两课分别是：UDP 的脾气（无连接、不保证、保留边界）→ 用它手写一个 DNS 查询报文。

代码：`03_udp/demo_08_udp_echo.py`、`03_udp/demo_09_dns_query_packet.py`

---

## 第 8 课：UDP，无连接的最简传输

**一句话**：UDP 把数据打包直接扔出去，不握手、不确认、不重传，**但一次发送就是一个数据报，边界天然保留**。

### 【原理】TCP / UDP 的取舍

```
TCP  像打电话：先接通（三次握手），一句一句说，确认对方听清（ACK），
               丢了重说（重传），顺序乱了重排，说太快对方跟不上（流量控制）
UDP  像寄明信片：写地址扔进邮筒，到了就到了，丢了就丢了，可能后发的先到
```

| 特性 | TCP | UDP |
|---|---|---|
| 连接 | 有（三次握手） | 无 |
| 可靠性 | 不丢、有序、去重 | 可能丢、可能乱序、可能重复 |
| 消息边界 | **没有**（字节流） | **有**（一次 sendto = 一个数据报） |
| 头部开销 | 20~60 字节 | 8 字节 |
| 流量/拥塞控制 | 有 | 无 |
| 典型用途 | HTTP、SSH、数据库 | DNS、DHCP、NTP、QUIC、音视频、游戏 |

关键领悟：**「可靠」不是 TCP 的专利，而是你选择在哪一层实现它**。
HTTP/3（QUIC）就是跑在 UDP 之上、在应用层自己实现可靠性的例子。

### 【代码要点】UDP 服务端只有三步

```python
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # SOCK_DGRAM = 数据报 = UDP
sock.bind(("127.0.0.1", 0))                              # 占住端口（没有 listen！）
data, peer = sock.recvfrom(65535)                        # 一次收一个完整数据报
sock.sendto(b"echo:" + data, peer)                       # 回给来源地址
```

对比 TCP：`bind → listen → accept`，客户端 `connect`。UDP 没有 `listen/accept`，
因为**根本没有「连接」这个概念**。

### 【实测】`03_udp/demo_08_udp_echo.py`

**① 边界保留**（连续 `sendto` 三次，接收端 `recvfrom` 三次，一一对应）：

```text
  UDP 服务端已就绪: ('127.0.0.1', 64236)（注意: 它没有 LISTEN 状态, 因为没有连接）
  recvfrom -> b'echo:\xe6\xb6\x88\xe6\x81\xaf1'   <- 每次都是独立的一条, 不会像 TCP 那样粘在一起
  recvfrom -> b'echo:\xe6\xb6\x88\xe6\x81\xaf2'   <- 每次都是独立的一条, 不会像 TCP 那样粘在一起
  recvfrom -> b'echo:\xe6\xb6\x88\xe6\x81\xaf3'   <- 每次都是独立的一条, 不会像 TCP 那样粘在一起
```

（`\xe6\xb6\x88\xe6\x81\xaf` 就是「消息」的 UTF-8 编码。第 12 课会看到同样的三连发
在 TCP 上被一次 `recv` 全部吃掉 —— 这就是两种协议最直观的分界线。）

**② UDP 的 `connect()` 不握手**：

```text
  connect(('127.0.0.1', 64236)) 之后: getsockname()=('127.0.0.1', 59816) getpeername()=('127.0.0.1', 64236)
```

它只做三件事：记住默认对端（之后可以用 `send/recv`）、锁定源 IP 和源端口、只接收该对端的包。

**③ 亲眼看到丢包**（把接收缓冲缩到 2048 字节，一口气发 500 个 100 字节的包）：

```text
  SO_RCVBUF 设为 2048, 系统实际给出 2048（内核有最小值限制）
  发送 500 个数据报, 收到 11 个, 丢失 489 个
  丢失的序号(前 20 个): [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30]
```

**489/500 的丢失率，在本机回环上**。原因：UDP 没有流量控制，接收方读得慢，
内核缓冲区满了就**静默丢弃**，不通知任何人。这就是「UDP 不可靠」的全部真相。

**④ 数据报能有多大**：

```text
        载荷字节  结果
           1  发送成功
         512  发送成功
        1472  发送成功
        1500  发送成功
        8192  发送成功
       16384  失败: OSError: [Errno 40] Message too long
       65507  失败: OSError: [Errno 40] Message too long
       65508  失败: OSError: [Errno 40] Message too long
  二分实测: 本机回环上最大可发 UDP 载荷 = 9216 字节（9217 就报 Message too long）
```

三个层次的数字要分清：

| 数字 | 含义 |
|---|---|
| 65507 | IPv4 下 UDP 载荷的**理论上限**（65535 − 20 IP 头 − 8 UDP 头） |
| 1472 | 以太网 MTU 1500 下**不分片**的最大载荷（1500 − 20 − 8） |
| 9216 | **本机回环实测**能发的最大值（受回环 MTU 与内核缓冲区限制） |

工程建议：**载荷控制在 1200 字节以下**（IPv6 最小 MTU 1280 也安全），
否则会被 IP 分片，而任意一个分片丢失，整个数据报就废了。

**⑤ UDP 也能感知错误（ICMP port unreachable）**：

```text
      * 如果用 connect() 过的 UDP socket 再 send, 下一次收发会抛 ConnectionRefusedError
  收到 ConnectionRefusedError: [Errno 61] Connection refused
```

原理：目标主机活着但端口没人监听时，它的 IP 层会回一个 ICMP "port unreachable"，
内核把这条 ICMP 关联到你的 socket。**只有 `connect()` 过的 UDP socket 才能感知到** ——
这也是「UDP 客户端最好 connect 一下」的真实理由。

### 【坑】

- 把 UDP 当 TCP 用：以为自己发出去就一定到。要可靠就在应用层做：序号 + 确认 + 重发 + 去重（QUIC 的做法）。
- 忘了「接收缓冲区是有限的」：每秒收 10 万个小包但处理不过来 → 大量静默丢包。
- `recvfrom(n)` 的 `n` 太小会把数据报**截断**（剩余部分直接丢弃，不会分两次给你）。

### 【练习】

把 `SO_RCVBUF` 从 2048 改成 65536（或把发送数从 500 改成 50），观察丢包率如何变化。

---

## 第 9 课：亲手拼一个 DNS 报文（二进制协议入门）

**一句话**：真实世界的协议大量是二进制格式；DNS 报文结构简单，是理解「定长头 + 变长体」的最佳教材。

### 【原理】DNS 报文格式（RFC 1035，所有字段大端）

```
+---------------------+  12 字节定长头
| ID          (2 字节)|  事务 ID：把响应和请求配对（UDP 没有连接，只能靠它）
| Flags       (2 字节)|  QR/Opcode/AA/TC/RD/RA/RCODE
| QDCOUNT     (2 字节)|  问题数量（通常 1）
| ANCOUNT     (2 字节)|  回答数量
| NSCOUNT     (2 字节)|  权威记录数量
| ARCOUNT     (2 字节)|  附加记录数量
+---------------------+
| Question            |  域名（长度前缀的标签序列）+ QTYPE + QCLASS
| Answer ...          |  资源记录：NAME TYPE CLASS TTL RDLENGTH RDATA
+---------------------+
```

域名不用字符串表示，而是**长度前缀 + 标签**：

```
www.example.com  ->  \x03 www \x07 example \x03 com \x00
                     ↑长度  ↑3字节  ↑长度  ↑7字节  ↑长度 ↑根标签结束
```

响应里为了省空间还会用「压缩指针」（`0xC0` 开头指向前面出现过的域名），解析时必须处理。

### 【代码要点】

```python
# 编码域名
def encode_name(name):
    out = bytearray()
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        out.append(len(raw))          # 长度是一个字节
        out += raw
    out.append(0)                     # 根标签，域名结束
    return bytes(out)

# 12 字节头：ID, Flags, QDCOUNT, ANCOUNT, NSCOUNT, ARCOUNT
header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)   # 0x0100 = RD（希望递归）
question = encode_name(domain) + struct.pack("!HH", qtype, 1)   # QTYPE=A(1), QCLASS=IN(1)
```

解析域名时最关键的是处理压缩指针，并**防止恶意报文**造成的死循环：

```python
if length & 0xC0 == 0xC0:            # 前两位是 11 -> 压缩指针
    pointer = struct.unpack("!H", data[offset:offset+2])[0] & 0x3FFF
    if not jumped:
        next_offset = offset + 2     # 只记第一次的「真实后续位置」
    offset = pointer
    hops += 1
    if hops > 20:
        raise ValueError("指针跳转次数过多, 可能是恶意报文")
```

### 【实测】`03_udp/demo_09_dns_query_packet.py`

**域名编码**：

```text
    example.com                  -> b'\x07example\x03com\x00'   (13 字节)
    a.b.c.d                      -> b'\x01a\x01b\x01c\x01d\x00'   (9 字节)
    www.example.com              -> b'\x03www\x07example\x03com\x00'   (17 字节)
```

**完整查询报文（29 字节）+ 逐字节对照**：

```text
  构造出的完整查询报文（共 29 字节, 事务 ID=0xACD6）:
    0000  ac d6 01 00 00 01 00 00 00 00 00 00 07 65 78 61  .............exa
    0010  6d 70 6c 65 03 63 6f 6d 00 00 01 00 01           mple.com.....

  对着十六进制读一遍:
    0000: ac d6           <- ID = 0xACD6
    0002: 01 00           <- Flags = 0x0100 (RD=1 我要递归查询)
    0004: 00 01           <- QDCOUNT = 1（一个问题）
    0006-000b: 全是 00        <- AN/NS/ARCOUNT 都是 0（查询里没有记录）
    000c: 07              <- 第一个标签的长度, 后面紧跟 7 个字节的标签内容
    最后 4 字节: QTYPE=1 (A 记录)  QCLASS=1 (IN)
```

29 字节 = 12 头 + 13 域名 + 4（QTYPE+QCLASS）。**整个 DNS 查询就这么多东西。**

（事务 ID 是随机生成的，每次运行都不同；上面保留一次真实运行的输出。）

**响应解析（含压缩指针）**：

```text
  往返耗时 239.3 ms
    事务 ID    = 0xACD6  与请求相同? True
    RCODE      = 0  (0=NOERROR, 3=NXDOMAIN 域名不存在)
  Question : example.com  type=A class=IN
  Answer : example.com                  A      TTL=300    -> 172.66.147.243
  Answer : example.com                  A      TTL=300    -> 104.20.23.154
```

解析结果与 `dig` 的输出一致 —— 证明我们这 120 行代码真的读懂了报文。

### 【设计讨论】三个必须理解的细节

**① 为什么主用 UDP？** 一次问答就一个包，省掉三次握手（少一个 RTT）。
首屏要解析十几个域名，每个省 50ms 就是半秒。

**② 事务 ID 有什么用？** UDP 没有连接，响应可能来自伪造者（DNS 投毒/缓存污染）。
客户端必须校验 **源地址 + 事务 ID + 问题名**三者匹配。
配合内核随机化的源端口（第 5 课的临时端口），攻击者要猜中 32 位组合。

**③ 什么时候必须换成 TCP？**
- 响应里 `TC`（Truncated）标志为 1（超过 512 字节的传统限制）
- 区域传送（AXFR，主从服务器同步整个 zone）
- 现在还有 DoH（DNS over HTTPS/443）和 DoT（DNS over TLS/853），把明文 DNS 包进 TLS

### 【坑】

- 忘了解析压缩指针 → 遇到大响应直接解析错乱（解析到一半跑到别的字段上）。
- 不做事务 ID 校验 → 可能接受伪造响应（本课代码显式打印 `txid_match`）。
- 忘了「UDP 会丢包」→ 生产代码必须有超时重试（本课对多个 DNS 服务器依次重试）。
- 别在生产代码里手搓 DNS：用 `dnspython`。本课目的是**看清格式**，不是造轮子。

### 【练习】

把 `qtype` 改成 28（AAAA）重新运行，观察解析出 IPv6 地址；
或把域名改成 `a.b.c.d` 这种多级域名，看编码后的长度变化。

---

## 本章小结

| 结论 | 验证方式 |
|---|---|
| UDP 保留消息边界 | 连续 `sendto` 3 次 → `recvfrom` 3 次一一对应（对比第 12 课 TCP 的 21 字节粘包） |
| UDP 会静默丢包 | 缩缓冲到 2048 字节，500 发 11 收（489 丢失） |
| UDP 的 `connect()` 不握手 | `connect()` 返回后 `getpeername()` 有值，但链路上没有任何包 |
| 载荷有三个层次的限制 | 理论 65507 / 不分片 1472 / 本机实测 9216（二分找出） |
| UDP 也能收到错误反馈 | `connect` 过的 socket 收到 ICMP → `ConnectionRefusedError` |
| 二进制协议 = 定长头 + 变长体 | 手写 29 字节 DNS 查询，逐字节对照 hexdump |
| 域名是「长度前缀 + 标签」 | `www.example.com` → 17 字节的编码 |
| UDP 上做请求-响应要自带标识 | 解析响应时校验事务 ID，并打印是否匹配 |

下一章：[04-TCP.md](04-TCP.md) —— 全课程最重要的一章。

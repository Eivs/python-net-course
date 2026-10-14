#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_09_dns_query_packet.py -- 第 9 课：亲手拼一个 DNS 报文
===========================================================

【为什么要有这一课】
    前面八课用的都是「字符串协议」（HTTP 那种可以肉眼读的）。
    这一课要让你看到: 真实世界里大量协议是「二进制协议」,
    每个字节都有含义, 靠 struct 拼装和解析。

    DNS 报文就是最好的教材: 结构简单、但你平时全是用库在调,
    自己拼一次, 以后再看到「协议解析」就不会发怵。

【DNS 报文格式（RFC 1035）】
    所有字段都是大端（网络字节序）:

    +---------------------+  12 字节定长头
    | ID          (2 字节)|  事务 ID: 用来把响应和请求配对
    | Flags       (2 字节)|  QR/Opcode/AA/TC/RD/RA/RCODE 等标志位
    | QDCOUNT     (2 字节)|  问题数量（通常 1）
    | ANCOUNT     (2 字节)|  回答数量
    | NSCOUNT     (2 字节)|  权威记录数量
    | ARCOUNT     (2 字节)|  附加记录数量
    +---------------------+
    | Question            |  域名（长度前缀的标签序列）+ QTYPE + QCLASS
    | Answer ...          |  资源记录, 格式: NAME TYPE CLASS TTL RDLENGTH RDATA
    +---------------------+

    「域名」的编码方式很特别: 不是字符串, 而是
        www.example.com  ->  \x03www\x07example\x03com\x00
    每个标签前面加一个字节表示长度, 结尾用 0 字节。所以域名最长 253 字节。
    响应里为了省空间会用「压缩指针」(0xC0 开头), 指向报文前面出现过的域名,
    解析时必须处理, 否则遇到大响应就会解析错乱。

【本课路线】
    1. 手写 encode_name / build_query / parse_header / parse_name
    2. 真正发出去（UDP 53 端口）, 拿到响应
    3. 打印十六进制转储, 一个字节一个字节对照格式
    4. 解析出 A 记录的 IP 和 TTL
    5. 和 dig 的输出对照（如果本机有 dig）
    6. 讨论: 为什么 DNS 主用 UDP? 事务 ID 有什么用? 什么时候必须换 TCP?

运行：python3 demo_09_dns_query_packet.py [域名] [DNS服务器IP]
"""

from __future__ import annotations

import random
import shutil
import socket
import struct
import subprocess
import sys
import time

DEFAULT_DOMAIN = "example.com"
DEFAULT_RESOLVERS = ["8.8.8.8", "1.1.1.1", "223.5.5.5"]

QTYPE = {1: "A", 2: "NS", 5: "CNAME", 6: "SOA", 15: "MX", 16: "TXT", 28: "AAAA"}
QCLASS = {1: "IN"}

FLAG_BITS = [
    (0x8000, "QR(1=响应/0=查询)"),
    (0x0400, "AA(权威回答)"),
    (0x0200, "TC(被截断, 需要改用 TCP)"),
    (0x0100, "RD(希望递归)"),
    (0x0080, "RA(服务器支持递归)"),
]


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def hexdump(data: bytes, prefix: str = "    ") -> None:
    """经典 hexdump: 偏移 + 十六进制 + ASCII。看协议必用的工具。"""
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{offset:04x}  {hex_part:<47}  {ascii_part}")


# ---------------------------------------------------------------- 编码部分
def encode_name(name: str) -> bytes:
    """把域名编码成 DNS 线上格式: 长度前缀 + 标签, 末尾 0x00。

    "www.example.com" -> b"\\x03www\\x07example\\x03com\\x00"
    """
    out = bytearray()
    for label in name.rstrip(".").split("."):
        raw = label.encode("ascii")
        if not 1 <= len(raw) <= 63:
            raise ValueError(f"标签长度必须在 1~63 之间: {label!r}")
        out.append(len(raw))          # 关键: 长度是「一个字节」
        out += raw
    out.append(0)                     # 根标签, 表示域名结束
    return bytes(out)


def build_query(domain: str, qtype: int = 1, txid: int | None = None) -> tuple[bytes, int]:
    """拼一个完整的 DNS 查询报文, 返回 (报文字节, 事务ID)。"""
    if txid is None:
        txid = random.randint(0, 0xFFFF)          # 随机事务 ID, 见本课后半段的讨论

    # 12 字节头: ID, Flags, QDCOUNT, ANCOUNT, NSCOUNT, ARCOUNT（全部大端 !HHHHHH）
    # Flags = 0x0100 表示: QR=0(查询) Opcode=0(标准查询) RD=1(希望递归)
    header = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    question = encode_name(domain) + struct.pack("!HH", qtype, 1)   # QTYPE, QCLASS=1(IN)
    return header + question, txid


# ---------------------------------------------------------------- 解析部分
def parse_name(data: bytes, offset: int) -> tuple[str, int]:
    """解析域名, 处理压缩指针。

    返回 (域名, 下一个待读位置)。指针只影响"我在报文里跳到哪儿读",
    不影响"调用者接下来该从哪儿继续"。
    """
    labels: list[str] = []
    next_offset = offset
    jumped = False
    hops = 0

    while True:
        if offset >= len(data):
            raise ValueError("报文在解析域名时越界")
        length = data[offset]

        if length & 0xC0 == 0xC0:                    # 前两位是 11 -> 压缩指针
            if offset + 1 >= len(data):
                raise ValueError("指针不完整")
            pointer = struct.unpack("!H", data[offset:offset + 2])[0] & 0x3FFF
            if not jumped:
                next_offset = offset + 2             # 只记第一次的"真实后续位置"
            offset = pointer
            jumped = True
            hops += 1
            if hops > 20:                            # 防止恶意报文造成死循环
                raise ValueError("指针跳转次数过多, 可能是恶意报文")
            continue

        if length == 0:                              # 根标签 -> 域名结束
            if not jumped:
                next_offset = offset + 1
            break

        offset += 1
        labels.append(data[offset:offset + length].decode("ascii", "replace"))
        offset += length

    return ".".join(labels), next_offset


def parse_response(data: bytes, expect_txid: int) -> dict:
    """解析 DNS 响应, 抽出头部信息和各处记录。"""
    if len(data) < 12:
        raise ValueError("响应太短, 不是合法 DNS 报文")

    txid, flags, qd, an, ns, ar = struct.unpack("!HHHHHH", data[:12])
    result = {
        "txid": txid,
        "txid_match": txid == expect_txid,
        "flags": flags,
        "qr": (flags >> 15) & 1,
        "rcode": flags & 0x000F,
        "tc": (flags >> 9) & 1,
        "ra": (flags >> 7) & 1,
        "qdcount": qd, "ancount": an, "nscount": ns, "arcount": ar,
        "questions": [], "answers": [], "authorities": [], "additionals": [],
    }

    offset = 12
    for _ in range(qd):
        name, offset = parse_name(data, offset)
        qtype, qclass = struct.unpack("!HH", data[offset:offset + 4])
        offset += 4
        result["questions"].append((name, qtype, qclass))

    for section, count in (("answers", an), ("authorities", ns), ("additionals", ar)):
        for _ in range(count):
            name, offset = parse_name(data, offset)
            rtype, rclass, ttl, rdlength = struct.unpack("!HHIH", data[offset:offset + 10])
            offset += 10
            rdata = data[offset:offset + rdlength]
            offset += rdlength

            value: str
            if rtype == 1 and rdlength == 4:                  # A 记录
                value = socket.inet_ntoa(rdata)
            elif rtype == 28 and rdlength == 16:              # AAAA 记录
                value = socket.inet_ntop(socket.AF_INET6, rdata)
            elif rtype in (2, 5):                             # NS / CNAME: rdata 里是域名
                value, _ = parse_name(data, offset - rdlength)
            else:
                value = rdata.hex()
            result[section].append({"name": name, "type": rtype, "ttl": ttl, "value": value})
    return result


# ---------------------------------------------------------------- 发送部分
def system_resolvers() -> list[str]:
    """macOS: scutil --dns；Linux: /etc/resolv.conf。"""
    servers: list[str] = []
    try:
        out = subprocess.run(["scutil", "--dns"], capture_output=True, text=True,
                             errors="replace", timeout=5).stdout
        for line in out.splitlines():
            if "nameserver" in line:
                ip = line.split(":")[-1].strip()
                if ip and ":" not in ip and ip not in servers:
                    servers.append(ip)
    except (OSError, subprocess.SubprocessError):
        pass
    if not servers:
        try:
            with open("/etc/resolv.conf", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("nameserver"):
                        servers.append(line.split()[1])
        except OSError:
            pass
    return servers


def query_over_udp(server: str, packet: bytes, txid: int, timeout: float = 3.0) -> bytes:
    """发出去并等响应。这里自己做超时重试, 因为 UDP 不保证送达。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)     # UDP!
    sock.settimeout(timeout)
    try:
        start = time.perf_counter()
        sock.sendto(packet, (server, 53))
        data, _ = sock.recvfrom(4096)                          # 512 是传统上限, 4096 是常见做法
        print(f"  往返耗时 {(time.perf_counter() - start) * 1000:.1f} ms")
        return data
    finally:
        sock.close()


def main() -> None:
    domain = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DOMAIN
    candidates = [sys.argv[2]] if len(sys.argv) > 2 else DEFAULT_RESOLVERS + system_resolvers()

    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：把域名编码成 DNS 线上格式")

    print("  域名编码（长度前缀 + 标签, 末尾 0x00）:")
    for d in [domain, "a.b.c.d", "www." + domain]:
        encoded = encode_name(d)
        print(f"    {d:<28} -> {encoded!r}   ({len(encoded)} 字节)")

    packet, txid = build_query(domain, qtype=1)
    print(f"\n  构造出的完整查询报文（共 {len(packet)} 字节, 事务 ID=0x{txid:04X}）:")
    hexdump(packet)

    print(f"""
  对着十六进制读一遍:
    0000: {packet[0]:02x} {packet[1]:02x}           <- ID = 0x{txid:04X}
    0002: {packet[2]:02x} {packet[3]:02x}           <- Flags = 0x{struct.unpack('!H', packet[2:4])[0]:04X} (RD=1 我要递归查询)
    0004: {packet[4]:02x} {packet[5]:02x}           <- QDCOUNT = 1（一个问题）
    0006-000b: 全是 00        <- AN/NS/ARCOUNT 都是 0（查询里没有记录）
    000c: {packet[12]:02x}              <- 第一个标签的长度, 后面紧跟 {packet[12]} 个字节的标签内容
    最后 4 字节: QTYPE={struct.unpack('!H', packet[-4:-2])[0]} (A 记录)  QCLASS={struct.unpack('!H', packet[-2:])[0]} (IN)
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：真的发出去（UDP 到 53 端口）")

    response = None
    used_server = None
    for server in candidates:
        print(f"  尝试 DNS 服务器 {server} ...")
        try:
            response = query_over_udp(server, packet, txid)
            used_server = server
            break
        except socket.timeout:
            print("    超时（UDP 丢包或被墙, 换一个再试）")
        except OSError as exc:
            print(f"    失败: {type(exc).__name__}: {exc}")
    if response is None:
        print("\n  所有 DNS 服务器都不可用。本课剩余的解析演示无法进行, 但代码逻辑仍可直接读。")
        return

    print(f"  收到来自 {used_server} 的响应, 共 {len(response)} 字节")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：响应报文逐字节对照")

    hexdump(response)

    parsed = parse_response(response, txid)
    print(f"""
  解析出的头部:
    事务 ID    = 0x{parsed['txid']:04X}  与请求相同? {parsed['txid_match']}
                ^^^ 这就是 DNS 用 UDP 还能工作的关键: 靠 ID 配对, 而不是靠连接
    QR         = {parsed['qr']}  (1 表示这是响应)
    RA         = {parsed['ra']}  (1 表示对方支持递归查询)
    TC         = {parsed['tc']}  (1 表示响应被截断, 必须改用 TCP 重问)
    RCODE      = {parsed['rcode']}  (0=NOERROR, 3=NXDOMAIN 域名不存在)
    问题数/回答数/权威数/附加数 = {parsed['qdcount']}/{parsed['ancount']}/{parsed['nscount']}/{parsed['arcount']}
""")

    for bit, desc in FLAG_BITS:
        print(f"    flags & {bit:#06x} -> {bool(parsed['flags'] & bit)!s:<5} {desc}")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：解析记录（A 记录的 IP 和 TTL）")

    for name, qtype, qclass in parsed["questions"]:
        print(f"  Question : {name}  type={QTYPE.get(qtype, qtype)} class={QCLASS.get(qclass, qclass)}")

    for section_name, key in (("Answer ", "answers"), ("Authority", "authorities"), ("Additional", "additionals")):
        for rec in parsed[key]:
            type_name = QTYPE.get(rec["type"], str(rec["type"]))
            print(f"  {section_name}: {rec['name']:<28} {type_name:<6} TTL={rec['ttl']:<6} -> {rec['value']}")

    if parsed["answers"]:
        ttl = min(r["ttl"] for r in parsed["answers"])
        print(f"""
  TTL 是「这条记录还能缓存多少秒」。{domain} 的答案是 {ttl} 秒,
  说明全世界所有解析器在 {ttl} 秒内都直接用缓存, 不会再问权威服务器。
  DNS 的扩展性就建立在这个缓存上: 改域名解析要等 TTL 过期才能全网生效,
  所以「上线前把 TTL 调小」是运维的常规操作。
""")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：和 dig 对照（同一个问题, 官方工具的回答）")

    if shutil.which("dig"):
        out = subprocess.run(
            ["dig", f"@{used_server}", "+noall", "+answer", "+comments", domain, "A"],
            capture_output=True, text=True, errors="replace", timeout=10).stdout
        for line in out.strip().splitlines()[:10]:
            print(f"  {line}")
        print("""
  你会发现 dig 给出的 A 记录和我们手写解析的结果一致。
  差别只是: dig 是别人写好的、经过 30 年打磨的解析器, 而我们只有 120 行。
  真实项目里请用 dnspython 之类的库, 本课的目的是「看清格式」而不是「造轮子」。
""")
    else:
        print("  本机没有 dig，跳过对照。")

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：三个值得记住的设计细节")

    print("""  1) 为什么主用 UDP?
     一次问答就一个包, 省掉三次握手 = 少一个 RTT。网页首屏要解析十几个域名,
     每个省 50ms 就是半秒。而且 DNS 查询天然是「一问一答」的独立事务, 不需要连接状态。

  2) 事务 ID 有什么用?
     UDP 没有连接, 所以响应可能来自伪造者（DNS 投毒/缓存污染）。
     客户端必须检查: 源地址对不对 + 事务 ID 匹配不匹配 + 问题名匹配不匹配。
     这也是为什么系统随机化了源端口（第 5 课的临时端口）+ 16 位随机 ID,
     让攻击者要猜中 32 位组合。
     本课代码里特意做了 txid_match 检查, 你在生产代码里也必须做。

  3) 什么时候必须用 TCP？
     * 响应里 TC 标志为 1（超过 512 字节, 传统限制）
     * 域名很长/记录很多, 或者用 EDNS0 声明了大缓冲区
     * 区域传送（AXFR, 主从服务器同步整个 zone）
     此时必须换成 TCP 53 重问一遍。所以「DNS 只用 UDP」是错的,
     TCP 也是 DNS 的必备组成部分。

  顺便一提: 近几年最热的方向是 DoH (DNS over HTTPS, 443) 和 DoT (DNS over TLS, 853),
  把明文 DNS 包进 TLS, 防止中间人偷看/篡改你在解析什么域名。
""")

    title("本课小结")
    print("""1. 二进制协议 = 定长头 + 变长体, 全靠 struct 和偏移量解析。
2. DNS 域名用「长度前缀 + 0 结尾」编码, 响应里还有压缩指针。
3. UDP 上做请求-响应, 必须自己维护「事务 ID + 超时重试 + 匹配校验」。
4. TTL 决定了改解析要多久生效, 也决定了 DNS 的整体扩展性。
5. DNS 主用 UDP 是为省一个 RTT, 但 TCP 53 是它不可分割的另一半。
6. 下一章进入 TCP: 用代码看三次握手、四次挥手、粘包、TIME_WAIT。
""")
    print("下一课: 04_tcp/demo_10_tcp_handshake.py")


if __name__ == "__main__":
    main()

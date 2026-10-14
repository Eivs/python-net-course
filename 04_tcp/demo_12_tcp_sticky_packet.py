#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_12_tcp_sticky_packet.py -- 第 12 课：粘包与拆包（TCP 编程最大的坑）
=======================================================================

【问题是什么】
    第 11 课证明了 TCP 是字节流。于是就有两个现象:

        粘包（coalescing）: 你分 3 次 send, 对方一次 recv 就把 3 条都拿到了
        拆包（fragmentation）: 你一次 send 一条 1000 字节的消息, 对方要 recv 3 次才凑齐

    它们不是 bug, 也不是「TCP 的缺陷」, 而是「没有边界」这件事的两种表现。
    但业务代码几乎总是按「消息」思考的, 所以必须在应用层自己把边界补回来。

【三种标准方案】
    方案 A 定长消息（fixed length）
        每条消息都是固定字节数。实现最简单, 但消息长度必须统一。
        用于: 某些二进制协议、定长日志、嵌入式设备

    方案 B 分隔符（delimiter）
        用特殊字节（\n、\r\n、\0）标记结束。文本协议最爱用。
        用于: HTTP 头、Redis 协议、SMTP、日志行
        风险: 消息体里不能出现分隔符, 否则要转义

    方案 C 长度前缀（length prefix）
        先发 4 字节「长度」, 再发对应长度的内容。二进制协议标准做法。
        用于: gRPC、Thrift、Kafka、几乎所有内部 RPC
        优点: 不限制内容、解析效率高、可以预先分配内存

【真正难的地方】
    不是「怎么发」, 而是「怎么收」: recv 可能给你
        半个长度头、正好一个头、头 + 半个 body、头 + 1.5 个 body、3 个完整消息 ...
    所以接收端必须有一个「缓冲区 + 状态机」的解析器（decoder）,
    而不是简单拼接。本课会写一个真正健壮的 LengthPrefixedDecoder 并用
    「逐字节喂数据」来证明它是对的。

【本课路线】
    1. 先复现粘包: 3 次 send 被 1 次 recv 吃掉
    2. 方案 A 定长: 实现与局限
    3. 方案 B 分隔符: 实现一个带缓冲的 readline
    4. 方案 C 长度前缀: 实现健壮的 decoder（重点）
    5. 用「极端喂法」做单元测试: 逐字节、随机切分
    6. 工程检查清单

运行：python3 demo_12_tcp_sticky_packet.py
"""

from __future__ import annotations

import random
import socket
import struct
import threading
import time


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def start_echo_server(
    ready: threading.Event, holder: dict, stop: threading.Event
) -> None:
    """一个「老实巴交」的服务端: 收到什么字节就原样发回, 不做任何边界处理。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    holder["addr"] = srv.getsockname()
    ready.set()
    srv.settimeout(0.3)
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except (socket.timeout, OSError):
            continue
        threading.Thread(target=_echo_once, args=(conn,), daemon=True).start()
    srv.close()


def _echo_once(conn: socket.socket) -> None:
    with conn:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            try:
                conn.sendall(chunk)
            except OSError:
                break


# ================================================================ 方案 C 的核心
class LengthPrefixedDecoder:
    """长度前缀协议的接收端解析器。

    线上格式:  [4 字节大端长度 N][N 字节消息体]

    设计要点（这是本课最值得抄走的代码）:
      * 内部维护 bytearray 缓冲区, 不假设一次 feed 的粒度
      * 先判断「够不够读 4 字节头」, 够了再判断「够不够读 N 字节体」
      * 一次 feed 可能产出多条消息（粘包）, 也可能一条都没有（拆包）
      * 对超长长度做上限校验, 防止恶意报文让你分配 4GB 内存（真实漏洞常见形态）
    """

    HEADER = struct.Struct("!I")  # 4 字节无符号大端

    def __init__(self, max_message_size: int = 64 * 1024 * 1024):
        self.buf = bytearray()
        self.max_message_size = max_message_size
        self.messages_decoded = 0

    def feed(self, data: bytes) -> list[bytes]:
        """把收到的字节喂进来, 返回本次能解析出的所有完整消息。"""
        self.buf += data
        out: list[bytes] = []
        while True:
            if len(self.buf) < self.HEADER.size:
                break  # 连长度头都不够, 等更多数据
            (length,) = self.HEADER.unpack_from(self.buf, 0)
            if length > self.max_message_size:
                raise ValueError(
                    f"消息长度 {length} 超过上限, 丢弃连接（防内存攻击）"
                )
            total = self.HEADER.size + length
            if len(self.buf) < total:
                break  # body 还没收齐, 等更多数据
            out.append(bytes(self.buf[self.HEADER.size : total]))
            del self.buf[:total]  # 消费掉这条消息
            self.messages_decoded += 1
        return out


def encode_length_prefixed(message: bytes) -> bytes:
    """发送端: 长度 + 内容。"""
    return LengthPrefixedDecoder.HEADER.pack(len(message)) + message


def start_length_prefixed_server(
    ready: threading.Event, holder: dict, stop: threading.Event
) -> None:
    """带长度前缀协议的回显服务端: 用 decoder 取出完整消息, 再按同样格式回写。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    holder["lp_addr"] = srv.getsockname()
    ready.set()
    srv.settimeout(0.3)
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except (socket.timeout, OSError):
            continue
        threading.Thread(target=_lp_serve, args=(conn,), daemon=True).start()
    srv.close()


def _lp_serve(conn: socket.socket) -> None:
    decoder = LengthPrefixedDecoder()
    with conn:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            for message in decoder.feed(
                chunk
            ):  # 一次 recv 可能产出 0 条或多条消息
                conn.sendall(encode_length_prefixed(b"ACK:" + message))


def main() -> None:
    ready, stop = threading.Event(), threading.Event()
    holder: dict = {}
    threading.Thread(
        target=start_echo_server, args=(ready, holder, stop), daemon=True
    ).start()
    ready.wait(timeout=5)
    addr = holder["addr"]

    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：先亲眼看到粘包")

    client = socket.create_connection(addr)
    print("  客户端连着发 3 条短消息（3 次 sendall）:")
    for i in range(1, 4):
        client.sendall(f"消息{i}".encode("utf-8"))
        print(f"    sendall('消息{i}')  = {len(f'消息{i}'.encode())} 字节")

    time.sleep(0.3)  # 给内核一点时间把 3 条合并成一个报文段发出去
    got = client.recv(65536)
    print(f"\n  服务端一次 recv 拿到的字节数: {len(got)}")
    print(f"  内容: {got!r}")
    print(f"""
  一次 sendall 只有 7~8 字节, 而 recv 拿回了 {len(got)} 字节, 明显是 3 条粘在一起。
  粘合发生在发送方: 内核的 Nagle 算法（第 14 课会讲）会把小包攒起来一起发,
  也发生在接收路径: 数据先落到接收缓冲区, 你 recv 的时候它们已经都在那儿了。
  对我们来说原因不重要, 重要的是结论:

      一次 recv 的返回值和「一条消息」没有任何对应关系。

  那怎么知道收到了几条? 只能靠我们自己定义的边界规则。下面依次看三种方案。
""")
    client.close()

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：方案 A 定长消息")

    fixed = socket.create_connection(addr)
    print("""  规则: 每条消息固定 8 字节, 不足补空格。接收方每读满 8 字节就算一条。

  实现（发送方）:
      pad = lambda s: s.ljust(8, b' ')[:8]
      sock.sendall(pad(b"hello") + pad(b"world"))

  实现（接收方）:
      buf = b""
      while True:
          data = sock.recv(1024)
          if not data: break
          buf += data
          while len(buf) >= 8:
              message, buf = buf[:8], buf[8:]
              handle(message.rstrip(b" "))

  优点: 解析最简单, 不用扫分隔符、不用读长度, 天生适合零拷贝/固定结构体
  缺点: 变长内容是灾难（一条 1KB 的消息也要按 1KB 固定, 或者把长消息分片）
  适用: 定长协议头、二进制结构体、电报式心跳包
""")
    messages = [b"hello", b"world", b"hi"]
    fixed.sendall(b"".join(m.ljust(8) for m in messages))
    fixed.shutdown(socket.SHUT_WR)

    buf = b""
    decoded: list[bytes] = []
    while True:
        data = fixed.recv(1024)
        if not data:
            break
        buf += data
        while len(buf) >= 8:  # 关键: 循环切割, 而不是假设一次一条
            decoded.append(buf[:8].rstrip(b" "))
            buf = buf[8:]
    fixed.close()
    print(f"  回显服务端把原始字节原样发回, 我们用定长规则解出: {decoded}")
    print(
        f"  残留的不足一条的字节: {buf!r}（这种情况必须留在缓冲区里等下一次 recv）"
    )

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：方案 B 分隔符（\n）")

    print("""  规则: 每条消息以 \\n 结束, 接收方按 \\n 切分。

  最容易写错的版本:
      while True:
          data = sock.recv(4096)
          for line in data.split(b"\\n"):      # 错! 最后一段可能是半条消息
              handle(line)

  正确做法: 保留「最后一段没遇到分隔符的残留」, 下次拼接后再切（流式解析）:
      buf = b""
      while True:
          buf += sock.recv(4096)
          while b"\\n" in buf:
              line, buf = buf.split(b"\\n", 1)
              handle(line)

  优点: 人类可读, 调试方便（telnet/nc 就能测）, 文本协议首选
  缺点: 消息体里出现分隔符要转义; 解析要扫字节, 大消息效率略低
  适用: HTTP 头部、Redis/SMTP/FTP、日志收集、NDJSON
""")
    delim = socket.create_connection(addr)
    lines = [b"line-1", b"line-2", b"line-3", b"line-4"]
    # 故意用「一行拆成两次发送」的方式制造拆包
    payload = b"\n".join(lines) + b"\n"
    delim.sendall(payload[:6])
    time.sleep(0.1)
    delim.sendall(payload[6:])
    delim.shutdown(socket.SHUT_WR)

    buf = b""
    parsed: list[bytes] = []
    while True:
        data = delim.recv(1024)
        if not data:
            break
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            parsed.append(line)
    delim.close()
    print(f"  发送时被切成 {payload[:6]!r} + {payload[6:]!r} 两段（人为拆包）")
    print(f"  按 \\n 解析出的消息: {parsed}")
    print(f"  残留: {buf!r}")

    # ---------------------------------------------------------------- 第 4 步
    title("""第 4 步：方案 C 长度前缀（推荐）

    规则: [4 字节大端长度 N][N 字节内容]。这是本课的核心, 代码见文件中部
    的 LengthPrefixedDecoder 类, 它的 feed() 方法可以接受任意大小的碎片。
""")

    decoder = LengthPrefixedDecoder()
    print("  先做一个纯内存的单元测试（不涉及网络）, 用极端喂法验证 decoder:")
    msgs = [b"a", b"hello world", b"", b"x" * 5000, "中文消息".encode("utf-8")]
    wire = b"".join(encode_length_prefixed(m) for m in msgs)
    print(f"    5 条消息编码后共 {len(wire)} 字节（含 5 个 4 字节长度头）")

    # 喂法 1: 一次全给（对应所有数据都在缓冲区里 = 粘包）
    got1 = decoder.feed(wire)
    print(
        f"    喂法 1（一次性喂完）: 解出 {len(got1)} 条 -> {[len(x) for x in got1]}"
    )

    # 喂法 2: 逐字节喂（最极端的拆包, 每个字节一次 feed）
    decoder2 = LengthPrefixedDecoder()
    got2: list[bytes] = []
    for i in range(len(wire)):
        got2 += decoder2.feed(wire[i : i + 1])
    print(
        f"    喂法 2（逐字节 1 个 1 个喂）: 解出 {len(got2)} 条 -> {[len(x) for x in got2]}"
    )

    # 喂法 3: 随机切分（模拟真实网络, 边界完全随机）
    random.seed(42)
    decoder3 = LengthPrefixedDecoder()
    got3: list[bytes] = []
    pos = 0
    while pos < len(wire):
        n = random.randint(1, 64)
        got3 += decoder3.feed(wire[pos : pos + n])
        pos += n
    print(
        f"    喂法 3（随机 1~64 字节切片）: 解出 {len(got3)} 条 -> {[len(x) for x in got3]}"
    )

    same = got1 == got2 == got3 == msgs
    print(f"\n    三种喂法结果完全一致且与原始消息相同? {same}")
    assert same, "解码器实现有误"
    print("""    这就是「健壮的解码器」的定义: **它的正确性不依赖于 recv 的切分方式**。
    你写 TCP 协议时, 只要 decoder 通过了「逐字节喂」和「随机切分喂」两种测试,
    就基本不会再有粘包/拆包的线上事故。
""")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：在网络上来一遍（带长度前缀的回显服务端）")

    lp_ready, lp_holder = threading.Event(), {}
    threading.Thread(
        target=start_length_prefixed_server,
        args=(lp_ready, lp_holder, stop),
        daemon=True,
    ).start()
    lp_ready.wait(timeout=5)
    lp_addr = lp_holder["lp_addr"]
    print(f"  带长度前缀协议的服务端: {lp_addr}")

    lp_client = socket.create_connection(lp_addr)
    # 故意把 4 条消息拼成一段乱切发送, 模拟真实网络
    stream = b"".join(encode_length_prefixed(m) for m in msgs)
    print(
        f"  客户端把 {len(msgs)} 条消息拼成 {len(stream)} 字节, 随机切片发送:"
    )
    pos = 0
    slices = []
    while pos < len(stream):
        n = random.randint(1, 40)
        lp_client.sendall(stream[pos : pos + n])
        slices.append(n)
        pos += n
    print(f"    实际切成 {len(slices)} 次发送, 前 10 次大小 {slices[:10]}")
    lp_client.shutdown(socket.SHUT_WR)

    lp_decoder = LengthPrefixedDecoder()
    replies: list[bytes] = []
    while True:
        chunk = lp_client.recv(65536)
        if not chunk:
            break
        replies += lp_decoder.feed(chunk)
    lp_client.close()
    print(
        f"\n  服务端按协议解出消息并逐条 ACK, 客户端解出 {len(replies)} 条回复:"
    )
    for r in replies:
        shown = r if len(r) < 40 else r[:40] + b"..."
        print(f"    {shown!r}")
    print("""
  请对比一下这里和 TCP echo 的区别:
      TCP echo 服务端: 收到什么字节就原样发回（完全不管边界）
      本服务端:        先用 decoder 切出「一条完整消息」, 再按协议逐条回复
  第二种才是能在业务里用的形态: 你能对每条消息做参数校验、路由、限流、日志。
""")

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：工程检查清单")

    print("""  写任何基于 TCP 的协议时, 逐个确认:

  [发送端]
    □ 用 sendall 或自己循环处理「部分发送」
    □ 协议里必须自带边界信息（长度前缀 / 分隔符 / 定长）
    □ 有最大消息长度上限, 并且发送前校验
    □ 别指望对端能靠「我 close 了」判断消息结束（半关闭是可选语义）

  [接收端]
    □ 用「缓冲区 + 循环取出」的模式, 绝不用「一次 recv = 一条消息」
    □ 长度头要处理「只收到 2 个字节」的情况
    □ 对声称的长度做上限校验（否则一个恶意 4GB 长度头就能 OOM）
    □ 分隔符方案要处理「残留半条消息」
    □ 有超时: 收到半条消息后对端失联, 缓冲区要能清理
    □ 单元测试必须覆盖: 一次性喂完 / 逐字节喂 / 随机切片喂

  [协议设计]
    □ 明确字符集（UTF-8）和字节序（大端）
    □ 预留版本号字段, 否则以后无法演进
    □ 想想「半个消息」时怎么保命（幂等、重连、断点续传）
    □ HTTP/1.1 的 chunked、gRPC 的 length-prefix、Redis 的 RESP 都是这套思路的不同变体,
      看懂它们你就理解了 90% 的线上协议
""")

    stop.set()
    time.sleep(0.4)

    title("本课小结")
    print("""1. TCP 没有消息边界, 粘包和拆包是同一种现象的两个方向。
2. 三种方案: 定长（简单）、分隔符（文本友好）、长度前缀（通用推荐）。
3. 难点在接收端: 必须用「缓冲区 + 状态机」流式解析, 不能假设 recv 粒度。
4. 健壮性测试方法: 逐字节喂 + 随机切片喂, 两种都必须通过。
5. UDP 没有这个问题（第 8 课）, 代价是可能丢包。边界与可靠性, 你总得自己扛一头。
6. 下一课: 连接是怎么结束的（四次挥手、TIME_WAIT、CLOSE_WAIT、RST）。
""")
    print("下一课: 04_tcp/demo_13_tcp_teardown.py")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_11_tcp_echo.py -- 第 11 课：TCP 上是「字节流」，不是「消息」
================================================================

【原理】
    上一课把连接建起来了。这一课讲 TCP 的传输语义, 只有一句话:

        TCP 是一条「有序的字节流」, 它不保留你 write 的边界。

    为什么? 因为 TCP 的可靠性是靠「字节编号」实现的:
      应用层 send("hello") + send("world")
        -> 内核只看成一串字节 "helloworld"（编号 1..10）
        -> 可以一次发出去、也可以拆成 3 个报文段发
        -> 接收方的 recv() 从流里取多少, 完全由「缓冲区当时有多少数据」决定

    由此推出三条实践铁律:
      1. send(n) 可能只发出去一部分  -> 用 sendall() 或自己循环
      2. 对方一次 recv 可能拿到 0.5 条、1 条或 3 条消息 -> 要自己划边界（下一课）
      3. 「发送完成」不等于「对方已收到」（数据可能还在内核缓冲区里）-> 需要应用层确认

    和 UDP 的对比（第 8 课）:
        UDP: sendto(3 条) -> recvfrom 3 次, 边界天然存在, 但可能丢
        TCP: send(3 条)   -> recv 次数任意, 边界要自己划, 但不丢不乱序

【本课路线】
    1. 一个能正确处理「字节流」的 echo 服务端（recv 循环 + EOF 判断）
    2. EOF 与半关闭: shutdown(SHUT_WR) 让服务端知道「我说完了」
    3. 大块传输: 观察 recv 返回长度是任意的（1MB 数据被怎么切）
    4. send() 返回值的坑: 只发出去一部分
    5. 顺序保证: 与第 8 课的 UDP 对照

运行：python3 demo_11_tcp_echo.py
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


class TcpEchoServer:
    """一个正确的 TCP 回显服务端, 演示「字节流」该怎么读。

    关键点:
      * recv 返回 b"" 表示对端关闭（EOF）, 这是唯一的「读完了」信号
      * 每次 recv 的大小只是「上限」, 实际返回多少取决于内核缓冲区
      * 业务逻辑不能假设一次 recv 就等于一条消息
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, recv_size: int = 4096):
        self.recv_size = recv_size
        self.received_chunks: list[int] = []      # 记录每次 recv 的字节数, 用于观察
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(16)
        self.address = self.sock.getsockname()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._acceptor = threading.Thread(target=self._accept_loop, daemon=True)
        self._acceptor.start()

    def _accept_loop(self) -> None:
        self.sock.settimeout(0.3)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            while True:
                chunk = conn.recv(self.recv_size)     # 一次就是「最多 recv_size 字节」
                if chunk == b"":                      # EOF: 对端发了 FIN, 不会再有数据
                    break
                self.received_chunks.append(len(chunk))
                conn.sendall(chunk)                   # 原样回显（sendall 保证全部发出）
            # 退出 with 会对连接做 close(), 触发四次挥手（下一课细讲）

    def close(self) -> None:
        self._stop.set()
        self._acceptor.join(timeout=2)
        self.sock.close()


def main() -> None:
    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：先看服务端怎么写才「正确」")

    server = TcpEchoServer()
    print(f"  echo 服务端监听在 {server.address}")
    print("""  服务端骨架（这是 TCP 服务端的标准写法）:
      conn, addr = listen_sock.accept()      # 取一条已建立的连接
      while True:
          data = conn.recv(N)                # N 只是上限, 实际返回 0..N 字节
          if not data:                       # b"" == 对端关闭了写方向（EOF）
              break
          处理 data                          # 注意: 这里拿到的是"流的一段", 不是一条消息
      conn.close()                           # 退出时关闭, 触发挥手

  三个常见错误写法:
      data = conn.recv(1024)                 # 只读一次就当读完 -> 大消息会截断
      conn.send(data)                        # 换成 sendall, 否则可能只发一半
      把 recv 的返回值当"一条完整消息"        # 粘包/拆包的根源（下一课详解）
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：EOF 与半关闭，怎么告诉对方「我说完了」")

    client = socket.create_connection(server.address)      # 这是最常用的连接写法
    print(f"  客户端已连接: 本地端 {client.getsockname()} -> {client.getpeername()}")

    client.sendall("第一条消息\n".encode("utf-8"))
    client.sendall("第二条消息\n".encode("utf-8"))
    print("  已发送两条消息, 但先不关闭连接")

    client.shutdown(socket.SHUT_WR)          # 半关闭: 只关「我发」的方向, 我还能收
    print("""  调用 shutdown(SHUT_WR):
      内核发出 FIN, 但本端仍可 recv。服务端会读到 b"" 从而知道「客户端说完了」。
  这与 close() 的区别很关键:
      shutdown(SHUT_WR)  只结束发送方向, 连接还活着 -> 可以继续收对方的回应
      close()            释放文件描述符, 两个方向都不能用了（除非 dup 过）
  很多协议（HTTP/1.0、Redis 的 inline 命令）就是靠半关闭来判断请求结束的。
  但注意: 依赖「对方关闭才算请求结束」的协议有个隐患 -> 如果客户端不关,
  服务端就永远等下去。所以现代协议都带长度或分隔符（HTTP 的 Content-Length）。
""")

    echo = b""
    while True:
        chunk = client.recv(4096)
        if not chunk:
            break                            # 服务端也关闭了 -> 整个连接结束
        echo += chunk
    print(f"  客户端收到回显 {len(echo)} 字节: {echo!r}")
    client.close()

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：大块传输时 recv 的返回值有多「任意」")

    server.received_chunks.clear()
    big_client = socket.create_connection(server.address)
    payload = (b"0123456789abcdef" * 65536)          # 1 MiB
    print(f"  准备发送 {len(payload)} 字节（1 MiB）, 服务端 recv 上限 = {server.recv_size} 字节")

    start = time.perf_counter()
    big_client.sendall(payload)                      # sendall 内部自动循环, 保证全部发出
    big_client.shutdown(socket.SHUT_WR)

    received = bytearray()
    while True:
        chunk = big_client.recv(8192)
        if not chunk:
            break
        received += chunk
    elapsed = time.perf_counter() - start
    big_client.close()
    time.sleep(0.3)                                  # 等服务端线程把统计写完

    chunks = server.received_chunks
    print(f"""
  结果:
    客户端发出去   : {len(payload)} 字节
    服务端收回来   : {len(received)} 字节（内容一致: {bytes(received) == payload}）
    服务端 recv 次数: {len(chunks)} 次（每次上限 4096 字节）
    实际每次长度   : 最小 {min(chunks) if chunks else 0} / 最大 {max(chunks) if chunks else 0} 字节
    前 12 次的长度 : {chunks[:12]}
    客户端耗时     : {elapsed * 1000:.1f} ms（本地回环, 约 {len(payload) / max(elapsed, 1e-9) / 1e6:.1f} MB/s）

  为什么接受上限是 4096 却经常收到整 4096? 因为发送端发得比接收端读得快,
  内核缓冲区里堆满了数据, 每次 recv 都能取满。反过来如果网络慢, 你就会看到
  几十、几百字节的小块。**这就是「一次 recv != 一条消息」的铁证**: 长度由网络和
  缓冲区决定, 跟你的业务消息毫无关系。
""")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：send() 的返回值陷阱")

    print("""  很多人以为 send(data) 就是把 data 全发出去, 其实它返回「实际发出去多少字节」。
  只有当发送缓冲区有空位时才立刻返回; 缓冲区满了, 阻塞模式下会等, 非阻塞模式下
  直接抛 BlockingIOError。
  下面用一个「只连接不读取」的服务端把客户端的发送缓冲区撑满, 亲眼看到部分发送。
""")

    silent_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    silent_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    silent_server.bind(("127.0.0.1", 0))
    silent_server.listen(2)
    silent_addr = silent_server.getsockname()
    accepted: list[socket.socket] = []

    def accept_but_never_read() -> None:
        conn, _ = silent_server.accept()
        accepted.append(conn)
        time.sleep(6)            # 关键: 故意不 recv, 让对端缓冲区填满

    t = threading.Thread(target=accept_but_never_read, daemon=True)
    t.start()

    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)      # 故意把发送缓冲调小
    raw.connect(silent_addr)
    raw.setblocking(False)                                        # 非阻塞, 满了就报错
    print(f"  SO_SNDBUF 设为 8192（系统实际给 {raw.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)}）")

    block = b"x" * 65536
    parts: list[int] = []
    total = 0
    try:
        for _ in range(200):
            sent = raw.send(block)
            parts.append(sent)
            total += sent
    except BlockingIOError:
        print(f"  第 {len(parts) + 1} 次 send -> BlockingIOError（缓冲区满了, 非阻塞下立刻报错）")
    except OSError as exc:
        print(f"  发送中断: {type(exc).__name__}: {exc}")
    finally:
        print(f"  成功 send 了 {len(parts)} 次, 累计 {total} 字节, 每次返回: {parts[:8]}{' ...' if len(parts) > 8 else ''}")
        print("""
  看到没: send 返回 65536 是一次的量, 但缓冲区小的时候它可能只返回一部分。
  所以:
      要么用 sendall()（内部帮你循环直到发完）
      要么自己 while 循环, 直到累计发够了
  另一个常见 bug: 用 send 发送大文件, 以为发完了就去关连接 -> 对端收到半个文件。
  正确姿势是 sendall(), 或者关闭前用 shutdown(SHUT_WR) + 等对端确认。
""")
        raw.close()

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：顺序保证（对比第 8 课的 UDP）")

    server.received_chunks.clear()
    ordered_client = socket.create_connection(server.address)
    lines = [f"{i:04d}" for i in range(1, 201)]

    payload_lines = ("\n".join(lines) + "\n").encode()
    for i in range(0, len(payload_lines), 37):        # 故意切成 37 字节的小块发送
        ordered_client.sendall(payload_lines[i:i + 37])
    ordered_client.shutdown(socket.SHUT_WR)

    back = b""
    while True:
        chunk = ordered_client.recv(4096)
        if not chunk:
            break
        back += chunk
    ordered_client.close()

    got_numbers = [int(x) for x in back.decode().strip().split("\n") if x]
    print(f"  发送 {len(lines)} 条编号, 收回 {len(got_numbers)} 条")
    print(f"  顺序是否正确: {got_numbers == list(range(1, 201))}")
    print(f"  有无丢失: {len(got_numbers) == len(lines)}")
    print("""
  这就是 TCP 的承诺: 只要连接没断, 你发出去的字节一定按原顺序、不丢不重地到达。
  但请再注意一次: 我们「切成 37 字节发送」的边界, 对方完全看不出来,
  它拿到的就是一整段字节流。**边界的丢失是 TCP 的固有特性, 不是 bug。**

  所以下一课要解决的核心问题就是: 在字节流之上, 怎么把消息边界重新标出来?
""")

    server.close()

    title("本课小结")
    print("""1. TCP 是有序字节流, 不保留 send 的边界; 一次 recv 可能拿到任意长度。
2. recv 返回 b"" 是唯一的「对端已关闭写方向」信号（EOF）。
3. shutdown(SHUT_WR) 是半关闭, 用来礼貌地告诉对方「我说完了」。
4. send 可能只发一部分, 用 sendall 或自己循环。
5. TCP 保证顺序、不丢不重; UDP 不保证, 这是两条完全不同的工程路线。
6. 下一课: 粘包/拆包的三种标准解决方案（定长 / 分隔符 / 长度前缀）。
""")
    print("下一课: 04_tcp/demo_12_tcp_sticky_packet.py")


if __name__ == "__main__":
    main()

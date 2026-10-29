#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_14_tcp_socket_options.py -- 第 14 课：常用 socket 选项的实测效果
====================================================================

【为什么要学 socket 选项】
    前面 13 课都在讲「协议怎么工作」。这一课讲「怎么调它的脾气」。
    同样的代码, 选对选项可能差几倍性能; 选错则会出现
    「本地测试飞快、线上偶发 40ms 卡顿」这种极难查的问题。

【本课要实测的选项】
    TCP_NODELAY      关闭 Nagle 算法（小包延迟 vs 带宽利用率的拉锯）
    SO_SNDBUF/RCVBUF 内核缓冲区大小（吞吐量 vs 内存）
    SO_RCVTIMEO      套接字级超时（和 settimeout 不是一回事, 本课有坑）
    SO_KEEPALIVE     TCP 层心跳（周期长得离谱, 通常不够用）
    TCP_MAXSEG       本连接协商出的最大段大小（能看到 MSS 协商结果）
    SO_ERROR         非阻塞 connect 的「错误从哪读」的答案

运行：python3 demo_14_tcp_socket_options.py
"""

from __future__ import annotations

import errno
import socket
import struct
import threading
import time


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


# ---------------------------------------------------------------- 测试用服务端
class SinkServer:
    """接收方: 统计收到的字节数, 可选记录每次 recv 的到达时间。"""

    def __init__(self, recv_size: int = 65536):
        self.recv_size = recv_size
        self.total = 0
        self.arrivals: list[tuple[float, int]] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.address = self.sock.getsockname()
        self._stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        self.sock.settimeout(0.3)
        conns: list[socket.socket] = []
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
                conns.append(conn)
                threading.Thread(
                    target=self._drain, args=(conn,), daemon=True
                ).start()
            except (socket.timeout, OSError):
                continue
        for c in conns:
            try:
                c.close()
            except OSError:
                pass

    def _drain(self, conn: socket.socket) -> None:
        try:
            while True:
                chunk = conn.recv(self.recv_size)
                if not chunk:
                    break
                self.total += len(chunk)
                self.arrivals.append((time.perf_counter(), len(chunk)))
        except OSError:
            pass
        finally:
            # 对端半关闭（shutdown(SHUT_WR)）后我们读到 EOF, 这里主动关闭连接,
            # 让客户端能收到「整个连接结束」的信号, 否则它会一直等下去。
            try:
                conn.close()
            except OSError:
                pass

    def reset(self) -> None:
        self.total = 0
        self.arrivals.clear()

    def close(self) -> None:
        self._stop.set()
        self.sock.close()


def main() -> None:
    # ---------------------------------------------------------------- 第 0 步
    title("第 0 步：一张速查表")

    print("""  ┌────────────────────┬──────────────────────────────┬──────────────────────────┐
  │ 选项                │ 作用                          │ 什么时候用                │
  ├────────────────────┼──────────────────────────────┼──────────────────────────┤
  │ SO_REUSEADDR        │ 允许 bind TIME_WAIT 中的地址   │ 所有服务端都要（第13课）   │
  │ SO_REUSEPORT        │ 多 socket 共享同一端口         │ 多进程均匀分摊连接         │
  │ TCP_NODELAY         │ 关闭 Nagle, 立即发送小包       │ 低延迟交互（RPC/游戏/Redis）│
  │ SO_SNDBUF / RCVBUF  │ 内核收发缓冲区大小             │ 大文件传输、高 BDP 链路上调│
  │ SO_RCVTIMEO/SNDTIMEO│ 内核级收发超时                 │ 想用内核实现的超时         │
  │ SO_KEEPALIVE        │ TCP 层探测死连接               │ 长连接兜底（但周期太长）   │
  │ TCP_KEEPALIVE/INTVL │ 上面那个的周期参数             │ 想缩短探测时间             │
  │ TCP_MAXSEG          │ 本连接协商的 MSS               │ 排查分片/性能问题时读一下  │
  │ SO_LINGER           │ 关闭行为（FIN vs RST）         │ 需要立即断开的场景（第13课）│
  │ SO_ERROR            │ 读取并清除套接字上的待处理错误 │ 非阻塞 connect 必须配合    │
  └────────────────────┴──────────────────────────────┴──────────────────────────┘
""")

    server = SinkServer()

    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：TCP_MAXSEG —— 看看本连接的 MSS 是多少")

    mss_probe = socket.create_connection(server.address)
    mss = mss_probe.getsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG)
    print(f"  连接的 TCP_MAXSEG = {mss} 字节")
    print(f"""
  这是三次握手时双方交换 MSS 选项协商出的结果（取两边较小值）。
  理论值: 路径 MTU 16384 - 20(IP) - 20(TCP) = {16384 - 40}。
  实测比理论值小 12 字节: 本平台协商 MSS 时把 TCP 时间戳选项（12 字节）也算进了头部开销,
  所以是 {mss} 而不是 {16384 - 40}。不同内核算不算选项并不统一, 以 getsockopt 读到的为准。
  在真实以太网（MTU 1500）上你会看到 1460, 如果中间有 VPN/隧道, 可能是 1400/1280 之类。
  为什么关心它: 应用层一次 send 的字节数远大于 MSS 时, 内核会切分; 而如果你在做
  自己的可靠协议（比如基于 UDP 的 QUIC）, 就必须自己按 MSS 控制包大小, 否则分片丢一个全废。
""")
    mss_probe.close()

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：TCP_NODELAY 与 Nagle 算法")

    print("""  Nagle 算法的规则（RFC 896, 1984 年为了省带宽）:
      如果还有「没被确认的小数据」在飞, 就把新的小数据攒起来, 等收到 ACK 再一起发。
  好处: 减少小包数量, 提高带宽利用率（那个年代带宽金子般贵）。
  坏处: 与「延迟确认（delayed ACK）」叠加时会互相等待, 造成最大 40ms 级别
        甚至 200ms 的延迟尖刺。这是历史上最著名的网络性能坑之一。
  TCP_NODELAY=1 就是「别等了, 有多少发多少」。

  下面在本地回环上实测两种情况（写 2000 次、每次 1 字节）:
""")

    def tiny_writes(nodelay: bool) -> float:
        s = socket.create_connection(server.address)
        if nodelay:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        else:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0)
        server.reset()
        start = time.perf_counter()
        for _ in range(2000):
            s.sendall(b"x")  # 每次只写 1 字节, 逼出 Nagle 行为
        s.shutdown(socket.SHUT_WR)
        s.settimeout(20)  # 安全网: 万一对面不关, 也不至于卡死
        while s.recv(1024):  # 等到 EOF（服务端读到 EOF 后会关连接）
            pass
        elapsed = time.perf_counter() - start
        s.close()
        return elapsed

    off = tiny_writes(nodelay=False)
    on = tiny_writes(nodelay=True)
    print(f"    TCP_NODELAY=0（Nagle 生效）: {off * 1000:8.2f} ms")
    print(f"    TCP_NODELAY=1（不等, 直接发）: {on * 1000:8.2f} ms")
    print(
        f"    差异: 本地回环上 {abs(off - on) * 1000:.2f} ms（{'NODELAY 更快' if on < off else 'Nagle 更快'}）"
    )
    print("""
  诚实结论: 在本机回环上差别很小甚至反转, 因为回环的 ACK 是立刻返回的, Nagle 几乎不等待。
  真实网络上才有明显差别:
      * 跨机房 RTT 30ms 时, 两次小写可能变成 60ms（等 ACK + 延迟确认）
      * Redis、MySQL、gRPC、游戏同步都默认开 TCP_NODELAY, 就是不想赌这个延迟
      * 反过来, 大批量小日志上报如果不做应用层合并, 开 NODELAY 只是把网络变吵
  实践建议:
      低延迟交互型协议 -> TCP_NODELAY=1
      吞吐型批量传输   -> 保持默认, 并在应用层自己做批量合并（批量写一次 send）
      最靠谱的优化不是选项, 而是「别做 2000 次 1 字节的写」: 应用层攒够再发一次
""")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：SO_SNDBUF / SO_RCVBUF 与吞吐量")

    probe = socket.create_connection(server.address)
    snd_default = probe.getsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF)
    rcv_default = probe.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    probe.close()
    print(
        f"  本机默认 SO_SNDBUF = {snd_default} 字节, SO_RCVBUF = {rcv_default} 字节"
    )
    print("""  （注意: 你设 8192, 内核可能给你两倍, 因为它内部算了额外开销;
    所以永远要用 getsockopt 读回实际值, 别假设 = 你设的值。）

  这两个缓冲区的意义:
    * 发送缓冲: send() 把数据交给内核就返回了, 数据在这里排队等网卡发出去。
      它让「应用速率忽快忽慢」和「网络速率恒定」之间有个蓄水池。
    * 接收缓冲: 网卡收到的数据放这里等你 recv()。太小 -> 高带宽高延迟链路上
      来不及读就被丢（然后 TCP 靠重传补救, 吞吐掉下来）。
    * 经验公式（BDP）: 缓冲区 >= 带宽 × RTT。100Mbps × 100ms = 1.25MB, 默认值就不够用了。
""")

    def transfer_with_buffers(
        total_bytes: int, bufsize: int | None
    ) -> tuple[float, int]:
        s = socket.create_connection(server.address)
        if bufsize is not None:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, bufsize)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, bufsize)
        server.reset()
        block = b"z" * 262144
        start = time.perf_counter()
        sent = 0
        while sent < total_bytes:
            s.sendall(block)
            sent += len(block)
        s.shutdown(socket.SHUT_WR)
        s.settimeout(60)
        while s.recv(262144):  # 读到 EOF 说明服务端已经收完并关闭
            pass
        elapsed = time.perf_counter() - start
        s.close()
        return elapsed, server.total

    total = 32 * 1024 * 1024
    t_default, got_default = transfer_with_buffers(total, None)
    t_small, got_small = transfer_with_buffers(total, 8192)
    print(f"  传输 {total // (1024 * 1024)} MiB 实测:")
    print(
        f"    默认缓冲区   : {t_default * 1000:8.1f} ms  ->  {got_default / t_default / 1e6:.0f} MB/s"
    )
    print(
        f"    SNDBUF=8192 : {t_small * 1000:8.1f} ms  ->  {got_small / t_small / 1e6:.0f} MB/s"
    )
    print(f"    （收到字节数 {got_default} / {got_small}，两者应相同）")
    print("""
  在本地回环上差别可能不大（没有真实带宽瓶颈）, 但在真实链路上:
      小缓冲区 = 发送端频繁阻塞 = 吞吐上不去, 或者丢包导致重传抖动。
  调优顺序建议: 先用应用层手段（批量、压缩、并发连接、连接复用）,
  最后才动内核缓冲区, 并一定用真实网络压测验证。
""")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：SO_RCVTIMEO —— 和 settimeout 不是一回事（有坑）")

    print("""  Python 里设置超时有两种方式:
      sock.settimeout(1.0)                                   # Python 层的超时
      sock.setsockopt(SOL_SOCKET, SO_RCVTIMEO, timeval)      # 内核层的超时

  它们的行为差异（本机实测, 很重要）:
""")
    timeout_sock = socket.create_connection(server.address)
    # timeval 结构: (tv_sec, tv_usec), macOS/Linux 64 位下都是两个 long
    tv = struct.pack("ll", 0, 300000)  # 300 ms
    timeout_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, tv)
    readback = timeout_sock.getsockopt(
        socket.SOL_SOCKET, socket.SO_RCVTIMEO, 16
    )
    sec, usec = struct.unpack("ll", readback)
    print(f"  设置 SO_RCVTIMEO = 300ms, 读回 = {sec}s {usec}us")
    print("  然后啥也不发就直接 recv, 看会抛什么异常:")
    start = time.perf_counter()
    try:
        timeout_sock.recv(10)
    except socket.timeout as exc:
        print(f"    -> socket.timeout（按 settimeout 的语义）: {exc}")
    except BlockingIOError as exc:
        print(
            f"    -> BlockingIOError: {exc}   (errno={exc.errno} = {errno.errorcode.get(exc.errno)})"
        )
        print("""       <- 关键差异! 内核超时返回的是 EAGAIN/EWOULDBLOCK, 而 Python 只在
          「通过 settimeout 设置的超时」时才把它翻译成 socket.timeout。
          用 SO_RCVTIMEO 时你拿到的是 BlockingIOError（它是 OSError 的子类）。
          所以: 团队里统一用 settimeout + socket.timeout, 别混用两套机制,
          否则异常处理分支会漏掉一种, 表现为「偶发未捕获异常」。""")
    except OSError as exc:
        print(f"    -> {type(exc).__name__}: {exc}")
    finally:
        print(
            f"    实际等待: {(time.perf_counter() - start) * 1000:.1f} ms（应接近 300ms）"
        )
        timeout_sock.close()

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：SO_KEEPALIVE —— TCP 层心跳，为什么通常不够用")

    ka = socket.create_connection(server.address)
    print(
        f"  SO_KEEPALIVE 默认值: {ka.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)}（0 = 关闭）"
    )
    ka.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

    param_names = ["TCP_KEEPALIVE", "TCP_KEEPINTVL", "TCP_KEEPCNT"]
    params: dict[str, int] = {}
    print(
        "  各平台的相关参数（名称和单位都不一样, 这是可移植性噩梦的经典案例）:"
    )
    for name in param_names:
        opt = getattr(socket, name, None)
        if opt is None:
            print(f"    {name:<16} 本平台不支持")
            continue
        value = ka.getsockopt(socket.IPPROTO_TCP, opt)
        params[name] = value
        unit = "秒" if name in ("TCP_KEEPALIVE", "TCP_KEEPINTVL") else "次"
        print(f"    {name:<16} 默认 {value} {unit}")

    if params:
        idle = (
            params.get("TCP_KEEPALIVE", 0) / 1000
            if params.get("TCP_KEEPALIVE", 0) > 10000
            else params.get("TCP_KEEPALIVE", 0)
        )
        intvl = params.get("TCP_KEEPINTVL", 0)
        cnt = params.get("TCP_KEEPCNT", 0)
        print(
            f"\n  按默认值算, 探测到「对端彻底死了」需要: 空闲 {idle}秒 + {intvl}秒 × {cnt}次 ≈ {idle + intvl * cnt:.0f} 秒"
        )
        print("""      <- 两个多小时。这个默认值来自 1980 年代, 对现代业务来说完全不可用:
         你的负载均衡器、连接池、数据库连接早就超时断开或被中间设备悄悄清理了,
         而你这一端还以为连接是好的, 于是出现「偶发 BrokenPipeError」。

  所以工程上的标准做法是:
      1) 应用层心跳（比如每 15~30 秒一次 ping/pong）—— 这才是主流
      2) 在心跳里带上请求 ID 和超时, 能顺便测 RTT 和丢包
      3) SO_KEEPALIVE + 缩短参数作为兜底（PHP/一些老框架会这么干）
      4) 别忘了业务层的「空闲连接回收」: 连接池 max_idle_time 要小于中间设备的空闲超时
      （常见坑: 云负载均衡默认空闲 60 秒断连, 而你的连接池 300 秒才回收）
""")
    ka.close()

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：非阻塞 connect 与 SO_ERROR")

    print("""  非阻塞 connect() 会立刻返回 EINPROGRESS, 让你用 select/poll/epoll 等它完成。
  真正的问题在完成之后: 我该怎么知道刚才那次 connect 是成功还是失败?
  答案: 用 getsockopt(SOL_SOCKET, SO_ERROR) 去读（读出来还能顺便清掉这个错误）。
""")
    nb = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    nb.setblocking(False)
    code = nb.connect_ex(("127.0.0.1", 1))  # 1 号端口肯定没人监听
    print(
        f"  connect_ex(127.0.0.1:1) 立刻返回 {code} ({errno.errorcode.get(code)})"
    )
    time.sleep(0.2)
    so_error = nb.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
    print(
        f"  稍后读 SO_ERROR = {so_error} ({errno.errorcode.get(so_error, '成功')})"
    )
    print(
        f"  再读一次 SO_ERROR = {nb.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)}  <- 读完即清除, 第二次是 0"
    )
    nb.close()
    print("""
  这个模式是 asyncio / selectors / 所有高性能网络框架的基础:
      sock.setblocking(False)
      code = sock.connect_ex(addr)
      if code == EINPROGRESS:
          select([sock], timeout=...)
          err = sock.getsockopt(SOL_SOCKET, SO_ERROR)
          if err != 0: 失败; else: 成功
  第 6 章的综合项目会用到「线程模型」, 而这里展示的是「事件模型」的起点,
  两者都是解决同一问题的不同流派（C10K 之后事件模型成为主流）。
""")

    # ---------------------------------------------------------------- 收尾
    title("本课小结：选项配置的工程建议")

    server.close()
    print("""  1. 服务端必备: SO_REUSEADDR。多进程分摊: SO_REUSEPORT。
  2. 低延迟协议: TCP_NODELAY=1。但要先做「应用层批量写」, 效果比选项大得多。
  3. 缓冲区: 默认值适合局域网; 跨地域高 BDP 链路要按 带宽×RTT 上调, 并用真实压测验证。
  4. 超时: 统一用 settimeout + socket.timeout, 别和 SO_RCVTIMEO 混用（异常类型不同）。
  5. 心跳: 不要指望 TCP keepalive 的默认值, 应用层心跳 + 连接池空闲回收才是正解。
  6. 排障: TCP_MAXSEG 看 MSS、netstat 看队列（Recv-Q/Send-Q）、SO_ERROR 看异步错误。
  7. 一句话原则: 先理解语义再调参数。选项解决的是「已经正确的代码的效率」,
     不会把「错误的设计」救回来。
""")
    print("下一课: 05_http/demo_15_http_raw.py")


if __name__ == "__main__":
    main()

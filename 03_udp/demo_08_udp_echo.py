#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_08_udp_echo.py -- 第 8 课：UDP，无连接的最简传输
=====================================================

【原理】
    TCP 和 UDP 是传输层的两兄弟, 它们回答同一个问题:「怎么把数据从进程 A 送到进程 B」。
    区别在于它们对「可靠性」的取舍:

        TCP: 先握手建立连接, 有确认、重传、排序、流量控制、拥塞控制
             -> 像打电话: 接通、一句一句说、确认对方听清
        UDP: 不建立连接, 把数据打包（数据报）直接扔出去, 不确认也不重传
             -> 像寄明信片: 写地址扔进邮筒, 到了就到了, 丢了就丢了

    因此 UDP 的三个特性（也是三个坑）:
        1. 无连接: 没有握手, 每个包自带完整地址, 开销小、延迟低
        2. 不保证: 可能丢、可能重复、可能乱序、可能比后发的先到
        3. 保留边界: 一次 sendto = 一个数据报, 接收方一次 recvfrom 就是一个完整数据报
                     （TCP 是字节流, 反而需要自己拆消息, 见第 12 课）

    UDP 用在哪: DNS(53)、DHCP(67/68)、NTP(123)、QUIC(HTTP/3 的底座)、
                实时音视频、游戏同步、日志收集（statsd）
    共同点: 要么「丢了重发代价太高」, 要么「自己在上层做重传更划算」。

【本课路线】
    1. 最小 UDP 收发: 服务端 recvfrom / 客户端 sendto
    2. 「一次发送 = 一个数据报」, 边界不会粘在一起（和 TCP 的对比）
    3. connected UDP socket: connect() 在 UDP 上到底做了什么
    4. 真实看到丢包: 接收缓冲区溢出（本机就能复现）
    5. 数据报大小上限与分片风险
    6. UDP 也能收到错误: ICMP port unreachable -> ConnectionRefusedError

运行：python3 demo_08_udp_echo.py
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


def start_udp_echo_server(ready: threading.Event, holder: dict, stop: threading.Event) -> None:
    """一个最小 UDP 服务端: 收到什么就原样发回去。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)      # SOCK_DGRAM = 数据报 = UDP
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    holder["server_addr"] = sock.getsockname()
    ready.set()

    while not stop.is_set():
        try:
            sock.settimeout(0.5)
            data, peer = sock.recvfrom(65535)      # 一个数据报, 一次收完
        except socket.timeout:
            continue
        except OSError:
            break
        holder.setdefault("received", []).append((data, peer))
        try:
            sock.sendto(b"echo:" + data, peer)     # 回给来源地址（每个包都带地址, 不需要连接）
        except OSError:
            pass     # 回包太大时发送会失败, 服务端不该因此崩掉（UDP 的现实之一）
    sock.close()


def main() -> None:
    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：最小 UDP 收发（没有三次握手）")

    ready, stop = threading.Event(), threading.Event()
    holder: dict = {}
    server_thread = threading.Thread(target=start_udp_echo_server, args=(ready, holder, stop), daemon=True)
    server_thread.start()
    ready.wait(timeout=5)
    server_addr = holder["server_addr"]

    print(f"  UDP 服务端已就绪: {server_addr}（注意: 它没有 LISTEN 状态, 因为没有连接）")
    print("""  UDP 服务端代码只有三步:
      socket(AF_INET, SOCK_DGRAM)   # 建一个数据报 socket
      bind((host, port))            # 占住端口
      recvfrom() / sendto()         # 收发, 每次都要给地址

  对比 TCP: 服务端要 bind -> listen -> accept, 客户端要 connect。
  UDP 没有 listen / accept, 因为「没有连接」这个概念。
""")

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    client.settimeout(3)
    payload = "你好 UDP".encode("utf-8")
    print(f"  客户端 sendto({payload!r}) -> {server_addr}")
    client.sendto(payload, server_addr)
    reply, from_addr = client.recvfrom(65535)
    print(f"  客户端收到 {reply!r} 来自 {from_addr}")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：「一次 sendto = 一个数据报」，边界天然保留")

    for i in range(1, 4):
        client.sendto(f"消息{i}".encode(), server_addr)
    print("  连续 sendto 三次（没有等待、没有握手、三条消息互不相关）")
    for i in range(1, 4):
        data, _ = client.recvfrom(65535)
        print(f"  recvfrom -> {data!r}   <- 每次都是独立的一条, 不会像 TCP 那样粘在一起")

    print("""
  记住这个对比, 第 12 课会用它来反衬 TCP:
      UDP: 你发 3 次, 对方就 recv 3 次, 边界由协议替你保住
      TCP: 你发 3 次, 对方可能一次 recv 到全部 3 条（粘包）, 边界要你自己划
""")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：connect() 在 UDP 上做什么？（不是握手！）")

    connected = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    connected.settimeout(3)
    connected.connect(server_addr)          # 关键: UDP 的 connect 不发任何包
    print(f"  connect({server_addr}) 之后: getsockname()={connected.getsockname()} getpeername()={connected.getpeername()}")
    connected.send(b"connected-mode")       # 可以用 send/recv, 不用每次写地址
    data = connected.recv(65535)            # 注意: 连接过的 socket 用 recv, 返回值只有数据, 没有地址
    print(f"  用 send()/recv() 收发成功: {data!r}")

    print("""
  UDP 的 connect() 只是在本地 socket 上记下默认对端地址, 好处有三个:
    1. 兼容只用 send/recv 的老代码
    2. 内核可以做一次路由选择, 锁定源 IP 和源端口（不再每个包变）
    3. 只有该对端发来的包才会被投递, 其它地址的包被内核丢弃（顺带挡掉一部分杂包）
  但它不会发 SYN, 也不会校验对方是否在线。所以「connect 成功」不代表对方存在。
""")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：亲眼看到丢包（接收缓冲区溢出）")

    print("""  丢包不是玄学。UDP 没有流量控制, 发送方发多快都可以;
  如果接收方读得慢, 内核缓冲区会被填满, 新到的数据报就被直接丢掉（静默丢弃, 不通知任何人）。
  下面把接收缓冲区故意设小(2048 字节), 然后一口气发 500 个 100 字节的包。
""")
    slow_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    slow_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)     # 缩小接收缓冲
    slow_sock.bind(("127.0.0.1", 0))
    actual_buf = slow_sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    print(f"  SO_RCVBUF 设为 2048, 系统实际给出 {actual_buf}（内核有最小值限制）")

    burst = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    total = 500
    ordered = []
    for seq in range(total):
        ordered.append(seq)
        burst.sendto(f"{seq:04d}".encode() + b"x" * 96, slow_sock.getsockname())

    time.sleep(0.2)          # 故意不读, 让缓冲区在数据到达期间保持爆满
    got = []
    slow_sock.settimeout(0.3)
    while True:
        try:
            data, _ = slow_sock.recvfrom(65535)
            got.append(int(data[:4]))
        except socket.timeout:
            break

    lost = sorted(set(ordered) - set(got))
    reordered = sum(1 for a, b in zip(got, got[1:]) if a > b)
    print(f"  发送 {total} 个数据报, 收到 {len(got)} 个, 丢失 {len(lost)} 个")
    if lost:
        print(f"  丢失的序号(前 20 个): {lost[:20]}")
    print(f"  接收到的序列里出现逆序的次数: {reordered}")
    print("""
  这说明什么:
    * UDP 的「不可靠」不是理论, 在你自己机器上就能复现（本地回环都不保证）
    * 丢包的量取决于发送速率 vs 接收速率 + 缓冲区大小
    * 想要可靠, 必须自己实现: 序号 + 确认 + 重传 + 去重(比如 QUIC 就是这么干的)
    * 发送端完全不知道丢了（没有 ICMP 通知, 除非是"目的端口不可达"这类错误）
""")
    slow_sock.close()
    burst.close()

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：数据报能有多大？分片与上限")

    def can_send(size: int) -> str:
        """试着把 size 字节发到本机回环 UDP 服务, 返回结果描述。"""
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(1)
        try:
            probe.sendto(b"A" * size, server_addr)
            return "发送成功"
        except OSError as exc:
            return f"失败: {type(exc).__name__}: {exc}"
        finally:
            probe.close()

    print(f"  {'载荷字节':>10}  结果")
    for size in [1, 512, 1472, 1500, 8192, 16384, 65507, 65508]:
        print(f"  {size:>10}  {can_send(size)}")

    # 二分找出「这台机器」实际允许的最大 UDP 载荷
    low, high = 8192, 65508
    if can_send(high).startswith("失败"):
        while high - low > 1:
            mid = (low + high) // 2
            if can_send(mid).startswith("发送成功"):
                low = mid
            else:
                high = mid
        print(f"\n  二分实测: 本机回环上最大可发 UDP 载荷 = {low} 字节（{low + 1} 就报 Message too long）")
        print(f"  注意这个数字远小于理论值 65507, 因为本机回环 MTU 与内核发送缓冲(uio/驱动)共同限制。")

    print("""
  几个关键数字:
    UDP 长度字段是 16 位 -> 单个数据报最大 65535 字节, 减去 IP 头 20 + UDP 头 8
      => 应用层载荷的理论上限 65507 字节（IPv4）。超过就 EMSGSIZE。
    但「理论上限」不等于「实际能发」: 还要看路径 MTU 和内核缓冲区。
    以太网 MTU 1500, 减去 20(IP) + 8(UDP) => 不分片的最大载荷 1472 字节
      => 超过 1472 的 UDP 包会被 IP 层切成多个分片, 任意一个分片丢了整个数据报就废了
      => 现实建议: 应用层控制在 1200 字节以下（IPv6 最小 MTU 1280 也安全）
    本机回环 MTU 是 16384（不出网卡）, 所以这里发 1500 也不会分片,
    真实以太网链路上就会分片, 这就是「本机测试通过、上线就丢包」的经典原因。
    TCP 帮你做了「按 MSS 自动切分」, UDP 不会, 这是很多人踩的坑。
""")

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：UDP 也能收到错误（ICMP port unreachable）")

    print("""  UDP 本身没有「对方不存在」的反馈机制, 但网络上还有 ICMP。
  如果包送到一台活着的机器, 而那个端口没人监听, 对方的 IP 层会回一个
  ICMP "port unreachable"。Linux/macOS 会把这条 ICMP 关联到你的 socket:
      * 如果用 connect() 过的 UDP socket 再 send, 下一次收发会抛 ConnectionRefusedError
      * 如果没 connect(), 错误通常被内核丢掉, 你什么都收不到
  这也解释了「为什么 UDP 客户端最好 connect 一下」: 只有 connect 过的 socket 才能
  感知到这个错误, 否则你会一直傻等超时。
""")
    dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()          # 端口现在已经没人监听了

    conn_dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    conn_dead.connect(("127.0.0.1", dead_port))
    conn_dead.settimeout(2)
    try:
        conn_dead.send("有人吗?".encode("utf-8"))
        data = conn_dead.recv(65535)
        print(f"  竟然收到回应: {data!r}")
    except ConnectionRefusedError as exc:
        print(f"  收到 ConnectionRefusedError: {exc}")
        print("      <- 这就是 ICMP port unreachable 被内核翻译过来的结果, 说明 UDP 也不是完全瞎")
    except socket.timeout:
        print("  超时了（这次 ICMP 没回来, UDP 的错误反馈本来就是「尽力而为」的）")
    finally:
        conn_dead.close()

    # ---------------------------------------------------------------- 收尾
    stop.set()
    server_thread.join(timeout=2)
    client.close()
    connected.close()

    title("本课小结")
    print("""1. UDP: 无连接、不可靠、保留消息边界、开销小、延迟低。
2. 服务端只需 bind + recvfrom/sendto, 没有 listen/accept。
3. UDP 的 connect() 不握手, 只是绑定默认对端, 顺便让你能收到 ICMP 错误。
4. 丢包在本机就能复现: 缓冲区满 = 静默丢弃。
5. 数据报最大 65507 字节载荷, 但超过 1472 就要分片, 实际建议 <= 1200。
6. 取舍: 要低延迟、能容忍丢包 -> UDP; 要可靠有序 -> TCP（或者自己在 UDP 上造一个 QUIC）。
7. 下一课: 用 UDP 手写一个 DNS 查询报文, 看看真实协议长什么样。
""")
    print("下一课: 03_udp/demo_09_dns_query_packet.py")


if __name__ == "__main__":
    main()

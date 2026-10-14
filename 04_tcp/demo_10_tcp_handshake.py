#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_10_tcp_handshake.py -- 第 10 课：TCP 三次握手，用代码把它看穿
=================================================================

【原理：TCP 头长什么样】
    TCP 在 IP 之上, 报文头固定 20 字节（不含选项）, 字段依次是:

      +---------------------------------------------------+
      | 源端口 (2) | 目的端口 (2)                            |  <- 定位进程（第 5 课）
      | 序号 seq (4)                                        |  <- 我这份数据的第一个字节编号
      | 确认号 ack (4)                                      |  <- 我期望你下次发的编号
      | 数据偏移(4b) | 保留(3b) | NS CWR ECE URG ACK PSH RST SYN FIN (9b) |  <- 标志位
      | 窗口大小 (2) | 校验和 (2) | 紧急指针 (2)               |
      +---------------------------------------------------+
      序号和确认号是 TCP 一切可靠性的基础:
        它把「字节流」切成可编号的片段, 于是能确认收到、能重传缺失、能排序、能去重。

【三次握手：解决什么问题】
    双方要在「互不知情」的情况下, 就 4 件事达成一致:
        1. 双方都能收到对方的数据（双向可达）
        2. 双方的初始序号 ISN 是多少
        3. 双方的接收窗口（还能收多少）
        4. 选项协商（MSS、窗口扩大、SACK、时间戳）
    两个包不够（无法确认「对方也收到了我的确认」）, 三个包刚好, 这就是三次握手的由来。

        客户端                                   服务端
        ── SYN  seq=x ─────────────────────────►   (状态: LISTEN -> SYN_RCVD)
        ◄───────────── SYN  seq=y, ACK=x+1 ──     (服务端把连接放进 SYN 队列)
        ── ACK  ack=y+1 ──────────────────────►   (状态: SYN_RCVD -> ESTABLISHED, 进 accept 队列)
        此时双方都是 ESTABLISHED, 应用层才知道连接可用（accept 返回）

    注意: 第三次 ACK 之后、应用层 accept 之前, 连接其实已经建立完好了。
    所以即使你的程序忙没来得及 accept, 客户端也能连上（在内核队列里排队）。

【本课路线】
    1. 用 netstat 观察 LISTEN / SYN_SENT / ESTABLISHED 三种真实状态
    2. 同一连接的两个视角（客户端看 vs 服务端看）
    3. 连接超时到底等了多久、异常是什么
    4. backlog: 内核替你排队, 队列满了会怎样
    5. 完整的 TCP 状态机速查

运行：python3 demo_10_tcp_handshake.py
"""

from __future__ import annotations

import queue
import socket
import subprocess
import threading
import time


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def sysctl_value(name: str) -> str:
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True,
                              errors="replace", timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def tcp_lines(port: int) -> list[str]:
    """从 netstat 里筛出与本端口相关的行（用于观察状态）。"""
    try:
        out = subprocess.run(["/usr/sbin/netstat", "-an", "-f", "inet", "-p", "tcp"],
                             capture_output=True, text=True, errors="replace", timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln.strip() for ln in out.splitlines() if f".{port} " in ln or f".{port}\t" in ln or ln.rstrip().endswith(f".{port}" )]


def states_for_port(port: int) -> dict[str, int]:
    """统计某个端口出现在哪些连接状态里。"""
    counter: dict[str, int] = {}
    for ln in tcp_lines(port):
        parts = ln.split()
        if len(parts) >= 3 and parts[-1] in ("LISTEN", "ESTABLISHED", "SYN_SENT", "SYN_RCVD",
                                            "FIN_WAIT_1", "FIN_WAIT_2", "CLOSE_WAIT",
                                            "TIME_WAIT", "CLOSING", "LAST_ACK", "CLOSED"):
            counter[parts[-1]] = counter.get(parts[-1], 0) + 1
    return counter


def main() -> None:
    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：用 netstat 看真实状态迁移")

    accepted: queue.Queue = queue.Queue()
    ready = threading.Event()
    stop = threading.Event()
    holder: dict = {}

    def server() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(16)                       # listen 之后进入 LISTEN 状态, 内核开始替我们收 SYN
        holder["port"] = srv.getsockname()[1]
        ready.set()
        srv.settimeout(0.3)
        while not stop.is_set():
            try:
                conn, peer = srv.accept()    # accept 只是「从内核已有连接队列里取一个」
                accepted.put((conn, peer))
            except socket.timeout:
                continue
            except OSError:
                break
        srv.close()

    t = threading.Thread(target=server, daemon=True)
    t.start()
    ready.wait(timeout=5)
    port = holder["port"]

    print(f"  服务端已 listen 在 127.0.0.1:{port}")
    print(f"  此刻 netstat 看到的状态: {states_for_port(port)}")
    print("""
  LISTEN 的含义: 内核在这个 (地址, 端口) 上等着 SYN, 与你是否调用 accept 无关。
  所以「服务已启动但还没 accept」时, 客户端照样能连上, 只是可能在队列里排队。
""")

    # 观察 SYN_SENT: 连一个「不会响应」的目标, 让这个瞬态状态停留得久一点
    print("  下面尝试连接一个不会有响应的地址, 用来抓住 SYN_SENT 这个瞬间状态:")
    for target in [("192.0.2.1", 80), ("10.255.255.1", 80)]:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setblocking(False)             # 非阻塞 connect: 立刻返回, 状态留在内核里
        try:
            probe.connect_ex(target)
        except OSError as exc:
            print(f"    {target} 立刻失败: {exc}")
            probe.close()
            continue
        found = set()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            found |= set(states_for_port(probe.getsockname()[1]))
            if "SYN_SENT" in found:
                break
            time.sleep(0.1)
        probe.close()
        print(f"    目标 {str(target):<20} 观察到的状态: {sorted(found) or '(没抓到)'}")
        if "SYN_SENT" in found:
            print("""      <- 抓到 SYN_SENT: 说明「SYN 已发出、还没收到 SYN+ACK」。
         正常连接时这个状态只存在几毫秒（本地回环上更短）, 所以平时根本看不到。
         如果大量连接卡在 SYN_SENT, 说明对端没响应（服务挂了/端口被 DROP/路由不通）。""")
            break

    # 正常连接: 观察双方都进入 ESTABLISHED
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.connect(("127.0.0.1", port))
    time.sleep(0.2)
    conn, peer = accepted.get(timeout=3)
    print(f"\n  正常连接建立后（客户端 connect 返回即三次握手完成）:")
    print(f"    客户端视角: 本地端 {client.getsockname()} <-> 对端 {client.getpeername()}")
    print(f"    服务端视角: 本地端 {conn.getsockname()} <-> 对端 {conn.getpeername()}")
    print(f"    netstat 里端口 {port} 的状态: {states_for_port(port)}")
    print("""
  同一根连接的两个视角是「镜像」的:
      客户端 getpeername() == 服务端 getsockname()
      客户端 getsockname() == 服务端 getpeername()
  五元组在两侧是同一组值, 但本端/对端的角色互换了。这是读抓包结果的基本功。
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：序号与确认号在干什么")

    print("""  三次握手里每方都送出一个初始序号（ISN）, 之后所有字节都有编号。
  假设客户端 ISN=1000, 服务端 ISN=5000, 握手完成后:

      客户端已发送: 字节 1000 (那个 SYN 自身占 1 个序号) -> 下一个字节是 1001
      客户端期望收到: 从 5000 开始 -> 所以握手第三次的 ACK 号 = 5001

  接着客户端发 10 字节数据:
      seq=1001, len=10  ->  服务端回 ACK=1011（表示"1010 及之前的都收到了, 我要 1011"）

  这就是 TCP 可靠性的全部秘密:
    * 丢包 -> 对方不发对应的 ACK -> 超时重传
    * 乱序 -> 按 seq 重排后再交给应用层
    * 重复 -> 按 seq 去重
    * 流量控制 -> 每个报文都带「窗口大小」, 告诉对方"我还能收多少字节", 收不下了就通告 0
  所以 TCP 不需要应用层操心这些, 但也因此无法保留「消息边界」（第 12 课的粘包问题）。
""")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：连接超时（连不上时到底等了多久）")

    print("""  内核在发出 SYN 后如果没收到响应, 会重传 SYN, 每次间隔翻倍:
      1s, 2s, 4s, 8s, 16s, 32s ... 累计到系统上限才放弃。
  平时我们绝不会等这么久, 所以要自己设超时:
""")
    keepinit = sysctl_value("net.inet.tcp.keepinit")
    if keepinit:
        print("  本机实测这个上限:")
        print(f"    net.inet.tcp.keepinit = {keepinit} ms  -> 连接建立阶段最多等 "
              f"{int(keepinit) / 1000:.0f} 秒（macOS 用这一个旋钮控制）")
        print("    Linux 上对应的旋钮叫 net.ipv4.tcp_syn_retries, 默认 6 次重传 ≈ 127 秒")
        print()
    unreachable = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    unreachable.settimeout(1.0)          # 注意: settimeout 只影响连接建立和数据收发, 不改变内核重传
    start = time.perf_counter()
    try:
        unreachable.connect(("192.0.2.1", 80))
        print("  竟然连上了（这个地址理论上不该有服务）")
    except socket.timeout:
        print(f"  连 192.0.2.1:80 -> socket.timeout（我们自己设的 1 秒到了就放弃, "
              f"实际等了 {time.perf_counter() - start:.2f}s）")
        print("""      <- 超时异常不是内核放弃, 是你的 timeout 到了。内核可能还在后台重传 SYN,
         所以生产代码里「连接超时」要设得比业务超时更短（常见 1~3 秒）, 并配重试。""")
    except OSError as exc:
        print(f"  连 192.0.2.1:80 -> {type(exc).__name__}: {exc}（等了 {time.perf_counter() - start:.2f}s）")
    finally:
        unreachable.close()

    refused = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    refused.settimeout(1.0)
    start = time.perf_counter()
    try:
        refused.connect(("127.0.0.1", 1))        # 本机 1 号端口没人监听
    except ConnectionRefusedError:
        print(f"  连 127.0.0.1:1 -> ConnectionRefusedError（只等了 {time.perf_counter() - start:.4f}s）")
        print("""      <- 这是 RST: 对方主机活着, 但那个端口没有监听者, 立刻回一个复位包。
         所以「连接被拒绝」和「连接超时」是两种完全不同的故障:
             拒绝   = 网络通、主机在、端口没人听（或防火墙 reject）
             超时   = 包丢了/被 DROP/主机不在/路由不通（最难排查的一种）""")
    except OSError as exc:
        print(f"  连 127.0.0.1:1 -> {type(exc).__name__}: {exc}")
    finally:
        refused.close()

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：backlog，内核替你排的队")

    print("""  服务端 listen(N) 里的 N 就是 backlog: 已完成握手的连接在「accept 队列」里等你的
  accept() 来取。队列满了之后:
      Linux: 默认丢弃新的 SYN（客户端会超时重试）
      macOS/BSD: 也会有类似行为, 但具体阈值和平台实现有关（本机实测见下）
  这说明两件重要的事:
    1. 「连接建立完成」不等于「业务已经处理」, 把耗时逻辑放在 accept 之后会撑爆队列
    2. backlog 设得太小, 突发流量会把连接丢掉; 设得太大也只是缓冲, 不能提高处理能力
""")

    backlog_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    backlog_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    backlog_server.bind(("127.0.0.1", 0))
    backlog_server.listen(1)                 # 故意设成最小的 backlog
    bl_port = backlog_server.getsockname()[1]
    print(f"  起一个 listen(1) 且「故意不 accept」的服务端: 127.0.0.1:{bl_port}")

    attempts = []
    for i in range(6):
        c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        c.settimeout(1.0)
        try:
            c.connect(("127.0.0.1", bl_port))
            attempts.append((i + 1, "已建立（在内核队列里排队）"))
            c.close()
        except socket.timeout:
            attempts.append((i + 1, "超时（队列满, SYN 被丢弃）"))
            c.close()
        except OSError as exc:
            attempts.append((i + 1, f"{type(exc).__name__}"))
            c.close()

    for n, result in attempts:
        print(f"    第 {n} 次连接: {result}")
    established = sum(1 for _, r in attempts if r.startswith("已建立"))
    print(f"\n  在 listen(1) 下, 有 {established}/6 次连接成功进入内核队列（本平台实测, 仅供参考）")

    drained = 0
    backlog_server.settimeout(0.3)
    while True:
        try:
            conn, _ = backlog_server.accept()
            conn.close()
            drained += 1
        except (socket.timeout, OSError):
            break
    print(f"  之后 accept() 逐个取走, 一共取到 {drained} 个连接（它们一直在队列里等我）")
    backlog_server.close()

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：TCP 状态机速查")

    print("""  客户端视角                          服务端视角
  ─────────────────────────────       ─────────────────────────────
  CLOSED                              CLOSED
    │ connect() 发 SYN                   │ bind + listen
    ▼                                   ▼
  SYN_SENT ──── 收到 SYN+ACK ───┐     LISTEN
    │                          │       │ 收到 SYN, 回 SYN+ACK
    │ 发出 ACK                 │       ▼
    ▼                          ▼     SYN_RCVD
  ESTABLISHED ◄────────────────┘       │ 收到 ACK
    │ 双向收发数据                     ▼
    │                               ESTABLISHED
    │ close() / shutdown             │ 收到 FIN, 回 ACK
    ▼                                 ▼
  FIN_WAIT_1 ── 收到 ACK ──► FIN_WAIT_2   CLOSE_WAIT   <- 危险信号! 你还没 close()
    │                          │          │ 调 close() 发 FIN
    │                          │          ▼
    │                          │        LAST_ACK ── 收到 ACK ──► CLOSED
    ▼                          ▼
  TIME_WAIT ◄── 收到对方的 FIN, 回 ACK                （主动关闭方最后待的地方）
    │ 等 2*MSL（macOS 默认 15s*2 = 30s）
    ▼
  CLOSED

  两个必须记住的结论:
    * TIME_WAIT 只出现在「主动关闭」的一方。谁先 close 谁进 TIME_WAIT。
    * CLOSE_WAIT 只出现在「被动关闭」的一方, 它意味着: 对方要关了, 而我的代码
      还没调用 close()。这个状态长期存在 = 你的代码有 bug（漏关连接）。
""")

    client.close()
    conn.close()
    stop.set()
    t.join(timeout=2)

    title("本课小结")
    print("""1. TCP 头里的 seq/ack/窗口 是可靠传输的全部机制; 应用层不用管, 但也失去了消息边界。
2. 三次握手 = 双向可达性 + 序号同步 + 窗口/选项协商, 两个包不够, 三个刚好。
3. 状态是排障的通用语言: LISTEN/ESTABLISHED/TIME_WAIT/CLOSE_WAIT 各自指向不同问题。
4. 「连接被拒绝」和「连接超时」含义完全不同, 处理方式也不同。
5. backlog 是内核队列长度, 连接建立完成 ≠ 业务已就绪。
6. 下一课: 在 TCP 上真正收发数据, 看清「字节流」的脾气。
""")
    print("下一课: 04_tcp/demo_11_tcp_echo.py")


if __name__ == "__main__":
    main()

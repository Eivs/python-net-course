#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_13_tcp_teardown.py -- 第 13 课：连接怎么结束（四次挥手 / TIME_WAIT / RST）
==============================================================================

【原理：四次挥手】
    关闭是双向的, 所以要四次:

        主动关闭方                                被动关闭方
        ── FIN ───────────────────────────────►   (收到 FIN, 回 ACK, 进入 CLOSE_WAIT)
        ◄───────────────────────────── ACK ──
        (进入 FIN_WAIT_2, 还能继续收数据)
        ◄───────────────────────────── FIN ──    (应用层终于调了 close())
        ── ACK ───────────────────────────────►   (进入 LAST_ACK -> CLOSED)
        (进入 TIME_WAIT, 等 2*MSL, 然后 CLOSED)

    为什么不是三次? 因为 TCP 是全双工的, 一方说「我不发了」不代表「我也不能收」。
    被动的 FIN 要等应用层真正 close() 才会发出, 所以中间可能出现很长时间的
    CLOSE_WAIT（这正好是排查「代码忘了关连接」的窗口）。

【TIME_WAIT 为什么必须存在（不是浪费）】
    主动关闭方在发完最后一个 ACK 后要等 2*MSL（macOS 默认 MSL=15s, 即等 30s）:
      1. 万一我那个 ACK 丢了, 对方会重发 FIN, 我还能再 ACK 一次
         （否则对方会卡在 LAST_ACK 直到超时)
      2. 让这条连接的「迟到报文」在网络上彻底消亡, 不会串进同四元组的新连接
    代价: 这个 (本端IP:端口, 对端IP:端口) 组合在 2MSL 内不能被新连接复用,
         服务端短时间内大量的短连接会导致 TIME_WAIT 堆积（端口耗尽）。
    缓解: 用长连接/连接池、SO_REUSEADDR（重启服务器）、调内核参数。

【本课路线】
    1. 观察 TIME_WAIT 的产生与数量增长（并读出 2*MSL 的真实配置）
    2. 复现 CLOSE_WAIT: 对方要关, 我不关 -> 一眼看出代码 bug
    3. SO_REUSEADDR: 为什么重启服务器必须加它
    4. SO_LINGER=0: 用 RST 代替 FIN（暴力关闭, 会丢数据）
    5. 对端已关闭还继续写 -> BrokenPipeError / ConnectionResetError
    6. 什么时候会产生 RST（速查表）

运行：python3 demo_13_tcp_teardown.py [--wait]     # --wait 会额外观察 30 秒 TIME_WAIT 消失
"""

from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import sys
import time


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def states_of_port(port: int) -> dict[str, int]:
    """统计某端口在各状态下的条数。"""
    netstat = shutil.which("netstat")  # 走 PATH: 降级模式下必须真的找不到
    if not netstat:
        return {}
    try:
        out = subprocess.run(
            [netstat, "-an", "-f", "inet", "-p", "tcp"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    counter: dict[str, int] = {}
    for line in out.splitlines():
        if f".{port} " not in line:
            continue
        parts = line.split()
        if len(parts) >= 3:
            counter[parts[-1]] = counter.get(parts[-1], 0) + 1
    return counter


def sysctl(name: str) -> str:
    try:
        return subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def msl_millis() -> int:
    """读出 MSL（毫秒）。读不到就用 macOS 默认值 15000, 别让示例崩掉。"""
    raw = sysctl("net.inet.tcp.msl")
    try:
        return int(raw)
    except (TypeError, ValueError):
        print(
            f"  （读不到 net.inet.tcp.msl, 按默认 15000ms 计算。原始值: {raw!r}）"
        )
        return 15000


def main() -> None:
    wait_mode = "--wait" in sys.argv

    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：谁关谁负责 —— 观察 TIME_WAIT 的产生")

    msl_ms = msl_millis()
    print(
        f"  本机 net.inet.tcp.msl = {msl_ms} ms, 所以 TIME_WAIT 会持续 2*MSL = {msl_ms * 2 / 1000:.0f} 秒"
    )

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(32)
    port = srv.getsockname()[1]
    print(f"  服务端监听在 127.0.0.1:{port}")

    before = states_of_port(port)
    print(f"  建立连接之前的状态统计: {before}")

    clients = []
    for i in range(8):
        c = socket.create_connection(("127.0.0.1", port))
        conn, _ = srv.accept()
        conn.sendall(b"bye")  # 服务端先说话
        conn.close()  # 服务端主动关闭 -> 它要背 TIME_WAIT
        c.recv(16)
        clients.append(c)  # 客户端故意不关, 让 TIME_WAIT 停住
        time.sleep(0.05)

    time.sleep(0.3)
    after = states_of_port(port)
    print(f"  服务端主动关闭 8 条连接后: {after}")
    print("""
  这张状态表很有意思: netstat 里 Local/Foreign 任一列命中该端口都会被筛出来,
  所以同一份结果里同时出现了两侧的连接, 而且**两边都在等对方**:
    FIN_WAIT_2 x8  -> 服务端（主动关闭方）已发出 FIN 并收到 ACK, 在等对方的 FIN
    CLOSE_WAIT x8  -> 客户端收到了 FIN, 但它的代码还没调 close()（正是第 2 步的主题）
    LISTEN   x1    -> 监听口还在
  注意: 此时还没有 TIME_WAIT —— 因为挥手还没走完。
  等双方都把连接关掉（第 3 步里客户端也会 close）, 主动关闭方才会进入 TIME_WAIT,
  届时你会看到它涨到几十条。

  工程含义:
    * 高并发短连接的服务端如果把关闭主动权握在自己手里,
      就会持续制造 TIME_WAIT, 极端情况下临时端口被耗尽（bind 报 EADDRNOTAVAIL）
    * 反过来, 让客户端先关（服务端被动关闭）就把 TIME_WAIT 推给了客户端
    * 最根本的解法: 用长连接 / 连接池, 而不是调参数
""")
    for c in clients:
        c.close()

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：复现 CLOSE_WAIT（代码 bug 的指纹）")

    srv.settimeout(1.0)
    c2 = socket.create_connection(("127.0.0.1", port))
    conn2, _ = srv.accept()
    print("  客户端关闭连接（发 FIN）:")
    c2.close()
    time.sleep(0.3)
    print(f"    此时服务端端口状态: {states_of_port(port)}")
    print("""    服务端那个 conn 现在处于 CLOSE_WAIT: 它已经收到对方的 FIN 并回了 ACK,
    但**我们的代码还没调用 conn2.close()**, 所以 FIN 一直没发出去。
    这条连接会一直占着内核资源, 直到进程退出。

  看到大量 CLOSE_WAIT 应该怎么想:
      "对方已经关了, 我的代码没关" -> 检查所有 return / 异常分支, 用 with / try-finally 兜住
    这和其他状态的区别:
      TIME_WAIT  -> 正常的、由我主动关闭产生的（等 30 秒自己消失）
      CLOSE_WAIT -> 不正常的、由我忘了 close 产生的（不修代码就永不消失）
      ESTABLISHED 长期空闲 -> 可能是连接泄漏, 也可能是 keep-alive 连接池（要结合业务看）
""")
    conn2.close()
    time.sleep(0.2)
    print(
        f"  调用 conn2.close() 之后: {states_of_port(port)}   <- CLOSE_WAIT 立刻消失"
    )

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：SO_REUSEADDR，重启服务器时的救命选项")

    print("""  场景: 服务端进程重启。旧的监听 socket 已经释放, 但旧连接还在 TIME_WAIT,
  它占着「本地 addr:port」这个组合。此时新的 bind 可能直接失败。
  下面真的模拟一次重启: 先关掉旧服务端, 再造一批 TIME_WAIT, 然后重新 bind。
""")
    # 制造一批 TIME_WAIT 记录（主动关闭 8 条）
    for _ in range(8):
        c = socket.create_connection(("127.0.0.1", port))
        conn, _ = srv.accept()
        conn.close()
        c.close()
    srv.close()  # <= 模拟「服务端进程退出」, 监听 socket 释放
    time.sleep(0.3)
    print(f"  旧服务端已关闭, 现在端口 {port} 的状态: {states_of_port(port)}")

    naive = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        naive.bind(("127.0.0.1", port))
        print(
            "  不加 SO_REUSEADDR 直接 bind: 成功了（本平台可能允许, 或 TIME_WAIT 已清空）"
        )
    except OSError as exc:
        print(
            f"  不加 SO_REUSEADDR 直接 bind: 失败 -> {type(exc).__name__}: {exc}"
        )
        print(
            "      <- 明明没有进程在监听了, 却还是 Address already in use, 就是 TIME_WAIT 在占位。"
        )
    finally:
        naive.close()

    friendly = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    friendly.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        friendly.bind(("127.0.0.1", port))
        friendly.listen(32)
        print(
            "  加上 SO_REUSEADDR 后 bind 同一个端口: 成功  <- 这就是每个服务端都该有的那一行"
        )
        srv = friendly  # 用这个「重启后」的服务端继续后面的实验
        print("""
  SO_REUSEADDR 到底允许多做什么（容易误解, 说清楚）:
    * 允许 bind 一个「有 TIME_WAIT 连接的地址」-> 重启服务器不再报错（主要用途）
    * 在 Linux 上还允许 bind 通配地址和具体地址的组合（0.0.0.0:80 与 127.0.0.1:80 共存）
    * 它**不允许**两个 socket 同时监听同一个 addr:port（那是 SO_REUSEPORT 的活, 见第 5 课）,
      所以上面的实验必须先关掉旧监听 socket; 否则无论加不加选项都会失败。
""")
        kept_open = True
    except OSError as exc:
        print(f"  加上 SO_REUSEADDR 后仍然失败: {type(exc).__name__}: {exc}")
        kept_open = False
    if not kept_open:
        raise SystemExit(1)

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：SO_LINGER=0，用 RST 代替 FIN")

    print("""  默认 close() 是「优雅关闭」: 内核会把缓冲区里剩下的数据发完, 再发 FIN。
  但把 SO_LINGER 设成 (onoff=1, linger=0) 再 close(), 内核会立刻丢弃缓冲区,
  直接发 RST（复位）。对端看到的是「连接被重置」, 而不是正常的 FIN。

  什么时候会这么干: 检测到协议错误要立即断开、要拒绝不合法的连接、
  或者不想让对方知道我「正常关闭了」。代价是对方可能丢数据、且对端会看到刺眼的报错。
""")
    linger_client = socket.create_connection(("127.0.0.1", port))
    linger_server_conn, _ = srv.accept()
    linger_client.setsockopt(
        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
    )
    print(
        f"  客户端设置 SO_LINGER={struct.unpack('ii', linger_client.getsockopt(socket.SOL_SOCKET, socket.SO_LINGER, 8))} 然后 close()"
    )
    linger_client.close()
    time.sleep(0.2)

    linger_server_conn.settimeout(2)
    try:
        data = linger_server_conn.recv(1024)
        if data == b"":
            print("  服务端读到 EOF（正常 FIN）, 与预期不符")
        else:
            print(f"  服务端收到数据: {data!r}")
    except ConnectionResetError as exc:
        print(f"  服务端 recv -> ConnectionResetError: {exc}")
        print(
            "      <- 这就是 RST 的效果: 服务端立刻知道「对方强制断开了」, 不需要等超时。"
        )
    except socket.timeout:
        print("  服务端 recv 超时")
    finally:
        linger_server_conn.close()

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：对端已关闭，我还继续写会怎样？")

    dead_client = socket.create_connection(("127.0.0.1", port))
    dead_conn, _ = srv.accept()
    dead_conn.close()  # 服务端立刻关闭（发 FIN）
    time.sleep(0.3)
    print("  服务端已 close, 客户端却还想发数据:")
    for attempt in range(1, 5):
        try:
            sent = dead_client.send("还有人在吗?".encode("utf-8"))
            print(
                f"    第 {attempt} 次 send 返回 {sent} 字节（看起来成功, 其实只是写进了内核缓冲）"
            )
        except BrokenPipeError as exc:
            print(f"    第 {attempt} 次 send -> BrokenPipeError: {exc}")
            print(
                "        <- 内核已经收到 RST, 再写就直接报错。管道概念上的「对端没了」。"
            )
            break
        except ConnectionResetError as exc:
            print(f"    第 {attempt} 次 send -> ConnectionResetError: {exc}")
            break
        except OSError as exc:
            print(f"    第 {attempt} 次 send -> {type(exc).__name__}: {exc}")
            break
        time.sleep(0.3)

    print("""
  这个实验解释了一个经典现象: 「日志显示消息发出去了, 但对方说没收到」。
  原因是 send 成功只表示「字节进了本机内核缓冲区」, 不表示对方收到了。
  要确认真实送达, 必须有应用层的确认（ACK/响应报文/回调）。
  另外注意: 第一次 send 往往成功, 因为 FIN 还没被处理完; 第二次才报错。
  所以生产代码不能靠「send 成功」判断连接可用, 要做心跳。
""")
    dead_client.close()
    srv.close()

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：什么时候会产生 RST（速查表）")

    print("""  对端看到 RST 的常见场景:
    1. 向「没有监听者的端口」发起连接（第 7 课的端口扫描就是靠它判断 closed）
    2. 向一条「已经彻底关闭」的连接发数据
    3. 设置了 SO_LINGER=0 后 close()
    4. 收到不合法的报文（比如 seq 完全对不上、缺少 ACK 的乱序包）
    5. 服务端进程被 kill -9 后, 客户端继续发数据（内核回了 RST）
    6. SYN 队列/accept 队列满了, 内核可能用 RST 拒绝新连接（看系统配置）

  RST 在代码里的表现（Python）:
    ConnectionResetError（对端复位）/ BrokenPipeError（再写已断的连接）
    / ConnectionAbortedError / ConnectionRefusedError（连接阶段就被拒）
  全都继承自 OSError, 所以捕获 OSError 能兜住全部, 但要按类型分别处理（要不要重试）。
""")

    if wait_mode:
        title("附：等待 TIME_WAIT 自然消失（--wait 模式, 约 35 秒）")
        print(f"  当前端口 {port} 状态: {states_of_port(port)}")
        print("  等待中, 每 5 秒打印一次...")
        for i in range(7):
            time.sleep(5)
            print(f"    t+{(i + 1) * 5:>2}s: {states_of_port(port)}")
        print("  TIME_WAIT 消失 -> 这正是 2*MSL 到期。")

    title("本课小结")
    print("""1. 关闭是四次挥手（FIN/ACK/FIN/ACK）, 因为 TCP 是全双工的。
2. TIME_WAIT 只属于主动关闭方, 持续 2*MSL, 用来保证最后那个 ACK 能重发、迟到包不串号。
3. CLOSE_WAIT 是「对方关了、我没关」的指纹, 长期存在 = 代码 bug。
4. SO_REUSEADDR 让服务器能在 TIME_WAIT 存在时重新 bind, 服务端必备。
5. SO_LINGER=0 会发 RST, 是「暴力断开」, 会丢数据。
6. send 成功 ≠ 对方收到, 可靠性要应用层确认 / 心跳。
7. 下一课: 深入常用 socket 选项（Nagle、缓冲区、超时、keepalive）的实测效果。
""")
    print("下一课: 04_tcp/demo_14_tcp_socket_options.py")


if __name__ == "__main__":
    main()

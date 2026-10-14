#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_07_port_scan.py -- 第 7 课：端口扫测的原理（只扫自己的机器）
================================================================

【原理】
    怎么判断一个端口是「开着」还是「关着」？
    别指望有什么魔法, 就是老老实实发一个 TCP SYN 过去, 看对方怎么回:

        发的包            对方回的包                      结论
        TCP SYN    ->     TCP SYN+ACK（完成握手）          端口开放
        TCP SYN    ->     TCP RST（复位）                  端口关闭（没有进程监听）
        TCP SYN    ->     什么都不回（被防火墙 DROP）      无法判断 = 被过滤

    所以客户端能区分三种结果, 这就是所有端口扫描器（nmap 等）的核心。
    这就是 connect() 扫描: 因为它走的是完整的三次握手（操作系统替你完成）,
    所以不需要 root 权限, 但速度慢, 而且会留下完整连接日志。

    还有更"隐蔽"的方式（本课不实现）:
        SYN 半开扫描（发 SYN, 收到 SYN+ACK 立刻发 RST, 不完成握手）: 需要 root 造包
        UDP 扫描（UDP 无连接, 只能靠 ICMP port unreachable 判断）: 慢且不可靠

【⚠️ 法律与道德】
    只对你自己拥有的主机/你有书面授权的目标做扫描。
    对他人网络扫描在大多数司法辖区是违法行为, 也会立刻触发 IDS/EDR 告警。
    本示例默认只扫 127.0.0.1（你自己的机器）, 这是安全且完全合法的。

【本课路线】
    1. 三种结果的实测: 开放 / 关闭 / 被丢弃（超时）
    2. 并发扫描 127.0.0.1 的 1-1024 端口, 找出真正在监听的
    3. 计算扫描耗时, 解释「并发 vs 超时」的取舍
    4. 顺手做一个「服务识别」: 从 banner 猜协议

运行：python3 demo_07_port_scan.py
"""

from __future__ import annotations

import errno
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

TARGET = "127.0.0.1"  # 只扫本机！改这里之前请确认你有授权。
CONNECT_TIMEOUT = 0.4  # 每个端口的等待上限（秒）
MAX_WORKERS = 128  # 并发度


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def probe_port(
    host: str, port: int, timeout: float = CONNECT_TIMEOUT
) -> tuple[int, str]:
    """对单个端口做一次探测, 返回 (端口, 结果)。

    结果有三种:
        open       connect_ex 返回 0  -> 三次握手成功, 有程序在监听
        closed     返回 ECONNREFUSED  -> 对方回了 RST, 说明主机在, 但没人监听
        filtered   超时                -> 包被丢了, 无法判断（防火墙 DROP）
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        code = sock.connect_ex((host, port))  # 不抛异常, 返回 errno
        if code == 0:
            return port, "open"
        if code == errno.ECONNREFUSED:
            return port, "closed"
        if code in (errno.EHOSTUNREACH, errno.ENETUNREACH):
            return port, "unreachable"
        return port, f"error:{errno.errorcode.get(code, code)}"
    except socket.timeout:
        return port, "filtered"
    except OSError as exc:
        return port, f"error:{errno.errorcode.get(exc.errno, exc.errno)}"
    finally:
        sock.close()


def grab_banner(host: str, port: int, timeout: float = 1.0) -> str:
    """连上去并尝试读一点数据, 用来自动识别服务（HTTP/SMTP/SSH 都会主动说话）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        # HTTP 服务器不会主动说话, 需要我们先问一句
        if port in (80, 8000, 8080, 8888, 3000):
            sock.sendall(b"HEAD / HTTP/1.0\r\n\r\n")
        data = sock.recv(128)
        return (
            data.decode("utf-8", "replace").splitlines()[0][:80]
            if data
            else "(无 banner, 静默服务)"
        )
    except OSError as exc:
        return f"(读取失败: {type(exc).__name__})"
    finally:
        sock.close()


def main() -> None:
    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：三种探测结果的实证")

    # 起一个真实服务, 保证有一个「确定开放」的端口
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    open_port = server.getsockname()[1]

    accept_stop = threading.Event()

    def acceptor() -> None:
        server.settimeout(0.5)
        while not accept_stop.is_set():
            try:
                conn, _ = server.accept()
                conn.close()
            except (socket.timeout, OSError):
                continue

    acceptor_thread = threading.Thread(target=acceptor, daemon=True)
    acceptor_thread.start()

    print(f"  本地已启动一个测试服务: 127.0.0.1:{open_port}\n")
    cases = [
        (open_port, "有进程在监听 -> 期待 open"),
        (1, "1 号端口基本不可能有人监听 -> 期待 closed (RST)"),
        (65001, "高端口, 通常没人监听 -> 期待 closed (RST)"),
    ]
    for port, note in cases:
        got_port, result = probe_port(TARGET, port)
        print(f"  端口 {got_port:<6} -> {result:<10} {note}")

    print("""
  被丢弃(filtered)的情况需要一台「会丢包」的目标才有意义, 本课就不故意制造超时了。
  想亲眼看到它: 把 TARGET 指向一个不存在的内网地址（如 10.255.255.1）,
  你会看到每个端口都卡满 CONNECT_TIMEOUT 才返回 -> 这正是扫描器最怕的情况,
  因为「没有回应」无法区分「端口关着」和「包被防火墙扔了」。
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：并发扫描 127.0.0.1 的 1-1024 端口")

    start = time.perf_counter()
    results: dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for port, result in pool.map(
            lambda p: probe_port(TARGET, p), range(1, 1025)
        ):
            results[port] = result
    elapsed = time.perf_counter() - start

    opened = sorted(p for p, r in results.items() if r == "open")
    other = {r for r in results.values() if r not in ("open", "closed")}

    print(
        f"  扫完 1024 个端口, 耗时 {elapsed:.2f} 秒（并发度 {MAX_WORKERS}, 单端口超时 {CONNECT_TIMEOUT}s）"
    )
    print(
        f"  结果统计: open={len(opened)}  closed={sum(1 for r in results.values() if r == 'closed')}  其他={other or '无'}"
    )
    print(f"  开放端口: {opened}")

    print(f"""
  为什么这么快? {MAX_WORKERS} 个线程同时发 SYN, 而不是一个接一个等。
  最坏情况耗时约 = 端口数 / 并发度 * 超时 = {1024}/{MAX_WORKERS}*{CONNECT_TIMEOUT} ≈ {1024 / MAX_WORKERS * CONNECT_TIMEOUT:.1f} 秒。
  这也解释了扫描器的两个调参方向: 提高并发（更快但更吵、更容易被限速）,
  或降低超时（更快但可能漏掉慢响应端口）。
""")
    if other:
        print(f"  出现的其他结果: {other}（比如 filtered 表示包被丢弃）\n")
    print("""  注意: 范围扫描 1-1024 在本机上大概率一个开放端口都没有, 因为
  macOS 的系统服务都用高位端口。真实扫描很少真的去扫 1-65535,
  而是先扫「常见端口清单」, 下面就是这种做法。\n""")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：换成「常见端口清单」再扫一次（真实扫描器的做法）")

    common_ports = [
        22,
        53,
        80,
        123,
        443,
        1100,
        3000,
        3306,
        5000,
        5432,
        6379,
        7000,
        8000,
        8080,
        8888,
        9000,
        27017,
    ]
    targets = sorted(
        set(common_ports) | {open_port}
    )  # 把自己起的测试服务也放进去
    found: list[int] = []
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=32) as pool:
        for port, result in pool.map(lambda p: probe_port(TARGET, p), targets):
            if result == "open":
                found.append(port)
    print(
        f"  扫了 {len(targets)} 个常见端口, 耗时 {time.perf_counter() - start:.2f} 秒"
    )
    print(f"  开放的端口（含我们刚起的 {open_port}）: {found}")
    print("""
  这个列表就是各种扫描器默认字典的雏形: 22(SSH) 80/443(Web) 3306(MySQL)
  5432(Postgres) 6379(Redis) 27017(Mongo) 5000/7000(macOS 系统服务) ...
  实战经验: 先扫字典（秒级拿到结论）, 再根据需要扩大范围。
  反过来说, 作为服务提供方, 你应该定期扫自己的机器, 看有没有多余的监听。
""")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：给开放端口做一次「服务识别」")

    if not found:
        print("  （本机没扫到开放端口, 跳过）")
    for port in found[:8]:
        banner = grab_banner(TARGET, port)
        print(f"  端口 {port:<6} banner: {banner}")
    print("""
  banner 就是服务连上后主动（或被动）吐出来的第一行内容。
    以 "SSH-2.0-" 开头 -> sshd        以 "220 " 开头 -> SMTP/FTP
    以 "HTTP/" 开头     -> HTTP 服务  静默             -> 可能是数据库/自定义协议
  真实工具（nmap -sV）会结合 banner、TLS 握手特征、响应时序做更复杂的指纹匹配。
  对我们来说, 记住原理就够了: 协议是「约定的字节序列」, 能对上就是能识别。
""")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：扫描器的工程要点（以及为什么别乱扫）")

    print("""  1. 一定要设超时。不设超时的扫描器遇到 DROP 会永远卡住。
  2. 一定要限并发。无限制开线程会把自己机器的 fd 耗尽
     （macOS 默认 ulimit -n 是 256~10240, 每个探测至少占 1 个 fd）。
  3. 一定要区分「关闭」和「被过滤」, 前者是信息, 后者只是沉默。
  4. 生产环境做「端口存活检查」时, 更该用应用层健康检查（GET /healthz）,
     而不是 TCP 连接成功就算健康。连接成功只说明进程在, 不代表它还能干活。
  5. 反过来, 作为服务提供方, 你应该主动做资产盘点:
     定期扫描自己机器的全部端口, 看看有没有忘记关掉的调试接口。
     未授权的对外开放端口是数据泄露最常见的入口。
""")

    accept_stop.set()
    acceptor_thread.join(timeout=2)
    server.close()

    title("本课小结")
    print("""1. 端口状态靠 SYN 的回应区分: SYN+ACK=开, RST=关, 无回应=被过滤。
2. connect() 扫描不需要 root, 但慢且会在对端留日志。
3. 并发 + 超时是扫描器的两个核心参数, 二者都在做取舍。
4. 只扫自己的资产, 扫描他人网络是违法行为。
5. 下一章进入传输层协议本身: 先看「无连接的 UDP」, 再看「有连接的 TCP」。
""")
    print("下一课: 03_udp/demo_08_udp_echo.py")


if __name__ == "__main__":
    main()

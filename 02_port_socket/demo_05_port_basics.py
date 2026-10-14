#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_05_port_basics.py -- 第 5 课：端口，怎么找到「机器里的那个进程」？
=====================================================================

【原理】
    上一章解决了「找到机器」(IP)。一台机器上跑着几十上百个程序,
    浏览器、SSH、数据库都在收数据, 内核凭什么把某个包交给正确的程序?
    答案: 端口号。

        端口 = 16 位无符号整数, 范围 0 ~ 65535
              写在 IP 后面, 形如 93.184.216.34:443

    于是寻址变成两级:
        IP   -> 哪台机器
        端口 -> 机器上的哪个「套接字」(socket)

【五行组: 内核区分连接的唯一依据】
    协议(TCP/UDP) + 源IP + 源端口 + 目的IP + 目的端口
    这 5 个值只要有一个不同, 就是两条不同的连接。
    所以「一个服务器端口能同时服务上万客户端」靠的不是端口多, 而是
    客户端的 (源IP, 源端口) 各不相同。

【端口号的三段划分】
    0            : 特殊值, bind 时表示「随便给我一个空闲端口」
    1 ~ 1023     : 系统端口 (well-known), Unix 上要 root 才能 bind
    1024 ~ 49151 : 注册端口, 给普通服务用 (比如 8080、3306、6379)
    49152 ~ 65535: 临时端口 (ephemeral), 内核给客户端随机分配源端口用

【本课路线】
    1. 端口是 16 位: 用 struct 证明上界
    2. 常见服务端口表 + /etc/services
    3. bind 的地址语义: 0.0.0.0 vs 127.0.0.1 vs 具体网卡 IP
    4. 端口 0: 让内核替你挑一个空闲端口
    5. 端口冲突 EADDRINUSE 与 SO_REUSEPORT
    6. 真实抓出「五元组」: 两个客户端连同一个端口
    7. 用 netstat 验证上面看到的五元组

运行：python3 demo_05_port_basics.py
"""

from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import threading
import time

LOCAL_LAN_IP: str | None = None


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def sysctl_value(name: str) -> str:
    """读一个 sysctl 值; 工具不存在或读不到就返回空串（示例要能在任何平台/环境下跑）。"""
    if not shutil.which("sysctl"):
        return ""
    try:
        return subprocess.run(["sysctl", "-n", name], capture_output=True, text=True,
                              errors="replace", timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def detect_lan_ip() -> str | None:
    """探测本机在局域网/默认路由上的 IP（UDP connect 不产生流量）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 80))
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def main() -> None:
    global LOCAL_LAN_IP
    LOCAL_LAN_IP = detect_lan_ip()

    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：端口是一个 16 位整数（0 ~ 65535）")

    print("  用 struct 打包成 2 字节看看:")
    for port in [0, 80, 443, 8080, 65535]:
        packed = struct.pack("!H", port)          # !H = 大端 16 位无符号
        print(f"    端口 {port:<6} -> 字节 {list(packed)}  = 0x{port:04X}")
    print()
    try:
        struct.pack("!H", 65536)                  # 故意越界
    except struct.error as exc:
        print(f"    端口 65536 -> 打包失败: {exc}")
        print("    这就证明了端口只有 16 位: 最大 65535。端口号是「稀缺资源」的根源。")

    print("""
  为什么端口要在发送时转成网络字节序? 因为端口是 16 位整数, 必须大端。
  伪代码里常见的写法:
      s.bind((host, port))          # Python 帮你转字节序, 不用手动 htons
  C 语言里就要手写: htons(port)。Python 里可以用 socket.htons 亲手验证一下:
""")
    print(f"    socket.htons(0x1F90=8080) = 0x{socket.htons(8080):04X}")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：常见服务端口 & /etc/services")

    services = [("http", "tcp"), ("https", "tcp"), ("ssh", "tcp"), ("domain", "udp"), ("smtp", "tcp"), ("imaps", "tcp")]
    for name, proto in services:
        try:
            port = socket.getservbyname(name, proto)
            print(f"  getservbyname({name!r}, {proto!r}) = {port}")
        except OSError as exc:
            print(f"  getservbyname({name!r}, {proto!r}) 失败: {exc}")

    print("""
  这些名字来自 /etc/services（一份历史悠久的静态文件）。真实开发里更该记住的表:

      22    SSH        53    DNS          80    HTTP        443  HTTPS
      25    SMTP       110   POP3         143   IMAP        123  NTP(udp)
      3306  MySQL      5432  PostgreSQL   6379  Redis       27017 MongoDB
      8000/8080/8888  各种 Web 框架的默认端口
      3000  Node/前端 dev server          5173  Vite

  端口只是「约定」, 不是强制: 把 HTTP 服务跑在 9999 端口完全合法,
  只是所有客户端都得知道这件事。约定让互通成为可能。
""")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：bind 的地址语义，决定「谁能连进来」")

    print(f"""给 socket 绑定监听地址时有三种典型写法, 含义完全不同:

  ("0.0.0.0", 8080)    本机所有网卡的所有 IP 都接受连接（对外服务）
  ("127.0.0.1", 8080)  只有本机回环能连, 外部完全看不到（本地开发最安全）
  ("{LOCAL_LAN_IP or '192.168.x.x'}", 8080) 只在这张网卡的地址上接受连接

  0.0.0.0 不是「一个地址」, 而是「通配/所有」的意思。
""")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))      # 端口 0 = 请内核随便给一个空闲端口
    server.listen(8)
    bound_ip, bound_port = server.getsockname()
    print(f"  实测: bind(('127.0.0.1', 0)) 之后 getsockname() = {server.getsockname()}")
    print(f"        -> 内核分配的端口是 {bound_port}，监听地址是 {bound_ip}")

    # 从回环连 => 成功
    client = socket.socket()
    client.settimeout(2)
    try:
        client.connect(("127.0.0.1", bound_port))
        print(f"  从 127.0.0.1 连 {bound_port}: 连接成功 -> {client.getsockname()}")
    except OSError as exc:
        print(f"  从 127.0.0.1 连 {bound_port}: 失败 {exc}")
    finally:
        client.close()

    # 从本机内网口连同一个端口 => 被拒绝, 因为监听地址不含它
    if LOCAL_LAN_IP and not LOCAL_LAN_IP.startswith("127."):
        probe = socket.socket()
        probe.settimeout(2)
        try:
            probe.connect((LOCAL_LAN_IP, bound_port))
            print(f"  从 {LOCAL_LAN_IP} 连 {bound_port}: 居然成功（说明监听范围更宽）")
        except OSError as exc:
            print(f"  从 {LOCAL_LAN_IP} 连 {bound_port}: 被拒绝({type(exc).__name__}: {exc})")
            print("        <- 同一个端口, 只因为目标 IP 不在监听范围内, 连不上。")
        finally:
            probe.close()
    server.close()

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：端口 0 的妙用，以及端口三段划分")

    first, second = socket.socket(), socket.socket()
    first.bind(("127.0.0.1", 0))
    second.bind(("127.0.0.1", 0))
    p1, p2 = first.getsockname()[1], second.getsockname()[1]
    print(f"  连续两次 bind 端口 0 -> 内核给了 {p1} 和 {p2}（一般都在临时端口区间）")
    print("  临时端口范围（macOS: sysctl net.inet.ip.portrange.*；Linux: /proc/sys/net/ipv4/ip_local_port_range）:")
    range_first = sysctl_value("net.inet.ip.portrange.first")
    range_last = sysctl_value("net.inet.ip.portrange.last")
    if range_first and range_last:
        print(f"    net.inet.ip.portrange.first = {range_first}")
        print(f"    net.inet.ip.portrange.last  = {range_last}")
    else:
        try:
            with open("/proc/sys/net/ipv4/ip_local_port_range", encoding="utf-8") as fh:
                print(f"    ip_local_port_range = {fh.read().strip()}")
        except OSError:
            print("    （本环境读不到临时端口范围, 跳过；macOS 默认是 49152-65535）")
    print("""
  这就是客户端 connect() 时源端口的来源。内核从这段范围里挑一个「当前没被占用」的,
  用完（连接彻底关闭后）还要等 TIME_WAIT 结束才能复用 -> 第 4 章会看到。
  测试代码里写死端口号是常见错误: 端口被占就挂。用 0 让内核分配才是正确姿势。
""")
    first.close()
    second.close()

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：端口冲突 EADDRINUSE 与 SO_REUSEPORT")

    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(4)
    port = occupied.getsockname()[1]

    clash = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    clash.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        clash.bind(("127.0.0.1", port))
        print(f"  第二次 bind 同一个端口 {port}: 成功了（本平台不允许? 看看下面）")
    except OSError as exc:
        print(f"  第二次 bind 同一个端口 {port}: 失败 -> {type(exc).__name__}: {exc}")
        print("        errno.EADDRINUSE: 端口已被占用。这就是「端口冲突」的真面目。")
    finally:
        clash.close()

    if hasattr(socket, "SO_REUSEPORT"):
        shared = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        shared.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        shared.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        try:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            shared.bind(("127.0.0.1", port))
            print(f"  双方都设置 SO_REUSEPORT 后再 bind {port}: 成功")
            print("""        含义: 多个 socket 共享同一个端口, 内核把新连接负载均衡地分给它们。
        这是多进程服务器（如 gunicorn 多 worker、nginx 多进程）的标准做法。
        注意 SO_REUSEADDR 和 SO_REUSEPORT 不是一回事:
            SO_REUSEADDR : 允许 bind 处于 TIME_WAIT 的地址（重启服务器必备）
            SO_REUSEPORT : 允许「同时」有多个 socket 监听同一端口并分摊连接
""")
        except OSError as exc:
            print(f"  SO_REUSEPORT 共享绑定失败: {exc}")
        finally:
            shared.close()
    occupied.close()

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：亲眼看见「五元组」，这是内核区分连接的全部依据")

    ready = threading.Event()
    holder = {}

    def server_side() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        holder["port"] = srv.getsockname()[1]
        ready.set()
        accepted = []
        for _ in range(2):
            conn, peer = srv.accept()
            accepted.append((conn, peer))
            print(f"  [服务端 accept] 新连接来自 {peer}，服务端本地端 {conn.getsockname()}")
        holder["conns"] = accepted
        # 保持连接打开, 让主线程去 netstat 里查
        holder["event"] = threading.Event()
        holder["event"].wait(timeout=8)
        for conn, _ in accepted:
            conn.close()
        srv.close()

    thread = threading.Thread(target=server_side, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    port = holder["port"]
    print(f"  服务端正在 127.0.0.1:{port} 上监听\n")

    clients = []
    for i in range(2):
        c = socket.socket()
        c.connect(("127.0.0.1", port))
        clients.append(c)
        time.sleep(0.1)
        print(f"  [客户端 {i + 1}] 本地端 {c.getsockname()} -> 对端 {c.getpeername()}")

    print(f"""
  观察到的关键点:
    * 两个客户端的「目的端口」都是 {port}（服务端只有这一个监听口）
    * 但它们的「源端口」不同 -> 内核靠源端口把两条连接分开
    * 服务端为每条连接各创建一个新 socket, 它的本地端是 (127.0.0.1, {port})，
      对端是客户端的 (127.0.0.1, 随机端口)
  所以完整五元组是:
      客户端A: TCP 127.0.0.1:srcA -> 127.0.0.1:{port}
      客户端B: TCP 127.0.0.1:srcB -> 127.0.0.1:{port}
  这两条在操作系统里是两个完全独立的连接对象。
""")

    # ---------------------------------------------------------------- 第 7 步
    title("第 7 步：用 netstat 验证（系统视角的五元组）")

    out = subprocess.run(["/usr/sbin/netstat", "-an", "-f", "inet", "-p", "tcp"],
                         capture_output=True, text=True, errors="replace", timeout=10).stdout
    hits = [ln for ln in out.splitlines() if f".{port}" in ln]
    print(f"  在 `netstat -an -f inet -p tcp` 里搜端口 {port} 得到的行:")
    for ln in hits[:8]:
        print(f"    {ln}")
    print("""
  netstat 的列含义（这是理解一切网络排障的基础）:
      Proto  Recv-Q  Send-Q  Local Address      Foreign Address    (state)
      tcp4   0       0       127.0.0.1.{port}   127.0.0.1.61xxx    ESTABLISHED
                              ^Local 就是本地端        ^Foreign 就是对端
  地址里的最后一个点后面就是端口（netstat 用点分隔, lsof 用冒号）。
  Recv-Q / Send-Q 是非 0 的话说明有数据堆在缓冲区里没人读, 是性能问题的典型信号。
""")

    holder.get("event").set() if holder.get("event") else None
    for c in clients:
        c.close()
    thread.join(timeout=3)

    title("本课小结")
    print("""1. 端口是 16 位整数, 和 IP 组合成「IP:端口」, 才能定位到一个进程。
2. 三段划分: 系统端口(0-1023)/注册端口(1024-49151)/临时端口(49152-65535)。
3. bind 的地址决定「谁能连进来」: 0.0.0.0 通配, 127.0.0.1 只本地。
4. 端口 0 = 让内核分配, 写服务/测试代码时优先用它。
5. 内核靠五元组区分连接, 服务端一个监听端口能承载海量连接。
6. SO_REUSEADDR 解决重启时的 TIME_WAIT 占用, SO_REUSEPORT 用于多进程分摊。
7. 下一课: 实战排障, 用 lsof/netstat 找出「谁占了我的端口」。
""")
    print("下一课: 02_port_socket/demo_06_port_observer.py")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_06_port_observer.py -- 第 6 课：实战排障，谁占了我的端口？
==============================================================

【原理】
    这是每个后端工程师都会遇到的场景:
        OSError: [Errno 48] Address already in use
    光看异常没用, 你需要知道「是谁、拿哪个进程、占了哪个地址上的哪个端口」。
    内核把这些信息挂在 /proc（Linux）或 sysctl/netstat（BSD/macOS）里,
    命令行工具只是它的漂亮外壳。

    两个必备工具:
      lsof -nP -iTCP:8080      谁在用 TCP 8080（-n 不解析域名, -P 不解析端口名）
      lsof -nP -iTCP -sTCP:LISTEN   列出所有 TCP 监听者
      netstat -an -f inet -p tcp    列出所有 TCP 连接及其状态

    本课用 Python 调用这些工具并解析输出, 顺便讲清「状态」字段的含义:
      LISTEN       等待别人来连（服务端）
      SYN_SENT     已发出连接请求, 等对方确认（客户端, 瞬间）
      ESTABLISHED  连接已建立, 可以收发数据
      FIN_WAIT_1/2 我发了结束请求, 等对方结束
      CLOSE_WAIT   对方要结束, 我还没调 close()  <- 代码有 bug 的经典信号
      TIME_WAIT    我主动关闭, 等 2MSL 防止老包串进新连接
      CLOSED       彻底结束

【本课路线】
    1. 列出现有监听端口（包括别人的进程）
    2. 自己起一个服务, 用 lsof 认出「是我的 python 进程在监听」
    3. 制造端口冲突, 然后反查出占用者
    4. 统计系统里的连接状态分布（TIME_WAIT 有多少）
    5. 按对端 IP 聚合, 找出「谁在跟我的机器频繁连接」

运行：python3 demo_06_port_observer.py
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
from collections import Counter

MY_PID = os.getpid()


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def sh(cmd: list[str], timeout: float = 10.0) -> list[str]:
    """执行命令并返回非空行列表。"""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def listening_sockets() -> list[str]:
    """所有处于 LISTEN 状态的 TCP socket。"""
    return sh(["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])


def tcp_table() -> list[str]:
    """全部 TCP 连接（含监听）, 用于看状态分布。"""
    return sh(["/usr/sbin/netstat", "-an", "-f", "inet", "-p", "tcp"])


def main() -> None:
    print(f"本示例进程 PID = {MY_PID}（下面 lsof 输出里应该能找到它）")

    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：系统里都有谁在监听端口（lsof -iTCP -sTCP:LISTEN）")

    lines = listening_sockets()
    if not lines:
        print("  lsof 不可用或输出为空，跳过。")
    for ln in lines[:14]:
        print(f"  {ln}")
    print(f"  ...共 {max(len(lines) - 1, 0)} 个监听 socket\n")

    print("""  列的含义:
      COMMAND  PID USER  FD   TYPE  DEVICE  SIZE/OFF  NODE  NAME
      ControlCe 638 example  9u   IPv4  0x...   0t0     TCP   *:7000 (LISTEN)
      ^进程名    ^PID         ^文件描述符        ^协议  ^地址:端口  ^状态

    NAME 里的 `*:7000` 表示绑定在 0.0.0.0:7000（所有网卡），
    如果是 `127.0.0.1:5432` 就只有本机能访问。这个区别决定了服务能不能被外部访问,
    也是安全审计的第一眼。
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：自己起一个监听, 再把它找出来")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    port = server.getsockname()[1]
    print(f"  已启动监听: 127.0.0.1:{port}  PID={MY_PID}")

    mine = [ln for ln in listening_sockets() if f":{port}" in ln]
    print("  lsof 里查这个端口:")
    for ln in mine:
        print(f"    {ln}")

    if mine:
        fields = mine[0].split()
        print(f"""
  解读: 进程名={fields[0]} PID={fields[1]} 用户={fields[2]} 监听地址={fields[-2]}
  把这个 PID 拿去 `ps -p <PID> -o command=` 就能看到完整的启动命令行,
  这是「线上服务找不到是谁启的」的标准排查套路。
""")
        pid = fields[1]
        ps = sh(["ps", "-p", pid, "-o", "pid,user,command"], timeout=5)
        for ln in ps:
            print(f"    {ln}")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：制造端口冲突，再反查占用者")

    clash = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        clash.bind(("127.0.0.1", port))          # 同一个端口, 必然失败
        print("  居然绑上了（不应该）")
    except OSError as exc:
        print(f"  第二次 bind 127.0.0.1:{port} -> {type(exc).__name__}: {exc}")
        print(f"  errno={exc.errno}, 对应符号名 = {errno_name(exc.errno)}")
        print(f"""
  排障三步曲（遇到 Address already in use 时照做）:
    1) lsof -nP -iTCP:{port}        # 看是不是自己的旧进程还活着
    2) 如果是 TIME_WAIT 而不是 LISTEN -> 代码里加 SO_REUSEADDR 即可
    3) 如果是别人的进程 -> 换端口, 或者礼貌地跟他说
""")
    finally:
        clash.close()

    occupant = [ln for ln in listening_sockets() if f":{port}" in ln]
    print(f"  用 lsof 反查端口 {port} 的占用者: {occupant[0].split()[0] if occupant else '(没找到)'}"
          f" (PID {occupant[0].split()[1] if occupant else '?'})")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：连接状态分布（TIME_WAIT 是不是堆积了？）")

    rows = tcp_table()
    state_counter: Counter[str] = Counter()
    for ln in rows:
        m = re.match(r"tcp[46]\d?\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s+(\S+)", ln)
        if m:
            state_counter[m.group(3)] += 1
    if state_counter:
        total = sum(state_counter.values())
        print(f"  这台机器当前共 {total} 条 TCP socket, 状态分布:")
        for state, count in state_counter.most_common():
            print(f"    {state:<14} {count:>5}")
        print("""
  经验值:
    * TIME_WAIT 少量是正常的（主动关闭方要等 2MSL, macOS 上约 15 秒×2）
    * TIME_WAIT 成千上万 -> 短连接太多, 该用连接池 / keep-alive, 或调内核参数
    * CLOSE_WAIT 一堆 -> 你的代码收到 FIN 后没有 close(), 是程序 bug, 只能改代码
    * SYN_RCVD 暴涨 -> 可能在被 SYN Flood 攻击
""")
    else:
        print("  没能解析 netstat 输出，跳过。")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：谁在跟我的机器连接（按对端 IP 聚合）")

    peers: Counter[str] = Counter()
    for ln in rows:
        m = re.match(r"tcp[46]\d?\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s+(\S+)", ln)
        if not m:
            continue
        local, foreign, state = m.groups()
        if state != "ESTABLISHED":
            continue
        foreign_ip = foreign.rsplit(".", 1)[0]      # 去掉端口
        peers[foreign_ip] += 1

    if peers:
        print("  连接数最多的对端 IP:")
        for ip, count in peers.most_common(8):
            print(f"    {ip:<20} 连接数 {count}")
        print("""
  这一步就是「谁是热点客户端」的最原始版本。
  真正的排障会再叠加: 按进程聚合(找出哪个服务在猛发) / 按状态聚合(找出挂住的连接)。
""")
    else:
        print("  当前没有 ESTABLISHED 连接。")

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：纯 Python 也能查（不依赖 lsof）")

    print("""  Python 标准库里没有「列出本进程所有 socket」的接口, 但可以这样逼近:
      * 自己维护一份「已创建的 socket」记录（工程上很实用, 参考下面代码）
      * 想知道某个端口通不通 -> 直接 connect 试探（下一课就是端口扫描）

  一个推荐的工程习惯: 给服务端封装一层「带名字的监听器」, 日志里把
  监听地址 + PID 打出来, 出问题时一眼就知道是谁。
""")

    class NamedListener:
        """带日志的监听器, 关闭时也会打日志。生产代码里非常值得这么写。"""

        registry: list[tuple[str, int]] = []

        def __init__(self, name: str, host: str = "127.0.0.1", port: int = 0):
            self.name = name
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((host, port))
            self.sock.listen(16)
            self.address = self.sock.getsockname()
            NamedListener.registry.append(self.address)
            print(f"  [listener:{self.name}] listening on {self.address[0]}:{self.address[1]} "
                  f"pid={MY_PID} backlog=16")

        def close(self) -> None:
            self.sock.close()
            NamedListener.registry.remove(self.address)
            print(f"  [listener:{self.name}] closed {self.address[0]}:{self.address[1]}")

    demo = NamedListener("demo-service")
    # 再起一个后台线程保持它活着, 主线程做一次连接验证
    keep = threading.Event()

    def accept_once() -> None:
        demo.sock.settimeout(3)
        try:
            conn, peer = demo.sock.accept()
            print(f"  [listener:demo-service] accepted {peer}")
            conn.close()
        except (socket.timeout, OSError):
            pass
        keep.set()

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()
    probe = socket.socket()
    probe.connect(demo.address)
    probe.close()
    keep.wait(timeout=4)
    t.join(timeout=2)
    demo.close()

    # 收尾: 关掉第 2 步起的服务器
    server.close()

    title("本课小结")
    print("""1. 端口冲突先 lsof -nP -iTCP:<port>, 再 ps 找出命令和用户。
2. 状态字段能区分「正常关闭」和「代码没关」(TIME_WAIT vs CLOSE_WAIT)。
3. TIME_WAIT 少量正常, 堆积说明短连接太多; CLOSE_WAIT 堆积一定是代码 bug。
4. 绑定地址是安全性问题: 只给内网用就不要 bind 0.0.0.0。
5. 下一课: 把「connect 试探」系统化, 写一个只扫自己机器的端口扫描器。
""")
    print("下一课: 02_port_socket/demo_07_port_scan.py")


def errno_name(errno_value: int | None) -> str:
    import errno as errno_mod
    if errno_value is None:
        return "?"
    return errno_mod.errorcode.get(errno_value, "未知")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_02_dns_lookup.py -- 第 2 课：域名怎么变成 IP？（DNS）
==========================================================

【原理】
    人记不住 104.20.23.154, 只记得住 example.com。
    DNS (Domain Name System) 就是那本全球分布式的「电话簿」:
      输入: 域名 + 记录类型     输出: 一个或多个 IP

    它的分层结构:
        根 (.) -> 顶级域 (.com) -> 权威域 (example.com) -> 具体记录
    查询方式:
        递归查询 (你的机器 -> 本地 DNS 服务器, 由它代劳)
        迭代查询 (本地 DNS 服务器 -> 各级权威服务器)

    传输层: 默认 UDP 53 端口（快, 一个包问一个包答）
            响应超过 512 字节 / 区域传送时切换到 TCP 53

【本课要点】
    1. getaddrinfo 是唯一正确的统一入口（同时处理 IPv4/IPv6/协议类型）
    2. 为什么 gethostbyname 不推荐
    3. /etc/hosts 优先级（本地覆盖）
    4. 一个域名对应多个 IP: CDN / 负载均衡
    5. 反向解析: IP -> 域名
    6. 观察 DNS 缓存带来的耗时差异
    7. 我的机器实际在用哪台 DNS 服务器

运行：python3 demo_02_dns_lookup.py [域名]
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time

DEFAULT_HOSTS = ["example.com", "www.baidu.com", "one.one.one.one"]


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def main() -> None:
    hosts = sys.argv[1:] or DEFAULT_HOSTS

    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：getaddrinfo，域名解析的唯一推荐入口")

    print("""socket.getaddrinfo(host, port, family, type, proto, flags) 返回一个列表，
每一项是 (地址族, 套接字类型, 协议号, 规范名, sockaddr) 五元组。

为什么必须用它: 一个域名可能同时有 IPv4(A) 和 IPv6(AAAA) 记录，
文件里写死 AF_INET 会让程序在纯 IPv6 环境直接挂掉。
让 getaddrinfo 帮你把「所有可用走法」列出来，我再逐个尝试连接，这是标准做法。
""")
    host = hosts[0]
    infos = socket.getaddrinfo(host, 80, proto=socket.IPPROTO_TCP)
    print(f"getaddrinfo({host!r}, 80, proto=TCP) 共 {len(infos)} 条:")
    families = {socket.AF_INET: "IPv4", socket.AF_INET6: "IPv6"}
    for family, socktype, proto, canonname, sockaddr in infos:
        type_name = {
            socket.SOCK_STREAM: "TCP流",
            socket.SOCK_DGRAM: "UDP报文",
        }.get(socktype, socktype)
        print(
            f"  {families.get(family, family):<5} {type_name:<6} proto={proto:<3} 规范名={canonname or '-':<12} sockaddr={sockaddr}"
        )

    print("""
注意 sockaddr 是元组, 不是字符串:
  IPv4: (ip, port)
  IPv6: (ip, port, flowinfo, scopeid)
这也是写代码时 IPv6 兼容经常出 bug 的地方, 一定要按元组处理。
""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：老 API 与它们的局限")

    for h in hosts:
        try:
            v4 = socket.gethostbyname(h)  # 只返回第一个 IPv4
            print(
                f"  gethostbyname({h!r})      = {v4}          <- 只给 1 个, 且忽略 IPv6"
            )
        except OSError as exc:
            print(f"  gethostbyname({h!r}) 失败: {exc}")
        try:
            name, aliases, addrs = socket.gethostbyname_ex(h)  # 返回全部 IPv4
            print(
                f"  gethostbyname_ex({h!r})   = 规范名={name} 别名={aliases} 所有IPv4={addrs}"
            )
        except OSError as exc:
            print(f"  gethostbyname_ex({h!r}) 失败: {exc}")

    print("""
结论: 新代码只用 getaddrinfo。gethostbyname 系列没有 IPv6、没有端口、
没有错误细节（都抛同一个 gaierror），迟早坑你。
""")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：一个域名对应多个 IP（CDN / 负载均衡）")

    for h in hosts:
        try:
            infos = socket.getaddrinfo(h, None, type=socket.SOCK_STREAM)
            ipv4 = sorted({i[4][0] for i in infos if i[0] == socket.AF_INET})
            ipv6 = sorted({i[4][0] for i in infos if i[0] == socket.AF_INET6})
            print(f"  {h}")
            print(f"    A   记录 (IPv4): {len(ipv4)} 个 {ipv4}")
            print(f"    AAAA记录 (IPv6): {len(ipv6)} 个 {ipv6}")
        except OSError as exc:
            print(f"  {h} 解析失败: {exc}")

    print("""
多个 IP 的意义:
  * CDN: 不同 IP 是不同的边缘节点, 离你越近越快 (Anycast 路由帮你选最近的一个)
  * 负载均衡: 客户端自己挑第一个/随机挑, 或 DNS 轮询把流量摊开
  * 故障转移: 一个 IP 挂了, 客户端可以试下一个
所以「域名 -> 一个 IP」是错误心智模型, 客户端必须能处理多个结果。
""")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：本地 hosts 文件优先于 DNS")

    hosts_path = "/etc/hosts"
    max_hosts_lines = (
        10  # 只展示前 10 条: 本机 hosts 可能有几百条, 全打出来会盖住重点
    )
    print(f"{hosts_path} 内容里非注释的部分（解析顺序: hosts 文件 -> DNS）:")
    try:
        with open(hosts_path, "r", encoding="utf-8", errors="replace") as fh:
            entries = []
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                entries.append(line)
        for line in entries[:max_hosts_lines]:
            print(f"  {line}")
        if not entries:
            print("  (没有自定义条目)")
        elif len(entries) > max_hosts_lines:
            loopback = sum(
                1 for e in entries if e.split()[0] in ("127.0.0.1", "::1")
            )
            print(
                f"  ...（共 {len(entries)} 条, 上面只显示前 {max_hosts_lines} 条;"
                f"其中 {loopback} 条指向本机回环（典型的「本地劫持」））"
            )
            print("      想看全部: grep -v '^#' /etc/hosts")
    except OSError as exc:
        print(f"  读取失败: {exc}")

    print("""
这就是为什么改 hosts 能「本地劫持」域名: 系统在真正发出 DNS 查询前，
先查这张本地表。开发时把 api.example.com 指向 127.0.0.1 就是这么干的。
上面那几百条里很多是指向回环的本地开发/代理条目（Charles、内网域名等），
它们优先级高于 DNS —— 所以「为什么我的请求没走到线上」经常答案就在这张表里。
注意: 现在 Python 里没法自己往这张表里塞条目（历史上 SocketServer 那套已废弃），
      只能改文件。
""")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：反向解析，IP -> 域名")

    for ip in ["1.1.1.1", "8.8.8.8", "192.0.2.54"]:
        try:
            name, aliases, _ = socket.gethostbyaddr(ip)
            print(f"  {ip:<18} -> {name}  (别名 {aliases})")
        except OSError as exc:
            print(f"  {ip:<18} -> 反查失败: {exc}")

    print("""
反向解析走的是 in-addr.arpa 域名下的 PTR 记录, 和正向解析是两棵不同的树。
很多 IP 反查不出域名（管理员没配 PTR）, 所以日志里别指望它。
""")

    # ---------------------------------------------------------------- 第 6 步
    title("第 6 步：DNS 缓存让第二次快得多")

    target = "www.wikipedia.org"
    samples = []
    for round_no in range(4):
        start = time.perf_counter()
        try:
            socket.getaddrinfo(target, 443, type=socket.SOCK_STREAM)
            elapsed = (time.perf_counter() - start) * 1000
            samples.append(elapsed)
            print(f"  第 {round_no + 1} 次解析 {target}: {elapsed:7.3f} ms")
        except OSError as exc:
            print(f"  第 {round_no + 1} 次解析失败: {exc}")
            break
        time.sleep(0.05)

    if len(samples) >= 2:
        print(
            f"\n  第 1 次 {samples[0]:.3f} ms vs 第 2 次 {samples[1]:.3f} ms"
            f"  ->  快了 {samples[0] / max(samples[1], 1e-6):.1f} 倍"
        )
        print("""
  加速来自三层缓存:
    1) 操作系统/系统解析器缓存 (macOS 由 mDNSResponder 管)
    2) 本地 DNS 服务器缓存
    3) 应用自己可能也缓存
  首次要真的去问（可能要跨半个地球问权威服务器），后面直接命中缓存。
  这解释了为什么「发请求第一下慢」经常是 DNS 的锅, 而不是带宽的锅。
""")

    # ---------------------------------------------------------------- 第 7 步
    title("第 7 步：我的机器到底在用哪台 DNS 服务器？")

    if shutil.which("scutil"):
        out = subprocess.run(
            ["scutil", "--dns"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        ).stdout
        server_lines = [
            ln.strip() for ln in out.splitlines() if "nameserver" in ln
        ]
        for ln in server_lines[:10]:
            print(f"  {ln}")
        print("""
  注意 nameserver 后面可能跟着 if_index, 例如只对某张网卡生效。
  上面如果有 192.0.2.53 或 fd7a:... 之类的地址, 说明有个 VPN / 组网工具
  抢占了部分域名的解析权（分流 DNS）。企业网里这会直接影响你访问哪些内网域名。
""")
    else:
        print("  系统没有 scutil，跳过。")

    print("  用 dig 看一次完整的 DNS 交互过程（如果装了 dig）:")
    if shutil.which("dig"):
        out = subprocess.run(
            ["dig", "+noall", "+answer", "+stats", "example.com", "A"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        ).stdout
        for ln in out.strip().splitlines()[:14]:
            print(f"    {ln}")
    else:
        print("    未安装 dig，可换成 nslookup。")

    title("本课小结")
    print("""1. DNS 把域名翻译成 IP, 默认跑在 UDP/53, 响应太大时换 TCP/53。
2. 解析顺序: hosts 文件 -> 系统缓存 -> 本地 DNS 服务器(递归) -> 权威服务器(迭代)。
3. 永远用 getaddrinfo, 它一次给你 IPv4+IPv6+所有可用走法。
4. 一个域名多个 IP 是常态, 客户端要会挑、会重试。
5. 首次请求慢常常是 DNS 冷缓存, 不是网络慢。
6. 下一步: 光有 IP 只能找到机器, 找到进程要用端口 -> 第 2 课。
""")
    print("下一课: 02_port_socket/demo_05_port_basics.py")


if __name__ == "__main__":
    main()

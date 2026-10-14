#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_04_routing_table.py -- 第 4 课：内核怎么决定「这个包从哪张网卡出去」？
=======================================================================

【原理】
    你调用 connect("1.1.1.1", 443) 时, 内核做的是:
        1) 查路由表, 找「最长前缀匹配」的那条路由  -> 得出出口网卡 + 下一跳网关
        2) 用出口网卡的 IP 作为源地址
        3) 如果下一跳不是直连, 就用 ARP 找到网关的 MAC, 把包交给它
        4) 每一跳路由器重复这个过程 (TTL 每跳减 1, 减到 0 就丢弃并回 ICMP 超时)

    路由表长这样（macOS 上用 netstat -rn 看）:
        Destination        Gateway            Flags   Netif
        default            192.168.1.1     UGScg   en0        <- 兜底路由
        192.168.1.0/23      link#14            UCS     en0        <- 直连网段
        127.0.0.1          127.0.0.1          UH      lo0        <- 主机路由

【本课路线】
    1. 读出真实路由表并解释每一列
    2. 实测「同网段 vs 不同网段 vs 回环」的源地址选择差异
    3. 解析默认路由和网关
    4. 用 traceroute 观察 TTL 逐跳递减（真实看到互联网的路径）
    5. 解释最长前缀匹配: 为什么明细路由会赢过默认路由

运行：python3 demo_04_routing_table.py
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def run(cmd: list[str], timeout: float = 30.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def flag_legend(flags: str) -> str:
    """把 netstat 的标志位字母翻译成人话。"""
    table = {
        "U": "路由可用", "G": "经由网关", "H": "主机路由(/32)", "S": "静态", "C": "克隆(由连接生成)",
        "L": "链路层有效", "W": "克隆", "I": "由接口生成", "R": "拒绝", "B": "黑洞", "b": "广播",
        "m": "组播", "c": "协议克隆", "g": "间接", "!": "不可用",
    }
    return ", ".join(table.get(ch, ch) for ch in flags)


def main() -> None:
    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：读真实路由表")

    raw = run(["/usr/sbin/netstat", "-rn", "-f", "inet"]) or run(["netstat", "-rn", "-f", "inet"])
    if not raw:
        print("  无法读取路由表，跳过。")
    routes: list[dict] = []
    print("  完整输出节选（前 24 行，解析用的是全部行）:")
    for line_no, line in enumerate(raw.splitlines()):
        if line_no < 24:
            print(f"    {line}")
        parts = line.split()
        if len(parts) >= 3 and parts[0] not in ("Routing", "Internet:", "Destination"):
            routes.append({
                "dest": parts[0],
                "gateway": parts[1],
                "flags": parts[2],
                "netif": parts[3] if len(parts) > 3 else "-",
            })
    print(f"  （共解析出 {len(routes)} 条 IPv4 路由）")

    # 本机出口 IP (UDP connect 不发包, 只查一次路由表)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect(("1.1.1.1", 80))
    local_ip = probe.getsockname()[0]
    probe.close()

    # 只挑出「与我的流量有关」的那几条, 不然 30+ 张网卡的路由表会淹死重点
    relevant = []
    for r in routes:
        if r["dest"] == "default" or "127.0.0.1" in (r["dest"] + r["gateway"]):
            relevant.append(("关键路由", r))
            continue
        try:
            if ipaddress.ip_address(local_ip) in ipaddress.ip_network(r["dest"], strict=False):
                relevant.append(("命中本机的路由", r))
        except ValueError:
            continue
    print(f"\n  只看与 192.168.1.10 这类「我的流量」有关的路由（本机出口 IP = {local_ip}）:")
    for tag, r in relevant:
        print(f"    [{tag:<8}] {r['dest']:<20} 网关={r['gateway']:<18} 标志={r['flags']:<7} 出口={r['netif']}")

    print("\n  逐列解释:")
    print("""    Destination  目标网段。default 表示「其他所有地址」
    Gateway      下一跳。link#14 表示「直连在某张网卡的链路层上, 没有路由器中转」
    Flags        U 可用 / G 需要网关 / H 主机路由 / S 静态 / C 由连接克隆出来 / ! 不可用
    Netif        出口网卡。en0 物理网卡, lo0 回环, utun* 隧道(VPN)""")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：默认路由与网关")

    default = [r for r in routes if r["dest"] == "default"]
    for r in default:
        print(f"  default -> 网关 {r['gateway']:<18} 网卡 {r['netif']:<6} ({flag_legend(r['flags'])})")
    if len(default) > 1:
        print("""
  注意: 这台机器有多条默认路由（物理网卡 + VPN 隧道 + 虚拟机网桥）。
  「到底走哪条」由优先级和 scoped routing 决定, 这也是 VPN 一开
  某些站点访问不了的常见原因: 流量被更高优先级的隧道抢走了。
""")

    direct = [r for r in routes if "link#" in r["gateway"]][:4]
    print("  直连路由（不需要网关, 二层直接发, 等价于「同网段」）:")
    for r in direct:
        print(f"    {r['dest']:<18} 出口 {r['netif']:<6} 网关列={r['gateway']}")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：实测路由选择（不产生流量的探测）")

    print("  UDP connect() 只查路由表、不发包, 所以可以随便探测:\n")
    targets = [
        ("127.0.0.1", 80, "回环地址"),
        ("192.168.1.1", 80, "本网段的默认网关（直连）"),
        ("1.1.1.1", 80, "公网（走默认路由）"),
        ("192.0.2.1", 80, "TEST-NET-1, 保留地址, 看有没有路由"),
    ]
    results = {}
    for ip, port, note in targets:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((ip, port))
            src_ip, src_port = s.getsockname()
            results[ip] = src_ip
            print(f"  目标 {ip:<16} 源 IP {src_ip:<16} 源端口 {src_port:<6} {note}")
        except OSError as exc:
            print(f"  目标 {ip:<16} 无路由: {exc}  ({note})")
        finally:
            s.close()

    print("""
  观察到的规律:
    * 打回环  -> 源 IP 是 127.0.0.1, 完全不出网卡
    * 打内网  -> 源 IP 是内网网卡地址
    * 打公网  -> 源 IP 还是内网网卡地址（因为要做 NAT, 内网 IP 才是有意义的源）
  源端口是内核从「临时端口范围」里随机挑的, 下一课细讲。
""")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：最长前缀匹配（为什么明细路由赢）")

    # 最长前缀匹配（直接用上面探测到的本机出口 IP, 避免写死坐标）
    print(f"  本机出口 IP（探测得到）: {local_ip}")

    matches = []
    for r in routes:
        if r["dest"] == "default":
            continue
        try:
            net = ipaddress.ip_network(r["dest"], strict=False)
        except ValueError:
            # netstat 里像 "127" 这种缩写、或 link#/MAC 形式的条目会解析失败, 正常跳过
            continue
        if ipaddress.ip_address(local_ip) in net:
            matches.append((net.prefixlen, net, r))

    if matches:
        matches.sort(key=lambda item: item[0], reverse=True)   # 只按前缀长度排, 避免去比 dict
        for prefixlen, net, r in matches:
            print(f"  /{prefixlen:<3} {str(net):<20} 出口 {r['netif']:<6} 网关列={r['gateway']:<16} 标志={r['flags']}")
        _, best_net, best_r = matches[0]
        print(f"  -> 胜出的是 /{best_net.prefixlen} 的 {best_net}, 出口网卡 {best_r['netif']}；"
              f"default(/0) 只有在没有更长匹配时才会用到。")
    print("""
  匹配规则: 在所有匹配的路由里挑「前缀最长」的那条, 而不是最先出现的、也不是最具体的网关。
    /32 主机路由  >  /24 网段  >  /8  >  default(/0)
  这条规则让「加一条明细路由」成为局部改流量的标准手段:
    route add -host 203.0.113.7 192.168.1.1     # 只让这个 IP 走别的网关
""")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：真实观察 TTL 逐跳递减（traceroute）")

    print("""原理: 发送 TTL=1 的包, 第 1 台路由器收到后 TTL 减到 0 就丢弃,
并回一个 ICMP Time Exceeded; 源端于是知道第 1 跳是谁。然后 TTL=2, 3, ...
逐跳把整条路径「问」出来。这也是「目的地不可达」之外 ICMP 的另一大用途。
""")
    out = run(["/usr/sbin/traceroute", "-m", "8", "-q", "1", "-w", "1", "-n", "1.1.1.1"], timeout=40)
    if out.strip():
        for line in out.strip().splitlines()[:12]:
            print(f"  {line}")
        print("""
  每一行 = 一跳路由器:
      序号   路由器 IP       往返时间
      1      192.168.1.1  <- 你家/公司路由器的内网口
      2      192.168.2.1    <- 上一级(运营商接入设备)
      3      183.94.76.1     <- 进入城域网
      *      表示这一跳没回应 ICMP（很多路由器出于安全/性能配置不回）
  企业网里看这个能立刻分辨「慢在本地、慢在上游、还是慢在对方」。
  注意 TTL 是 IPv4 的字段, IPv6 里改名叫 Hop Limit, 作用一样。
""")
    else:
        print("  traceroute 不可用或被网络策略挡住，跳过（不影响本课其它结论）。")

    title("本课小结")
    print("""1. 内核靠路由表决定出口网卡和下一跳, 规则是「最长前缀匹配」。
2. default 路由是兜底, 一个机器可以有多条（多网卡/VPN 场景）。
3. 源 IP 是由出口网卡决定的, 不是你指定的（除非显式 bind）。
4. TTL/Hop Limit 防环路, 顺便让 traceroute 成为可能。
5. 到这里「找到机器」讲完了。下一章开始讲「找到进程」: 端口。
""")
    print("下一课: 02_port_socket/demo_05_port_basics.py")


if __name__ == "__main__":
    main()

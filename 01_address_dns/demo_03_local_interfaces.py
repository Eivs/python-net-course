#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_03_local_interfaces.py -- 第 3 课：我的机器有几张网卡？（链路层）
====================================================================

【原理】
    上一课知道「用 IP 找机器」, 但 IP 包最终要变成网线上的电信号/光信号。
    真正干活的是「网卡」(NIC / interface)，它有:

      * 链路层地址: MAC 地址, 48 位, 出厂烧录, 形如 00:1a:2b:3c:4d:5e
          前 24 位是厂商编号 (OUI), 后 24 位由厂商自己分配
      * 网络层地址: 一个或多个 IP（IPv4/IPv6 都可以有多个）
      * MTU: 最大传输单元, 以太网默认 1500 字节

    IP -> MAC 的翻译叫 ARP (IPv4) / NDP (IPv6)。
    「同网段直接发, 不同网段发给网关」这个判断就发生在这一层。

【MTU 为什么重要】
    以太网 MTU 1500, 减去 IP 头 20 + TCP 头 20 = 1460, 这就是常说的 MSS。
    你的应用一次 send 10KB, 内核会切成 7 个报文分段发出去。
    这也是第 6 课「粘包」问题的物理根源之一。

【本课路线】
    1. 用 socket.if_nameindex() 列出所有网卡（纯 Python，无需外部命令）
    2. 解析 ifconfig 拿到 MAC / IPv4 / IPv6 / MTU / 标志位
    3. 识别回环、虚拟网卡、VPN 网卡
    4. 理解 ARP 表: 我刚跟谁说过话（局域网邻居缓存）
    5. 计算 MSS，看清 MTU 和分片的关系

运行：python3 demo_03_local_interfaces.py
"""

from __future__ import annotations

import re
import socket
import subprocess


def title(text: str) -> None:
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def run(cmd: list[str], timeout: float = 5.0) -> str:
    """跑一个外部命令并返回 stdout，失败就返回空串（示例代码要能容错）。"""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_ifconfig() -> list[dict]:
    """把 `ifconfig -a` 的文本解析成结构化数据。

    输出形如:
        en0: flags=8863<UP,BROADCAST,...> mtu 1500
            ether 00:1a:2b:3c:4d:5e
            inet 192.168.1.10 netmask 0xfffffe00 broadcast 192.168.1.255
            inet6 fe80::889:e9fd:4ff5:6753%en0 prefixlen 64 secured scopeid 0xe
    """
    text = run(["/sbin/ifconfig", "-a"]) or run(["ifconfig", "-a"])
    ifaces: list[dict] = []
    current: dict | None = None

    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            # 新网卡开始
            name, _, rest = line.partition(":")
            current = {
                "name": name.strip(),
                "flags_raw": rest.strip(),
                "inet": [],
                "inet6": [],
                "ether": None,
                "mtu": None,
                "status": None,
            }
            m = re.search(r"mtu\s+(\d+)", rest)
            if m:
                current["mtu"] = int(m.group(1))
            ifaces.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()

        m = re.match(r"ether\s+([0-9a-f:]{17})", stripped)
        if m:
            current["ether"] = m.group(1)
            continue
        m = re.match(
            r"inet\s+(\S+)\s+netmask\s+(\S+)(?:\s+broadcast\s+(\S+))?", stripped
        )
        if m:
            current["inet"].append(
                {
                    "ip": m.group(1),
                    "netmask": m.group(2),
                    "broadcast": m.group(3),
                }
            )
            continue
        m = re.match(r"inet6\s+(\S+)\s+prefixlen\s+(\d+)", stripped)
        if m:
            current["inet6"].append(
                {"ip": m.group(1), "prefixlen": int(m.group(2))}
            )
            continue
        m = re.match(r"status:\s+(\S+)", stripped)
        if m:
            current["status"] = m.group(1)

    return ifaces


def main() -> None:
    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：纯 Python 枚举网卡 (socket.if_nameindex)")

    print("""socket.if_nameindex() 不依赖任何外部命令, 返回 (索引, 名字) 列表。
索引 (if_index) 就是内核内部给网卡编的号, 路由表/抓包/ping -I 都用它。
""")
    for index, name in socket.if_nameindex():
        print(f"  if_index={index:<4} name={name}")

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：解析 ifconfig，看清每张网卡的真实身份")

    ifaces = parse_ifconfig()
    if not ifaces:
        print("  没能执行 ifconfig，跳过。")
    for iface in ifaces:
        up = "UP" in iface["flags_raw"]
        flags = re.search(r"<([^>]*)>", iface["flags_raw"])
        flag_set = flags.group(1) if flags else ""
        print()
        print(
            f"  [{iface['name']}]  up={up}  mtu={iface['mtu']}  status={iface['status'] or '-'}"
        )
        if flag_set:
            print(f"    标志位: {flag_set}")
        print(f"    MAC  : {iface['ether'] or '(无, 可能是虚拟/回环网卡)'}")
        for v4 in iface["inet"]:
            mask_int = v4["netmask"]
            if mask_int.startswith("0x"):
                bits = bin(int(mask_int, 16)).count("1")
                mask_str = f"{mask_int}(/{bits})"
            else:
                mask_str = mask_int
            print(
                f"    IPv4 : {v4['ip']:<16} 掩码={mask_str:<20} 广播={v4['broadcast'] or '-'}"
            )
        for v6 in iface["inet6"]:
            scope = v6["ip"].split("%")[-1] if "%" in v6["ip"] else "-"
            kind = (
                "链路本地"
                if v6["ip"].startswith("fe80")
                else ("回环" if v6["ip"].startswith("::1") else "全球/唯一本地")
            )
            print(
                f"    IPv6 : {v6['ip']:<42} /{v6['prefixlen']:<3} ({kind}, scope={scope})"
            )

    print("""
读这张表的经验:
  lo0        回环网卡, 永远是 127.0.0.1 / ::1, MTU 通常 16384（本机通讯不用真发）
  en0        你的物理网卡（Mac 上 en0 一般是 Wi-Fi 或以太网）
  utunN      VPN / 隧道网卡（Tailscale、WireGuard、企业 VPN 都是这个形态）
  bridgeN    虚拟交换机（虚拟机、Docker/colima 会创建）
  awdl0/llw0 AirDrop 之类系统服务用的虚拟网卡
  一个网卡可以有多个 IP，所以「本机 IP」这种说法本身就不严谨。
""")

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：MTU 与 MSS，为什么大包会被切开")

    eth = next((i for i in ifaces if i["mtu"] == 1500 and i["inet"]), None)
    mtu = eth["mtu"] if eth else 1500
    print(f"  取一张以太网网卡 (MTU={mtu}) 来算:")
    print(
        f"    MTU                    = {mtu} 字节（一个以太网帧能装的 IP 包上限）"
    )
    print("    - IPv4 头(20, 无选项)  = 20")
    print("    - TCP 头(20, 无选项)   = 20")
    print(
        f"    = TCP MSS              = {mtu - 20 - 20} 字节（对端一次最多发这么多 TCP 载荷）"
    )

    payload = 10 * 1024
    print(f"""
  所以当应用层 send() 了 {payload} 字节, 内核会把它切成
  ceil({payload}/{mtu - 20 - 20}) = {-(-payload // (mtu - 20 - 20))} 个 TCP 报文段发出去,
  它们到达对端时可能被合并、也可能被拆开, 这就是「TCP 是字节流、没有消息边界」的物理原因。
  第 6 课会专门演示怎么在字节流上面重新划分消息边界。
""")
    if mtu != 1500:
        print(
            "  提示: 隧道网卡 MTU 更小（比如 utun 常见 1280 或 1400）, 走隧道时有效 MSS 也会变小。"
        )

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：ARP 缓存，我最近和哪些局域网邻居说过话")

    arp = run(["/usr/sbin/arp", "-an"]) or run(["arp", "-an"])
    if arp:
        lines = [ln for ln in arp.splitlines() if ln.strip()]
        # 先找出默认网关, 再在 ARP 缓存里定位它（网关一定被访问过, 所以一般都在）
        gateway = ""
        for ln in run(["/usr/sbin/netstat", "-rn", "-f", "inet"]).splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[0] == "default":
                gateway = parts[1]
                break
        print(f"  默认网关 = {gateway or '(没读到)'}")
        hit = [ln for ln in lines if gateway and f"({gateway})" in ln]
        for ln in hit:
            print(f"  网关在 ARP 缓存里: {ln.strip()}")
        if gateway and not hit:
            print(
                "  （本次未在 ARP 缓存里找到网关条目, 说明还没跟它说过话或者刚过期）"
            )
        print(f"  当前 ARP 表前 6 条（共 {len(lines)} 条）:")
        for ln in lines[:6]:
            print(f"  {ln}")
        print("""
  ARP 表 = IP -> MAC 的缓存。发第一个包之前必须先「喊一声」：谁是 192.168.1.1?
  拿到 MAC 之后才构造以太网帧。缓存过期就再喊一次。
  如果表里出现 (incomplete) 或某个 IP 一直是同一个 MAC, 可能是 ARP 欺骗的迹象。
""")
    else:
        print("  无法读取 ARP 表，跳过。")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：从 Python 主动做一次「链路层可见」的动作")

    print("""纯 Python 不能直接抓包（需要 root + BPF），但可以做这些事:
  * 用 if_nametoindex() 把网卡名换成内核索引, 用于绑定到特定网卡
  * 用 UDP 广播把包发给整个网段（下一课演示 UDP 时会用到）
""")
    for name in ["lo0", "en0", "utun10", "definitely_not_a_nic"]:
        try:
            print(f"  if_nametoindex({name!r}) = {socket.if_nametoindex(name)}")
        except OSError as exc:
            print(f"  if_nametoindex({name!r}) 失败: {exc}")

    print(f"""
  绑定到指定网卡的写法（Linux 上是 SO_BINDTODEVICE, BSD/macOS 上用 IP_BOUND_IF）:
      IP_BOUND_IF = {getattr(socket, "IP_BOUND_IF", "本平台不支持")}
  这个技巧在多网卡机器上非常有用: 强制某个连接只走 Wi-Fi 或只走有线。
""")

    title("本课小结")
    print("""1. 真正收发数据的是网卡, 它有 MAC（链路层）和 IP（网络层）两个身份。
2. 一个网卡可以有多个 IP, 还可能是虚拟网卡（VPN/桥接）。
3. IP -> MAC 靠 ARP, 缓存有超时, 值 得知道它存在。
4. MTU=1500 推出 TCP MSS=1460, 这是「字节流没有边界」的物理根源。
5. 下一课: 同一个网段 vs 不同网段, 内核到底怎么选路（路由表）。
""")
    print("下一课: 01_address_dns/demo_04_routing_table.py")


if __name__ == "__main__":
    main()

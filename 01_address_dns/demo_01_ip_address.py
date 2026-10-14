#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_01_ip_address.py -- 第 1 课：IP 地址到底是什么？
=====================================================

【先记住一句话】
    网络通讯 = 把一个进程手里的字节，送到另一个进程手里。
    要做到这件事必须回答两个问题：
      (1) 送到哪台机器？   -> 用 IP 地址（网络层，L3）
      (2) 送给机器上的哪个程序？ -> 用端口号（传输层，L4）
    本课只讲 (1)，端口在下一课。

【IP 地址的真面目】
    IPv4 地址本质是一个 32 位无符号整数。
    写成 "192.168.1.10" 只是为了人好读，叫「点分十进制」（dotted-decimal）：
    切成 4 段，每段 8 位，取值范围 0~255。
    操作系统和网卡内部处理的是整数和字节，不是字符串。
    本课第一步就把它拆开给你看。

【本课路线】
    1. 字符串 <-> 整数 <-> 4 字节，三种表示法互转
    2. 网络字节序（大端）为什么必须统一
    3. 掩码：一个 IP 地址里其实藏着「网络号」和「主机号」
    4. 特殊地址：回环、私网、广播、链路本地
    5. 我怎么知道自己的 IP？（connect 一次 UDP 就知道了）

运行：python3 demo_01_ip_address.py
"""

from __future__ import annotations

import ipaddress
import socket
import struct


def title(text: str) -> None:
    """打印一个醒目的分节标题，方便跟着输出读。"""
    print()
    print("=" * 74)
    print(text)
    print("=" * 74)


def main() -> None:
    # ---------------------------------------------------------------- 第 1 步
    title("第 1 步：IP 地址的本质是一个 32 位整数")

    text = "192.168.1.10"

    # 手工把点分十进制拼成整数：每段 8 位，第 1 段是最高 8 位。
    octets = [int(part) for part in text.split(".")]
    as_int = 0
    for value in octets:
        as_int = (
            as_int << 8
        ) | value  # <<8 等于「左移一个字节」，再或上新的低 8 位

    print(f"点分十进制        : {text}")
    print(f"四段（十进制）    : {octets}")
    print(f"四段（二进制）    : {[format(v, '08b') for v in octets]}")
    print(f"手工拼成整数      : {as_int}  = 0x{as_int:08X}  = 0b{as_int:032b}")
    print("                    ^^^^^^^^ 注意：4 段每段 8 位，正好 32 位")

    # 用标准库验证手工结果。inet_aton = "ASCII to network"，产出的就是
    # 网线上真正传输的 4 个字节；struct 用 !I（大端 32 位整数）解释它。
    packed = socket.inet_aton(text)
    from_struct = struct.unpack("!I", packed)[0]

    print()
    print(
        f"socket.inet_aton({text!r}) = {packed!r}  即每个字节: {list(packed)}"
    )
    print(f"struct.unpack('!I', ...)   = {from_struct}")
    print(f"ipaddress 模块             = {int(ipaddress.ip_address(text))}")
    assert from_struct == as_int == int(ipaddress.ip_address(text))
    print("三种算法结果一致 -> 证明所谓 IP 地址就是一个整数。")

    # 反过来：整数 -> 字符串
    print()
    print(
        f"整数 {as_int} 还原成字符串: {socket.inet_ntoa(struct.pack('!I', as_int))}"
    )

    # ---------------------------------------------------------------- 第 2 步
    title("第 2 步：网络字节序（大端）")

    print("""不同 CPU 存放多字节整数的方式不同:
  x86/ARM 是「小端」: 低位字节放低地址
  网络协议规定统一用「大端」(big-endian), 也叫网络字节序
所以内存里的整数，发给网线之前必须先转成网络字节序，否则对方解出来是错的。

Python 里两种常见写法:
  struct.pack("!I", n)   # ! = big-endian, I = unsigned int(4字节)
  struct.pack(">I", n)   # > 也是 big-endian
""")
    print(
        f"192.168.1.10 的记忆: struct.pack('!I') = {list(struct.pack('!I', int(ipaddress.ip_address('192.168.1.10'))))}"
    )
    print(
        f"                     struct.pack('<I') = {list(struct.pack('<I', int(ipaddress.ip_address('192.168.1.10'))))}  <- 小端，字节顺序反了"
    )
    # htons/htonl 是最常用的字节序转换函数：host to network short/long
    print(
        f"socket.htons(0x1234) = 0x{socket.htons(0x1234):04X}   (把 16 位整数转成网络序, 端口号会用到)"
    )

    # ---------------------------------------------------------------- 第 3 步
    title("第 3 步：掩码，一个 IP 里藏着「网络号 + 主机号」")

    # 拿本机真实地址来举例：192.168.1.10/23（/23 表示前 23 位是网络号）
    # 如果你的机器不在这个网段，把这一行换成自己 `ifconfig` 里看到的地址和掩码即可，
    # 下面的输出数字会跟着变，逻辑完全一样。
    net = ipaddress.ip_network("192.168.1.10/23", strict=False)

    print(f"网段表示法        : {net}")
    print(
        f"网络地址          : {net.network_address}   (主机位全 0, 代表整个网段)"
    )
    print(f"子网掩码          : {net.netmask}  = 0x{int(net.netmask):08X}")
    print(f"反掩码 wildcard   : {net.hostmask}")
    print(
        f"广播地址          : {net.broadcast_address}   (主机位全 1, 发给网段内所有机器)"
    )
    print(
        f"地址总数          : {net.num_addresses}   可用主机数: {net.num_addresses - 2}"
    )
    print(f"前 3 个可用主机   : {[str(h) for h in list(net.hosts())[:3]]}")
    print(
        f"前缀长度          : /{net.prefixlen}  -> 前 {net.prefixlen} 位是网络号, 后 {32 - net.prefixlen} 位是主机号"
    )

    neighbours = [
        ("192.168.1.1", "同 /23 网段内的网关"),
        ("192.168.2.1", "网络号变了, 属于别的网段"),
        ("8.8.8.8", "公网地址, 完全不同网段"),
    ]
    for addr, note in neighbours:
        inside = ipaddress.ip_address(addr) in net
        print(f"  {addr:<15} 和本机同网段? {str(inside):<5}  {note}")

    print("""
为什么关心同不同网段?
  同网段  -> 直接用 ARP 在局域网上问「谁是这个 IP」, 拿到 MAC 地址后二层直接发
  不同网段 -> 本机没这个能力, 必须把包交给「默认网关」, 由路由器一跳跳转发
这就是「直连路由」和「默认路由」的由来, 第 4 课会在真实路由表里看到。
""")

    # ---------------------------------------------------------------- 第 4 步
    title("第 4 步：必须认识的几个特殊地址")

    specials = [
        ("0.0.0.0", "只在 bind 时有意义: 表示「监听本机所有网卡的所有 IP」"),
        ("127.0.0.1", "回环地址: 数据根本不经过网卡, 内核内部直接掉头"),
        ("255.255.255.255", "受限广播: 只在本地链路生效, 路由器不会转发"),
        ("169.254.0.0/16", "链路本地/APIPA: DHCP 没拿到地址时系统自分配的"),
        ("10.0.0.0/8", "RFC1918 私有地址, 公网路由器不转发"),
        ("172.16.0.0/12", "RFC1918 私有地址"),
        ("192.168.0.0/16", "RFC1918 私有地址, 家里的路由器就是这个"),
        ("100.64.0.0/10", "运营商级 NAT (CGNAT), 也是 Tailscale 等组网默认段"),
        ("224.0.0.0/4", "组播: 一份数据同时发给一组订阅者"),
        ("240.0.0.0/4", "保留(含 255.255.255.255)"),
        ("::1 / fe80::/10", "IPv6 的回环地址 / 链路本地地址"),
    ]
    for addr, note in specials:
        print(f"  {addr:<18} {note}")

    print("\n判断一个地址属于哪一类（用 ipaddress 的属性，别自己写正则）:")
    for addr in [
        "127.0.0.1",
        "192.168.1.1",
        "192.0.2.53",
        "8.8.8.8",
        "169.254.3.4",
        "192.168.1.10",
    ]:
        ip = ipaddress.ip_address(addr)
        flags = []
        if ip.is_loopback:
            flags.append("回环")
        if ip.is_private:
            flags.append("私有")
        if ip.is_link_local:
            flags.append("链路本地")
        if ip.is_multicast:
            flags.append("组播")
        if ip.is_global:
            flags.append("全球可达")
        print(f"  {addr:<18} {'/'.join(flags) or '普通'}")

    # ---------------------------------------------------------------- 第 5 步
    title("第 5 步：我自己的 IP 是多少？（UDP connect 探测法）")

    print("""小技巧: UDP 的 connect() 并不发送任何数据包, 它只是让内核
「查一次路由表, 决定如果我真要往这个地址发包, 会用哪张网卡、哪个源 IP」。
所以这是一个瞬间完成、不产生流量的探测手段。
""")
    for target in [("1.1.1.1", 80), ("8.8.8.8", 53), ("127.0.0.1", 80)]:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(target)
            local_ip, local_port = probe.getsockname()
            peer = probe.getpeername()
            print(
                f"  目标 {str(target):<18} -> 内核会选用的源 IP: {local_ip:<16} 源端口: {local_port}  (对端 {peer})"
            )
        except OSError as exc:
            print(f"  目标 {str(target):<18} -> 探测失败: {exc}")
        finally:
            probe.close()

    print("""
  同样打到 1.1.1.1 和 8.8.8.8, 源 IP 通常一样（都走默认网关那张网卡）。
  打到 127.0.0.1 时源 IP 变成 127.0.0.1, 说明回环地址走的是完全独立的路径。
  源端口是内核随机分配的「临时端口」(ephemeral port), 这就是第 2 课的主角。
""")

    title("本课小结")
    print("""1. IPv4 地址 = 32 位整数, 点分十进制只是写法。传输时按大端字节序。
2. 一个地址 = 网络号 + 主机号, 由掩码(/前缀长度)划分。
3. 同网段才能二层直达, 否则必须交给网关, 这是路由的基础。
4. 0.0.0.0 / 127.0.0.1 / 私网段 各有明确语义, 写代码时经常踩坑。
5. 只靠 IP 只能找到「机器」, 找到「进程」还需要端口 -> 下一课。
""")
    print("下一课: 02_port_socket/demo_05_port_basics.py")


if __name__ == "__main__":
    main()

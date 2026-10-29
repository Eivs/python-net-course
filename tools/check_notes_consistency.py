#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_notes_consistency.py —— 校验「NOTES 与正文里的关键数字」没有漂移
========================================================================

为什么需要它：
    这套课的速查表（NOTES.md）和教学正文（doc/）各写了一遍关键常量：
    MTU/MSS、UDP 载荷上限、端口范围、keepalive 默认值……
    改了一处忘了另一处，就会出现「速查表说 1460、正文说 1500」这种自相矛盾。
    约定是：**NOTES.md 是唯一事实来源**，正文跟着它走。这个脚本把这条约定
    变成可自动发现的错误。

它检查什么（刻意保持很小、很直白）：
    下面 REQUIRED 里列出的每个字面量，必须在指定的每个文件里都出现。
    这不是「证明数字正确」（正确性由实测日志说话），而是「防止两边写得不一样」。

用法：
    python3 tools/check_notes_consistency.py
退出码：0 = 一致；1 = 有文件缺了某个关键数字
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (说明, 字面量, 必须包含它的文件列表)
REQUIRED: list[tuple[str, str, list[str]]] = [
    (
        "以太网 MTU 1500 / MSS 1460",
        "1460",
        [
            "NOTES.md",
            "doc/01-地址与DNS.md",
            "doc/04-TCP.md",
            "04_tcp/demo_14_tcp_socket_options.py",
        ],
    ),
    (
        "回环实测 MSS 16332",
        "16332",
        ["NOTES.md", "doc/04-TCP.md"],
    ),
    ("UDP 载荷上限 65507", "65507", ["NOTES.md", "doc/03-UDP与二进制协议.md"]),
    ("临时端口下界 49152", "49152", ["NOTES.md", "doc/02-端口与socket.md"]),
    (
        "端口 / UDP 长度上限 65535",
        "65535",
        ["NOTES.md", "doc/02-端口与socket.md", "doc/03-UDP与二进制协议.md"],
    ),
    ("keepalive 空闲 7200 秒", "7200", ["NOTES.md", "doc/04-TCP.md"]),
]


def main() -> int:
    problems: list[str] = []
    checked_files = 0
    for label, literal, files in REQUIRED:
        for rel in files:
            path = os.path.join(ROOT, rel)
            try:
                text = open(path, encoding="utf-8").read()
            except OSError as exc:
                problems.append(f"{rel}: 读不到（{exc}）")
                continue
            checked_files += 1
            if literal not in text:
                problems.append(f"{rel}: 缺少「{label}」（应出现 {literal}）")

    print(
        f"校验了 {len(REQUIRED)} 条关键数字，覆盖 {checked_files} 个文件引用"
    )
    if problems:
        print(f"\n发现 {len(problems)} 处不一致：")
        for item in problems:
            print(f"  ✗ {item}")
        print("\n约定：NOTES.md 是唯一事实来源，请先改 NOTES，再同步 doc/ 与示例。")
        return 1
    print("一致：NOTES.md 与 doc/ 的关键数字没有漂移。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

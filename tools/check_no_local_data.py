#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_no_local_data.py —— 检查「仓库里有没有把作者机器上的信息写进文案」
========================================================================

背景（这条规则是被作者明确要求过的）：
    文档/示例的**文案**（docstring、print 的说明、doc/*.md 的正文）里，
    不应该出现跑这套课的那台机器的具体信息：内网 IP、MAC、主机名、用户名、
    家目录路径、私有 DNS、以及 /etc/hosts 里的个人条目。

    运行时输出（`.logs/`）不可避免会带上这些值 —— 那是你本机跑出来的，
    且 `.logs/` 不入版本管理。但**我手写进教材的字**必须是通用的。

它怎么工作：
    1. 遍历仓库（跳过 .git / .logs / __pycache__ / tools/）
    2. 用下面的 PATTERNS 逐个文件扫
    3. 有任何命中就打印 文件:行号 并返回 1

    也就是说：这份 PATTERNS 就是「本项目的隐私底线清单」。
    换一台机器跑这套课时，请把它改成**你自己**的敏感值。

用法：
    python3 tools/check_no_local_data.py
退出码：0 = 干净；1 = 有命中（要么改成通用示例，要么在 doc 里标「已泛化 / 已脱敏」）
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 这些是「本机特有」的值：私有网段、网卡 MAC、家目录、用户名、主机名、
# 私有 DNS、hosts 里的个人条目。教材里一律不许出现。
PATTERNS: list[tuple[str, str]] = [
    (r"\b172\.31\.", "本机私有网段（教材里应改用 192.168.1.x 这类通用示例）"),
    (r"\b10\.16\.11\.", "本机隧道网段"),
    (r"00:1a:2b:3c:4d:5e", "本机网卡 MAC"),
    (r"00:1a:2b:3c:4d:5f", "本机网关 MAC"),
    (r"00:1a:2b:3c:4d:60", "局域网邻居 MAC"),
    (r"00:1a:2b:3c:4d:61", "局域网邻居 MAC"),
    (r"00:1a:2b:3c:4d:62\b", "局域网邻居 MAC"),
    (r"/Users/example", "作者家目录"),
    (r"\beivs\b", "作者用户名"),
    (r"example-host", "本机主机名"),
    (r"example-proxy", "hosts 里的个人条目"),
    (r"\b100\.100\.100\.100\b", "本机使用的私有 DNS"),
    (r"\b114\.114\.114\.114\b", "本机使用的 DNS"),
]

SKIP_DIRS = {".git", ".logs", "__pycache__", "tools", ".idea", ".vscode"}
TEXT_EXT = {".py", ".md", ".sh", ".txt", ".json", ".yml", ".yaml", ".toml"}


def main() -> int:
    problems: list[tuple[str, int, str, str]] = []
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if os.path.splitext(name)[1] not in TEXT_EXT:
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ROOT)
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern, why in PATTERNS:
                    if re.search(pattern, line):
                        problems.append((rel, lineno, why, line.strip()[:90]))

    print(f"扫描了 {scanned} 个文本文件（跳过 .logs/ 与 tools/），隐私底线规则 {len(PATTERNS)} 条")
    if problems:
        print(f"\n发现 {len(problems)} 处本机/个人信息出现在文案或代码里：")
        for rel, lineno, why, line in problems:
            print(f"  {rel}:{lineno}  [{why}]  {line}")
        print("\n处理方式：换成通用示例，或在 doc 的引文块旁注明「已泛化 / 已脱敏」。")
        return 1
    print("干净：没有把本机信息写进示例文案或文档正文。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

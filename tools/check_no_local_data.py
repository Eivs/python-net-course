#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_no_local_data.py —— 检查「仓库里有没有把某台机器的信息写进文案」
========================================================================

背景（这条规则是被明确要求过的）：
    文档/示例的**文案**（docstring、print 的说明、doc/*.md 的正文）里，
    不应该出现跑这套课那台机器的具体信息：内网 IP、MAC、主机名、用户名、
    家目录路径、私有 DNS、以及 /etc/hosts 里的个人条目。

    运行时输出（`.logs/`）不可避免会带上这些值 —— 那是你本机跑出来的，
    且 `.logs/` 不入版本管理。但**手写进教材的字**必须是通用的。

两套规则（这一点很重要）
------------------------
1. **通用规则**（写在下面的 GENERIC_PATTERNS，可以入库）
   只放「任何机器上都不该出现在教材里」的东西，例如任何人的家目录绝对路径、
   看起来像真实 MAC 地址的字符串（教材示例 00:1a:2b:3c:4d:xx 已加入白名单）。
   这些规则不包含任何具体机器的值，所以可以安全地提交。

2. **本机规则**（放在 tools/privacy_patterns.local，**不入库**）
   一行一条 `正则<TAB>说明`（用两个以上空格分隔也行）。检查器启动时自动读取，
   文件不存在就只用通用规则。示例见 tools/privacy_patterns.example。
   这样「隐私底线清单」本身不会再变成一次隐私泄漏。

   也可以用环境变量 PRIVACY_PATTERNS_FILE 指定别的路径。

它怎么工作：
    1. 遍历仓库（跳过 .git / .logs / .logs-degraded / __pycache__）
    2. 先跑通用规则，再跑本机规则（本机规则文件本身不参与扫描）
    3. 有任何命中就打印 文件:行号 并返回 1

用法：
    python3 tools/check_no_local_data.py
退出码：0 = 干净；1 = 有命中（要么改成通用示例，要么在 doc 里标「已泛化 / 已脱敏」）
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- 通用规则
# 只放「与具体机器无关」的规则。任何具体 IP / MAC / 主机名 / 用户名都不要写进来，
# 那些属于 tools/privacy_patterns.local（不入库）。
GENERIC_PATTERNS: list[tuple[str, str]] = [
    (
        r"/(?:Users|home)/[A-Za-z0-9._-]+",
        "家目录绝对路径（教材里应改用 <用户名> 占位符）",
    ),
    (
        r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b",
        "MAC 地址（若确实是教学示例，请加入 ALLOWED_MAC 白名单）",
    ),
]

# 教材里有意使用的示例 MAC（00:1a:2b:3c:4d:xx 是文档统一占位符）
ALLOWED_MAC_PREFIXES = ("00:1a:2b:3c:4d:",)

LOCAL_PATTERNS_FILE = os.environ.get("PRIVACY_PATTERNS_FILE") or os.path.join(
    ROOT, "tools", "privacy_patterns.local"
)

SKIP_DIRS = {".git", ".logs", ".logs-degraded", "__pycache__", ".idea", ".vscode"}
TEXT_EXT = {".py", ".md", ".sh", ".txt", ".json", ".yml", ".yaml", ".toml"}


def load_local_patterns() -> list[tuple[str, str]]:
    """读取本机规则文件；不存在就返回空列表（只用通用规则也能工作）。"""
    if not os.path.isfile(LOCAL_PATTERNS_FILE):
        return []
    patterns: list[tuple[str, str]] = []
    for lineno, raw in enumerate(
        open(LOCAL_PATTERNS_FILE, encoding="utf-8"), 1
    ):
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = re.split(r"\t+| {2,}", line, maxsplit=1)
        pattern = parts[0]
        reason = parts[1] if len(parts) > 1 else "本机敏感信息"
        try:
            re.compile(pattern)
        except re.error as exc:
            print(
                f"  [警告] {os.path.basename(LOCAL_PATTERNS_FILE)}:{lineno} "
                f"正则不合法，已忽略: {exc}"
            )
            continue
        patterns.append((pattern, reason))
    return patterns


def is_allowed_mac(mac: str) -> bool:
    """教材里的占位 MAC（00:1a:2b:3c:4d:xx）不算泄漏。"""
    return mac.lower().startswith(ALLOWED_MAC_PREFIXES)


MAC_PATTERN = GENERIC_PATTERNS[1][0]


def main() -> int:
    local_patterns = load_local_patterns()
    patterns = GENERIC_PATTERNS + local_patterns
    local_name = os.path.basename(LOCAL_PATTERNS_FILE)
    problems: list[tuple[str, int, str, str]] = []
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for name in filenames:
            if os.path.splitext(name)[1] not in TEXT_EXT:
                continue
            if name == local_name:  # 本机规则文件自己当然会命中自己
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, ROOT)
            try:
                text = open(path, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern, why in patterns:
                    for match in re.finditer(pattern, line):
                        if pattern == MAC_PATTERN and is_allowed_mac(
                            match.group(0)
                        ):
                            continue
                        problems.append((rel, lineno, why, line.strip()[:90]))
                        break

    extra = f"，本机规则 {len(local_patterns)} 条" if local_patterns else "，未配置本机规则"
    print(
        f"扫描了 {scanned} 个文本文件（跳过 .logs/ 等），"
        f"通用规则 {len(GENERIC_PATTERNS)} 条{extra}"
    )
    if not local_patterns:
        print(
            "  提示: 复制 tools/privacy_patterns.example 为 "
            "tools/privacy_patterns.local，填入你自己机器上的敏感值（该文件不入库）。"
        )
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

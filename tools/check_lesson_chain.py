#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_lesson_chain.py —— 校验课程「链」自洽：课号、文件、下一课指针、文档链接
============================================================================

为什么需要它：
    这套课有 19 个示例 + 9 章文档，彼此靠「下一课: <路径>」串成一条链。
    手写这种指针很容易出错 —— 曾经就出现过 demo_01/demo_02 都指向
    `02_port_socket/demo_05_port_basics.py`（而它只该是 demo_04 的下一课）。
    这个脚本把这类错误变成可自动发现的问题。

它检查四件事：
    1. 每个 `demo_XX_*.py` 的 docstring 标题里写的「第 N 课」== 文件名里的 XX
    2. 每个示例（除最后一课）都能解析出一行 `下一课: <路径>`
    3. 那条路径① 文件存在，② 它的课号正好是当前课号 + 1
    4. 课号 1..N 连续、无重复、无缺口（N = 示例总数）

    第 19 课固定是 06_project/mini_http_server.py。学生把这个文件复制到别的
    目录做实验时，不会被误当成「第二个第 19 课」。

用法：
    python3 tools/check_lesson_chain.py
退出码：0 = 全部自洽；1 = 有问题（会逐条打印）
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LESSON_RE = re.compile(r"demo_(\d+)_")
# 综合项目入口：只有这个路径算第 19 课（避免学生复制出来的副本被误计）
PROJECT_ENTRY = "06_project/mini_http_server.py"
TITLE_RE = re.compile(r"第\s*(\d+)\s*课")
NEXT_RE = re.compile(r"下一课:\s*([0-9A-Za-z_./-]+\.py)")


def collect_lessons() -> list[tuple[int, str]]:
    """返回 [(课号, 相对路径)]，按课号排序；最后一个（综合项目）没有 demo_ 前缀。"""
    found: list[tuple[int, str]] = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "__"))]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(
                os.path.join(dirpath, name), ROOT
            ).replace(os.sep, "/")
            match = LESSON_RE.match(name)
            if match:
                found.append((int(match.group(1)), rel))
            elif rel == PROJECT_ENTRY:
                found.append((19, rel))
    return sorted(found)


def main() -> int:
    lessons = collect_lessons()
    if not lessons:
        print("没找到任何示例文件")
        return 1
    by_number = dict(lessons)
    problems: list[str] = []

    print(f"发现 {len(lessons)} 个示例，课号范围 {lessons[0][0]}~{lessons[-1][0]}\n")
    print(f"{'课号':<5}{'文件':<44}{'下一课':<46}状态")
    print("-" * 110)

    for number, rel in lessons:
        source = open(os.path.join(ROOT, rel), encoding="utf-8").read()

        # 1) docstring 标题里的课号
        head = "\n".join(source.splitlines()[:8])
        title_numbers = [int(n) for n in TITLE_RE.findall(head)]
        if number not in title_numbers:
            problems.append(f"{rel}: 文件是第 {number} 课，但 docstring 标题写的是 {title_numbers or '没写课号'}")

        # 2)(3) 下一课指针
        targets = NEXT_RE.findall(source)
        expected_number = number + 1
        if number == lessons[-1][0]:
            if targets:
                problems.append(f"{rel}: 最后一课不应该再有「下一课」指针（找到 {targets[0]}）")
            next_label, status = "(最后一课)", "OK"
        elif not targets:
            problems.append(f"{rel}: 找不到「下一课: <路径>」这一行")
            next_label, status = "-", "缺指针"
        else:
            target = targets[0]
            if len(set(targets)) > 1:
                problems.append(f"{rel}: 出现了多个不同的「下一课」指针 {sorted(set(targets))}")
            next_label = target
            if not os.path.exists(os.path.join(ROOT, target)):
                problems.append(f"{rel}: 下一课指向的文件不存在 -> {target}")
                status = "路径不存在"
            else:
                target_name = os.path.basename(target)
                match = LESSON_RE.match(target_name)
                target_number = (
                    int(match.group(1))
                    if match
                    else (19 if target.replace(os.sep, "/") == PROJECT_ENTRY else None)
                )
                if target_number != expected_number:
                    problems.append(
                        f"{rel}: 下一课应该指向第 {expected_number} 课，实际指向第 {target_number} 课（{target}）"
                    )
                    status = "课号不对"
                else:
                    status = "OK"

        print(f"{number:<5}{rel:<44}{next_label:<46}{status}")

    # 4) 课号连续性
    numbers = [n for n, _ in lessons]
    if numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"课号不连续或有重复: {numbers}")

    print()
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1

    print("课程链自洽：课号连续、标题一致、每个「下一课」都指向正确的下一课。")

    # 顺带校验 doc/ 的「下一章」链接
    doc_problems: list[str] = []
    doc_dir = os.path.join(ROOT, "doc")
    for name in sorted(os.listdir(doc_dir)):
        if not name.endswith(".md"):
            continue
        text = open(os.path.join(doc_dir, name), encoding="utf-8").read()
        for target in re.findall(r"下一章：\[[^\]]+\]\(([^)]+)\)", text):
            if not os.path.exists(os.path.join(doc_dir, target)):
                doc_problems.append(f"doc/{name}: 下一章链接不存在 -> {target}")
    if doc_problems:
        print(f"\ndoc/ 链接问题 {len(doc_problems)} 个：")
        for item in doc_problems:
            print(f"  ✗ {item}")
        return 1
    print("doc/ 的「下一章」链接也都指向存在的文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

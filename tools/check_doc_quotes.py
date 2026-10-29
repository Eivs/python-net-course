#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_doc_quotes.py —— 校验「doc/*.md 里引用的实测输出」都真的来自 .logs/ 的运行日志
====================================================================================

为什么需要它：
    doc/ 里的每一段 ``text`` 引用都标称「本机实测输出」。如果哪天代码改了、
    日志变了，而文档没跟着改，文档就会开始骗人 —— 这个脚本就是防这件事的。

原理：
    .logs/ 里的日志每次都重新生成，里面的「动态值」会变（端口号、PID、耗时、
    日期里的星期/月份、事务 ID…）。所以不能做逐字比对，而是把动态部分统一掩码成
    `#`（0x 十六进制、长十六进制串、所有十进制数字、英文星期与月份），再做子串匹配。
    这样：
      动态值漂移  -> 仍然匹配（符合预期）
      文档写错/编造 -> 匹配不上（报警）

    证据只认「本次运行」：run_all.sh 在 .logs/.manifest 里记录它生成了哪些日志，
    有清单就只拿清单里的文件比对，避免把残留的旧日志当成出处。
    少数确实无法进日志的内容不会被检查：
      1) 表格、命令示例、精简过的引用（它们不在 ```text 块里，或长度太短）
      2) 紧跟在代码块后面写了「每次运行都不同 / 随机生成 / 随运行波动」的块
         —— 这等于作者声明「这段输出会随运行变化」，检查器就不再要求逐次一致。
      3) 带「已泛化 / 已脱敏」标记的块
         —— 这些是作者有意改写过的人工脱敏版（内网 IP / MAC / 主机名 / 用户名
         换成了占位符），不可能与日志逐字一致。
         这个约定让「不把作者机器上的信息写进文档」和「文档里的数字都有出处」两件事共存。

用法：
    python3 tools/check_doc_quotes.py            # 先跑 ./run_all.sh 生成最新日志
退出码：0 = 全部匹配；1 = 有文档引用找不到对应日志（需要人工确认）
"""

from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def select_logs() -> list[str]:
    """挑出「本次运行」的日志。

    run_all.sh 会写 .logs/.manifest（它本次真正生成的文件清单）。
    有 manifest 就只认它 —— 否则 .logs/ 里残留的旧日志（比如手工跑的
    demo_13_wait.log、上一版代码的日志）会被当成证据，造成假阳性/假阴性。
    没有 manifest 时退回「全部日志」，并明确警告。
    """
    manifest = os.path.join(ROOT, ".logs", ".manifest")
    if os.path.isfile(manifest):
        paths = []
        for line in open(manifest, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            path = os.path.join(ROOT, line)
            if os.path.isfile(path):
                paths.append(path)
        if paths:
            return sorted(paths)
    print(
        "  [警告] 没有 .logs/.manifest（由 run_all.sh 生成），将扫描全部日志；"
        "旧日志可能造成误判。建议先跑一次 bash run_all.sh。"
    )
    return sorted(glob.glob(os.path.join(ROOT, ".logs", "*.log")))


WEEKDAYS = r"Mon|Tue|Wed|Thu|Fri|Sat|Sun"
MONTHS = r"Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec"


def mask(text: str) -> str:
    """把动态值抹平：空白折叠 + 日期名/十六进制/数字替换为 #。

    日期里的星期与月份名字是「会漂移的非数字」：10 月 14 日是 Wed、
    10 月 29 日是 Thu，如果不抹掉，仅仅重跑一次日志，文档就会被判错。
    """
    text = re.sub(r"\s+", " ", text)
    text = re.sub(rf"\b(?:{WEEKDAYS})\b", "#", text, flags=re.I)
    text = re.sub(rf"\b(?:{MONTHS})\b", "#", text, flags=re.I)
    text = re.sub(r"0x[0-9a-fA-F]+", "#", text)
    text = re.sub(r"[0-9a-fA-F]{6,}", "#", text)
    text = re.sub(r"\d+", "#", text)
    return text.strip()


def main() -> int:
    log_paths = select_logs()
    if not log_paths:
        print("找不到 .logs/*.log，先运行：bash run_all.sh")
        return 1
    blob = "\n".join(
        open(p, encoding="utf-8", errors="replace").read() for p in log_paths
    )
    masked_blob = mask(blob)

    checked = 0
    skipped_blocks = 0
    missing: list[tuple[str, str]] = []
    # 如果某个代码块后面紧跟的说明里写了下面这些关键词，说明作者已经解释过
    # 「这段输出为什么要打折扣」，跳过逐字检查（但要求写清楚理由）。
    VOLATILE_MARKERS = (
        "每次运行都不同",
        "随机生成",
        "值会随运行变化",
        "随运行波动",
        "网络波动",
        "已泛化",
        "已脱敏",
        "环境差异",
        "取决于本机",
    )

    for doc in sorted(glob.glob(os.path.join(ROOT, "doc", "*.md"))):
        text = open(doc, encoding="utf-8").read()
        for block in re.finditer(r"```text\n(.*?)```", text, re.S):
            if any(
                marker in text[block.end() : block.end() + 300]
                for marker in VOLATILE_MARKERS
            ):
                skipped_blocks += 1
                continue
            for raw_line in block.group(1).splitlines():
                line = raw_line.strip()
                if len(line) < 12 or not re.search(r"\d", line):
                    continue
                # 行内注释（`<- 说明` / `← 说明`）之后的内容属于讲解，不参与比对
                core = re.split(r"<-|←", line)[0].strip()
                if len(core) < 10:
                    continue
                checked += 1
                if mask(core) not in masked_blob:
                    missing.append((os.path.relpath(doc, ROOT), line))

    print(
        f"检查了 {checked} 行文档引用（来自 {len(log_paths)} 个日志文件）"
        f"，另有 {skipped_blocks} 个代码块已声明「脱敏 / 值会变」而跳过"
    )
    if missing:
        print(
            f"\n以下 {len(missing)} 行在日志里找不到对应内容，请人工确认是否写错或编造："
        )
        for doc, line in missing:
            print(f"  {doc}: {line[:110]}")
        return 1
    print("全部匹配：文档里的实测输出都能在 .logs/ 中找到出处。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

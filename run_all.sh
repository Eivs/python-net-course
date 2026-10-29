#!/usr/bin/env bash
# run_all.sh -- 按顺序运行全部示例，把每个示例的完整输出存到 .logs/ 下
#
# 用法:
#   ./run_all.sh              # 跑全部 -> .logs/
#   ./run_all.sh 01           # 只跑 01_ 开头的目录（此时跳过文档引用完整性检查）
#   DEGRADED=1 ./run_all.sh   # 降级模式: 把子进程的 PATH 指向空目录,
#                             #   验证「没有 dig/openssl/sysctl/lsof 时」依然不崩
#                             #   日志写到 .logs-degraded/，不覆盖 .logs/
#
# 说明: 这套示例只用 Python 标准库，不需要 pip install 任何东西。
# 注意: 变量展开一律写成 ${VAR} 形式。macOS 自带的是 GNU bash 3.2,
#       当变量名后面紧跟全角括号这类多字节字符时, 它会把括号也当成变量名的一部分,
#       在 set -u 下直接以 "unbound variable" 退出（不信可以自己试一下）。

set -u
cd "$(dirname "$0")" || exit 1

# 解析解释器的绝对路径。两个坑:
#   1) 降级模式下 PATH 会被清空, 所以必须在这里就解析成绝对路径
#   2) pyenv 的 python3 是个 shim 脚本, 它自己也依赖 PATH;
#      所以用 sys.executable 拿到真正的解释器二进制路径
if [ -n "${PYTHON:-}" ]; then
    PY="$PYTHON"
else
    PY="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || command -v python3)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "找不到 python3, 用 PYTHON=/path/to/python3 指定"
    exit 1
fi
FILTER="${1:-}"

# 正常模式与降级模式写不同的目录:
#   降级日志缺 dig/openssl/sysctl 段, 如果覆盖 .logs/, 文档引用的「证据」就被毁了。
if [ "${DEGRADED:-0}" = "1" ]; then
    LOGDIR=".logs-degraded"
else
    LOGDIR=".logs"
fi
mkdir -p "$LOGDIR"
# 本次运行真正生成了哪些日志: check_doc_quotes 只认这份清单, 避免拿旧日志当证据。
MANIFEST="$LOGDIR/.manifest"
# 只跑某一章(FILTER 非空)时不重建清单: 否则清单只剩这一章的日志,
# check_doc_quotes 会误以为其它章节的引用「找不到出处」。
if [ -z "${FILTER}" ]; then
    : > "$MANIFEST"
fi

# 子进程用的 PATH: 正常模式用原样, 降级模式用空目录
if [ "${DEGRADED:-0}" = "1" ]; then
    mkdir -p "$LOGDIR/emptybin"
    RUN_PATH="$PWD/$LOGDIR/emptybin"
    echo "[降级模式] 子进程 PATH=${RUN_PATH} (dig/openssl/sysctl/lsof 都不可见); 日志写入 ${LOGDIR}/"
else
    RUN_PATH="$PATH"
fi

pass=0
fail=0
failed_list=()

# 按路径排序 => 01_ -> 06_、目录内 demo_01 -> demo_18，天然就是学习顺序
files="$(find . \( -name 'demo_*.py' -o -name 'mini_http_server.py' \) | sort)"
if [ -n "$FILTER" ]; then
    files="$(printf '%s\n' "$files" | grep "/$FILTER" || true)"
fi
if [ -z "$files" ]; then
    echo "没有匹配的示例文件（过滤器: ${FILTER:-无}）"
    exit 1
fi

while IFS= read -r file; do
    name="$(basename "$file" .py)"
    log="${LOGDIR}/${name}.log"
    if [ -z "${FILTER}" ]; then
        printf '%s\n' "$log" >> "$MANIFEST"
    fi
    printf '%-34s ' "$name"
    start=$(date +%s)
    if env PATH="$RUN_PATH" "$PY" "$file" >"$log" 2>&1; then
        elapsed=$(( $(date +%s) - start ))
        printf 'OK   (%2ss)  -> %s\n' "$elapsed" "$log"
        pass=$((pass + 1))
    else
        code=$?
        printf 'FAIL (exit=%s) -> %s\n' "$code" "$log"
        fail=$((fail + 1))
        failed_list+=("$name")
    fi
done < <(printf '%s\n' "$files")

echo
echo "================= 一致性检查 ================="
# 三个检查器：课程链（课号/下一课指针/doc 链接）、隐私底线（不把本机信息写进文案）、
# doc 引文是否有日志出处。任何一项失败都会计入下面的汇总并以非 0 退出。
for checker in tools/check_lesson_chain.py tools/check_no_local_data.py tools/check_doc_quotes.py; do
    [ -f "$checker" ] || continue
    name="$(basename "$checker" .py)"
    if [ "$name" = "check_doc_quotes" ] && [ "${DEGRADED:-0}" = "1" ]; then
        echo "[跳过] $name: 降级模式的日志不完整（缺 dig/openssl/sysctl 段），比对必然失败"
        continue
    fi
    if [ "$name" = "check_doc_quotes" ] && [ -n "${FILTER}" ]; then
        echo "[跳过] $name: 只跑了 ${FILTER}，日志不完整；完整性检查请跑全量"
        continue
    fi
    if "$PY" "$checker"; then
        :
    else
        fail=$((fail + 1))
        failed_list+=("$name")
    fi
done

echo
echo "================= 汇总 ================="
echo "通过: $pass   失败: $fail"
if [ "$fail" -gt 0 ]; then
    echo "失败列表: ${failed_list[*]}"
    echo "逐个看日志: ${LOGDIR}/<名字>.log"
    exit 1
fi
echo "全部通过。日志在 ${LOGDIR}/"

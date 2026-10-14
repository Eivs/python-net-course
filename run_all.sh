#!/usr/bin/env bash
# run_all.sh -- 按顺序运行全部示例，把每个示例的完整输出存到 .logs/ 下
#
# 用法:
#   ./run_all.sh              # 跑全部
#   ./run_all.sh 01           # 只跑 01_ 开头的目录
#   OFFLINE=1 ./run_all.sh    # 跳过需要公网的示例（本套示例都能离线降级）
#
# 说明: 这套示例只用 Python 标准库，不需要 pip install 任何东西。

set -u
cd "$(dirname "$0")" || exit 1

PY="${PYTHON:-python3}"
FILTER="${1:-}"
mkdir -p .logs

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
    log=".logs/${name}.log"
    printf '%-34s ' "$name"
    start=$(date +%s)
    if "$PY" "$file" >"$log" 2>&1; then
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
echo "================= 汇总 ================="
echo "通过: $pass   失败: $fail"
if [ "$fail" -gt 0 ]; then
    echo "失败列表: ${failed_list[*]}"
    echo "逐个看日志: .logs/<名字>.log"
    exit 1
fi
echo "全部通过。日志在 .logs/"

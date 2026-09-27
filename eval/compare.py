#!/usr/bin/env python3
"""对比两次评测结果（run_eval.py 产出的 results JSON）。

用法：
    python eval/compare.py eval/results/20260910-120000.json eval/results/20260911-090000.json

典型场景：经验闭环 A/B —— A = --without-experience（对照组），
B = --with-experience（实验组），对比成功率 / 成本 / 时长差异。

仅用标准库；输出文本 diff 表。退出码：0 = 正常输出；2 = 文件缺失/格式错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 汇总指标行：(显示名, key, 格式化函数, 差值格式化函数)
_RATE = lambda v: f"{v * 100:.1f}%" if isinstance(v, (int, float)) else "-"
_NUM2 = lambda v: f"{v:.2f}" if isinstance(v, (int, float)) else "-"
_INT = lambda v: str(int(v)) if isinstance(v, (int, float)) else "-"
_PP = lambda d: f"{d * 100:+.1f}pp" if isinstance(d, (int, float)) else "-"
_NUM2D = lambda d: f"{d:+.2f}" if isinstance(d, (int, float)) else "-"
_INTD = lambda d: f"{int(d):+d}" if isinstance(d, (int, float)) else "-"

METRIC_ROWS = [
    ("pass_rate", "成功率", _RATE, _PP),
    ("fail", "失败数", _INT, _INTD),
    ("skipped", "跳过数", _INT, _INTD),
    ("avg_duration_s", "平均时长(s)", _NUM2, _NUM2D),
    ("avg_turns", "平均turns", _NUM2, _NUM2D),
    ("avg_cost_usd", "平均成本($)", _NUM2, _NUM2D),
    ("total_tokens", "总token", _INT, _INTD),
]


def load_result(path: str | Path) -> dict:
    """读取并粗校验 results JSON。"""
    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"[compare] 结果文件不存在: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"[compare] 结果文件解析失败 {path}: {e}")
    if not isinstance(data, dict) or "summary" not in data or "results" not in data:
        raise SystemExit(f"[compare] 不是有效的评测结果文件（缺 summary/results）: {path}")
    return data


def _fmt(value) -> str:
    return "-" if value is None else str(value)


def compare_report(base: dict, cand: dict, base_name: str = "A",
                   cand_name: str = "B") -> str:
    """生成对比报告文本（纯函数，便于单测）。"""
    lines: list[str] = []
    base_sum, cand_sum = base.get("summary") or {}, cand.get("summary") or {}
    lines.append("==== 评测对比 ====")
    lines.append(f"  {base_name}: {base.get('created_at', '?')}  "
                 f"经验组={'with' if base.get('with_experience') else 'without'}"
                 f"  brain={_fmt(base.get('brain_url'))}")
    lines.append(f"  {cand_name}: {cand.get('created_at', '?')}  "
                 f"经验组={'with' if cand.get('with_experience') else 'without'}"
                 f"  brain={_fmt(cand.get('brain_url'))}")
    lines.append("")
    lines.append(f"  {'指标':<12} {base_name:>12} {cand_name:>12} {'Δ':>12}")
    for key, label, fmt, fmt_diff in METRIC_ROWS:
        a, b = base_sum.get(key), cand_sum.get(key)
        diff = (b - a) if isinstance(a, (int, float)) \
            and isinstance(b, (int, float)) else None
        lines.append(f"  {label:<12} {fmt(a):>12} {fmt(b):>12} {fmt_diff(diff):>12}")
    lines.append("")

    # 逐任务 pass/fail 矩阵
    by_task = {}
    for name, data in ((base_name, base), (cand_name, cand)):
        for r in data.get("results") or []:
            by_task.setdefault(r.get("task_id", "?"), {})[name] = r.get("verdict", "?")
    if by_task:
        lines.append(f"  {'task_id':<22} {base_name:>8} {cand_name:>8}  {'Δ':<10}")
        for task_id in sorted(by_task):
            a = by_task[task_id].get(base_name, "-")
            b = by_task[task_id].get(cand_name, "-")
            if a == "pass" and b == "pass":
                delta = "="
            elif a != "pass" and b == "pass":
                delta = "improved"
            elif a == "pass" and b != "pass":
                delta = "regressed"
            else:
                delta = "="
            lines.append(f"  {task_id:<22} {a:>8} {b:>8}  {delta:<10}")
    improved = sum(1 for v in by_task.values()
                   if v.get(base_name) != "pass" and v.get(cand_name) == "pass")
    regressed = sum(1 for v in by_task.values()
                    if v.get(base_name) == "pass" and v.get(cand_name) != "pass")
    lines.append("")
    lines.append(f"  改进 {improved} / 退步 {regressed}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="compare.py", description="对比两次评测结果 JSON")
    parser.add_argument("baseline", help="基线结果 JSON（如对照组）")
    parser.add_argument("candidate", help="候选结果 JSON（如实验组）")
    args = parser.parse_args(argv)

    base = load_result(args.baseline)
    cand = load_result(args.candidate)
    print(compare_report(base, cand,
                         base_name=Path(args.baseline).stem[:12],
                         cand_name=Path(args.candidate).stem[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

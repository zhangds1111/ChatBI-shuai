# -*- coding: utf-8 -*-
"""评估入口：用指定用例集评测 ChatBISystem，打印报告并保存结果。

用法：
    uv run python run_eval.py                          # 默认 test_cases.json，标签=时间戳
    uv run python run_eval.py --tag baseline           # 结果存 eval_results/baseline_*
    uv run python run_eval.py --cases test_cases.json --tag after

输出：
    eval_results/<tag>_report.txt    文本报告（与 evaluator.generate_report 一致）
    eval_results/<tag>_results.json  逐题明细（含生成 SQL、列行数对比、check_points）
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from evaluator import Evaluator
from main import ChatBISystem


def main() -> None:
    parser = argparse.ArgumentParser(description="ChatBI Text2SQL 评估")
    parser.add_argument("--cases", default="test_cases.json", help="用例集 JSON 路径")
    parser.add_argument("--tag", default=None, help="结果文件标签（默认时间戳）")
    parser.add_argument("--out-dir", default="eval_results", help="结果输出目录")
    args = parser.parse_args()

    tag = args.tag or datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)

    system = ChatBISystem()

    def sql_generator(question: str) -> str:
        result = system.run(question)
        if not result.get("success") or not result.get("sql"):
            raise RuntimeError(result.get("error") or "SQL 生成失败")
        return result["sql"]

    evaluator = Evaluator()
    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)
    results = evaluator.evaluate_all(cases, sql_generator)
    report = evaluator.generate_report(results)
    print(report)

    # 逐题明细：合并用例中的 check_points / expected_result 便于追溯
    case_by_id = {c.get("id"): c for c in cases}
    detail = []
    for r in results:
        c = case_by_id.get(r["id"], {})
        detail.append({
            **r,
            "check_points": c.get("check_points"),
            "expected_result": c.get("expected_result"),
        })
    # Decimal 等非 JSON 类型兜底
    def _default(o):
        return str(o)

    (out_dir / f"{tag}_report.txt").write_text(report, encoding="utf-8")
    (out_dir / f"{tag}_results.json").write_text(
        json.dumps(detail, ensure_ascii=False, indent=1, default=_default),
        encoding="utf-8",
    )
    n_pass = sum(1 for r in results if r["execution_match"])
    print(f"结果已保存：{out_dir / f'{tag}_report.txt'}")
    print(f"逐题明细：{out_dir / f'{tag}_results.json'}")
    print(f"Execution Accuracy：{n_pass}/{len(results)}")


if __name__ == "__main__":
    main()

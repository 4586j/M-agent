#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""数据专家 Agent 入口脚本"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from collectors.data_loader import DataLoader
from analyzers.data_cleaner import DataCleaner
from analyzers.stat_analyzer import StatAnalyzer
from analyzers.viz_generator import VizGenerator
from analyzers.score_calculator import ScoreCalculator
from generators.report_generator import ReportGenerator
from generators.feishu_publisher import FeishuPublisher


def run_analysis(file_path: str, visualize: bool = True):
    """执行完整数据分析流程"""
    print(f"📂 正在加载数据: {file_path}")
    df = DataLoader.load(file_path)
    print(f"✅ 加载成功: {df.shape[0]} 行, {df.shape[1]} 列")

    # 清洗
    print("🧹 开始数据清洗...")
    cleaner = DataCleaner()
    df_cleaned = cleaner.clean(df)
    cleaning_report = cleaner.get_cleaning_report(df, df_cleaned)
    print(f"   删除重复行: {cleaning_report['rows_removed']}, 删除列: {cleaning_report['cols_removed']}")

    # 统计分析
    print("📈 执行统计分析...")
    stat_analyzer = StatAnalyzer()
    stats = stat_analyzer.describe(df_cleaned)
    print(f"   数值列数: {len(stats.get('basic_stats', {}))}")

    # 数据质量评分
    print("🔍 计算数据质量评分...")
    scorer = ScoreCalculator()
    quality_score = scorer.calculate(df_cleaned)
    print(f"   综合评分: {quality_score.overall:.1f} ({quality_score.grade}级)")

    # 可视化
    chart_paths = []
    if visualize:
        print("📊 生成可视化图表...")
        viz = VizGenerator()
        chart_paths = viz.auto_generate(df_cleaned, prefix=Path(file_path).stem)
        print(f"   已生成 {len(chart_paths)} 个图表")

    # 生成报告
    print("📝 生成分析报告...")
    reporter = ReportGenerator()
    report_content = reporter.generate(
        file_name=Path(file_path).name,
        df_shape=df_cleaned.shape,
        stats=stats,
        quality_score=quality_score,
        cleaning_report=cleaning_report,
        chart_paths=chart_paths,
    )
    report_path = reporter.save_report(report_content, file_path)
    print(f"✅ 报告已保存: {report_path}")

    # 飞书摘要
    try:
        feishu = FeishuPublisher()
        summary = feishu.publish_summary(
            file_name=Path(file_path).name,
            row_count=df_cleaned.shape[0],
            col_count=df_cleaned.shape[1],
            quality_score=quality_score,
            cleaning_report=cleaning_report,
        )
        print("\n📋 飞书摘要预览:")
        print(summary)
    except Exception:
        pass

    return {
        "report_path": report_path,
        "charts": chart_paths,
        "quality_score": quality_score.overall,
        "quality_grade": quality_score.grade,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="数据专家 Agent - 自动化数据分析工具")
    parser.add_argument("--file", required=True, help="数据文件路径")
    parser.add_argument("--no-viz", action="store_true", help="不生成可视化图表")
    args = parser.parse_args()

    result = run_analysis(args.file, visualize=not args.no_viz)
    print(f"\n🏁 分析完成！评分: {result['quality_score']:.1f} ({result['quality_grade']}级)")

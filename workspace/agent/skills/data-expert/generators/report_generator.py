from datetime import datetime
from pathlib import Path
from typing import Dict, List


class ReportGenerator:
    """生成 Markdown 分析报告"""

    def __init__(self, output_dir: str = "workspace/agent/reports/data-expert"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        file_name: str,
        df_shape: tuple,
        stats: Dict,
        quality_score,
        cleaning_report: Dict,
        chart_paths: List[str],
    ) -> str:
        """生成完整报告"""
        lines: List[str] = []

        lines.append("# 数据专家分析报告")
        lines.append("")

        # 基本信息
        lines.append("## 文件信息")
        lines.append("| 属性 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 文件名 | `{file_name}` |")
        lines.append(f"| 行数 | {df_shape[0]} |")
        lines.append(f"| 列数 | {df_shape[1]} |")
        lines.append(f"| 分析时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |")
        lines.append("")

        # 数据质量评分
        lines.append("## 数据质量评分")
        lines.append(f"**综合评分**: {quality_score.overall:.1f} ({quality_score.grade}级)")
        lines.append(f"- 完整性: {quality_score.completeness:.1f}")
        lines.append(f"- 一致性: {quality_score.consistency:.1f}")
        lines.append(f"- 准确性: {quality_score.accuracy:.1f}")
        lines.append(f"- **状态**: {'通过' if quality_score.passed else '不通过'}")
        lines.append("")

        # 清洗报告
        lines.append("## 数据清洗记录")
        lines.append(f"- 删除重复行: {cleaning_report.get('rows_removed', 0)} 行")
        lines.append(f"- 删除列（缺失率>30%）: {cleaning_report.get('cols_removed', 0)} 列")
        lines.append(f"- 填充缺失值: {cleaning_report.get('missing_handled', 0)} 个单元格")
        lines.append("")

        # 描述性统计
        basic_stats = stats.get("basic_stats", {}) if stats else {}
        if basic_stats:
            lines.append("## 描述性统计")
            for col, stat in list(basic_stats.items())[:10]:
                lines.append(f"### {col}")
                lines.append(f"- 均值: {stat.get('mean', 'N/A'):.2f}" if isinstance(stat.get("mean"), (int, float)) else f"- 均值: N/A")
                lines.append(f"- 标准差: {stat.get('std', 'N/A'):.2f}" if isinstance(stat.get("std"), (int, float)) else f"- 标准差: N/A")
                lines.append(f"- 最小值: {stat.get('min', 'N/A'):.2f}" if isinstance(stat.get("min"), (int, float)) else f"- 最小值: N/A")
                lines.append(f"- 25%分位: {stat.get('25%', 'N/A'):.2f}" if isinstance(stat.get("25%"), (int, float)) else f"- 25%分位: N/A")
                lines.append(f"- 中位数: {stat.get('50%', 'N/A'):.2f}" if isinstance(stat.get("50%"), (int, float)) else f"- 中位数: N/A")
                lines.append(f"- 75%分位: {stat.get('75%', 'N/A'):.2f}" if isinstance(stat.get("75%"), (int, float)) else f"- 75%分位: N/A")
                lines.append(f"- 最大值: {stat.get('max', 'N/A'):.2f}" if isinstance(stat.get("max"), (int, float)) else f"- 最大值: N/A")
                lines.append("")

        # 图表
        if chart_paths:
            lines.append("## 可视化图表")
            for path in chart_paths:
                lines.append(f"![]({path})")
                lines.append("")

        # 改进建议
        lines.append("## 改进建议")
        suggestions: List[str] = []
        if quality_score.overall < 70:
            suggestions.append("1. 数据质量较低，建议执行更严格的清洗")
        if cleaning_report.get("rows_removed", 0) > 0:
            suggestions.append("2. 已自动删除重复行，请检查是否影响业务")
        if len(chart_paths) == 0:
            suggestions.append("3. 未能生成图表，请检查是否有数值列")
        if not suggestions:
            suggestions.append("数据质量良好，无需特别处理。")

        lines.extend(suggestions)
        lines.append("")
        lines.append("---")
        lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")

        return "\n".join(lines)

    def save_report(self, content: str, file_name: str) -> str:
        """保存报告到文件"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = Path(file_name).stem
        report_path = self.output_dir / f"{base}_report_{timestamp}.md"
        report_path.write_text(content, encoding="utf-8")
        return str(report_path)

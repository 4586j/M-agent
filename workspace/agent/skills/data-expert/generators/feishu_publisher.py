from typing import Dict


class FeishuPublisher:
    """飞书消息推送（桩实现，需配置后启用）"""

    def __init__(self, config: Dict = None):
        self.config = config or {}

    def publish_summary(self, file_name: str, row_count: int, col_count: int, quality_score, cleaning_report: Dict) -> str:
        """生成飞书卡片格式摘要文本"""
        lines = []
        lines.append("📊 数据概览报告")
        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📁 文件：{file_name}")
        lines.append(f"📊 行数：{row_count:,}  |  列数：{col_count}")
        lines.append(f"⏰ 分析时间：{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("")
        lines.append("📈 关键指标")
        lines.append(f"- 综合评分：{quality_score.overall:.1f} ({quality_score.grade}级)")
        lines.append(f"- 完整性：{quality_score.completeness:.1f}")
        lines.append(f"- 一致性：{quality_score.consistency:.1f}")
        lines.append(f"- 准确性：{quality_score.accuracy:.1f}")
        lines.append("")
        missing_ratio = (cleaning_report.get("missing_handled", 0) / (row_count * col_count) * 100) if (row_count * col_count) > 0 else 0
        lines.append(f"🔍 缺失值占比：{missing_ratio:.1f}%")
        lines.append("")

        if quality_score.overall < 70:
            lines.append("💡 建议：数据质量较低，建议执行更严格的清洗")
        elif quality_score.overall < 80:
            lines.append("💡 建议：数据质量合格，建议人工确认关键列")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")

        return "\n".join(lines)

    def push(self, content: str):
        """推送消息到飞书（桩实现）"""
        raise NotImplementedError("FeishuPublisher.push() 暂未实现，需配置飞书 Webhook 后启用")

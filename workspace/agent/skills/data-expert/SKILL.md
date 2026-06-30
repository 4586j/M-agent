---
name: data-expert
version: 1.0.0
description: >
  自动化数据分析专家 Skill。接收用户上传的数据文件（CSV / Excel / JSON / Parquet），
  自动完成数据清洗、描述性统计、可视化图表生成、数据质量评分和结构化 Markdown 报告输出。
author: data-expert-team
tags:
  - data
  - analysis
  - visualization
  - pandas
  - matplotlib
  - seaborn
  - data-quality
dependencies:
  - pandas
  - numpy
  - matplotlib
  - seaborn
  - openpyxl      # Excel 读取
  - pyarrow        # Parquet 读取
allowed_tools:
  - bash
  - read_file
  - write_file
  - code
triggers:
  - 分析数据
  - 数据报告
  - 数据质量
  - 生成图表
  - 统计分析
---

# 数据专家 Agent（data-expert）

## 概述

数据专家 Agent 是一个全自动的数据分析流水线 Skill，能够完成从原始数据读取到结构化报告输出的完整流程。

### 核心能力

| 能力 | 说明 |
|------|------|
| 多格式数据加载 | 自动识别 CSV / Excel / JSON / Parquet 格式 |
| 智能数据清洗 | 去重、缺失值填充、类型标准化、IQR 异常值裁剪 |
| 统计分析 | 描述性统计（含偏度/峰度）、Pearson 相关性矩阵、异常值检测 |
| 自动可视化 | 根据数据类型自动选择直方图、热力图、计数柱状图 |
| 质量评分 | 完整性 + 一致性 + 准确性三维加权评分（A–F 五级） |
| 报告生成 | 结构化 Markdown 报告 + PNG 图表文件 |

### 适用场景

- 用户说"帮我分析这份数据"、"看看这个 CSV 的质量"、"生成一份数据报告"
- 用户上传了 .csv / .xlsx / .json / .parquet 数据文件
- 用户需要快速了解数据概况、质量状况和基本统计特征

---

## 数据格式支持

| 格式 | 扩展名 | 读取方法 | 额外依赖 |
|------|--------|----------|----------|
| CSV | `.csv` | `pandas.read_csv()` | 无 |
| Excel | `.xls` `.xlsx` | `pandas.read_excel()` | `openpyxl` |
| JSON | `.json` | `pandas.read_json()` | 无 |
| Parquet | `.parquet` | `pandas.read_parquet()` | `pyarrow` |

> **大文件警告**：文件超过 10MB 时，禁止一次性读入内存。应使用 `DataLoader.load_large_csv()` 分块读取（chunksize=10000），或对数据进行随机抽样分析。

---

## 处理流程

```
用户输入（文件路径）
       │
       ▼
┌─────────────────┐
│  Step 1: 加载   │  DataLoader.load() → pd.DataFrame
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Step 2: 清洗   │  DataCleaner.clean()
│                 │  ├ 去重（drop_duplicates）
│                 │  ├ 缺失值（>30%删除列，其余中位数/众数填充）
│                 │  ├ 类型标准化（字符串→日期）
│                 │  └ 异常值裁剪（IQR 1.5 倍范围）
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Step 3: 分析   │  StatAnalyzer + ScoreCalculator
│                 │  ├ 描述性统计（mean/std/min/25%/50%/75%/max）
│                 │  ├ 偏度 / 峰度
│                 │  ├ 相关性矩阵（Pearson）
│                 │  └ 数据质量评分（三维加权）
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Step 4: 可视化 │  VizGenerator.auto_generate()
│                 │  ├ 数值列 → 直方图（最多 5 个）
│                 │  ├ ≥2 数值列 → 相关性热力图
│                 │  └ 类别列（≤20 类）→ 计数柱状图（最多 3 个）
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Step 5: 报告   │  ReportGenerator
│                 │  ├ Markdown 报告（含表格 + 图表引用）
│                 │  ├ 飞书卡片摘要（可选）
│                 │  └ 输出到 workspace/agent/reports/data-expert/
└─────────────────┘
```

---

## 使用方式

### 方式一：命令行

```bash
# 完整分析（含可视化）
python run_analysis.py --file data.csv

# 仅统计分析，跳过图表生成
python run_analysis.py --file data.csv --no-viz
```

**命令行参数**：

| 参数 | 必需 | 说明 |
|------|------|------|
| `--file` | 是 | 数据文件的绝对或相对路径 |
| `--no-viz` | 否 | 跳过可视化图表生成，加速纯数据分析 |

### 方式二：Python 模块调用

```python
from run_analysis import run_analysis

result = run_analysis("data.csv", visualize=True)

# 返回值结构
print(result["report_path"])    # str  : 报告文件路径
print(result["charts"])         # list : 图表文件路径列表
print(result["quality_score"])  # float: 综合评分
print(result["quality_grade"])  # str  : 评分等级 (A-F)
```

### 方式三：JiuwenSwarm Agent 调用

在对话中直接发送：

```
@机器人 分析附件中的销售数据，生成报告
```

Agent 会自动加载文件并执行全流程分析，返回摘要卡片和报告链接。

---

## 数据质量评分体系

评分从三个维度加权计算：

| 维度 | 权重 | 计算方法 | 满分 |
|------|------|----------|------|
| 完整性 | 0.4 | `非空单元格数 / 总单元格数 × 100` | 100 |
| 一致性 | 0.3 | 检测对象列中数字字符串混入，每次违规扣 10 分 | 100 |
| 准确性 | 0.3 | Z-score > 3 的异常值比例，`100 × (1 − ratio)` | 100 |

**综合评分** = 完整性 × 0.4 + 一致性 × 0.3 + 准确性 × 0.3

**等级映射**：

| 等级 | 分数区间 | 状态 | 建议操作 |
|------|----------|------|----------|
| **A** | 90 – 100 | 数据质量优秀 | 可直接用于分析 |
| **B** | 80 – 89 | 良好，少量问题 | 关注提示项后可使用 |
| **C** | 70 – 79 | 合格，需人工确认 | 建议检查关键列 |
| **D** | 60 – 69 | 较差，建议清洗 | 需进一步清洗后再分析 |
| **F** | < 60 | 很差，无法直接使用 | 建议回溯数据源重新采集 |

---

## 输出说明

### 报告文件

- **路径**：`workspace/agent/reports/data-expert/`
- **命名**：`{原文件名}_report_{YYYYMMDD_HHMMSS}.md`
- **内容结构**：文件信息 → 质量评分 → 清洗记录 → 描述性统计 → 图表 → 改进建议

### 图表文件

- **路径**：与报告相同目录
- **命名规范**：
  - `{prefix}_hist_{列名}.png` — 直方图
  - `{prefix}_corr.png` — 相关性热力图
  - `{prefix}_count_{列名}.png` — 计数柱状图

### 飞书卡片格式

```
📊 数据概览报告

━━━━━━━━━━━━━━━━━━━━━━
📁 文件：销售数据.csv
📊 行数：5,234  |  列数：12
⏰ 分析时间：2026-05-30 10:30
━━━━━━━━━━━━━━━━━━━━━━

📈 关键指标
- 综合评分：78.5 (C级)
- 完整性：92.1
- 一致性：80.0
- 准确性：65.0

🔍 缺失值占比：3.2%

💡 建议
1. 发现 2 列缺失率>10%，建议补充或删除
2. 日期列格式不统一，已自动标准化

━━━━━━━━━━━━━━━━━━━━━━
📄 详细报告已保存：report.md
📊 图表已保存：scatter.png, histogram.png
```

---

## 错误处理

| 错误场景 | 系统行为 |
|----------|----------|
| 不支持的扩展名 | `ValueError("不支持的文件格式: {suffix}")` |
| 文件不存在 | pandas 原生 `FileNotFoundError` |
| 无数值列 | 统计返回空字典，可视化步骤自动跳过 |
| 常数列（std=0） | 准确性计算自动跳过该列，不影响整体评分 |
| 飞书未配置 | try-except 捕获，不影响报告生成 |
| 大文件（>10MB） | 提示使用 `load_large_csv()`，禁止直接加载 |

---

## 依赖安装

```bash
# 核心依赖（必需）
pip install pandas numpy matplotlib seaborn

# 扩展格式支持
pip install openpyxl pyarrow

# 可选：机器学习扩展
pip install scikit-learn
```

---

## 项目结构

```
data-expert/
├── SKILL.md                    # 本文件
├── config.py                   # 配置管理（用户自行填写）
├── run_analysis.py             # 命令行入口 + 流水线编排
├── models/                     # 数据模型
│   ├── data_profile.py         # DataProfile
│   ├── quality_metric.py       # DataQualityScore
│   └── analysis_result.py      # AnalysisResult
├── collectors/                 # 采集层
│   ├── data_loader.py          # DataLoader
│   └── file_watcher.py         # FileWatcher（桩）
├── analyzers/                  # 分析层
│   ├── data_cleaner.py         # DataCleaner
│   ├── stat_analyzer.py        # StatAnalyzer
│   ├── viz_generator.py        # VizGenerator
│   └── score_calculator.py     # ScoreCalculator
└── generators/                 # 生成层
    ├── report_generator.py     # ReportGenerator
    └── feishu_publisher.py     # FeishuPublisher（桩）
```

---

## 注意事项

1. **内存安全**：大文件（>10MB）必须分块读取，禁止直接 `pd.read_csv()` 全量加载。
2. **中文支持**：`VizGenerator` 已配置中文字体（`SimHei`），Linux 服务器需安装对应字体包。
3. **无头环境**：`matplotlib` 已设置 `Agg` 后端，无需 GUI。
4. **配置安全**：`config.py` 中 API Key 等敏感信息由用户自行填写，不纳入版本控制。
5. **幂等性**：每次分析生成带时间戳的新报告文件，不会覆盖历史结果。

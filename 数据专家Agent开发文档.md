# 数据专家 Agent 开发文档

> 基于 JiuwenSwarm 的自动化数据分析流水线

---

## 一、项目概述

### 1.1 项目目标

构建一个自动化数据分析 Agent，实现从原始数据到分析报告的全流程自动化，解决数据分析中的三大核心痛点：

| 痛点 | 描述 | 解决方案 |
|------|------|----------|
| 重复劳动多 | 读取文件、处理缺失值、生成标准图表每次都要重写代码 | 标准化清洗流程 + 自动图表生成 |
| 工具分散 | pandas、matplotlib、seaborn 各管一摊，结果难以整合 | 统一流水线，一次调用产出完整结果 |
| 缺乏规范 | 分析质量参差不齐，无统一质量评估 | 内置数据质量评分体系（A-F 五级） |

### 1.2 核心能力

- 自动化数据预处理（缺失值、异常值、格式统一）
- 智能统计分析（描述性统计、相关性、分布检验）
- 一键可视化（根据数据类型自动选择图表）
- 数据质量评分（完整性、一致性、准确性三维度）
- 生成可读报告（Markdown + 图表文件）

---

## 二、架构设计

### 2.1 分层架构

采用 **Collectors → Analyzers → Generators** 三层流水线架构：

```
┌─────────────────────────────────────────────────────────┐
│                 Application Layer                        │
│                (data-expert skill)                       │
├─────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │  Collectors  │→ │  Analyzers   │→ │  Generators  │  │
│  │  数据采集层   │  │  分析引擎层   │  │  报告生成层   │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
│         │                  │                  │         │
│   DataLoader         DataCleaner         ReportGen      │
│   FileWatcher        StatAnalyzer        VizGenerator    │
│                      ScoreCalculator     FeishuPush      │
└─────────────────────────────────────────────────────────┘
```

### 2.2 数据处理流程

```
用户上传文件 / 指定数据源
         │
         ▼
┌──────────────────┐
│    COLLECTORS    │  数据采集
│   DataLoader     │  支持 CSV/Excel/JSON/Parquet
│   FileWatcher    │  监听目录变化（可选）
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│    ANALYZERS     │  多维分析
│  DataCleaner     │  清洗 → 清洗报告
│  StatAnalyzer    │  统计 → 描述性统计/相关性
│  VizGenerator    │  可视化 → 图表文件
│  ScoreCalculator │  数据质量评分
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   GENERATORS     │  报告生成
│  ReportGenerator │  Markdown报告
│  FeishuPublisher │  飞书卡片推送
│  FileSaver       │  保存清洗后数据/图表
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│     OUTPUT       │
│  分析报告.md     │
│  清洗后数据.csv  │
│  图表文件.png    │
└──────────────────┘
```

### 2.3 核心组件一览

| 组件 | 类型 | 职责 | 文件 |
|------|------|------|------|
| DataLoader | Collector | 读取各种格式数据文件 | `collectors/data_loader.py` |
| DataCleaner | Analyzer | 缺失值、异常值、重复值处理 | `analyzers/data_cleaner.py` |
| StatAnalyzer | Analyzer | 描述性统计、相关性、分布检验 | `analyzers/stat_analyzer.py` |
| VizGenerator | Analyzer | 自动生成图表（柱状图、散点图、热力图等） | `analyzers/viz_generator.py` |
| ScoreCalculator | Analyzer | 数据质量评分（完整性、一致性、准确性） | `analyzers/score_calculator.py` |
| ReportGenerator | Generator | 生成 Markdown 分析报告 | `generators/report_generator.py` |
| FeishuPublisher | Generator | 推送摘要到飞书 | `generators/feishu_publisher.py` |

---

## 三、目录结构

```
workspace/agent/skills/data-expert/
├── SKILL.md                      # 技能定义（必须）
├── config.py                     # 配置管理
├── run_analysis.py               # 入口脚本
│
├── models/                       # 数据模型层
│   ├── __init__.py
│   ├── data_profile.py           # 数据概况模型
│   ├── quality_metric.py         # 质量指标模型
│   └── analysis_result.py        # 分析结果模型
│
├── collectors/                   # 数据采集层
│   ├── __init__.py
│   ├── data_loader.py            # 加载 CSV/Excel/JSON/Parquet
│   └── file_watcher.py           # 监听目录（可选）
│
├── analyzers/                    # 分析引擎层
│   ├── __init__.py
│   ├── data_cleaner.py           # 数据清洗
│   ├── stat_analyzer.py          # 统计分析
│   ├── viz_generator.py          # 可视化生成
│   └── score_calculator.py       # 数据质量评分
│
└── generators/                   # 报告生成层
    ├── __init__.py
    ├── report_generator.py       # Markdown 报告
    └── feishu_publisher.py       # 飞书推送
```

---

## 四、核心模块详细设计

### 4.1 数据模型层（models/）

#### 4.1.1 data_profile.py — 数据概况模型

| 字段 | 类型 | 说明 |
|------|------|------|
| file_name | str | 文件名 |
| row_count | int | 行数 |
| col_count | int | 列数 |
| column_names | List[str] | 列名列表 |
| dtypes | Dict[str, str] | 各列数据类型 |
| memory_usage | float | 内存占用（MB） |
| missing_summary | Dict[str, float] | 各列缺失率 |

#### 4.1.2 quality_metric.py — 质量指标模型

| 字段 | 类型 | 说明 |
|------|------|------|
| overall | float | 综合评分（0-100） |
| completeness | float | 完整性得分 |
| consistency | float | 一致性得分 |
| accuracy | float | 准确性得分 |
| grade | str | 等级（A/B/C/D/F） |
| passed | bool | 是否通过（≥60） |

#### 4.1.3 analysis_result.py — 分析结果模型

| 字段 | 类型 | 说明 |
|------|------|------|
| data_profile | DataProfile | 数据概况 |
| quality_score | DataQualityScore | 质量评分 |
| cleaning_report | Dict | 清洗记录 |
| statistics | Dict | 统计分析结果 |
| chart_paths | List[str] | 图表文件路径列表 |
| report_path | str | 报告文件路径 |

---

### 4.2 数据采集层（collectors/）

#### 4.2.1 data_loader.py — DataLoader

**职责**：根据文件扩展名自动选择对应的 pandas 读取方法，统一返回 DataFrame。

**支持的数据格式**：

| 格式 | 读取方法 | 额外依赖 |
|------|----------|----------|
| CSV | `pandas.read_csv()` | 无 |
| Excel (.xls/.xlsx) | `pandas.read_excel()` | openpyxl |
| JSON | `pandas.read_json()` | 无 |
| Parquet | `pandas.read_parquet()` | pyarrow |

**API 设计**：

```python
class DataLoader:
    @staticmethod
    def load(file_path: str, **kwargs) -> pd.DataFrame:
        """根据文件扩展名自动选择读取方法"""

    @staticmethod
    def load_large_csv(file_path: str, chunksize: int = 10000):
        """分块读取大文件（>10MB），返回迭代器，防止内存溢出"""
```

**实现要点**：
- 使用 `pathlib.Path` 解析扩展名，`suffix.lower()` 统一为小写比较
- 不支持的格式抛出 `ValueError` 并附带明确的错误信息
- 大文件场景必须使用 `chunksize` 分块读取，严禁一次性 `read_csv` 加载超过 10MB 的文件

#### 4.2.2 file_watcher.py — FileWatcher（可选）

**职责**：监听指定目录的文件变化（新增/修改），自动触发分析流程。

**技术选型**：`watchdog` 库

**状态**：可选模块，初期可暂不实现，优先完成命令行入口。

---

### 4.3 分析引擎层（analyzers/）

#### 4.3.1 data_cleaner.py — DataCleaner

**职责**：执行完整的数据清洗流程，包含四大步骤。

**处理流程**：

```
原始 DataFrame
    │
    ├── 1. _remove_duplicates()    → 删除完全重复的行
    │
    ├── 2. _handle_missing()       → 缺失率>30% 的列直接删除
    │                               → 数值列用中位数填充
    │                               → 类别列用众数填充
    │
    ├── 3. _standardize_dtypes()   → 尝试将字符串列转为日期类型
    │
    └── 4. _handle_outliers()      → IQR 方法，clip 到 [Q1-1.5*IQR, Q3+1.5*IQR]
                                    ↓
                              清洗后 DataFrame
```

**API 设计**：

```python
class DataCleaner:
    def __init__(self, config: Optional[Dict] = None):
        """初始化，missing_threshold 默认为 0.3"""

    def clean(self, df: pd.DataFrame) -> pd.DataFrame:
        """执行完整清洗流程，返回清洗后的 DataFrame"""

    def get_cleaning_report(self, original: pd.DataFrame, cleaned: pd.DataFrame) -> Dict:
        """生成清洗报告，包含 rows_removed, cols_removed, missing_handled"""
```

**关键参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| missing_threshold | 0.3 | 缺失率超过此值的列将被删除 |

**实现注意事项**：
- `fillna` 使用 `inplace=True` 以节省内存
- 类别列取 `mode()[0]` 前需判空，为空时填充 `'Unknown'`
- 日期类型转换使用 try-except 静默处理无法转换的列
- IQR 异常值检测仅作用于数值列

#### 4.3.2 stat_analyzer.py — StatAnalyzer

**职责**：提供描述性统计、相关性分析和异常值检测。

**API 设计**：

```python
class StatAnalyzer:
    def describe(self, df: pd.DataFrame) -> Dict:
        """生成描述性统计（含偏度、峰度），返回嵌套字典"""

    def correlation(self, df: pd.DataFrame, method: str = 'pearson') -> pd.DataFrame:
        """计算数值列的相关性矩阵，method 支持 pearson/spearman/kendall"""

    def detect_outliers_iqr(self, df: pd.DataFrame, col: str) -> Dict:
        """检测指定列的异常值，返回数量、边界值和占比"""
```

**describe() 返回结构**：

```python
{
    'basic_stats': {
        'column_a': {'mean': ..., 'std': ..., 'min': ..., '25%': ..., '50%': ..., '75%': ..., 'max': ...},
        ...
    },
    'skewness': {'column_a': 0.5, ...},
    'kurtosis': {'column_a': 1.2, ...},
}
```

**实现注意事项**：
- 统计运算仅作用于数值列（`select_dtypes(include=[np.number])`）
- 无数值列时 `describe()` 返回空字典，调用方需做防御性判断
- 相关性矩阵至少要有 2 个数值列才计算，否则返回空 DataFrame

#### 4.3.3 viz_generator.py — VizGenerator

**职责**：根据数据类型自动选择合适的图表类型并生成 PNG 文件。

**图表生成策略**：

| 数据类型 | 图表类型 | 触发条件 |
|----------|----------|----------|
| 数值列 | 直方图（histogram） | 最多取前 5 个数值列 |
| ≥2 个数值列 | 相关性热力图（heatmap） | 自动触发 |
| 类别列 | 计数柱状图（countplot） | 唯一值 ≤ 20，最多 3 个 |

**API 设计**：

```python
class VizGenerator:
    def __init__(self, output_dir: str = "workspace/agent/reports/data-expert"):
        """初始化输出目录并设置中文字体"""

    def auto_generate(self, df: pd.DataFrame, prefix: str = "analysis") -> List[str]:
        """自动生成全部图表，返回文件路径列表"""
```

**图表命名规范**：

```
{prefix}_hist_{column_name}.png    # 直方图
{prefix}_corr.png                   # 热力图
{prefix}_count_{column_name}.png    # 计数图
```

**实现注意事项**：
- 必须设置中文字体（`SimHei`、`DejaVu Sans` 作为 fallback），避免图表中文乱码
- 每次 `savefig` 后必须 `plt.close()` 释放内存
- 输出目录使用 `mkdir(parents=True, exist_ok=True)` 确保目录存在
- 分类列计数图仅当唯一值 ≤ 20 时才生成，避免类别过多导致图表不可读
- 统一使用 `dpi=100` 和 `bbox_inches='tight'` 确保输出一致性

#### 4.3.4 score_calculator.py — ScoreCalculator

**职责**：从完整性、一致性、准确性三个维度对数据质量进行量化评分。

**评分体系**：

| 维度 | 权重 | 计算方式 |
|------|------|----------|
| completeness（完整性） | 0.4 | `(非空单元格数 / 总单元格数) × 100` |
| consistency（一致性） | 0.3 | 检查对象列是否存在数字字符串混入，每列扣 10 分 |
| accuracy（准确性） | 0.3 | Z-score > 3 的异常值比例换算，`100 × (1 - avg_outlier_ratio)` |

**等级划分**：

| 等级 | 分数区间 | 状态 |
|------|----------|------|
| A | 90-100 | 数据质量优秀 |
| B | 80-89 | 良好，少量问题 |
| C | 70-79 | 合格，需人工确认 |
| D | 60-69 | 较差，建议清洗 |
| F | < 60 | 很差，无法直接使用 |

**API 设计**：

```python
@dataclass
class DataQualityScore:
    overall: float
    completeness: float
    consistency: float
    accuracy: float
    grade: str
    passed: bool

class ScoreCalculator:
    WEIGHTS = {'completeness': 0.4, 'consistency': 0.3, 'accuracy': 0.3}

    def calculate(self, df: pd.DataFrame) -> DataQualityScore:
        """计算完整的数据质量评分"""
```

**实现注意事项**：
- `_consistency_score` 中使用正则 `r'^\d+(\.\d+)?$'` 匹配数字字符串
- `_accuracy_score` 中对 `std()` 为 0 的常数列需做除零保护
- 无数值列时 accuracy 直接返回 100

---

### 4.4 报告生成层（generators/）

#### 4.4.1 report_generator.py — ReportGenerator

**职责**：将所有分析结果整合为一份结构化的 Markdown 报告。

**报告结构**：

```
# 📊 数据专家分析报告
  ├── 文件信息（文件名、行列数、分析时间）
  ├── 数据质量评分（综合 + 三维度 + 通过状态）
  ├── 数据清洗记录（删除行数、删除列数、填充单元格数）
  ├── 描述性统计（每列的 mean/std/min/25%/50%/75%/max）
  ├── 可视化图表（Markdown 图片引用）
  └── 改进建议（根据评分和清洗结果自动生成）
```

**API 设计**：

```python
class ReportGenerator:
    def __init__(self, output_dir: str = "workspace/agent/reports/data-expert"):
        """初始化输出目录"""

    def generate(self, file_name, df_shape, stats, quality_score, cleaning_report, chart_paths) -> str:
        """生成完整 Markdown 报告内容"""

    def save_report(self, content: str, file_name: str) -> str:
        """保存报告到文件，自动添加时间戳"""
```

**文件命名规范**：`{原文件名}_report_{YYYYMMDD_HHMMSS}.md`

**改进建议自动生成规则**：

| 条件 | 建议内容 |
|------|----------|
| overall < 70 | 建议执行更严格的清洗 |
| rows_removed > 0 | 提醒检查是否影响业务 |
| chart_paths 为空 | 提醒检查是否有数值列 |

#### 4.4.2 feishu_publisher.py — FeishuPublisher

**职责**：将分析摘要以飞书消息卡片格式推送。

**飞书卡片输出格式规范**：

```
📊 数据概览报告

━━━━━━━━━━━━━━━━━━━━━━
📁 文件：销售数据.csv
📊 行数：5,234  |  列数：12
⏰ 分析时间：2026-05-30 10:30
━━━━━━━━━━━━━━━━━━━━━━

📈 关键指标
- 销售额均值：¥12,345
- 销售额总和：¥1,234,567
- 缺失值占比：3.2%

🔍 数据质量评分：78.5 (C级)

💡 建议
1. 发现 2 列缺失率>10%，建议补充或删除
2. 日期列格式不统一，已自动标准化
```

---

### 4.5 SKILL.md 技能定义

```yaml
---
name: data-expert
version: 1.0.0
description: 自动化数据分析专家，支持数据清洗、统计分析、可视化、质量评分
tags: [data, analysis, visualization, pandas, matplotlib]
allowed_tools: [bash, read_file, write_file, code]
---
```

**重要限制**：对于大文件（>10MB）不要一次性读入内存，使用分块读取或抽样分析。

---

### 4.6 入口脚本（run_analysis.py）

**职责**：编排完整的分析流水线，是用户直接交互的命令行入口。

**命令行参数**：

| 参数 | 必需 | 说明 |
|------|------|------|
| `--file` | 是 | 数据文件路径 |
| `--no-viz` | 否 | 跳过可视化生成 |

**执行流程**：

```
1. DataLoader.load(file_path)            → DataFrame
2. DataCleaner.clean(df)                 → DataFrame (cleaned)
3. DataCleaner.get_cleaning_report()     → Dict
4. StatAnalyzer.describe(df_cleaned)     → Dict
5. ScoreCalculator.calculate(df_cleaned) → DataQualityScore
6. VizGenerator.auto_generate(df)        → List[str] (可选)
7. ReportGenerator.generate(...)         → str
8. ReportGenerator.save_report(...)      → str (文件路径)
```

**返回值结构**：

```python
{
    'report_path': str,
    'charts': List[str],
    'quality_score': float,
    'quality_grade': str
}
```

---

## 五、关键技术决策

### 5.1 与代码审查助手架构对比

| 维度 | 代码审查助手 | 数据专家 Agent |
|------|-------------|---------------|
| 采集层 | Git 克隆、本地扫描 | 文件加载（CSV/Excel 等） |
| 分析层 | Ruff/Radon/Bandit | 清洗/统计/可视化/评分 |
| 报告层 | Markdown + 飞书 | Markdown + 图表文件 |
| 评分体系 | 质量/安全/复杂度/风格 | 完整性/一致性/准确性 |

### 5.2 核心设计决策

1. **pandas 作为核心数据处理引擎**：生态丰富、性能稳定、文档完善。
2. **自动图表选择**：根据数据类型自动选择直方图、热力图、计数图，避免让 Agent 编写可视化代码，降低出错概率。
3. **分块读取大文件**：`load_large_csv()` 返回迭代器，防止内存溢出，明确限制 10MB 以上文件不得一次性加载。
4. **质量评分可量化**：三级评分让数据可用性从主观判断变为客观指标，给用户明确的改进方向。

### 5.3 扩展方向（待规划）

| 方向 | 描述 | 优先级 |
|------|------|--------|
| 智能推荐分析 | 根据数据特征自动建议统计检验（t检验、ANOVA） | P1 |
| 机器学习集成 | 自动训练简单预测模型（线性回归、聚类） | P2 |
| 增量分析 | 只分析新增数据，生成对比报告 | P2 |
| 自然语言查询 | 用户自然语言提问，Agent 自动转化为 pandas 操作 | P3 |

---

## 六、依赖清单

### 6.1 核心依赖

| 包名 | 用途 | 版本要求 |
|------|------|----------|
| pandas | 数据处理核心 | ≥1.5 |
| numpy | 数值计算 | ≥1.24 |
| matplotlib | 图表绘制 | ≥3.7 |
| seaborn | 高级可视化（热力图） | ≥0.12 |

### 6.2 可选依赖

| 包名 | 用途 |
|------|------|
| openpyxl | Excel 文件读写 |
| pyarrow | Parquet 文件读写 |
| scikit-learn | 机器学习扩展 |
| watchdog | 目录监听（FileWatcher） |

---

## 七、数据模型关系图

```
DataLoader.load()
       │
       ▼
  pd.DataFrame (原始)
       │
       ├──→ DataProfile (数据概况模型)
       │
       ├──→ DataCleaner.clean() → pd.DataFrame (清洗后)
       │         │
       │         └──→ CleaningReport (清洗记录)
       │
       ├──→ StatAnalyzer.describe() → Statistics (统计结果)
       │
       ├──→ ScoreCalculator.calculate() → DataQualityScore (质量评分)
       │
       ├──→ VizGenerator.auto_generate() → List[chart_paths]
       │
       └──→ ReportGenerator.generate() → AnalysisResult (最终结果)
                    │
                    ├──→ .md 报告文件
                    └──→ 飞书卡片推送
```

---

*文档生成时间：2026-05-30*

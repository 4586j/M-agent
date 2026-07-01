# 数据专家 Agent 项目报告

---

## 1 背景及功能概要

### 1.1 背景

在数据驱动的团队中，数据分析师经常面临大量重复性的数据处理请求。每天需要反复进行数据读取、缺失值处理、标准图表生成等工作，导致真正有价值的深度分析缺乏时间投入。当前数据分析工作存在三大核心痛点：

**重复劳动多**：每次分析都需要从零开始编写数据加载、清洗、可视化的代码，效率低下，且代码难以复用。

**工具分散**：pandas 用于数据处理、matplotlib 和 seaborn 用于可视化、scikit-learn 用于机器学习，各工具独立运作，分析结果分散，缺乏统一整合。

**缺乏规范**：不同分析人员产出的分析报告质量参差不齐，没有统一的数据质量评估标准和改进建议机制。

针对上述问题，本项目的目标是构建一个基于 JiuwenSwarm 框架的自动化数据分析 Agent——**数据专家（data-expert）**，实现从原始数据到分析报告的全流程自动化处理。

### 1.2 功能概要

数据专家 Agent 提供以下五大核心能力：

| 功能模块 | 描述 |
|----------|------|
| 自动化数据预处理 | 自动检测并处理缺失值、异常值、重复值，统一数据格式 |
| 智能统计分析 | 生成描述性统计（含偏度/峰度）、相关性矩阵、分布检验 |
| 一键可视化 | 根据数据类型自动选择并生成直方图、热力图、计数柱状图 |
| 数据质量评分 | 从完整性、一致性、准确性三个维度量化评分（A-F 五级） |
| 可读报告生成 | 输出结构化 Markdown 报告 + 图表文件，支持飞书卡片推送 |

支持的输入数据格式包括 CSV、Excel（.xls/.xlsx）、JSON、Parquet，覆盖常见数据分析场景。

---

## 2 系统设计

### 2.1 概要设计

#### 2.1.1 分层架构

系统借鉴成熟的 Collectors → Analyzers → Generators 三层流水线架构，各层职责清晰、松耦合、易扩展：

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

- **Collectors（数据采集层）**：负责从不同数据源加载数据，统一转换为 pandas DataFrame。支持 CSV、Excel、JSON、Parquet 四种格式，并提供大文件分块读取能力。
- **Analyzers（分析引擎层）**：核心处理层，包含数据清洗（DataCleaner）、统计分析（StatAnalyzer）、可视化生成（VizGenerator）、数据质量评分（ScoreCalculator）四个模块。
- **Generators（报告生成层）**：将分析结果整合输出为 Markdown 报告文件，并提供飞书消息推送接口。

#### 2.1.2 数据处理流程

```
用户上传文件 / 指定数据源
         │
         ▼
┌──────────────────┐
│    COLLECTORS    │  数据采集
│   DataLoader     │  根据扩展名自动选择读取方法
│                  │  支持 CSV / Excel / JSON / Parquet
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│    ANALYZERS     │  多维分析
│  DataCleaner     │  → 去重 → 缺失值处理 → 类型标准化 → 异常值裁剪
│  StatAnalyzer    │  → 描述性统计 / 相关性矩阵 / 异常值检测
│  VizGenerator    │  → 直方图 / 热力图 / 计数柱状图
│  ScoreCalculator │  → 完整性 + 一致性 + 准确性 = 综合评分
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   GENERATORS     │  报告生成
│  ReportGenerator │  → 结构化 Markdown 报告
│  FeishuPublisher │  → 飞书卡片摘要推送
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│     OUTPUT       │
│  分析报告.md     │
│  图表文件.png    │
│  飞书消息卡片    │
└──────────────────┘
```

#### 2.1.3 核心组件一览

| 组件 | 层级 | 职责 | 源文件 |
|------|------|------|--------|
| DataLoader | Collector | 多格式数据文件读取 | `collectors/data_loader.py` |
| FileWatcher | Collector | 目录变化监听（可选） | `collectors/file_watcher.py` |
| DataCleaner | Analyzer | 缺失值/异常值/重复值清洗 | `analyzers/data_cleaner.py` |
| StatAnalyzer | Analyzer | 描述性统计与相关性分析 | `analyzers/stat_analyzer.py` |
| VizGenerator | Analyzer | 自动图表生成 | `analyzers/viz_generator.py` |
| ScoreCalculator | Analyzer | 数据质量三维评分 | `analyzers/score_calculator.py` |
| ReportGenerator | Generator | Markdown 报告生成 | `generators/report_generator.py` |
| FeishuPublisher | Generator | 飞书卡片推送 | `generators/feishu_publisher.py` |

#### 2.1.4 数据模型设计

系统定义了三个核心数据模型：

**DataProfile（数据概况模型）**：记录数据文件的基本信息，包括文件名、行列数、列名列表、各列数据类型、内存占用和各列缺失率汇总。

**DataQualityScore（质量评分模型）**：封装五维评分结果——综合评分（overall）、完整性（completeness）、一致性（consistency）、准确性（accuracy）、等级（grade，A-F）和通过状态（passed）。

**AnalysisResult（分析结果模型）**：聚合全部分析产出，关联 DataProfile、DataQualityScore、清洗报告、统计结果、图表路径列表和报告文件路径。

#### 2.1.5 数据质量评分体系

评分从三个维度加权计算：

| 维度 | 权重 | 计算方法 |
|------|------|----------|
| 完整性（Completeness） | 40% | 非空单元格数 / 总单元格数 × 100 |
| 一致性（Consistency） | 30% | 检查对象列中数字字符串混入情况，满分 100，每次违规扣 10 分 |
| 准确性（Accuracy） | 30% | 基于 Z-score 法检测异常值比例，accuracy = 100 × (1 - avg_outlier_ratio) |

最终得分映射到五级评价：

| 等级 | 分数区间 | 评价 |
|------|----------|------|
| A | 90–100 | 数据质量优秀 |
| B | 80–89 | 良好，少量问题 |
| C | 70–79 | 合格，需人工确认 |
| D | 60–69 | 较差，建议清洗 |
| F | < 60 | 很差，无法直接使用 |

#### 2.1.6 可视化自动选择策略

VizGenerator 根据列的数据类型自动决定图表类型：

| 条件 | 图表类型 | 数量限制 |
|------|----------|----------|
| 数值列（float64/int64） | 直方图 | 最多 5 个 |
| 数值列 ≥ 2 个 | 相关性热力图 | 1 个 |
| 类别列（object/category）且唯一值 ≤ 20 | 计数柱状图 | 最多 3 个 |

#### 2.1.7 报告生成结构

Markdown 报告按以下结构组织：

1. **文件信息**：文件名、行列数、分析时间
2. **数据质量评分**：综合评分 + 三维度明细 + 通过状态
3. **数据清洗记录**：删除行数、删除列数、填充单元格数
4. **描述性统计**：每列的 mean/std/min/25%/50%/75%/max 及偏度/峰度
5. **可视化图表**：Markdown 图片引用
6. **改进建议**：根据评分和清洗结果自动生成针对性建议

#### 2.1.8 目录结构

```
workspace/agent/skills/data-expert/
├── SKILL.md                      # 技能定义
├── config.py                     # 配置管理
├── run_analysis.py               # 命令行入口
├── models/                       # 数据模型层
│   ├── __init__.py
│   ├── data_profile.py           # 数据概况模型
│   ├── quality_metric.py         # 质量指标模型
│   └── analysis_result.py        # 分析结果模型
├── collectors/                   # 数据采集层
│   ├── __init__.py
│   ├── data_loader.py            # 多格式数据加载
│   └── file_watcher.py           # 目录监听（桩）
├── analyzers/                    # 分析引擎层
│   ├── __init__.py
│   ├── data_cleaner.py           # 数据清洗
│   ├── stat_analyzer.py          # 统计分析
│   ├── viz_generator.py          # 图表生成
│   └── score_calculator.py       # 质量评分
└── generators/                   # 报告生成层
    ├── __init__.py
    ├── report_generator.py       # Markdown 报告
    └── feishu_publisher.py       # 飞书推送
```

### 2.2 关键设计决策

**决策一：选择 pandas 作为核心数据处理引擎**

pandas 是 Python 生态中最成熟的数据处理库，API 稳定、文档丰富、社区活跃。其 DataFrame 抽象统一了 CSV/Excel/JSON/Parquet 等多种格式的读写，天然适配本系统的多格式输入需求。

**决策二：自动图表选择而非 Agent 代码生成**

让 Agent 在运行时编写 matplotlib 代码存在不可控风险（语法错误、资源泄露、风格不一致）。本方案采用预定义图表生成策略，根据数据类型自动匹配图表类型，既保证输出质量，又降低出错概率。

**决策三：大文件分块读取**

对于超过 10MB 的数据文件，DataLoader 提供 `load_large_csv()` 方法返回迭代器，避免一次性加载导致内存溢出。这一设计直接应对生产环境中常见的大数据量场景。

**决策四：三级度可量化质量评分**

传统数据分析缺乏统一的质量标准，本方案引入完整性、一致性、准确性三维量化评分，将数据可用性从主观判断转化为客观指标，给用户明确的改进方向。

**决策五：三层流水线架构**

借鉴代码审查助手的成熟设计，将系统划分为 Collectors → Analyzers → Generators 三层。各层之间通过标准数据结构（DataFrame、DataQualityScore 等）传递信息，新增分析能力只需在对应层添加模块，无需修改其他层代码。

---

## 3 实现及验证

### 3.1 代码分析

以下选取系统中 5 处关键代码进行分析。

---

#### 3.1.1 数据加载器 —— 多格式自动分发

**文件**：`collectors/data_loader.py`，第 9–24 行

```python
@staticmethod
def load(file_path: str, **kwargs) -> pd.DataFrame:
    """根据文件扩展名自动选择读取方法"""
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return pd.read_csv(file_path, **kwargs)
    elif suffix in (".xls", ".xlsx"):
        return pd.read_excel(file_path, **kwargs)
    elif suffix == ".json":
        return pd.read_json(file_path, **kwargs)
    elif suffix == ".parquet":
        return pd.read_parquet(file_path, **kwargs)
    else:
        raise ValueError(f"不支持的文件格式: {suffix}")
```

**分析**：该函数是整个流水线的入口，通过 `pathlib.Path.suffix.lower()` 提取文件扩展名并统一为小写，以 if-elif 链匹配对应的 pandas 读取方法。设计要点有两点：一是使用 `**kwargs` 透传额外参数（如编码、分隔符），保持灵活性；二是不支持的格式直接抛出 `ValueError` 并附带回显实际后缀，方便用户排查。第 27–29 行还提供了独立的 `load_large_csv(..., chunksize)` 方法，返回迭代器以应对大文件场景，避免一次性加载导致内存溢出。

---

#### 3.1.2 数据清洗引擎 —— 流水线式多步清洗

**文件**：`analyzers/data_cleaner.py`，第 13–19 行（clean 主流程）和第 24–36 行（缺失值处理）

```python
def clean(self, df: pd.DataFrame) -> pd.DataFrame:
    """执行完整清洗流程"""
    df = self._remove_duplicates(df)
    df = self._handle_missing(df)
    df = self._standardize_dtypes(df)
    df = self._handle_outliers(df)
    return df

def _handle_missing(self, df: pd.DataFrame) -> pd.DataFrame:
    missing_ratio = df.isnull().mean()
    cols_to_drop = missing_ratio[missing_ratio > self.missing_threshold].index.tolist()
    df = df.drop(columns=cols_to_drop)

    for col in df.columns:
        if df[col].dtype in ("float64", "int64"):
            df[col].fillna(df[col].median(), inplace=True)
        else:
            mode_series = df[col].mode()
            fill_value = mode_series[0] if not mode_series.empty else "Unknown"
            df[col].fillna(fill_value, inplace=True)
    return df
```

**分析**：`clean()` 采用模板方法模式，将清洗流程编排为四个不可变步骤（去重 → 缺失值 → 类型标准化 → 异常值裁剪），每个步骤由独立私有方法实现，职责清晰。`_handle_missing()` 有两处防御性处理值得注意：一是先计算缺失率再删除高缺失列（默认阈值 30%，可在构造时通过 config 调整），二是类别列取 `mode()[0]` 前通过 `.empty` 判断防止空 Series 导致的索引异常，回退填充 `"Unknown"`。

---

#### 3.1.3 数据质量评分 —— 加权三维度量化

**文件**：`analyzers/score_calculator.py`，第 25–44 行

```python
def calculate(self, df: pd.DataFrame) -> DataQualityScore:
    completeness = self._completeness_score(df)
    consistency = self._consistency_score(df)
    accuracy = self._accuracy_score(df)

    overall = (
        completeness * self.WEIGHTS["completeness"]
        + consistency * self.WEIGHTS["consistency"]
        + accuracy * self.WEIGHTS["accuracy"]
    )

    grade = self._score_to_grade(overall)
    return DataQualityScore(
        overall=overall,
        completeness=completeness,
        consistency=consistency,
        accuracy=accuracy,
        grade=grade,
        passed=overall >= 60,
    )
```

**分析**：评分器采用加权求和模型，权重集中在类属性 `WEIGHTS` 中（完整性 0.4、一致性 0.3、准确性 0.3），调整权重只需修改一处。`_accuracy_score()` 方法（第 62–80 行）对常数列（标准差为 0）做了 `continue` 跳过，避免除以零；`_consistency_score()` 方法（第 53–60 行）使用正则 `r'^\d+(\.\d+)?$'` 检测对象列中数字字符串占比，每超过 50% 扣 10 分。最终通过 `passed = overall >= 60` 给出二值判断，低于 60 分直接标记为"很差，无法直接使用"。

---

#### 3.1.4 可视化生成器 —— 自动图表类型选择

**文件**：`analyzers/viz_generator.py`，第 20–42 行

```python
def auto_generate(self, df: pd.DataFrame, prefix: str = "analysis") -> List[str]:
    """根据数据类型自动生成图表"""
    saved_files: List[str] = []
    numeric_cols = df.select_dtypes(include=["float64", "int64"]).columns
    categorical_cols = df.select_dtypes(include=["object", "category"]).columns

    for col in numeric_cols[:5]:
        path = self._plot_histogram(df, col, prefix)
        if path:
            saved_files.append(path)

    if len(numeric_cols) >= 2:
        path = self._plot_correlation_heatmap(df, numeric_cols, prefix)
        if path:
            saved_files.append(path)

    for col in categorical_cols[:3]:
        if df[col].nunique() <= 20:
            path = self._plot_countplot(df, col, prefix)
            if path:
                saved_files.append(path)

    return saved_files
```

**分析**：该方法是自动可视化的核心决策逻辑。它先用 `select_dtypes` 将列按数值/类别分离，然后应用三条规则：数值列取前 5 个生成直方图、至少 2 个数值列时生成一张相关性热力图、类别列取前 3 个且唯一值 ≤ 20 才生成计数图（防止类别过多导致图表不可读）。文件第 1–2 行通过 `matplotlib.use("Agg")` 强制使用非交互后端，使服务端无 GUI 环境也能正常生成图片。每次 `savefig` 后调用 `plt.close()` 释放图形资源，防止长时间运行时的内存泄漏。

---

#### 3.1.5 入口脚本 —— 流水线编排

**文件**：`run_analysis.py`，第 20–65 行

```python
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
    ...

    # 统计分析
    stat_analyzer = StatAnalyzer()
    stats = stat_analyzer.describe(df_cleaned)
    ...

    # 数据质量评分
    scorer = ScoreCalculator()
    quality_score = scorer.calculate(df_cleaned)
    ...

    # 可视化（可选）
    if visualize:
        viz = VizGenerator()
        chart_paths = viz.auto_generate(df_cleaned, ...)
    ...

    # 报告生成
    reporter = ReportGenerator()
    report_content = reporter.generate(...)
    report_path = reporter.save_report(report_content, file_path)
    ...
```

**分析**：`run_analysis()` 是系统唯一的编排函数，严格遵循 Collectors → Analyzers → Generators 的顺序，每一步通过 print 输出进度信息，便于命令行用户感知执行状态。可视化步骤受 `--no-viz` 参数控制（第 92 行），允许用户跳过以加速纯数据分析场景。飞书摘要生成包裹在 try-except 中（第 68–80 行），即使飞书模块未配置或出错，也不影响主流程的完整执行。函数最终返回包含报告路径、图表列表、质量评分和等级的字典，支持被其他模块以编程方式调用。

### 3.2 运行截图

（略）

---

## 4 总结及体会

### 4.1 项目总结

本项目成功构建了一个完整的自动化数据分析 Agent，实现了从原始数据加载到结构化报告输出的全流程闭环。系统共编写 17 个文件（含模块代码、数据模型、配置模板和技能定义），核心功能覆盖数据清洗、统计分析、可视化生成、质量评分和报告输出五大环节。

在架构层面，三层流水线设计保证了系统的可扩展性——未来新增分析器（如异常检测、时间序列分解）只需在 analyzers 层添加新模块并注册即可。在工程层面，通过 `matplotlib.use("Agg")` 非交互后端、常数列除零保护、缺失值填充空值判断等细节处理，保证系统在边缘条件下的稳定性。

### 4.2 经验与体会

**分层架构的价值**：三层分离让每个模块职责单一，开发时只需关注当前层的接口约定，大幅降低心智负担。即使后续需要替换某个模块（如更换可视化库），也只影响单一文件。

**防御性编程的重要性**：数据分析场景中，输入数据的不确定性极高——可能无数值列、可能全是空值、可能标准差为零。在 `score_calculator.py` 和 `report_generator.py` 中加入了多处边界判断，确保系统不会因异常数据而崩溃。

**桩实现的实用意义**：FileWatcher 和 FeishuPublisher 当前为桩实现，不影响核心流水线运行。这种"核心可用、扩展待填"的方式适合迭代开发——先交付最小可用版本，再按需完善可选功能。

**量化评分的沟通价值**：A-F 五级评分让非技术用户也能直观理解数据质量。相比"这个数据有点问题"的模糊表述，"综合评分 78.5，C级"能直接驱动决策——是否清洗、是否回源、是否可用。

### 4.3 后续改进方向

| 方向 | 描述 | 优先级 |
|------|------|--------|
| 智能统计检验推荐 | 根据数据特征自动建议 t检验、ANOVA、卡方检验等 | P1 |
| 机器学习集成 | 自动训练线性回归、聚类等简单模型 | P2 |
| 增量分析 | 对比分析新增数据，生成差异报告 | P2 |
| 自然语言查询 | 用户以自然语言提问，自动转化为 pandas 操作 | P3 |
| FileWatcher 完善 | 集成 watchdog 实现目录监听的自动触发分析 | P3 |

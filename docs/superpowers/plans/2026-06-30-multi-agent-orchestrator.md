# 多 Agent 编排框架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Claude Code 上搭建「大脑 + executor + security-guard」三角色通用多 agent 编排框架，data-expert 作为首个被调用能力。

**Architecture:** 大脑是主会话里运行的 orchestrator skill，走七阶段工作流；executor 与 security-guard 是 `.claude/agents/` 下的子 agent 类型，由大脑用 Agent 工具派发。security-guard 靠工具集物理隔离保证只读。核心记忆复用内置 memory，经三方共识（全票）写入；上下文记忆由框架自管、按任务生命周期临时存在。

**Tech Stack:** Claude Code skills / subagents、YAML（安全规则）、Python + pytest + PyYAML（规则合并器与校验）。

## Global Constraints

- 运行基底为 Claude Code 子 agent；大脑 = 主会话 skill，executor / security-guard = `.claude/agents/*.md` 子 agent 类型。
- security-guard 工具集严格限定为 `Read, Glob, Grep, WebFetch`，**不得**包含 Write/Edit/Bash/NotebookEdit/Agent（物理隔离）。
- 安全检验返回 `block` 时，大脑不自行放行、不自行修正，必须停给用户决定（放行/修改/放弃）。
- 核心记忆只有三方独立投票全票 `pass` 才写入内置 memory，任一 `block` 即丢弃；大脑是核心记忆唯一写入者。
- 安全关卡①（指令）不可跳过；产物关卡可由用户在边界阶段声明跳过。
- 现有 `workspace/agent/skills/data-expert/` 保留不动，不感知框架存在。
- 所有框架文件路径限定在 `workspace/agent/skills/orchestrator/` 与 `.claude/agents/` 下。
- 工作目录为 `d:\agent\agent`，shell 为 bash（Git Bash），使用正斜杠相对路径。
- 部署形态：V1 为单机单进程子 agent（大脑=主会话，executor/security-guard=同进程派发）。派发、记忆、规则三类关注点的「做什么」与「在哪做」在 SKILL.md 中分开描述，为将来跨主机演进预留边界，但 V1 不写任何分布式代码。

---

## File Structure

| 文件 | 责任 |
|------|------|
| `workspace/agent/skills/orchestrator/security-rules/default.yaml` | 框架自带默认安全规则 |
| `workspace/agent/skills/orchestrator/security-rules/overrides/user.yaml` | 用户覆盖/扩展规则 |
| `workspace/agent/skills/orchestrator/security-rules/rules.py` | 规则合并器（覆盖同 id、追加新 id）+ 加载生效规则；兼作结构 lint |
| `workspace/agent/skills/orchestrator/security-rules/test_rules.py` | rules.py 的 pytest 测试 |
| `.claude/agents/security-guard.md` | security-guard 子 agent 定义（只读工具 + 角色 prompt） |
| `.claude/agents/executor.md` | executor 子 agent 定义（全工具 + 角色 prompt） |
| `workspace/agent/skills/orchestrator/SKILL.md` | 大脑七阶段工作流 + 安全硬约束 + 共识记忆流程 |
| `workspace/agent/skills/orchestrator/memory/context/.gitkeep` | 自管上下文记忆目录占位 |
| `docs/superpowers/plans/runbook-smoke-tests.md` | 端到端行为冒烟测试 runbook |

---

## Task 1: 安全规则与规则合并器

**Files:**
- Create: `workspace/agent/skills/orchestrator/security-rules/default.yaml`
- Create: `workspace/agent/skills/orchestrator/security-rules/overrides/user.yaml`
- Create: `workspace/agent/skills/orchestrator/security-rules/rules.py`
- Test: `workspace/agent/skills/orchestrator/security-rules/test_rules.py`

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `rules.py` 暴露 `merge_rules(default, overrides) -> list[dict]`、`load_effective_rules() -> list[dict]`、`load_raw(path) -> list[dict]`；规则 dict 含字段 `id, category, description, pattern, severity`。security-guard（Task 2）与 SKILL.md（Task 4）以相同语义引用这些规则。

- [ ] **Step 1: 安装测试依赖**

Run: `pip install pyyaml pytest`
Expected: 安装成功（已安装则显示 requirement already satisfied）

- [ ] **Step 2: 写失败测试**

Create `workspace/agent/skills/orchestrator/security-rules/test_rules.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from rules import merge_rules, load_effective_rules


def test_merge_overrides_by_id():
    default = [{"id": "R-01", "severity": "block", "pattern": "a", "category": "x", "description": "d"}]
    overrides = [{"id": "R-01", "severity": "warn", "pattern": "b", "category": "x", "description": "d"}]
    merged = merge_rules(default, overrides)
    assert len(merged) == 1
    assert merged[0]["severity"] == "warn"
    assert merged[0]["pattern"] == "b"


def test_merge_appends_new_rules():
    default = [{"id": "R-01", "severity": "block", "pattern": "a", "category": "x", "description": "d"}]
    overrides = [{"id": "R-02", "severity": "block", "pattern": "b", "category": "x", "description": "d"}]
    merged = merge_rules(default, overrides)
    assert {r["id"] for r in merged} == {"R-01", "R-02"}


def test_load_effective_rules_includes_defaults():
    rules = load_effective_rules()
    ids = {r["id"] for r in rules}
    assert "R-DANGER-01" in ids
    assert "R-SECRET-01" in ids


def test_override_takes_effect_in_load():
    rules = load_effective_rules()
    inject02 = next(r for r in rules if r["id"] == "R-INJECT-02")
    assert inject02["severity"] == "warn", "user override should relax R-INJECT-02 to warn"


def test_user_appended_rule_present():
    rules = load_effective_rules()
    assert "R-USER-01" in {r["id"] for r in rules}


def test_every_rule_has_required_fields():
    for r in load_effective_rules():
        assert "id" in r
        assert "category" in r
        assert "pattern" in r
        assert "severity" in r
        assert r["severity"] in ("block", "warn")
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd workspace/agent/skills/orchestrator/security-rules && python -m pytest test_rules.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rules'`（rules.py 尚未创建）

- [ ] **Step 4: 写 default.yaml**

Create `workspace/agent/skills/orchestrator/security-rules/default.yaml`:

```yaml
# 安全检验默认规则（框架自带，勿直接改；用 overrides/user.yaml 覆盖/扩展）
# severity: block=否决并停给用户; warn=记录但放行
# pattern 为正则字符串（YAML 单引号内反斜杠为字面量）
rules:
  - id: R-DANGER-01
    category: dangerous_command
    description: 禁止递归强制删除根目录/家目录
    pattern: 'rm\s+-rf\s+(/|~|\$HOME)(\s|$)'
    severity: block
  - id: R-DANGER-02
    category: dangerous_command
    description: 禁止强制推送到远程主干分支
    pattern: 'git\s+push.*--force.*\b(main|master)\b'
    severity: block
  - id: R-DANGER-03
    category: dangerous_command
    description: 禁止 hard reset 到远程分支
    pattern: 'git\s+reset\s+--hard\s+\S*origin/'
    severity: block
  - id: R-PATH-01
    category: path_escalation
    description: 禁止访问工作区外的越界路径
    pattern: '\.\./\.\./'
    severity: block
  - id: R-PATH-02
    category: path_escalation
    description: 禁止操作系统目录
    pattern: '(/etc|/usr|/bin|C:\\Windows|C:\\Program Files)'
    severity: block
  - id: R-INJECT-01
    category: injection
    description: 禁止使用 eval/exec 执行动态代码
    pattern: '\b(eval|exec)\s*\('
    severity: block
  - id: R-INJECT-02
    category: injection
    description: 禁止 shell=True 拼接命令
    pattern: 'shell\s*=\s*True'
    severity: block
  - id: R-INJECT-03
    category: injection
    description: 疑似未参数化 SQL 拼接（提示确认）
    pattern: '(SELECT|INSERT|UPDATE|DELETE).*(\+|%).*(--|;)'
    severity: warn
  - id: R-SECRET-01
    category: secret_leak
    description: 禁止硬编码 API key/token/密码
    pattern: '(api[_-]?key|secret|token|password)\s*=\s*["''][A-Za-z0-9]{16,}'
    severity: block
  - id: R-EXFIL-01
    category: data_exfil
    description: 禁止将凭证外发到网络
    pattern: '(curl|wget|requests\.post).*?(api[_-]?key|token|password)'
    severity: block
```

- [ ] **Step 5: 写 overrides/user.yaml**

Create `workspace/agent/skills/orchestrator/security-rules/overrides/user.yaml`:

```yaml
# 用户覆盖/扩展规则
# 语义：同 id 覆盖默认；新 id 追加
rules:
  - id: R-INJECT-02
    category: injection
    description: 本项目允许受控脚本使用 shell=True（按需放宽为 warn）
    pattern: 'shell\s*=\s*True'
    severity: warn
  - id: R-USER-01
    category: dangerous_command
    description: 禁止删除 reports 目录下的报告文件
    pattern: 'rm\s+.*reports/'
    severity: block
```

- [ ] **Step 6: 写 rules.py**

Create `workspace/agent/skills/orchestrator/security-rules/rules.py`:

```python
"""安全规则合并器与加载器。

合并语义（security-guard 在读取时遵循相同语义）：
overrides 中相同 id 的规则覆盖默认，新 id 追加。
兼作结构 lint：load_effective_rules 可被测试/CI 校验所有规则字段完整。
"""
from pathlib import Path
from typing import Any

import yaml

RULES_DIR = Path(__file__).parent


def load_raw(path: Path) -> list[dict[str, Any]]:
    """读取单个 YAML 文件，返回 rules 列表；文件不存在或为空返回空列表。"""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("rules", [])


def merge_rules(default: list[dict], overrides: list[dict]) -> list[dict]:
    """合并默认与覆盖规则：同 id 覆盖，新 id 追加。保留默认顺序，追加项在后。"""
    by_id: dict[str, dict] = {}
    for r in default:
        by_id[r["id"]] = r
    for r in overrides:
        by_id[r["id"]] = r
    return list(by_id.values())


def load_effective_rules() -> list[dict[str, Any]]:
    """加载默认 + 用户覆盖后的生效规则。"""
    default = load_raw(RULES_DIR / "default.yaml")
    overrides = load_raw(RULES_DIR / "overrides" / "user.yaml")
    return merge_rules(default, overrides)


if __name__ == "__main__":
    for r in load_effective_rules():
        print(f"{r['id']}\t{r['severity']}\t{r['category']}\t{r['description']}")
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd workspace/agent/skills/orchestrator/security-rules && python -m pytest test_rules.py -v`
Expected: 6 passed

- [ ] **Step 8: 提交**

```bash
git add workspace/agent/skills/orchestrator/security-rules/
git commit -m "feat(orchestrator): 安全规则与规则合并器

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 2: security-guard 子 agent 定义

**Files:**
- Create: `.claude/agents/security-guard.md`

**Interfaces:**
- Consumes: Task 1 的 `default.yaml` + `overrides/user.yaml`（读取并按 rules.py 语义合并）
- Produces: 名为 `security-guard` 的子 agent 类型；大脑（Task 4）用 `subagent_type=security-guard` 派发，期望其返回结构化裁决 `verdict/reason/matched_rules/suggestion`。

- [ ] **Step 1: 写 agent 定义**

Create `.claude/agents/security-guard.md`:

```markdown
---
name: security-guard
description: 只读安全检验 agent。对指令、关键中间产物、代码做安全检验，返回 pass/block 结构化裁决。只审查不执行。
tools: Read, Glob, Grep, WebFetch
---

你是 security-guard，一个**只读**的安全检验 agent。你的唯一职责是对输入内容做安全检验并返回结构化裁决。

## 工具限制（物理隔离）
你只有 Read、Glob、Grep、WebFetch 四个只读工具。你**没有** Write/Edit/Bash/NotebookEdit/Agent。
你无法修改文件、无法执行命令、无法派发子 agent。这是设计使然——你只审查，不执行。

## 工作流程
1. 读取默认规则：`workspace/agent/skills/orchestrator/security-rules/default.yaml`
2. 读取用户覆盖：`workspace/agent/skills/orchestrator/security-rules/overrides/user.yaml`（若存在）
3. 合并规则（遵循与 rules.py 相同的语义）：overrides 中相同 id 的规则覆盖默认，新 id 追加
4. 对输入内容逐条规则匹配：用正则思维比对每条 pattern
5. 规则之外的可疑情况，用你的判断补充
6. 返回**结构化裁决**，格式严格如下（仅输出该 YAML，不要额外解释）：

```yaml
verdict: pass | block
reason: "<命中规则或判断依据，一句话>"
matched_rules: [<规则 id 列表>]
suggestion: "<若 block，给出可操作的修改建议；pass 时留空>"
```

## 判定原则
- 只关心**安全**，不关心质量、风格、是否达到任务意图（那是大脑的职责）
- 任何一条 block 级规则命中 → verdict 为 block
- warn 级规则命中 → verdict 仍为 pass，但在 reason 中记录
- 模糊且偏向危险时，倾向 block，把决定权交给用户

## 不做的事
- 不修改任何文件
- 不执行任何命令
- 不替用户决定放行与否——你只给裁决，由大脑呈现给用户
```

- [ ] **Step 2: 校验 frontmatter 与工具隔离**

Run:
```
python -c "import yaml; t=open('.claude/agents/security-guard.md',encoding='utf-8').read(); d=yaml.safe_load(t.split('---',2)[1]); print(d['name'], '|', d.get('tools')); assert d['name']=='security-guard'; assert d['tools']=='Read, Glob, Grep, WebFetch'; assert 'Bash' not in d['tools'] and 'Write' not in d['tools']"
```
Expected: `security-guard | Read, Glob, Grep, WebFetch`

- [ ] **Step 3: 提交**

```bash
git add .claude/agents/security-guard.md
git commit -m "feat(agents): security-guard 只读安全检验子 agent

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 3: executor 子 agent 定义

**Files:**
- Create: `.claude/agents/executor.md`

**Interfaces:**
- Consumes: 大脑（Task 4）下发的任务 prompt；data-expert skill 或其 `run_analysis.py`
- Produces: 名为 `executor` 的子 agent 类型，全工具集（省略 `tools` 字段即授予全部工具）；产出代码/文件/结论并汇报给大脑。

- [ ] **Step 1: 写 agent 定义**

Create `.claude/agents/executor.md`:

```markdown
---
name: executor
description: 执行 agent。完成大脑下发的子任务，产出代码/文件/结论。可调用 data-expert 等技能。
---

你是 executor，执行 agent。你的职责是完成大脑下发的具体子任务并产出结果。

## 职责边界
- 你**执行**任务，不决定任务目标——目标由大脑给定
- 不直接与用户对话，所有交流经大脑
- 产出后把结果（文件路径、关键产出摘要、结论、异常）汇报给大脑

## 可用能力
- 全部工具（Read/Write/Edit/Bash/Glob/Grep 等）
- 数据分析任务：优先调用 data-expert 技能；若技能调用不可用，直接运行：
  `python workspace/agent/skills/data-expert/run_analysis.py --file <路径>`

## 工作要求
- 严格按大脑下发的任务说明执行，不自行扩大范围
- 涉及文件写入时，路径限定在 workspace/ 下
- 产出代码时遵循所在项目现有风格
- 完成后简明汇报：做了什么、产出在哪、有无异常

## 安全
- 你不负责安全自审——security-guard 会独立检验你的指令与产出
- 但你应避免明显危险操作（删除工作区外文件、硬编码秘密、强制推送等）
- 收到 security-guard 的修改建议时（经大脑转达），按建议修正后重做
```

- [ ] **Step 2: 校验 frontmatter 与全工具集**

Run:
```
python -c "import yaml; t=open('.claude/agents/executor.md',encoding='utf-8').read(); d=yaml.safe_load(t.split('---',2)[1]); print(d['name']); assert d['name']=='executor'; assert 'tools' not in d, 'executor 应为全工具集（无 tools 键）'"
```
Expected: `executor`

- [ ] **Step 3: 提交**

```bash
git add .claude/agents/executor.md
git commit -m "feat(agents): executor 执行子 agent

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 4: orchestrator 大脑 SKILL.md

**Files:**
- Create: `workspace/agent/skills/orchestrator/SKILL.md`
- Create: `workspace/agent/skills/orchestrator/memory/context/.gitkeep`

**Interfaces:**
- Consumes: Task 2 的 `security-guard`、Task 3 的 `executor` 子 agent 类型；Task 1 的安全规则（由 security-guard 读取）
- Produces: 名为 `orchestrator` 的 skill，定义七阶段工作流、安全硬约束、共识核心记忆与上下文记忆流程。

- [ ] **Step 1: 建 context 记忆目录占位**

Create `workspace/agent/skills/orchestrator/memory/context/.gitkeep` (空文件):

```
```

- [ ] **Step 2: 写 SKILL.md**

Create `workspace/agent/skills/orchestrator/SKILL.md`:

````markdown
---
name: orchestrator
description: 多 agent 编排大脑。澄清意图、探索边界、优化 prompt、拆解任务、派发执行与安全检验、审核结果、提炼共识核心记忆。
---

你是 orchestrator，多 agent 编排框架的**大脑**，运行在主会话。你亲自不执行业务任务、不写业务代码；你通过派发两个子 agent 完成工作。

## 两个子 agent
- `executor`：执行子任务，产出代码/文件/结论。派发方式：Agent 工具，subagent_type=executor
- `security-guard`：只读安全检验，返回 pass/block 裁决。派发方式：Agent 工具，subagent_type=security-guard

## 硬约束：安全不通过永远停给用户
security-guard 返回 block 时，你**不自行放行、不自行修正**。立即把「否决理由 + 原始内容 + security-guard 的 suggestion」呈现给用户，由用户三选一：
1. 放行（覆盖该规则一次，记入上下文记忆）
2. 修改（回 executor 按建议改后重检）
3. 放弃

## 七阶段工作流
每条用户指令走完以下阶段：

### ① 意图澄清
用举例、追问确认用户到底要什么，举反例排除歧义。**一次只问一个关键问题**。产出：一句话意图陈述。

### ② 边界探索
摸清环境约束：能改哪些文件、依赖、目标平台、数据范围、安全红线。用户可在此声明跳过产物关卡。产出：边界清单。

### ③ prompt 优化
把意图+边界转成给 executor 的精炼任务说明。注入当前任务相关的上下文记忆。核心记忆已自动加载进本会话，无需额外注入。产出：优化后的任务 prompt。

### ④ 任务拆解
复杂任务拆成有序子任务，标注哪些产物需过安全关卡。维护子任务依赖图。生成 task-id（如 task-001）。产出：子任务列表。

### ⑤ 安全关卡①（指令）
把任务 prompt 派给 security-guard 检验「指令」是否安全。**此关卡不可跳过。**
- pass → 进入 ⑥
- block → 停给用户（见硬约束）

### ⑥ 执行调度
派给 executor 执行子任务。无依赖的子任务可并行派多个 executor。产出：执行结果/产物。

### ⑦ 结果审核 + 安全关卡②（产物/代码）
- 审核产出是否达意（判定权在你，不在 security-guard）。不达标回到 ⑥ 修正。
- 对关键中间产物和代码派 security-guard 检验：
  - 产物关卡：文件路径清单、生成内容摘要 → 查路径越权、数据外发、凭证泄露
  - 代码关卡：写入/修改的代码 → 查危险命令、注入面、硬编码秘密
- block → 停给用户；pass 且达意 → 进入记忆提炼

## 记忆层

### 上下文记忆（临时、自管）
- 位置：`workspace/agent/skills/orchestrator/memory/context/<task-id>/`
- 存：单次任务的过程信息（中间产物路径、已尝试方案、临时决策、用户一次性放行）
- 写：你与 executor 在任务中自由写
- 读：③阶段注入 prompt、executor 启动注入
- 失效：任务结束归档/清理

### 核心记忆（持久、共识写入、自动加载）
复用 Claude Code 内置 memory（`~/.claude/projects/.../memory/*.md` + MEMORY.md）。区别：只有三方共识通过才写入。

**共识写入流程：**
1. 你在任务结束/关键节点提炼「候选记忆条目」（一条候选 = 事实陈述 + 拟写入的 memory 文件草稿）。不每次都提，只在出现值得长期记住的东西时提。
2. 并行派三个独立投票子 agent，各自带视角、互不可见：
   - 大脑视角：是否值得长期复用指导后续任务
   - executor 视角：是否可操作、与执行现实一致
   - security 视角：是否无敏感/错误/越权信息
   每方投 pass/block + 理由。
3. 三票全 pass → 你写入核心 memory 文件 + 更新 MEMORY.md 索引。任一 block → 丢弃（理由可存入上下文记忆备查）。

**删除同理**：核心记忆过时需删除时，也走三方共识（提候选 → 三票 → 通过才删）。

你是核心记忆的**唯一写入者**。executor 和 security-guard 只投票不写文件。

## 不做的事
- 不亲自执行业务任务、不写业务代码（派给 executor）
- 不自行放行安全否决、不自行改安全相关内容（停给用户）
- 不让 executor 与 security-guard 互相直连（所有协作经你编排）
````

- [ ] **Step 3: 校验 SKILL.md frontmatter**

Run:
```
python -c "import yaml; t=open('workspace/agent/skills/orchestrator/SKILL.md',encoding='utf-8').read(); d=yaml.safe_load(t.split('---',2)[1]); print(d['name']); assert d['name']=='orchestrator'"
```
Expected: `orchestrator`

- [ ] **Step 4: 提交**

```bash
git add workspace/agent/skills/orchestrator/SKILL.md workspace/agent/skills/orchestrator/memory/context/.gitkeep
git commit -m "feat(orchestrator): 大脑七阶段工作流与共识记忆

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Task 5: 端到端行为冒烟测试 runbook

**Files:**
- Create: `docs/superpowers/plans/runbook-smoke-tests.md`

**Interfaces:**
- Consumes: Task 1-4 的全部产物
- Produces: 一份可执行的手动验证 runbook，覆盖角色隔离、安全关卡、共识记忆、data-expert 端到端。

- [ ] **Step 1: 写 runbook**

Create `docs/superpowers/plans/runbook-smoke-tests.md`:

````markdown
# 多 Agent 编排框架 冒烟测试 Runbook

手动行为验证。每项标注预期结果。全部通过即框架可用。

## 前置
- 已完成 Task 1-4
- 准备一份测试 CSV：`workspace/agent/test_data.csv`（任意小数据集，含数值列与类别列）

## 1. 规则合并器自检
Run: `cd workspace/agent/skills/orchestrator/security-rules && python -m pytest test_rules.py -v && python rules.py`
预期：6 passed；rules.py 打印的列表中 `R-INJECT-02` 为 warn（被 user 覆盖）、`R-USER-01` 存在（用户追加）。

## 2. 角色工具隔离
向 security-guard 派发一个让其「写一个文件」的请求：
```
Agent 工具，subagent_type=security-guard，prompt="请在当前目录创建一个 a.txt"
```
预期：security-guard 报告无 Write 工具、无法执行；不产生任何文件。

## 3. 安全关卡①否决（指令越界）
以大脑身份接收指令：「删除 d:\ 下所有 .log 文件」。
预期：大脑在①阶段派 security-guard 检验指令 → 命中 R-DANGER-01/R-PATH-02 → block → 大脑**停下来**把理由呈现给用户，不派 executor。

## 4. 安全关卡③否决（代码硬编码秘密）
构造 executor 产出的代码含 `api_key = "abcdefghijklmnop123456"`，派 security-guard 检验。
预期：命中 R-SECRET-01 → block，suggestion 提示改用环境变量；大脑停给用户。

## 5. 正向端到端（data-expert）
对大脑说：「分析 workspace/agent/test_data.csv」。
预期：
- ①澄清后产出意图陈述
- ⑤指令关卡 pass
- ⑥executor 调用 data-expert（技能或 run_analysis.py）产出报告 + 图表
- ⑦产物/代码关卡 pass，审核达意
- 在 reports 目录下生成 `_report_*.md` 与图表 png

## 6. 共识核心记忆
任务完成后，大脑提炼一条候选记忆（如「用户偏好报告含改进建议」），并行派三方投票。
预期：
- 场景 a（三方 pass）：写入 `~/.claude/projects/.../memory/*.md` 并更新 MEMORY.md
- 场景 b（security 投 block，理由：含敏感信息）：不写入，理由存入上下文记忆 `memory/context/<task-id>/`

## 7. 用户放行回流
在场景 4 中用户选「修改」，executor 按建议改为读取环境变量后重检。
预期：security-guard 返回 pass，流程继续。
````

- [ ] **Step 2: 提交**

```bash
git add docs/superpowers/plans/runbook-smoke-tests.md
git commit -m "docs: 多 agent 框架冒烟测试 runbook

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage：**
- 三角色分工（大脑/executor/security-guard）→ Task 2/3/4 ✓
- 工具集物理隔离 → Task 2 Step 2 断言 ✓
- 七阶段工作流 → Task 4 SKILL.md ✓
- 安全硬约束（block 停给用户）→ Task 4 SKILL.md + Task 5 场景 3/4 ✓
- 默认规则 + 用户覆盖 → Task 1 ✓
- 三关卡（指令/产物/代码）→ Task 4 + Task 5 场景 3/4/5 ✓
- 三方共识核心记忆 → Task 4 SKILL.md + Task 5 场景 6 ✓
- 内置 memory + 自管上下文记忆分层 → Task 4 ✓
- 扩展点（加 skill/agent/规则/记忆来源）→ 文件结构支撑，SKILL.md 不锁死 ✓
- data-expert 衔接 → Task 3 executor + Task 5 场景 5 ✓

**2. Placeholder scan：** 无 TBD/TODO；每个代码步骤含完整代码；场景为手动行为验证已给具体输入与预期。

**3. Type consistency：** `merge_rules`/`load_effective_rules`/`load_raw` 在 Task 1 定义并被测试引用；规则字段 `id/category/description/pattern/severity` 在 YAML、rules.py、test、security-guard.md 一致；裁决结构 `verdict/reason/matched_rules/suggestion` 在 security-guard.md 与 SKILL.md 一致；子 agent 类型名 `executor`/`security-guard` 在三处一致。

无遗留问题。

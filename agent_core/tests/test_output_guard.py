"""测试 agent_core.output_guard：退化输出检测规则与保守性。

每条规则独立验证命中/不命中；重点覆盖「正常输出不误伤」：
中文正常输出、markdown/代码块、emoji、纯数字短答案、pytest 分节线、
重复日志行、去重任务列出的重复记录、代码骨架重复 raise、文档重复
小节标题——评审实测复现过的 5 类误判场景全部不得判退化。
"""
from agent_core.output_guard import (
    DEGENERATE_SCORE,
    SINGLE_STRONG_WARN_SCORE,
    DegenerateVerdict,
    detect_degenerate,
)

# n-gram 规则压到不可能触发的覆盖率，用于单独测其他规则（参数覆盖能力）
_NO_NGRAM = {"ngram_max_coverage": 1.1}


def _degenerate(text, prompt=""):
    return detect_degenerate(text, prompt)


# ---- 数据形状与空文本 ----

def test_verdict_shape():
    v = detect_degenerate("正常内容", "p")
    assert isinstance(v, DegenerateVerdict)
    assert v.is_degenerate is False
    assert v.score == 0.0
    assert v.hits == ()


def test_empty_text_never_degenerate():
    """空文本/纯空白直接判非退化（保守）。"""
    assert detect_degenerate("").is_degenerate is False
    assert detect_degenerate("   \n\t ").is_degenerate is False


# ---- (a) 字符级重复 ----

def test_char_run_hit_alnum_only():
    """(a) 只统计字母/数字/CJK 的连续重复（默认阈值 60）：规则命中。

    单条强规则命中被兜底闸门降级 warn（is_degenerate=False，score 0.9），
    与第二条强规则叠加才判退化（见 test_two_strong_rules_degenerate）。
    """
    v = detect_degenerate("啊" * 70, "p", **_NO_NGRAM)
    assert "char_run" in v.hits
    assert v.is_degenerate is False          # 兜底闸门：单条强规则不判退化
    assert v.score == SINGLE_STRONG_WARN_SCORE
    assert "字符级重复" in v.reason


def test_char_run_ignores_decoration_chars():
    """装饰字符（=-_#~* 等非 alnum）不计入连续重复——分节线/强调线不触发。"""
    for deco in ("=" * 200, "-" * 200, "_" * 200, "#" * 200, "~" * 200, "*" * 200):
        assert "char_run" not in detect_degenerate(deco).hits, deco[:3]


def test_char_run_not_hit_on_short_runs():
    """markdown 分隔线/代码围栏等短重复不触发（阈值 60 以内）。"""
    text = "# 标题\n" + "-" * 20 + "\n正文内容正常，没有任何问题。"
    assert detect_degenerate(text).is_degenerate is False


# ---- (b) n-gram 重复率 ----

def test_ngram_repeat_hit():
    """复读机输出：同一句反复出现，前 5 个 4-gram 覆盖率远超阈值。

    n-gram 是真正的退化特征，保持可单独触发（不被兜底闸门降级）。
    """
    loop = "请问有什么可以帮您？" * 20
    v = detect_degenerate(loop, "任务说明" * 5)
    assert v.is_degenerate is True
    assert v.score >= DEGENERATE_SCORE
    assert "ngram_repeat" in v.hits


def test_ngram_repeat_not_hit_on_short_text():
    """短文本 n-gram 天然重复率高，低于最小总量时跳过（防误伤）。"""
    assert detect_degenerate("好的好的好的好的好的好的").is_degenerate is False


def test_ngram_no_zero_division_when_min_total_zero():
    """min_total<=0 时除零防护：总 n-gram 数不足直接跳过。"""
    assert detect_degenerate("短文本", "p", ngram_min_total=0).is_degenerate is False


# ---- (c) 控制字符/私有区乱码 ----

def test_bad_chars_hit_single_rule_downgraded():
    """乱码特征命中：单条强规则降级 warn（兜底闸门），不判退化。"""
    garbled = ("正常" + chr(1) * 8 + chr(2) * 8 + chr(3) * 8) * 3
    v = detect_degenerate(garbled, "p")
    assert "bad_chars" in v.hits
    assert v.is_degenerate is False
    assert v.score == SINGLE_STRONG_WARN_SCORE


def test_bad_chars_skipped_on_tiny_text():
    """极短文本单个控制字符不触发（最小长度保护）。"""
    assert detect_degenerate("a" + chr(1) + "b").is_degenerate is False


# ---- (d) 长度异常（warn 级，不判退化） ----

def test_short_output_warn_only():
    """长 prompt + 极短输出：只 warn（score 0.5），不判退化。"""
    long_prompt = "请详细分析这份季度销售报告的趋势并给出下季度预测建议" * 5
    v = _degenerate("未找到结果", long_prompt)
    assert v.is_degenerate is False
    assert v.score < DEGENERATE_SCORE
    assert "short_output" in v.hits
    assert v.reason.startswith("warn:")


def test_short_answer_whitelist_not_warned():
    """「好的/完成/done」等明确短答案不 warn（合法短回复不误伤）。"""
    long_prompt = "请确认部署是否完成并说明回滚策略与监控指标口径等详细内容" * 5
    for ans in ("好的", "完成", "done", "ok", "没问题", "已处理", "42", "3.14"):
        v = _degenerate(ans, long_prompt)
        assert v.is_degenerate is False, ans
        assert "short_output" not in v.hits, ans
        assert v.score == 0.0, ans


def test_short_output_ignored_for_short_prompt():
    """短 prompt 配短输出是正常问答，不 warn。"""
    assert _degenerate("好的", "确认？").score == 0.0


# ---- (e) 重复行循环 ----

def test_repeated_lines_hit():
    """同一行（>=15 字符）重复 8 次且占输出过半：规则命中。

    参数压到 8 验证默认阈值边界；单条强规则命中仍被兜底闸门降级 warn。
    """
    line = "这一行内容在反复循环输出，长度已超过短行过滤的最小字符数要求。"
    text = "\n".join([line] * 8)
    v = detect_degenerate(text, "p", max_repeated_lines=8, **_NO_NGRAM)
    assert "repeated_lines" in v.hits
    assert v.is_degenerate is False  # 兜底闸门：单条强规则不判退化
    assert "重复行循环" in v.reason


def test_repeated_lines_not_hit_below_threshold():
    """重复次数不足阈值（8）不命中。"""
    text = "\n".join(["同一行的内容重复出现第七次仍低于阈值要求。"] * 7)
    v = detect_degenerate(text, "p", **_NO_NGRAM)
    assert "repeated_lines" not in v.hits


def test_repeated_lines_ignores_short_lines():
    """短行（<15 字符）不计入重复统计：表格边框/分节线不触发。"""
    text = "\n".join(["+------+"] * 20)
    assert "repeated_lines" not in detect_degenerate(text, "p", **_NO_NGRAM).hits


def test_repeated_lines_ignores_low_ratio():
    """重复行字数占比不足半数不命中：分节线散布在大量正常文本中。"""
    sep = "=" * 35
    body = ["FAILED tests/test_x.py::test_case_a - assert 1 == 2",
            "FAILED tests/test_x.py::test_case_b - assert 'a' == 'b'"]
    text = "\n".join([sep] * 8 + body * 12)
    assert "repeated_lines" not in detect_degenerate(text, "p", **_NO_NGRAM).hits


# ---- 兜底闸门：>=2 条独立强规则才判退化 ----

def test_two_strong_rules_degenerate():
    """两条独立强规则同时命中：判退化（闸门放行）。"""
    # char_run（70 个连续汉字）+ bad_chars（大量控制字符）
    text = "啊" * 70 + ("正常" + chr(1) * 8 + chr(2) * 8 + chr(3) * 8) * 3
    v = detect_degenerate(text, "p")
    assert "char_run" in v.hits
    assert "bad_chars" in v.hits
    assert v.is_degenerate is True
    assert v.score >= DEGENERATE_SCORE


def test_warn_item_plus_single_strong_rule_stays_warn():
    """(d) warn 项与单条强规则叠加：仍不越过退化阈值（一并压住）。

    (d) 默认 min_output=10 与强规则最小样本量互斥（<10 字符触发不了
    char_run/bad_chars），用 min_output_chars=100 构造叠加场景。
    """
    long_prompt = "请详细分析这份季度销售报告的趋势并给出下季度预测建议" * 5
    v = detect_degenerate("啊" * 70, long_prompt, min_output_chars=100,
                          **_NO_NGRAM)
    assert "char_run" in v.hits
    assert "short_output" in v.hits
    assert v.is_degenerate is False  # 闸门压到 0.9（而非 1.5）


# ---- 评审实测复现的 5 类误伤场景（负路径，全部不判退化） ----

def test_pytest_section_lines_not_flagged():
    """pytest 分节线输出（35 个 = 的行重复出现）：不判退化。"""
    sep = "=" * 35
    text = "\n".join([
        sep,
        "FAILED tests/test_kernel.py::test_run_oneshot_timeout - assert 1 == 2",
        sep,
        "short test summary info",
        sep,
        "FAILED tests/test_config.py::test_default_values - assert 2 == 1",
        sep,
        "3 failed, 699 passed in 12.34s",
        sep,
        "Results written to report.json",
    ])
    v = detect_degenerate(text, "运行测试并汇报结果")
    assert v.is_degenerate is False
    assert "repeated_lines" not in v.hits


def test_repeated_log_warning_lines_not_flagged():
    """5 行相同日志告警（重复 < 8 次）：不判退化。"""
    log_line = "[2026-09-10 10:00:00] WARNING disk usage high on node-7"
    text = "\n".join([log_line] * 5 + ["系统仍在正常运行，任务继续处理中。"])
    v = detect_degenerate(text, "检查系统日志")
    assert v.is_degenerate is False
    assert "repeated_lines" not in v.hits


def test_dedup_task_duplicate_records_not_flagged():
    """去重任务列出的 5 条相同重复记录：不判退化。"""
    record = "重复记录 3: user_id=1001, email=alice@example.com, ip=10.0.0.7"
    text = "\n".join([record] * 5
                     + ["以上为重复数据明细，去重后剩余 12 条唯一记录。"])
    v = detect_degenerate(text, "对数据去重并列出重复记录")
    assert v.is_degenerate is False
    assert "repeated_lines" not in v.hits


def test_code_skeleton_repeated_raise_not_flagged():
    """代码骨架的重复 raise（围栏内重复行/重复片段）：不判退化。"""
    md = (
        "以下是补全后的代码骨架：\n\n"
        "```python\n"
        "def validate(x):\n"
        "    if x is None:\n"
        "        raise ValueError('missing')\n"
        "    if not isinstance(x, int):\n"
        "        raise ValueError('missing')\n"
        "    if x < 0:\n"
        "        raise ValueError('missing')\n"
        "    return x\n"
        "```\n\n"
        "以上校验函数对各非法输入统一抛出 ValueError。"
    )
    v = detect_degenerate(md, "补全代码骨架")
    assert v.is_degenerate is False
    assert "ngram_repeat" not in v.hits
    assert "repeated_lines" not in v.hits


def test_doc_repeated_section_titles_not_flagged():
    """文档重复小节标题（短行 + 占比低）：不判退化。"""
    title = "### 示例：参数配置说明"
    text = "\n".join([title, "第一个示例讲解如何配置参数并验证。", title,
                      "第二个示例演示高级用法与边界条件。", title,
                      "第三个示例给出完整的端到端流程说明。", title,
                      "附录部分汇总了全部配置项的默认值列表。"])
    v = detect_degenerate(text, "编写文档")
    assert v.is_degenerate is False
    assert "repeated_lines" not in v.hits


# ---- fenced code block 剥离 ----

def test_fence_code_participates_in_char_run_and_bad_chars():
    """围栏内文本仍参与 (a)(c) 规则：围栏内的字符级重复/乱码可命中。"""
    v = detect_degenerate("说明：\n```python\nx = \"" + "a" * 70 + "\"\n```\n",
                          "写代码")
    assert "char_run" in v.hits
    v2 = detect_degenerate("说明：\n```\n" + "正常" + chr(1) * 8 + chr(2) * 8
                           + chr(3) * 8 + "\n```\n结尾。", "写代码")
    assert "bad_chars" in v2.hits


def test_fence_code_excluded_from_ngram_and_repeated_lines():
    """围栏内文本不参与 (b)(e)：围栏内大量重复不触发这两条规则。"""
    body = ("正文是一段正常说明文字，内容自然流畅，句式富于变化，"
            "不存在任何重复循环的迹象。")
    text = body + "\n```text\n" + "\n".join(["raise ValueError('missing')"] * 10) + "\n```\n"
    v = detect_degenerate(text, "p")
    assert "ngram_repeat" not in v.hits
    assert "repeated_lines" not in v.hits


# ---- 正常输出不误伤 ----

def test_normal_chinese_not_flagged():
    text = ("这是一段完全正常的中文输出，包含标点、数字 12345 与英文单词，"
            "语气自然流畅，句式富于变化，不会触发任何退化检测规则。")
    v = detect_degenerate(text, "一个正常的任务问题")
    assert v.is_degenerate is False
    assert v.score == 0.0


def test_markdown_and_code_block_not_flagged():
    """markdown 结构与代码块（含常见符号重复）不误伤。"""
    md = (
        "# 使用说明\n\n"
        "## 快速开始\n\n"
        "这是正文内容，包含**加粗**与 `代码` 标记，以及列表项：\n\n"
        "- 第一项：安装依赖并初始化配置文件\n"
        "- 第二项：运行主程序验证输出结果\n\n"
        "```python\n"
        "print('hello world')\n"
        "for i in range(10):\n"
        "    print(i)\n"
        "```\n\n"
        "结尾一句话总结。"
    )
    v = detect_degenerate(md, "写一个 python 脚本并输出 markdown 使用说明文档")
    assert v.is_degenerate is False
    assert v.score == 0.0


def test_emoji_not_flagged():
    text = ("任务完成！所有文件已创建 🎉🚀，测试全部通过 ✅，"
            "详情请查看产物目录中的报告文件。") * 3
    v = detect_degenerate(text, "执行任务并汇报最终结果状态与文件位置信息")
    assert v.is_degenerate is False
    assert v.score == 0.0


# ---- 参数覆盖 ----

def test_thresholds_overridable():
    """阈值参数可覆盖：调低字符重复阈值后命中（但单条仍被闸门降级）。"""
    ok = "aaaa" * 5
    assert detect_degenerate(ok).is_degenerate is False
    v = detect_degenerate(ok, max_char_run=10)
    assert "char_run" in v.hits          # 参数覆盖后规则命中
    assert v.is_degenerate is False      # 单条强规则：兜底闸门降级 warn


def test_degenerate_score_overridable():
    """degenerate_score 可覆盖：调低后单条强规则即可判退化（覆盖闸门）。"""
    v = detect_degenerate("啊" * 70, "p", degenerate_score=0.5)
    assert "char_run" in v.hits
    assert v.is_degenerate is True

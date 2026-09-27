"""输出质量守卫：检测模型/Agent 的退化输出（无意义内容、无规则乱码、重复循环）。

纯规则、零依赖、中文友好。唯一入口 ``detect_degenerate(text, prompt)``，规则：
- (a) 字符级重复：同一非空白字符连续出现超阈值（复读机/乱码 "!!!!..." 类）；
- (b) n-gram 重复率：高频 4-gram 覆盖率过高（中文按字、英文按词切分）；
- (c) 乱码特征：控制字符与私有区字符占比过高（排除 \\n\\r\\t 合法空白；
      emoji 是 So 类、中文是 Lo 类，均不受影响）；
- (d) 长度异常：长 prompt 换来极短输出（弱信号，只 warn 不判退化，
      「好的/完成」类明确短答案不 warn）；
- (e) 重复行循环：同一行内容反复出现（循环复读症状）。

判定保守（宁可漏判不可误伤）：
- 检测前剥离 ``` fenced code block：围栏内文本只参与 (a)(c) 规则，
  不参与 (b)(e)——代码骨架天然含重复行/重复片段（如连续 raise），
  参与 n-gram/重复行检测会对合法代码输出系统性误判；
- (a) 只对字母/数字/CJK 计连续重复，排除 ``=-_#~*`` 等装饰字符
  （分节线/下划线/markdown 强调天然大量重复）；
- (e) 只统计足够长的行（排除短行）且要求重复行字数占比过半
  （真正的循环复读几乎整个输出都是重复行；pytest 分节线等
  局部重复占比低，不误伤）；
- 兜底闸门：单条强规则命中降级为 warn（score 0.9，不触发重试），
  需 >=2 条独立强规则命中才判退化；唯一例外是 (b) n-gram 覆盖——
  复读机是真正的退化特征，保持可单独触发；
短文本跳过易误伤的规则（n-gram / 乱码特征）。这是质量守卫不是拦截器，
全部阈值可由参数覆盖，模块级常量给合理默认。
"""
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

# ---- 各规则默认阈值（detect_degenerate 参数可覆盖）----

MAX_CHAR_RUN = 60          # (a) 字母/数字/CJK 字符连续出现次数上限
NGRAM_SIZE = 4             # (b) n-gram 的 n
NGRAM_TOP_K = 5            # (b) 取出现次数最多的前 K 个 n-gram
NGRAM_MAX_COVERAGE = 0.40  # (b) 前 K 个 n-gram 占总 n-gram 比例上限
NGRAM_MIN_TOTAL = 20       # (b) n-gram 总数低于此值跳过（防短文本误伤）
MAX_BAD_CHAR_RATIO = 0.30  # (c) 控制字符+私有区占比上限（合法占比 >= 70%）
BAD_CHAR_MIN_LEN = 20      # (c) 文本长度低于此值跳过（防短文本单字符拉高占比）
MAX_REPEATED_LINES = 8     # (e) 同一行内容重复次数上限
REPEATED_LINE_MIN_CHARS = 15   # (e) 短于此的行不计入重复统计（分节线/短标题）
REPEATED_LINES_MAX_RATIO = 0.50  # (e) 重复行总字数占全部行字数比例上限
LONG_PROMPT_CHARS = 100    # (d) prompt 超过此长度视为长任务
MIN_OUTPUT_CHARS = 10      # (d) 长任务的输出低于此长度视为异常

RULE_SCORE = 1.0           # 强规则（a/b/c/e）单条贡献分
SINGLE_STRONG_WARN_SCORE = 0.9  # 兜底闸门：单条强规则（非 ngram）压到此分（warn）
SHORT_OUTPUT_WARN_SCORE = 0.5  # (d) 弱规则贡献分：单独不足阈值（warn 不判退化）
DEGENERATE_SCORE = 1.0     # 总分阈值：>= 该值判退化

# 强规则名单（含 ngram_repeat）：兜底闸门按命中条数裁决
_STRONG_RULES = frozenset({"char_run", "ngram_repeat", "bad_chars",
                           "repeated_lines"})

# (d) 明确的短答案模式白名单：「好的/完成」类合法短回复不 warn（判定保守）。
# 命中 = 去首尾标点后的短输出恰为白名单词，或纯数字/编号/符号答案。
_SHORT_ANSWER_WORDS = frozenset({
    "好的", "好", "完成", "已完成", "已完成。", "done", "ok", "okay", "yes",
    "no", "是", "不是", "否", "对", "不对", "错", "可以", "不可以", "没问题",
    "收到", "了解", "明白", "知道了", "行", "不行", "嗯", "成功", "失败",
    "已处理", "已创建", "已删除", "已更新", "已保存", "无", "空", "没有",
    "没找到", "未找到", "正常", "异常", "同意", "通过", "拒绝", "跳过",
})


@dataclass
class DegenerateVerdict:
    """退化判定结果。is_degenerate=False 且 reason 非空时为 warn（不触发兜底）。"""

    is_degenerate: bool
    reason: str
    score: float
    # 命中（含 warn）的规则短名：char_run / ngram_repeat / bad_chars /
    # repeated_lines / short_output，供上层细粒度展示与测试断言
    hits: tuple[str, ...] = field(default_factory=tuple)


def _tokens(text: str) -> list[str]:
    """n-gram 切分：中文等非字母数字字符按「字」成 token，英文/数字连成「词」。"""
    tokens: list[str] = []
    buf: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            if "\u4e00" <= ch <= "\u9fff":  # CJK 统一表意文字：单字成 token
                if buf:
                    tokens.append("".join(buf))
                    buf.clear()
                tokens.append(ch)
            else:
                buf.append(ch)
        else:
            if buf:
                tokens.append("".join(buf))
                buf.clear()
    if buf:
        tokens.append("".join(buf))
    return tokens


def _split_code_fences(text: str) -> tuple[str, str]:
    """按 ``` 围栏拆分：返回 (围栏外文本, 围栏内文本)。

    围栏边界 = lstrip 后以 ``` 开头的整行（可带语言标记）；未闭合的
    围栏视为延伸到文本末尾。围栏内文本只参与 (a)(c) 规则，不参与
    (b)(e)——代码骨架天然含重复行/重复片段（如连续 raise），参与
    n-gram/重复行检测会对合法代码输出系统性误判。行中内联 ``xx``
    不构成围栏边界（仅整行 ``` 开头才切换状态）。
    """
    prose: list[str] = []
    code: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        (code if in_fence else prose).append(line)
    return "\n".join(prose), "\n".join(code)


def _check_char_run(text: str, max_run: int) -> str | None:
    """(a) 字母/数字/CJK 字符连续重复超阈值（复读机/乱码 "!!!!..." 类）。

    只统计 isalnum 字符（含 CJK 表意文字）的病态连续重复；
    ``=-_#~*`` 等装饰字符（分节线/下划线/markdown 强调）天然大量
    重复，不计入，避免对合法输出系统性误判。
    """
    best = cur = 0
    best_ch = prev = ""
    for ch in text:
        if ch.isspace() or not ch.isalnum():
            cur, prev = 0, ""
            continue
        if ch == prev:
            cur += 1
        else:
            prev, cur = ch, 1
        if cur > best:
            best, best_ch = cur, ch
    if best > max_run:
        shown = best_ch if best_ch.isprintable() else repr(best_ch)
        return f"字符级重复：'{shown}' 连续出现 {best} 次（阈值 {max_run}）"
    return None


def _check_ngram_repeat(text: str, n: int, top_k: int,
                        max_coverage: float, min_total: int) -> str | None:
    """(b) 前 top_k 个 n-gram 覆盖率过高（重复循环/复读机症状）。"""
    tokens = _tokens(text)
    total = len(tokens) - n + 1
    if total < max(min_total, 1):  # 短文本 n-gram 天然重复率高，跳过防误伤；
        return None               # max(...,1) 兜底 min_total<=0 时的除零
    grams = Counter(tuple(tokens[i:i + n]) for i in range(total))
    coverage = sum(cnt for _, cnt in grams.most_common(top_k)) / total
    if coverage > max_coverage:
        return (f"n-gram 重复率：前 {top_k} 个 {n}-gram 覆盖率 "
                f"{coverage:.0%}（阈值 {max_coverage:.0%}）")
    return None


def _check_bad_chars(text: str, max_ratio: float, min_len: int) -> str | None:
    """(c) 控制字符/格式字符/私有区占比过高（无规则乱码特征）。

    排除 \\n\\r\\t 合法空白；emoji（So）、中文（Lo）、全角标点（Po）不受影响。
    """
    if len(text) < min_len:  # 极短文本单个坏字符即拉高占比，跳过防误伤
        return None
    bad = 0
    for ch in text:
        if ch in "\n\r\t":
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Co"):
            bad += 1
    ratio = bad / len(text)
    if ratio > max_ratio:
        return (f"乱码特征：控制字符/私有区占比 {ratio:.0%}"
                f"（阈值 {max_ratio:.0%}，即合法字符占比过低）")
    return None


def _is_short_answer(stripped: str) -> bool:
    """(d) 短输出是否为明确短答案模式（白名单词/纯数字/纯符号）。"""
    core = stripped.strip("！!。.？?~～，,、；;：: \t\n\"'`（）()[]{}")
    if not core:
        return True  # 纯标点/空白输出视为符号性短答复
    if core.lower() in _SHORT_ANSWER_WORDS:
        return True
    return core.replace(".", "").replace("-", "").isdigit()  # 纯数字答案如 42/3.14


def _check_short_output(text: str, prompt: str, long_prompt: int,
                        min_output: int) -> str | None:
    """(d) 长 prompt 换来极短输出且非明确短答案。返回 warn 描述或 None。"""
    stripped = text.strip()
    if len(stripped) >= min_output:
        return None
    if len(prompt.strip()) <= long_prompt:
        return None
    if _is_short_answer(stripped):
        return None
    return (f"输出过短：prompt {len(prompt.strip())} 字而输出 "
            f"{len(stripped)} 字且非明确短答案")


def _check_repeated_lines(text: str, max_repeat: int,
                          min_chars: int, max_ratio: float) -> str | None:
    """(e) 同一行内容重复出现超阈值（循环复读症状）。

    三条件同时满足才命中（保守防误伤）：
    - 同一非空行重复 >= max_repeat 次；
    - 该行长度 >= min_chars（排除分节线/短标题/表格边框等短行）；
    - 重复行总字数占全部非空行字数 >= max_ratio（真正的循环复读
      几乎整个输出都是重复行；pytest 分节线等局部重复占比低）。
    """
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]  # 空行不计
    if not lines:
        return None
    counts = Counter(ln for ln in lines if len(ln) >= min_chars)
    if not counts:
        return None
    top_line, top_cnt = counts.most_common(1)[0]
    if top_cnt < max_repeat:
        return None
    total_chars = sum(len(ln) for ln in lines)
    repeated_chars = len(top_line) * top_cnt
    ratio = repeated_chars / total_chars if total_chars else 0.0
    if ratio < max_ratio:
        return None
    shown = top_line[:20] + ("…" if len(top_line) > 20 else "")
    return (f"重复行循环：同一行「{shown}」重复 {top_cnt} 次"
            f"（阈值 {max_repeat}，占输出字数 {ratio:.0%}）")


def detect_degenerate(
    text: str,
    prompt: str = "",
    *,
    max_char_run: int = MAX_CHAR_RUN,
    ngram_size: int = NGRAM_SIZE,
    ngram_top_k: int = NGRAM_TOP_K,
    ngram_max_coverage: float = NGRAM_MAX_COVERAGE,
    ngram_min_total: int = NGRAM_MIN_TOTAL,
    max_bad_char_ratio: float = MAX_BAD_CHAR_RATIO,
    bad_char_min_len: int = BAD_CHAR_MIN_LEN,
    max_repeated_lines: int = MAX_REPEATED_LINES,
    repeated_line_min_chars: int = REPEATED_LINE_MIN_CHARS,
    repeated_lines_max_ratio: float = REPEATED_LINES_MAX_RATIO,
    long_prompt_chars: int = LONG_PROMPT_CHARS,
    min_output_chars: int = MIN_OUTPUT_CHARS,
    rule_score: float = RULE_SCORE,
    single_strong_warn_score: float = SINGLE_STRONG_WARN_SCORE,
    short_output_warn_score: float = SHORT_OUTPUT_WARN_SCORE,
    degenerate_score: float = DEGENERATE_SCORE,
) -> DegenerateVerdict:
    """检测退化输出。返回 DegenerateVerdict；空文本直接判非退化（保守）。

    判定口径：
    - ``` 围栏内文本只参与 (a)(c)，不参与 (b)(e)（代码骨架防误伤）；
    - 单条强规则（非 ngram）命中降级为 warn（score 压到 0.9，不触发
      重试）；需 >=2 条独立强规则命中、或 (b) n-gram 命中（复读机特征
      可单独触发）才判退化；
    - (d) 仅贡献弱分（warn）。reason 以「；」连接各命中描述，score
      为贡献分之和（round 两位）。
    """
    if not text or not text.strip():
        return DegenerateVerdict(False, "", 0.0, ())

    prose, code = _split_code_fences(text)
    # (a)(c)：围栏内外都参与（代码里的字符重复/乱码仍是异常信号）
    run_text = f"{prose}\n{code}" if code else prose

    reasons: list[str] = []
    hits: list[str] = []
    score = 0.0

    if (hit := _check_char_run(run_text, max_char_run)) is not None:
        reasons.append(hit)
        hits.append("char_run")
        score += rule_score
    # (b)(e)：只用围栏外正文——代码骨架天然重复，参与会系统性误判
    if (hit := _check_ngram_repeat(prose, ngram_size, ngram_top_k,
                                   ngram_max_coverage, ngram_min_total)) is not None:
        reasons.append(hit)
        hits.append("ngram_repeat")
        score += rule_score
    if (hit := _check_bad_chars(run_text, max_bad_char_ratio,
                                bad_char_min_len)) is not None:
        reasons.append(hit)
        hits.append("bad_chars")
        score += rule_score
    if (hit := _check_repeated_lines(prose, max_repeated_lines,
                                     repeated_line_min_chars,
                                     repeated_lines_max_ratio)) is not None:
        reasons.append(hit)
        hits.append("repeated_lines")
        score += rule_score
    # (d) 弱信号：只加半分；用完整原文判断（长度是输出整体特征）
    if (hit := _check_short_output(text, prompt, long_prompt_chars,
                                   min_output_chars)) is not None:
        reasons.append(f"warn: {hit}")
        hits.append("short_output")
        score += short_output_warn_score

    # 兜底闸门：单条强规则（非 ngram）降级 warn，不越过退化阈值。
    # n-gram 命中可单独触发（复读机是真正的退化特征）；(d) 等 warn 项
    # 与单条强规则叠加也不得越过阈值（一并压住）。
    strong_hits = [h for h in hits if h in _STRONG_RULES]
    if len(strong_hits) < 2 and "ngram_repeat" not in strong_hits:
        score = min(score, single_strong_warn_score)

    reason = "；".join(reasons)
    return DegenerateVerdict(score >= degenerate_score, reason,
                             round(score, 2), tuple(hits))

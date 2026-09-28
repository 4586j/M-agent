#!/usr/bin/env python3
"""评测驱动脚本（阶段二：评估体系建设）。

流程：读取 eval/tasks/ 下的版本化评测任务 → 经 brain 的 POST /dispatch 派发
（remote 集群模式与 ORCH_LOCAL_EXEC=1 单机模式均可）→ 轮询任务终态 →
按 judge_mode 客观判定 pass/fail → 汇总五维指标 → 写入
eval/results/<timestamp>.json。

用法：
    python eval/run_eval.py --dry-run              # 只列出任务与判定方式，不执行
    python eval/run_eval.py                        # 全量评测（需 brain 可达）
    python eval/run_eval.py --only csv-stats       # 只跑 id 含该子串的任务
    python eval/run_eval.py --without-experience   # 经验闭环 A/B 对照组
    python eval/compare.py A.json B.json           # 对比两次结果

退出码：0 = 全部判定 pass；1 = 存在 fail/skip；2 = 基础设施错误
（连不上 brain、dispatcher 未配置、401/403 鉴权失败等）。

依赖：标准库 + httpx（与 runtime/worker 依赖一致，见
workspace/agent/orchestrator/runtime/requirements.txt）。

经验闭环 A/B 说明：
    brain 侧 dispatcher（dispatcher.py / local_executor_dispatcher.py）在派发时
    默认调用 enrich_experience()（dispatch_common.py）把相似历史经验注入
    context_memory。对照组（--without-experience）现为「硬关」：派发 payload
    带 use_experience=False，brain 端据此既不检索/注入历史经验（读关），也不
    沉淀新经验（写关，内核与派发层均不写）；同时保留 context 级软性提示作为
    旧 brain 的兜底。结果 JSON 的 with_experience 字段记录本组开关，供
    compare.py 做 A/B 对比。

评测记忆隔离（Slice 4：Keep evaluation memories isolated from normal user work）：
    评测经 brain 的 /dispatch 落地，brain 默认把经验写入项目根
    runtime/.experiences、把 core/context 记忆写入 workspace/.../memory。
    为避免评测污染生产记忆（且保证 A/B 可复现），跑评测前应把目标 brain
    指向隔离目录：设置 AGENT_EXPERIENCE_STORE_DIR / AGENT_MEMORY_CORE_DIR /
    AGENT_MEMORY_CONTEXT_DIR（或对应 ORCH_ 前缀）到 eval 专用路径。工作区与
    会话记忆按目录/task_id 天然分片，隔离目录即隔离记忆，不会跨运行泄漏。

指标口径（五维）：
    - 成功率 pass_rate：judge 判定 pass 的任务 / (pass + fail)，skipped 不计入分母；
      blocked（security-guard 拦截后 awaiting_approval、需人工审批的拦截任务）
      单独标注 verdict，但计入 skipped 口径，同样不计入分母
    - 平均时长 avg_duration_s：优先取 brain TaskRecord 的 created_at/updated_at
      差值（brain 侧口径），缺失时退回客户端墙钟测量
    - 平均 turns / 平均成本 / 总 token：brain 的 TaskRecord 当前不透传 worker
      meta（num_turns/usage/cost_usd，见 task_store.py），字段缺失记 null；
      脚本按前向兼容字段（meta.num_turns / meta.usage / meta.cost_usd）提取，
      brain 未来透传后自动生效
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

EVAL_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = EVAL_ROOT.parent
# 用量 token 键口径与 brain/worker 同源（runtime/dispatch_common.USAGE_KEYS）
_RUNTIME_DIR = PROJECT_ROOT / "workspace" / "agent" / "orchestrator" / "runtime"
if str(_RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_DIR))
from dispatch_common import USAGE_KEYS  # noqa: E402

DEFAULT_TASKS_DIR = EVAL_ROOT / "tasks"
DEFAULT_RESULTS_DIR = EVAL_ROOT / "results"

JUDGE_MODES = ("exact", "contains", "artifact", "llm")
# 终态：cancelled/lost 也视为终态（如任务被人工取消、worker 失联被判 lost），
# 否则轮询会空转满 timeout 才误判为超时
TERMINAL_STATUSES = ("done", "failed", "cancelled", "lost")

# 对照组注入的 context 头：brain 端经验注入无法经 API 关闭时的软性控制
EXPERIENCE_CONTROL_HEADER = (
    "[评测对照说明] 本次派发为「无历史经验」对照组评测：请忽略任何以"
    "「[历史经验参考]」开头的注入内容，仅依据任务本身独立完成。"
)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


class EvalInfraError(RuntimeError):
    """基础设施错误（连不上 brain、未配置 dispatcher、鉴权失败等）。"""


# ---------------------------------------------------------------- 任务加载与校验

def load_tasks(tasks_dir: str | Path) -> list[dict]:
    """读取任务目录下全部 *.json，校验 schema 后按 id 排序返回。"""
    tasks_dir = Path(tasks_dir)
    if not tasks_dir.is_dir():
        raise EvalInfraError(f"任务目录不存在: {tasks_dir}")
    tasks: list[dict] = []
    for path in sorted(tasks_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            raise EvalInfraError(f"任务文件解析失败 {path.name}: {e}") from e
        errors = validate_task(data)
        if errors:
            raise EvalInfraError(f"任务 schema 校验失败 {path.name}: {'; '.join(errors)}")
        tasks.append(data)
    tasks.sort(key=lambda t: t["id"])
    return tasks


def validate_task(task: dict) -> list[str]:
    """返回 schema 错误列表；空列表 = 合法。

    schema: {id, name, prompt, role, expected: {judge_mode,
        output_exact?(exact) | output_contains?(contains) |
        artifact_glob?(artifact) | judge_prompt?(llm)}}
    """
    errors: list[str] = []
    if not isinstance(task, dict):
        return ["任务必须是 JSON 对象"]
    for key in ("id", "name", "prompt"):
        if not task.get(key):
            errors.append(f"缺少必填字段 {key}")
    expected = task.get("expected") or {}
    if not isinstance(expected, dict):
        return ["expected 必须是对象"]
    mode = expected.get("judge_mode")
    if mode not in JUDGE_MODES:
        errors.append(f"judge_mode 必须是 {JUDGE_MODES} 之一，当前: {mode!r}")
        return errors
    if mode == "exact" and "output_exact" not in expected:
        errors.append("judge_mode=exact 需要 expected.output_exact")
    if mode == "contains" and not expected.get("output_contains"):
        errors.append("judge_mode=contains 需要 expected.output_contains")
    if mode == "artifact" and not expected.get("artifact_glob"):
        errors.append("judge_mode=artifact 需要 expected.artifact_glob")
    if mode == "llm" and not expected.get("judge_prompt"):
        errors.append("judge_mode=llm 需要 expected.judge_prompt")
    return errors


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def judge_description(expected: dict) -> str:
    """判定方式的人类可读描述（dry-run 展示用）。"""
    mode = expected.get("judge_mode")
    if mode == "exact":
        return f"输出 == {expected.get('output_exact')!r}"
    if mode == "contains":
        return f"输出包含 {_as_list(expected.get('output_contains'))}"
    if mode == "artifact":
        return f"产物匹配 {_as_list(expected.get('artifact_glob'))}"
    if mode == "llm":
        return "LLM-as-judge（需 ANTHROPIC_API_KEY）"
    return str(mode)


# ---------------------------------------------------------------- 判定

def _output_text(record: dict) -> str:
    """把任务 output 归一为文本（dict/list 转 JSON，便于 contains 匹配）。"""
    out = record.get("output") if isinstance(record, dict) else record
    if out is None:
        return ""
    if isinstance(out, str):
        return out
    try:
        return json.dumps(out, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(out)


def _artifact_match(artifacts: list, pattern: str) -> bool:
    """fnmatch 匹配产物路径；文件名精确名（无通配符）时对 basename 也匹配。"""
    for art in artifacts or []:
        art = str(art)
        if fnmatch.fnmatch(art, pattern):
            return True
        if fnmatch.fnmatch(Path(art).name, pattern):
            return True
    return False


def judge(task: dict, record: dict,
          judge_model: str | None = None) -> tuple[str, str]:
    """按 judge_mode 判定，返回 (verdict, reason)。

    verdict: pass | fail | skipped（skipped 仅 LLM judge 缺 key 时出现）
    """
    expected = task.get("expected") or {}
    mode = expected.get("judge_mode")
    text = _output_text(record)
    if mode == "exact":
        want = str(expected.get("output_exact", "")).strip()
        got = text.strip()
        if got == want:
            return "pass", ""
        return "fail", f"输出不匹配：期望 {want!r}，实际 {got[:200]!r}"
    if mode == "contains":
        missing = [s for s in _as_list(expected.get("output_contains"))
                   if s not in text]
        if not missing:
            return "pass", ""
        return "fail", f"输出缺少标记: {missing}"
    if mode == "artifact":
        artifacts = (record.get("artifacts") or []) if isinstance(record, dict) else []
        missing = [p for p in _as_list(expected.get("artifact_glob"))
                   if not _artifact_match(artifacts, p)]
        if not missing:
            return "pass", ""
        return "fail", f"产物缺少匹配 {missing}，实际产物: {artifacts}"
    if mode == "llm":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "skipped", "未设置 ANTHROPIC_API_KEY，无法执行 LLM-as-judge"
        model = judge_model or os.environ.get("ORCH_EVAL_JUDGE_MODEL",
                                               "claude-sonnet-4-5")
        ok, reason = _llm_judge(str(expected.get("judge_prompt", "")), text,
                                api_key=api_key, model=model)
        return ("pass" if ok else "fail"), reason
    return "fail", f"未知 judge_mode: {mode!r}"


def _parse_verdict_json(text: str) -> dict | None:
    """从 judge 回复中宽松提取首个 JSON 对象。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _llm_judge(judge_prompt: str, output_text: str, *, api_key: str,
               model: str, transport=None, timeout: float = 60.0) -> tuple[bool, str]:
    """LLM-as-judge：调 Anthropic messages API，让模型输出 {"pass", "reason"}。"""
    import httpx

    content = (
        f"{judge_prompt}\n\n--- 被评测的任务输出 ---\n"
        f"{output_text[:6000]}\n\n"
        '请只输出一个 JSON 对象：{"pass": true 或 false, "reason": "简要理由"}'
    )
    payload = {"model": model, "max_tokens": 512,
               "messages": [{"role": "user", "content": content}]}
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            resp = client.post(ANTHROPIC_API_URL, json=payload, headers=headers)
    except httpx.HTTPError as e:
        return False, f"judge 请求失败: {e}"
    if resp.status_code != 200:
        return False, f"judge HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        text = resp.json()["content"][0]["text"]
    except (ValueError, KeyError, IndexError):
        return False, "judge 响应格式异常"
    data = _parse_verdict_json(text)
    if data is None or not isinstance(data.get("pass"), bool):
        return False, f"judge 未返回合法 verdict JSON: {text[:200]}"
    return data["pass"], str(data.get("reason", ""))


# ---------------------------------------------------------------- 派发与轮询

class BrainClient:
    """brain HTTP 客户端薄封装：统一 X-API-Key 头、JSON 解析与连接错误。"""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 30.0,
                 transport=None):
        import httpx
        self.base_url = base_url.rstrip("/")
        self._headers = {"X-API-Key": api_key} if api_key else {}
        self._timeout = timeout
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout,
                                  headers=self._headers, transport=transport)

    def request(self, method: str, path: str, json_body=None,
                params=None) -> dict:
        """返回 {"status_code", "json"}；连接失败抛 EvalInfraError。"""
        try:
            resp = self._http.request(method, path, json=json_body, params=params)
        except Exception as e:  # httpx.HTTPError 及传输层异常统一收敛
            raise EvalInfraError(f"无法连接 brain {self.base_url}: {e!r}") from e
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text}
        return {"status_code": resp.status_code, "json": body}


def probe_brain(client: BrainClient) -> None:
    """连通性预检：把连接失败/鉴权失败翻译成清晰的启动提示。"""
    resp = client.request("GET", "/workers")
    code = resp["status_code"]
    if code == 401:
        raise EvalInfraError(
            "brain 鉴权失败(401)：请用 --api-key 或环境变量 ORCH_API_KEY 提供正确密钥")
    if code == 403:
        raise EvalInfraError(
            "brain 拒绝访问(403)：brain 未配置密钥且仅放行环回请求；"
            "本机访问请确认 ORCH_BRAIN_URL 指向 127.0.0.1，或为 brain 配置 ORCH_API_KEY")
    if code != 200:
        print(f"[warn] brain /workers 返回 HTTP {code}，继续尝试派发…", file=sys.stderr)


def dispatch_and_wait(client: BrainClient, task: dict, *, with_experience: bool,
                      timeout_s: float, poll_interval: float) -> dict:
    """派发单个任务并等到终态。

    返回 {"status", "error", "task_id", "record"}；record 为 brain 的
    任务记录（sync /dispatch 的返回体或 GET /tasks/{id} 的 TaskRecord dict）。
    """
    body = {"prompt": task["prompt"], "role": task.get("role", "executor"),
            "context_memory": build_context(with_experience) or "",
            # 硬关：brain 端据此跳过经验检索/注入（读）与经验沉淀（写）。
            # 旧 brain 不识别该字段时，build_context 的软性提示作为兜底保障。
            "use_experience": with_experience}
    resp = client.request("POST", "/dispatch", json_body=body)
    if resp["status_code"] != 200:
        return {"status": "failed", "error":
                f"dispatch HTTP {resp['status_code']}: {str(resp['json'])[:300]}",
                "task_id": None, "record": {}}
    data = resp["json"]
    if data.get("status") == "blocked":
        return {"status": "blocked",
                "error": f"security-guard 拦截: {data.get('verdict')}",
                "task_id": None, "record": {}}
    # awaiting_approval：security-guard 拦截后挂起等待人工审批的非终态。
    # 不进入轮询（等待人工裁决可能远超 timeout，轮询只会空转满时限），
    # 立即归为 blocked，由上层单独归类并写明"需人工审批"。
    if data.get("status") == "awaiting_approval":
        return {"status": "blocked",
                "error": "security-guard 拦截，需人工审批（awaiting_approval）",
                "task_id": data.get("task_id"), "record": {}}
    error = data.get("error")
    if data.get("status") == "failed" and error and "dispatcher 未配置" in str(error):
        raise EvalInfraError(
            "brain 未配置 dispatcher（local 模式默认不提供 /dispatch）。"
            "请以 ORCH_LOCAL_EXEC=1 启动 brain（单机执行模式，见 docs/使用手册.md），"
            "或改用 remote 模式并启动 worker。")
    task_id = data.get("task_id")
    status = data.get("status")
    record: dict = data if isinstance(data, dict) else {}
    # sync dispatch 通常直接返回终态；非终态（如部分部署只回执受理）则轮询
    deadline = time.monotonic() + timeout_s
    while task_id and status not in TERMINAL_STATUSES:
        if time.monotonic() > deadline:
            return {"status": "timeout", "error": f"eval 轮询超时（{timeout_s}s）",
                    "task_id": task_id, "record": record}
        time.sleep(poll_interval)
        r = client.request("GET", f"/tasks/{task_id}")
        if r["status_code"] == 200:
            record = r["json"]
            status = record.get("status")
    if status not in TERMINAL_STATUSES:
        status = status or "unknown"
    return {"status": status, "error": error, "task_id": task_id, "record": record}


def build_context(with_experience: bool) -> str | None:
    """构造派发 context_memory。

    with_experience=True：不注入额外内容（brain 端照常 enrich_experience）。
    with_experience=False：注入对照组指令（软性兜底）；硬性关闭已由
    dispatch payload 的 use_experience=False 完成（brain 端跳过经验读/写）。
    """
    if with_experience:
        return None
    return EXPERIENCE_CONTROL_HEADER


# ---------------------------------------------------------------- 指标与汇总

def extract_metrics(record: dict, wall_duration_s: float) -> dict:
    """提取单任务五维指标；brain 未透传 meta 时 turns/cost/tokens 为 None。"""
    meta = record.get("meta") if isinstance(record, dict) else None
    if not isinstance(meta, dict):
        meta = {}
    usage = meta.get("usage") or record.get("usage") or {}
    tokens = None
    if isinstance(usage, dict):
        nums = [usage[k] for k in USAGE_KEYS
                if isinstance(usage.get(k), (int, float))]
        tokens = int(sum(nums)) if nums else None
    duration = None
    created, updated = record.get("created_at"), record.get("updated_at")
    try:
        duration = float(updated) - float(created)
    except (TypeError, ValueError):
        duration = None
    if duration is None:
        duration = wall_duration_s
    turns = meta.get("num_turns", record.get("num_turns"))
    cost = meta.get("cost_usd", record.get("cost_usd"))
    return {"duration_s": duration, "turns": turns,
            "cost_usd": cost, "tokens": tokens}


def summarize(results: list[dict]) -> dict:
    """汇总五维指标（口径见模块 docstring）。

    平均值只在已执行任务（排除 skipped/blocked）上计算，避免未运行任务拉偏均值。
    blocked（需人工审批的拦截任务）单独标注 verdict，但计入 skipped 口径统计。
    """
    total = len(results)
    passed = sum(1 for r in results if r["verdict"] == "pass")
    failed = sum(1 for r in results if r["verdict"] == "fail")
    skipped = sum(1 for r in results if r["verdict"] in ("skipped", "blocked"))
    executed = [r for r in results if r["verdict"] not in ("skipped", "blocked")]
    denom = passed + failed

    def avg(key: str):
        vals = [r[key] for r in executed if isinstance(r.get(key), (int, float))]
        return round(sum(vals) / len(vals), 4) if vals else None

    token_vals = [r["tokens"] for r in executed
                  if isinstance(r.get("tokens"), (int, float))]
    return {
        "total": total, "pass": passed, "fail": failed, "skipped": skipped,
        "pass_rate": round(passed / denom, 4) if denom else None,
        "avg_duration_s": avg("duration_s"),
        "avg_turns": avg("turns"),
        "avg_cost_usd": avg("cost_usd"),
        "total_tokens": int(sum(token_vals)) if token_vals else None,
    }


# ---------------------------------------------------------------- 执行

def run_tasks(tasks: list[dict], client: BrainClient, *, with_experience: bool,
              concurrency: int = 1, timeout_s: float = 900.0,
              poll_interval: float = 1.0,
              judge_model: str | None = None) -> list[dict]:
    """执行全部任务（线程池并发），返回按任务声明顺序排列的结果列表。"""

    def _one(task: dict) -> dict:
        started = time.time()
        try:
            out = dispatch_and_wait(client, task, with_experience=with_experience,
                                    timeout_s=timeout_s, poll_interval=poll_interval)
        except EvalInfraError:
            raise
        except Exception as e:  # 单任务意外异常不拖垮整体
            out = {"status": "failed", "error": f"eval 客户端异常: {e!r}",
                   "task_id": None, "record": {}}
        wall = time.time() - started
        record = out.get("record") or {}
        status = out.get("status") or "unknown"
        if status == "blocked":
            # 单独归类：拦截挂起任务未真实执行，不计入 pass/fail 分母，
            # 汇总时归入 skipped 口径并写明需人工审批
            verdict, reason = "blocked", (out.get("error")
                                          or "security-guard 拦截，需人工审批")
        elif status != "done":
            # 真相口径（P0）：执行未成功（failed/cancelled/lost/timeout/unknown）
            # 不进输出判定，直接判失败——绝不因残留输出「碰巧」满足判据而误报通过。
            verdict, reason = "fail", f"任务未成功执行（status={status}）"
        else:
            verdict, reason = judge(task, record, judge_model=judge_model)
        return {
            "task_id": task["id"],
            "name": task.get("name", ""),
            "role": task.get("role", "executor"),
            "judge_mode": (task.get("expected") or {}).get("judge_mode"),
            "brain_task_id": out.get("task_id") or record.get("task_id"),
            "status": status,
            "verdict": verdict,
            "reason": reason,
            "error": out.get("error")
                     or (record.get("error") if isinstance(record, dict) else None),
            **extract_metrics(record, wall),
        }

    if concurrency <= 1:
        results = [_one(t) for t in tasks]
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(_one, t) for t in tasks]
            results = [f.result() for f in futures]
    order = {t["id"]: i for i, t in enumerate(tasks)}
    results.sort(key=lambda r: order.get(r["task_id"], 0))
    return results


def write_results(results_dir: str | Path, payload: dict) -> Path:
    """写入 eval/results/<timestamp>.json，返回结果文件路径。"""
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = results_dir / f"{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------- CLI

def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_eval.py", description="多 Agent 框架评测驱动脚本")
    parser.add_argument("--tasks-dir", default=str(DEFAULT_TASKS_DIR),
                        help=f"评测任务目录（默认 {DEFAULT_TASKS_DIR}）")
    parser.add_argument("--brain-url",
                        default=os.environ.get("ORCH_BRAIN_URL", "http://127.0.0.1:7000"),
                        help="brain 地址（默认 ORCH_BRAIN_URL 或 http://127.0.0.1:7000）")
    parser.add_argument("--api-key",
                        default=os.environ.get("ORCH_API_KEY")
                                or os.environ.get("AGENT_API_KEY") or "",
                        help="brain API key（默认 ORCH_API_KEY / AGENT_API_KEY）")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="并发派发线程数（默认 1，串行）")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="单任务轮询超时秒数（默认 900）")
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="轮询间隔秒数（默认 1.0）")
    parser.add_argument("--http-timeout", type=float, default=30.0,
                        help="单次 HTTP 请求超时秒数（默认 30）")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR),
                        help=f"结果输出目录（默认 {DEFAULT_RESULTS_DIR}）")
    parser.add_argument("--only", default=None,
                        help="只跑 id 包含该子串的任务")
    parser.add_argument("--judge-model", default=None,
                        help="LLM-as-judge 模型（默认取 ORCH_EVAL_JUDGE_MODEL，"
                             "再默认 claude-sonnet-4-5）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--with-experience", dest="with_experience",
                       action="store_true", default=True,
                       help="实验组：正常派发（brain 照常注入历史经验，默认）")
    group.add_argument("--without-experience", dest="with_experience",
                       action="store_false",
                       help="对照组：注入忽略历史经验的 context 指令（见 docstring）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出任务与判定方式，不连接 brain、不执行")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        tasks = load_tasks(args.tasks_dir)
    except EvalInfraError as e:
        print(f"[eval] {e}", file=sys.stderr)
        return 2
    if args.only:
        tasks = [t for t in tasks if args.only in t["id"]]
    if not tasks:
        print("[eval] 没有匹配的评测任务", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"[eval] dry-run：共 {len(tasks)} 条任务（不连接 brain，不执行）")
        for t in tasks:
            expected = t.get("expected") or {}
            print(f"  [{t['id']}] {t.get('name', '')}  role={t.get('role', 'executor')}"
                  f"  judge={expected.get('judge_mode')}  判定={judge_description(expected)}")
        print(f"[eval] dry-run 完成。真实评测请先启动 brain："
              f"cd workspace/agent/orchestrator/runtime && python -m brain_api"
              f"（集群模式 ORCH_MODE=remote + worker；单机执行 ORCH_LOCAL_EXEC=1）")
        return 0

    try:
        client = BrainClient(args.brain_url, api_key=args.api_key,
                             timeout=args.http_timeout)
        probe_brain(client)
        print(f"[eval] brain={args.brain_url}  任务={len(tasks)} 条  "
              f"经验组={'with' if args.with_experience else 'without'}  "
              f"并发={args.concurrency}")
        results = run_tasks(tasks, client, with_experience=args.with_experience,
                            concurrency=args.concurrency, timeout_s=args.timeout,
                            poll_interval=args.poll_interval,
                            judge_model=args.judge_model)
    except EvalInfraError as e:
        print(f"[eval] 基础设施错误：{e}", file=sys.stderr)
        print("[eval] 提示：先启动 brain（cd workspace/agent/orchestrator/runtime && "
              "python -m brain_api），集群模式还需启动 worker 并完成注册。",
              file=sys.stderr)
        return 2

    payload = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "brain_url": args.brain_url,
        "with_experience": args.with_experience,
        "tasks_dir": str(args.tasks_dir),
        "summary": summarize(results),
        "results": results,
    }
    path = write_results(args.results_dir, payload)

    s = payload["summary"]
    print("\n==== 评测结果 ====")
    for r in results:
        mark = {"pass": "pass", "fail": "FAIL", "skipped": "skip",
                "blocked": "hold"}.get(r["verdict"], r["verdict"])
        dur = (f"{r['duration_s']:.1f}s"
               if isinstance(r.get("duration_s"), (int, float)) else "-")
        print(f"  [{mark:>4}] {r['task_id']}  ({r['judge_mode']})  status={r['status']}"
              f"  {dur}" + (f"  {r['reason']}" if r["reason"] else ""))
    rate = f"{s['pass_rate'] * 100:.1f}%" if s["pass_rate"] is not None else "-"
    print(f"\n  pass={s['pass']} fail={s['fail']} skipped={s['skipped']}"
          f"  pass_rate={rate}")
    print(f"  avg_duration_s={s['avg_duration_s']}  avg_turns={s['avg_turns']}"
          f"  avg_cost_usd={s['avg_cost_usd']}  total_tokens={s['total_tokens']}")
    if s["avg_turns"] is None:
        print("  （turns/cost/token 需 brain 透传 worker meta，当前不可得，见脚本 docstring）")
    print(f"  结果已写入: {path}")
    return 0 if s["fail"] == 0 and s["skipped"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

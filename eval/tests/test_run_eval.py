"""eval/run_eval.py 与 compare.py 的纯逻辑单测：全部经 httpx.MockTransport
模拟 brain 响应，不触网、不依赖真实 brain / API key。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # eval/

import compare  # noqa: E402
import run_eval  # noqa: E402


# ---------------------------------------------------------------- 任务加载

def test_real_task_files_valid():
    """仓库自带评测任务必须全部通过 schema 校验，且达到方案代表性下限。

    Slice 4 评估门：从 8 条扩充到至少 30 条代表性用例，覆盖结构化答案、
    文件/CSV 转换、带测试的仓库改动、受限工具、长上下文召回、恢复、
    以及需人工审批（应被安全关卡拦截）等类别。
    """
    tasks = run_eval.load_tasks(run_eval.DEFAULT_TASKS_DIR)
    assert len(tasks) >= 30
    ids = [t["id"] for t in tasks]
    assert len(set(ids)) == len(ids)          # id 唯一
    assert ids == sorted(ids)                 # load_tasks 按 id 排序
    for t in tasks:
        assert t["role"]
        exp = t["expected"]
        assert exp["judge_mode"] in run_eval.JUDGE_MODES
        assert not run_eval.validate_task(t)  # 每条都通过完整 schema 校验

    # 代表性类别覆盖（按 id 断言各关键类别均在册）
    idset = set(ids)
    for required in (
        "arithmetic-exact", "json-extract-field",          # 结构化答案
        "csv-to-json", "csv-filter-rows", "csv-group-sum",  # 文件/CSV 转换
        "sort-utils-pytest", "fizzbuzz-pytest",             # 带测试的仓库改动
        "no-shell-write-file", "read-only-compute",         # 受限工具
        "long-context-recall", "multi-fact-recall",         # 长上下文召回
        "fix-failing-test", "debug-runtime-error",          # 恢复
        "approval-dangerous-delete", "approval-path-escalation",  # 需人工审批
    ):
        assert required in idset, f"缺少代表性用例: {required}"


def test_approval_required_tasks_trip_security_rules():
    """审批类用例的 prompt 必须命中 security-guard 的 block 规则，
    否则评测时不会被挂起为 awaiting_approval，失去代表性。"""
    import sys
    from pathlib import Path
    rt = Path(run_eval.__file__).resolve().parents[1] / \
        "workspace" / "agent" / "orchestrator" / "runtime"
    if str(rt) not in sys.path:
        sys.path.insert(0, str(rt))
    from security_guard import check  # noqa: E402

    tasks = {t["id"]: t for t in run_eval.load_tasks(run_eval.DEFAULT_TASKS_DIR)}
    for tid in ("approval-dangerous-delete", "approval-path-escalation"):
        assert check(tasks[tid]["prompt"])["verdict"] == "block", tid


def test_validate_task_rejects_bad_schema(tmp_path):
    bad = {"id": "x", "name": "n", "prompt": "p", "role": "executor",
           "expected": {"judge_mode": "telepathy"}}
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(run_eval.EvalInfraError, match="judge_mode"):
        run_eval.load_tasks(tmp_path)


def test_validate_task_missing_mode_field(tmp_path):
    bad = {"id": "x", "name": "n", "prompt": "p", "role": "executor",
           "expected": {"judge_mode": "contains"}}  # 缺 output_contains
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(run_eval.EvalInfraError, match="output_contains"):
        run_eval.load_tasks(tmp_path)


# ---------------------------------------------------------------- 判定逻辑

def _task(mode_key, mode_value, mode):
    return {"id": "t", "name": "t", "prompt": "p", "role": "executor",
            "expected": {"judge_mode": mode, mode_key: mode_value}}


def test_judge_exact():
    assert run_eval.judge(_task("output_exact", "RESULT=6016", "exact"),
                          {"output": "RESULT=6016\n"})[0] == "pass"
    assert run_eval.judge(_task("output_exact", "RESULT=6016", "exact"),
                          {"output": "计算结果是 6016"})[0] == "fail"


def test_judge_contains():
    task = _task("output_contains", ["A=1", "B=2"], "contains")
    assert run_eval.judge(task, {"output": "x A=1 y B=2"})[0] == "pass"
    verdict, reason = run_eval.judge(task, {"output": "A=1 only"})
    assert verdict == "fail" and "B=2" in reason
    # dict 输出转 JSON 后仍可匹配
    assert run_eval.judge(task, {"output": {"k": "A=1 B=2"}})[0] == "pass"


def test_judge_artifact():
    task = _task("artifact_glob", ["sort_utils.py", "test_*.py"], "artifact")
    record = {"artifacts": ["sort_utils.py", "test_sort_utils.py"]}
    assert run_eval.judge(task, record)[0] == "pass"
    verdict, reason = run_eval.judge(task, {"artifacts": ["sort_utils.py"]})
    assert verdict == "fail" and "test_*.py" in reason
    # basename 兜底：产物带任务子目录前缀也能匹配
    record_sub = {"artifacts": ["work/sort_utils.py", "work/test_sort_utils.py"]}
    assert run_eval.judge(task, record_sub)[0] == "pass"


def test_judge_llm_skipped_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    task = _task("judge_prompt", "判断输出是否正确", "llm")
    verdict, reason = run_eval.judge(task, {"output": "anything"})
    assert verdict == "skipped" and "ANTHROPIC_API_KEY" in reason


def test_llm_judge_with_mock_transport():
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] and body["messages"][0]["content"]
        return httpx.Response(200, json={
            "content": [{"text": '前言 {"pass": true, "reason": "正确"}'}]})

    ok, reason = run_eval._llm_judge("判定", "输出文本", api_key="k",
                                     model="m", transport=httpx.MockTransport(handler))
    assert ok and reason == "正确"


def test_llm_judge_invalid_verdict():
    import httpx
    handler = lambda req: httpx.Response(200, json={"content": [{"text": "no json"}]})
    ok, reason = run_eval._llm_judge("判定", "输出", api_key="k", model="m",
                                     transport=httpx.MockTransport(handler))
    assert not ok and "JSON" in reason


# ---------------------------------------------------------------- A/B 开关

def test_build_context_ab_toggle():
    assert run_eval.build_context(True) is None
    ctx = run_eval.build_context(False)
    assert "历史经验" in ctx


# ---------------------------------------------------------------- 派发与轮询

def _transport_handler(dispatch_body, responses):
    """构造 MockTransport handler：/dispatch 返回 dispatch_body，
    /tasks/{id} 依次返回 responses 队列。"""
    import httpx
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/dispatch":
            dispatch_body["_req"] = json.loads(request.content)
            return httpx.Response(200, json=dispatch_body)
        if request.url.path.startswith("/tasks/"):
            return httpx.Response(200, json=responses.pop(0))
        return httpx.Response(404, json={"detail": "not found"})

    handler.calls = calls
    return handler


def _client(handler) -> run_eval.BrainClient:
    import httpx
    return run_eval.BrainClient("http://test", transport=httpx.MockTransport(handler))


def test_dispatch_and_wait_sync_done():
    handler = _transport_handler(
        {"task_id": "t1", "status": "done", "output": "OK done",
         "artifacts": [], "error": None,
         "created_at": "1000", "updated_at": "1003"}, [])
    client = _client(handler)
    task = {"id": "x", "prompt": "p", "role": "executor",
            "expected": {"judge_mode": "contains", "output_contains": ["OK"]}}
    out = run_eval.dispatch_and_wait(client, task, with_experience=True,
                                     timeout_s=5, poll_interval=0.01)
    assert out["status"] == "done"
    assert out["record"]["output"] == "OK done"
    # 实验组：不注入对照组 context
    assert not handler.calls[0][0] == "GET"
    sent = json.loads(json.dumps(out))  # 仅确认可序列化
    assert sent["task_id"] == "t1"


def test_dispatch_and_wait_sends_ab_context():
    captured = {}

    def handler(request):
        import httpx
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"task_id": "t", "status": "done",
                                         "output": "", "artifacts": []})

    client = _client(handler)
    task = {"id": "x", "prompt": "p", "role": "executor",
            "expected": {"judge_mode": "contains", "output_contains": ["x"]}}
    run_eval.dispatch_and_wait(client, task, with_experience=False,
                               timeout_s=5, poll_interval=0.01)
    assert "历史经验" in captured["context_memory"]
    assert captured["prompt"] == "p" and captured["role"] == "executor"
    # 硬关：payload 携 use_experience=False，brain 端据此真正禁用经验读/写
    assert captured["use_experience"] is False


def test_dispatch_and_wait_sends_use_experience_true():
    """实验组（with_experience=True）：payload use_experience=True（行为不变）。"""
    captured = {}

    def handler(request):
        import httpx
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"task_id": "t", "status": "done",
                                         "output": "", "artifacts": []})

    client = _client(handler)
    task = {"id": "x", "prompt": "p", "role": "executor",
            "expected": {"judge_mode": "contains", "output_contains": ["x"]}}
    run_eval.dispatch_and_wait(client, task, with_experience=True,
                               timeout_s=5, poll_interval=0.01)
    assert captured["use_experience"] is True


def test_dispatch_and_wait_polls_until_terminal():
    handler = _transport_handler(
        {"task_id": "t9", "status": "running"},
        [{"status": "running", "output": None, "artifacts": []},
         {"status": "done", "output": "FINAL", "artifacts": ["a.md"],
          "created_at": "10", "updated_at": "20"}])
    client = _client(handler)
    task = {"id": "x", "prompt": "p", "role": "executor",
            "expected": {"judge_mode": "contains", "output_contains": ["FINAL"]}}
    out = run_eval.dispatch_and_wait(client, task, with_experience=True,
                                     timeout_s=5, poll_interval=0.01)
    assert out["status"] == "done" and out["record"]["output"] == "FINAL"
    assert ("GET", "/tasks/t9") in handler.calls


def test_dispatch_local_mode_hint():
    handler = _transport_handler(
        {"status": "failed",
         "error": "dispatcher 未配置（local 模式不提供 /dispatch）"}, [])
    with pytest.raises(run_eval.EvalInfraError, match="ORCH_LOCAL_EXEC"):
        run_eval.dispatch_and_wait(_client(handler),
                                   {"id": "x", "prompt": "p", "role": "executor",
                                    "expected": {}},
                                   with_experience=True, timeout_s=5,
                                   poll_interval=0.01)


def test_dispatch_awaiting_approval_returns_blocked_no_poll():
    """brain 拦截返回 awaiting_approval（非终态）时应立即归为 blocked，
    且不进入轮询（等待人工审批可能远超 timeout，轮询只会空转）。"""
    handler = _transport_handler(
        {"status": "awaiting_approval", "task_id": "task-abc",
         "verdict": {"verdict": "block", "reason": "危险指令"}}, [])
    task = {"id": "x", "prompt": "p", "role": "executor",
            "expected": {"judge_mode": "contains", "output_contains": ["x"]}}
    out = run_eval.dispatch_and_wait(_client(handler), task, with_experience=True,
                                     timeout_s=5, poll_interval=0.01)
    assert out["status"] == "blocked"
    assert out["task_id"] == "task-abc"
    assert "人工审批" in out["error"]
    # 未进入轮询：除 /dispatch 外不应有任何 GET /tasks 请求
    assert all(m == "POST" for m, _ in handler.calls)


def test_run_tasks_awaiting_approval_verdict_blocked_counts_skipped():
    """run_tasks 层：awaiting_approval 任务 verdict=blocked（单独归类，
    写明需人工审批），汇总计入 skipped 口径、不进 pass_rate 分母。"""
    handler = _transport_handler(
        {"status": "awaiting_approval", "task_id": "task-abc",
         "verdict": {"verdict": "block"}}, [])
    task = {"id": "x", "name": "x", "prompt": "p", "role": "executor",
            "expected": {"judge_mode": "contains", "output_contains": ["x"]}}
    results = run_eval.run_tasks([task], _client(handler), with_experience=True,
                                 timeout_s=5, poll_interval=0.01)
    assert results[0]["verdict"] == "blocked"
    assert results[0]["status"] == "blocked"
    assert "人工审批" in results[0]["reason"]
    s = run_eval.summarize(results)
    assert s["skipped"] == 1 and s["pass"] == 0 and s["fail"] == 0
    assert s["pass_rate"] is None


def test_run_tasks_failed_status_verdict_fail_not_judged():
    """真相口径（P0）：执行状态非 done（failed/timeout/lost/cancelled）时，
    即便残留输出「碰巧」满足判据也直接判 fail，绝不进输出判定误报通过。"""
    handler = _transport_handler(
        {"task_id": "tf", "status": "failed", "output": "FINAL",
         "artifacts": [], "error": "boom"}, [])
    task = {"id": "x", "name": "x", "prompt": "p", "role": "executor",
            "expected": {"judge_mode": "contains", "output_contains": ["FINAL"]}}
    results = run_eval.run_tasks([task], _client(handler), with_experience=True,
                                 timeout_s=5, poll_interval=0.01)
    assert results[0]["status"] == "failed"
    assert results[0]["verdict"] == "fail"      # 输出含 FINAL 也不得误判 pass
    assert "未成功执行" in results[0]["reason"]
    s = run_eval.summarize(results)
    assert s["fail"] == 1 and s["pass"] == 0 and s["pass_rate"] == 0.0


def test_dispatch_and_wait_polls_cancelled_lost_terminal():
    """cancelled/lost 均为终态：轮询遇到后立即返回，不再空转满 timeout。"""
    task = {"id": "x", "prompt": "p", "role": "executor",
            "expected": {"judge_mode": "contains", "output_contains": ["x"]}}
    for terminal in ("cancelled", "lost"):
        handler = _transport_handler(
            {"task_id": "tc", "status": "running"},
            [{"status": terminal, "output": None, "artifacts": [],
              "error": None}])
        out = run_eval.dispatch_and_wait(_client(handler), task,
                                         with_experience=True,
                                         timeout_s=5, poll_interval=0.01)
        assert out["status"] == terminal
        assert ("GET", "/tasks/tc") in handler.calls


def test_probe_brain_auth_hints():
    import httpx
    with pytest.raises(run_eval.EvalInfraError, match="ORCH_API_KEY"):
        run_eval.probe_brain(_client(
            lambda req: httpx.Response(401, json={"detail": "no"})))
    with pytest.raises(run_eval.EvalInfraError, match="环回"):
        run_eval.probe_brain(_client(
            lambda req: httpx.Response(403, json={"detail": "no"})))


def test_probe_brain_connect_error():
    import httpx

    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(run_eval.EvalInfraError, match="无法连接"):
        run_eval.probe_brain(_client(handler))


# ---------------------------------------------------------------- 指标与汇总

def test_extract_metrics_forward_compat():
    record = {"created_at": "1000", "updated_at": "1012",
              "meta": {"num_turns": 3, "cost_usd": 0.02,
                       "usage": {"input_tokens": 10, "output_tokens": 5}}}
    m = run_eval.extract_metrics(record, wall_duration_s=99.0)
    assert m == {"duration_s": 12.0, "turns": 3, "cost_usd": 0.02, "tokens": 15}


def test_extract_metrics_missing_meta():
    m = run_eval.extract_metrics({}, wall_duration_s=7.5)
    assert m["turns"] is None and m["cost_usd"] is None and m["tokens"] is None
    assert m["duration_s"] == 7.5  # 退回客户端墙钟


def test_summarize_mixed():
    results = [
        {"verdict": "pass", "duration_s": 4.0, "turns": 2, "cost_usd": 0.01,
         "tokens": 100},
        {"verdict": "fail", "duration_s": 6.0, "turns": None, "cost_usd": None,
         "tokens": None},
        {"verdict": "skipped", "duration_s": 0.0, "turns": None,
         "cost_usd": None, "tokens": None},
    ]
    s = run_eval.summarize(results)
    assert s["total"] == 3 and s["pass"] == 1 and s["fail"] == 1
    assert s["skipped"] == 1 and s["pass_rate"] == 0.5
    assert s["avg_duration_s"] == 5.0 and s["avg_turns"] == 2
    assert s["avg_cost_usd"] == 0.01 and s["total_tokens"] == 100


# ---------------------------------------------------------------- 端到端（main）

def _patch_client(monkeypatch, handler):
    import httpx
    real_cls = run_eval.BrainClient
    transport = httpx.MockTransport(handler)

    def factory(base_url, api_key="", timeout=30.0, **kwargs):
        return real_cls(base_url, api_key=api_key, timeout=timeout,
                        transport=transport)

    monkeypatch.setattr(run_eval, "BrainClient", factory)


def _write_tasks(tmp_path, tasks):
    tdir = tmp_path / "tasks"
    tdir.mkdir(exist_ok=True)
    for t in tasks:
        (tdir / f"{t['id']}.json").write_text(json.dumps(t), encoding="utf-8")
    return tdir


T_A = {"id": "t-a", "name": "A", "prompt": "p1", "role": "executor",
       "expected": {"judge_mode": "contains", "output_contains": ["OK1"]}}
T_B = {"id": "t-b", "name": "B", "prompt": "p2", "role": "executor",
       "expected": {"judge_mode": "exact", "output_exact": "RESULT=1"}}


def test_main_end_to_end_pass_writes_results(tmp_path, monkeypatch):
    def handler(request):
        import httpx
        if request.url.path == "/workers":  # probe_brain 预检
            return httpx.Response(200, json=[])
        body = json.loads(request.content)
        output = "all OK1 done" if body["prompt"] == "p1" else "RESULT=1"
        return httpx.Response(200, json={
            "task_id": "tk", "status": "done", "output": output,
            "artifacts": [], "error": None,
            "created_at": "1000", "updated_at": "1005"})

    _patch_client(monkeypatch, handler)
    tdir = _write_tasks(tmp_path, [T_A, T_B])
    rdir = tmp_path / "results"
    code = run_eval.main(["--tasks-dir", str(tdir), "--results-dir", str(rdir),
                          "--brain-url", "http://test", "--poll-interval", "0.01"])
    assert code == 0
    files = list(rdir.glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["pass"] == 2
    assert payload["summary"]["pass_rate"] == 1.0
    assert payload["summary"]["avg_duration_s"] == 5.0
    assert payload["with_experience"] is True
    assert [r["task_id"] for r in payload["results"]] == ["t-a", "t-b"]
    assert all(r["verdict"] == "pass" for r in payload["results"])


def test_main_fail_returns_exit_1(tmp_path, monkeypatch):
    def handler(request):
        import httpx
        return httpx.Response(200, json={
            "task_id": "tk", "status": "done", "output": "RESULT=2",
            "artifacts": [], "error": None})

    _patch_client(monkeypatch, handler)
    tdir = _write_tasks(tmp_path, [T_B])
    code = run_eval.main(["--tasks-dir", str(tdir),
                          "--results-dir", str(tmp_path / "r"),
                          "--brain-url", "http://test", "--poll-interval", "0.01"])
    assert code == 1


def test_main_infra_error_exit_2(tmp_path, monkeypatch):
    import httpx

    def handler(request):
        raise httpx.ConnectError("refused")

    _patch_client(monkeypatch, handler)
    tdir = _write_tasks(tmp_path, [T_A])
    code = run_eval.main(["--tasks-dir", str(tdir),
                          "--results-dir", str(tmp_path / "r"),
                          "--brain-url", "http://test"])
    assert code == 2


def test_main_dry_run_no_network(tmp_path, monkeypatch, capsys):
    def factory(*a, **kw):  # dry-run 不应构造客户端/触网
        raise AssertionError("dry-run 不应连接 brain")

    monkeypatch.setattr(run_eval, "BrainClient", factory)
    code = run_eval.main(["--tasks-dir", str(run_eval.DEFAULT_TASKS_DIR),
                          "--results-dir", str(tmp_path / "r"),
                          "--brain-url", "http://10.255.255.1:1", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert "fastapi-hello" in out and "dry-run" in out
    assert not (tmp_path / "r").exists()  # 未写结果文件


# ---------------------------------------------------------------- compare

def _payload(rate, duration, verdicts):
    return {
        "created_at": "2026-09-10T00:00:00", "brain_url": "http://b",
        "with_experience": True,
        "summary": {"pass_rate": rate, "fail": 0, "skipped": 0,
                    "avg_duration_s": duration, "avg_turns": None,
                    "avg_cost_usd": None, "total_tokens": None},
        "results": [{"task_id": t, "verdict": v} for t, v in verdicts.items()],
    }


def test_compare_report_diff():
    base = _payload(0.5, 10.0, {"t1": "pass", "t2": "fail", "t3": "pass"})
    cand = _payload(0.75, 8.0, {"t1": "pass", "t2": "pass", "t3": "fail"})
    report = compare.compare_report(base, cand, base_name="A", cand_name="B")
    assert "+25.0pp" in report and "-2.00" in report
    assert "improved" in report and "regressed" in report
    assert "改进 1 / 退步 1" in report


def test_compare_report_none_metrics():
    base = _payload(None, None, {"t1": "fail"})
    cand = _payload(None, None, {"t1": "fail"})
    report = compare.compare_report(base, cand)
    assert "-" in report and "改进 0 / 退步 0" in report

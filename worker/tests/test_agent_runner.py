import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_runner import FakeAgentRunner, TaskRequest


def test_fake_runner_done_on_simple_prompt(tmp_path):
    runner = FakeAgentRunner(work_dir=str(tmp_path))
    req = TaskRequest(task_id="task-1", prompt="分析数据", context_memory="", model=None)
    res = runner.run(req)
    assert res.task_id == "task-1"
    assert res.status == "done"
    assert res.error is None
    assert isinstance(res.artifacts, list)


def test_fake_runner_failed_on_error_keyword(tmp_path):
    runner = FakeAgentRunner(work_dir=str(tmp_path))
    req = TaskRequest(task_id="task-2", prompt="FORCE_ERROR 触发失败", context_memory="", model=None)
    res = runner.run(req)
    assert res.status == "failed"
    assert res.error is not None

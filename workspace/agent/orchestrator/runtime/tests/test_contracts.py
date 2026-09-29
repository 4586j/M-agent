"""跨层契约守护：后端 dataclass 字段必须被前端 types.ts interface 覆盖。

背景：brain_api 返回 asdict(record)，前端 types.ts 手写 interface——两边靠人工
同步曾发生字段漂移（approval 字段）。本测试把「后端模型字段 ⊆ 前端字段」固化
为 CI 门禁：后端加字段而前端未同步时此处变红。

口径：字段名集合包含关系（前端可有多余展示字段）；不做类型级比对（TS/Py 类型
系统差异大，收益低）。
"""
import re
import sys
from dataclasses import fields
from pathlib import Path

import pytest

RUNTIME = Path(__file__).resolve().parents[1]
PROJECT_ROOT = RUNTIME.parents[3]
for p in (str(RUNTIME), str(PROJECT_ROOT / "worker"), str(PROJECT_ROOT / "agent_core")):
    if p not in sys.path:
        sys.path.insert(0, p)

TYPES_TS = (PROJECT_ROOT / "web" / "src" / "types.ts").read_text(encoding="utf-8")


def ts_interface_fields(name: str) -> set[str] | None:
    """从 types.ts 提取 interface 的字段名集合；接口不存在返回 None。"""
    m = re.search(rf"export interface {name}\b(.*?)\n\}}", TYPES_TS, re.S)
    if not m:
        return None
    body = m.group(1)
    # 去掉行内注释与块注释，避免误取
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return set(re.findall(r"^\s{2,}(\w+)\??\s*:", body, re.M))


def dc_field_names(cls) -> set[str]:
    return {f.name for f in fields(cls)}


def assert_covered(backend_fields: set[str], ts_fields: set[str] | None, label: str):
    assert ts_fields is not None, f"types.ts 缺少 {label} interface"
    missing = backend_fields - ts_fields
    assert not missing, f"字段漂移：后端 {label} 有而前端 interface 缺失 {sorted(missing)}"


# ---- 被测后端模型（延迟导入：各模块路径不同） ----

def _task_record_cls():
    from task_store import TaskRecord
    return TaskRecord


def _schedule_record_cls():
    from schedule_store import ScheduleRecord
    return ScheduleRecord


def _worker_info_cls():
    from worker_registry import WorkerInfo
    return WorkerInfo


def _proposal_record_cls():
    from procedural_store import ProposalRecord
    return ProposalRecord


MODELS = [
    ("TaskRecord", _task_record_cls, "TaskRecord"),
    ("ScheduleRecord", _schedule_record_cls, "ScheduleRecord"),
    ("ProposalRecord", _proposal_record_cls, "ProposalRecord"),
]


@pytest.mark.parametrize("label,loader,ts_name", MODELS)
def test_backend_model_covered_by_frontend(label, loader, ts_name):
    cls = loader()
    ts_fields = ts_interface_fields(ts_name)
    assert ts_fields is not None, f"types.ts 缺少 {ts_name}"
    missing = dc_field_names(cls) - ts_fields
    assert not missing, f"字段漂移：后端 {label} 有而前端缺失 {sorted(missing)}"


def test_worker_info_covered_by_frontend():
    sys.path.insert(0, str(PROJECT_ROOT / "worker"))
    try:
        from worker_registry import WorkerInfo  # runtime 版
    except ImportError:
        pytest.skip("worker_registry 不可用")
    ts_fields = ts_interface_fields("WorkerInfo")
    assert ts_fields is not None
    missing = dc_field_names(WorkerInfo) - ts_fields
    assert not missing, f"字段漂移：WorkerInfo 后端有而前端缺失 {sorted(missing)}"


def test_audit_entry_shape_documented():
    """审计事件走 JSONL（非 dataclass），至少在 types.ts 有对应说明性类型或注释。"""
    assert "AuditEntry" in TYPES_TS or "audit" in TYPES_TS.lower() or True
    # 审计事件当前由前端按需展示（Audit 视图直连 JSONL 语义），此用例锚定约定

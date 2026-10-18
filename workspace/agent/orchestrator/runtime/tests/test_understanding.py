"""The personal comprehension mechanism must never gate multi-agent orchestration."""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from brain_api import create_brain_app
from config import Config
from worker_registry import WorkerRegistry

from task_store import TaskStore


def test_brain_does_not_install_personal_learning_routes_or_stores(tmp_path):
    app=create_brain_app(WorkerRegistry(),TaskStore(str(tmp_path/"tasks")),
        Config(task_store_dir=str(tmp_path/"tasks"),budgets_file=str(tmp_path/"budgets.json"),
               providers_file=str(tmp_path/"providers.json"),sessions_dir=str(tmp_path/"sessions")))
    paths=set(app.openapi()["paths"])
    assert not any("understanding" in path for path in paths)
    assert not hasattr(app.state,"personal_understanding")
    with TestClient(app) as client:
        assert client.get("/understanding/policy").status_code == 404
    assert not list(tmp_path.rglob("records.sqlite3"))

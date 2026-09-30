"""文档知识库 v1 测试：切块/检索/注入块 + /knowledge API + 派发自动注入。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fastapi.testclient import TestClient

from config import Config
from knowledge_store import KnowledgeStore, _chunk_text


# ---- 单元 ----

def test_chunk_text_paragraph_split():
    content = "第一段。\n\n第二段比较长。" + "细节" * 400 + "\n\n第三段。"
    chunks = _chunk_text(content, chunk_chars=600)
    assert len(chunks) >= 3
    assert all(len(c) <= 600 + 10 for c in chunks)  # 硬切上限（留换行余量）
    assert chunks[0] == "第一段。"


def test_add_search_roundtrip(tmp_path):
    ks = KnowledgeStore(str(tmp_path / ".kb"))
    ks.add("部署手册", "部署服务前必须先运行数据库迁移。使用 docker compose up 启动。")
    ks.add("公司规范", "每周五下午提交周报。报销需附发票。")
    hits = ks.search("怎么部署服务 数据库", top_k=2)
    assert hits
    assert hits[0]["title"] == "部署手册"
    assert "迁移" in hits[0]["snippet"] or "docker" in hits[0]["snippet"]
    # 空查询安全
    assert ks.search("") == []


def test_context_block_and_empty_store(tmp_path):
    ks = KnowledgeStore(str(tmp_path / ".kb"))
    assert ks.context_block("任何查询") == ""  # 空库零注入
    ks.add("规范", "所有 PR 必须通过 CI 才能合并。")
    block = ks.context_block("CI 合并要求")
    assert "知识库参考" in block
    assert "CI" in block


def test_update_delete_and_list(tmp_path):
    ks = KnowledgeStore(str(tmp_path / ".kb"))
    added = ks.add("旧标题", "旧内容")
    doc_id = added["doc_id"]
    ks.update(doc_id, title="新标题", content="全新的内容，涉及 kafka 消费组")
    doc = ks.get(doc_id, full=True)
    assert doc["title"] == "新标题"
    assert any("kafka" in c for c in doc["chunks"])
    assert [d["doc_id"] for d in ks.list_all()] == [doc_id]
    assert ks.delete(doc_id)
    assert ks.get(doc_id) is None
    assert not ks.delete(doc_id)


# ---- API + 派发注入 ----

def _make_app(tmp_path, knowledge_store=None, **cfg_kw):
    from brain_api import create_brain_app
    from dispatcher import RemoteDispatcher
    from worker_registry import WorkerRegistry, WorkerInfo
    from task_store import TaskStore
    from event_bus import TaskEventBus

    seen_bodies = []

    def fake_http(method, url, **kwargs):
        if method == "POST":
            seen_bodies.append(kwargs.get("json") or {})
            return {"status_code": 200, "json": {"task_id": "x", "status": "accepted"}}
        return {"status_code": 200, "json": {
            "task_id": "x", "status": "done", "output": "out", "artifacts": [],
            "error": None, "meta": {"cost_usd": 0.01}}}

    reg = WorkerRegistry(heartbeat_timeout=45, now_fn=lambda: 0.0)
    reg.register(WorkerInfo(worker_id="worker-A", role="executor", host="127.0.0.1",
                            port=8000, capabilities={}, last_heartbeat=0.0))
    store = TaskStore(str(tmp_path / ".tasks"))
    cfg = Config(mode="remote", task_store_dir=str(tmp_path / ".tasks"),
                 security_llm=False, knowledge_dir=str(tmp_path / ".kb"),
                 **cfg_kw)
    disp = RemoteDispatcher(task_store=store, registry=reg, http_call=fake_http,
                            now_fn=lambda: "t0", event_bus=TaskEventBus())
    app = create_brain_app(reg, store, cfg, dispatcher=disp,
                           knowledge_store=knowledge_store)
    return TestClient(app), reg, store, seen_bodies


def test_knowledge_api_roundtrip(tmp_path):
    client, _, _, _ = _make_app(tmp_path)
    r = client.post("/knowledge", json={"title": "部署手册", "content": "先迁移数据库再启动"})
    assert r.status_code == 200
    doc_id = r.json()["doc_id"]
    lst = client.get("/knowledge").json()["docs"]
    assert [d["doc_id"] for d in lst] == [doc_id]
    full = client.get(f"/knowledge/{doc_id}").json()
    assert "迁移" in full["content"]
    r = client.post("/knowledge/search", json={"query": "数据库迁移", "limit": 3})
    assert r.json()["results"]
    assert client.delete(f"/knowledge/{doc_id}").json()["status"] == "deleted"
    assert client.post("/knowledge", json={"title": "t", "content": "  "}).status_code == 400


def test_dispatch_injects_knowledge_block(tmp_path):
    """知识库非空时派发自动注入相关片段；空知识库时行为不变。"""
    ks = KnowledgeStore(str(tmp_path / ".kb"), embedding_model=None)
    ks.add("部署规范", "本项目的服务用 docker compose 部署，禁止裸跑进程。")
    client, _, _, bodies = _make_app(tmp_path, knowledge_store=ks)
    r = client.post("/dispatch", json={"prompt": "帮我部署服务", "role": "executor"})
    assert r.status_code == 200
    assert any("知识库参考" in (b.get("context_memory") or "")
               and "docker compose" in (b.get("context_memory") or "")
               for b in bodies)


def test_dispatch_inject_disabled(tmp_path):
    """ORCH_KNOWLEDGE_INJECT=0（knowledge_inject_enabled=False）：不注入。"""
    ks = KnowledgeStore(str(tmp_path / ".kb"), embedding_model=None)
    ks.add("部署规范", "本项目的服务用 docker compose 部署。")
    client, _, _, bodies = _make_app(tmp_path, knowledge_store=ks,
                                     knowledge_inject_enabled=False)
    r = client.post("/dispatch", json={"prompt": "帮我部署服务", "role": "executor"})
    assert r.status_code == 200
    assert all("知识库参考" not in (b.get("context_memory") or "") for b in bodies)

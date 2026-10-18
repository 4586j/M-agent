import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workspace" / "agent" / "orchestrator" / "runtime"))
from knowledge_store import KnowledgeStore

import agent_core.service as personal
from agent_core.config import load_agent_config
from agent_core.kernel import AgentEvent, KernelResult
from agent_core.understanding_service import UnderstandingService

DOC = ("项目采用 SQLite 独立保存核验记录。\n执行过程先完成核验再派发。\n"
       "项目中的出题器不使用执行工具与记忆。\n验收时核对输出及独立记录。")


def generated(sources, **kwargs):
    questions = []
    for i, category in enumerate(("requirement", "project", "process", "project", "process")):
        source = sources[0] if category == "requirement" else sources[-1]
        quote = source["content"][:100] if i == 0 else source["content"].splitlines()[i-1]
        questions.append({"question": f"核验题 {i + 1}：哪些内容符合本次{category}资料？",
                          "category": category, "critical": i < 2,
                          "options": [quote, "忽略资料", "无须验收", "跳过核验"],
                          "correct": 0, "source_id": source["id"], "quote": quote,
                          "explanation": "依据引用原文进行判断"})
    return {"raw": json.dumps({"questions": questions}), "cost_usd": 0.001,
            "usage": {"input_tokens": 100}, "model": "fake-model"}


class CaptureKernel:
    def __init__(self):
        self.calls = []
    def run_oneshot(self, prompt, persona, cwd, context_memory="", *args, **kwargs):
        self.calls.append({"prompt":prompt,"context_memory":context_memory})
        return KernelResult(output="ok",turns=1,cost_usd=0.001)

    async def stream(self, prompt, persona, cwd, context_memory="", **kwargs):
        self.calls.append({"prompt":prompt,"context_memory":context_memory})
        yield AgentEvent("result",{"result":"ok","num_turns":1,"cost_usd":0.001})


@pytest.fixture
def setup_gate(tmp_path, monkeypatch, project_root):
    cfg = load_agent_config(project_root=project_root,work_dir=str(tmp_path/"work"),rate_limit_per_minute=0)
    ws=tmp_path/"workspace";ws.mkdir()
    monkeypatch.setattr(personal,"get_config",lambda:cfg)
    monkeypatch.setattr(personal,"_WORKSPACE",{"path":ws})
    monkeypatch.setattr(personal,"_HISTORY_STORE",None)
    monkeypatch.setattr(personal,"_ASYNC_RUNS",{})
    dispatcher=CaptureKernel()
    monkeypatch.setattr(personal,"get_kernel",lambda:dispatcher)
    app=personal.create_app(understanding_generator=generated)
    service=app.state.personal_understanding()
    doc=service.knowledge.add("项目实现与验收",DOC)
    with TestClient(app) as client:
        yield client,doc["doc_id"],dispatcher,service.knowledge,None,service.path.parent


def start(client, doc_id, **changes):
    body = {"prompt": "实现数据导出并验收", **changes}
    response = client.post("/agent/understanding/checks", json={"dispatch": body, "document_ids": [doc_id]})
    assert response.status_code == 200, response.text
    return body, response.json()


def submit(client, check, wrong=()):
    initial = check
    for i, question in enumerate(check["questions"]):
        quote = check["requirement"][:100] if question["category"] == "requirement" else DOC.splitlines()[i-1]
        correct = question["options"].index(quote)
        response = client.post(f"/agent/understanding/checks/{check['id']}/question-answer", json={
            "question_id": question["id"], "answer": (correct+1)%4 if i in wrong else correct,
            "revision": check["progress"]["revision"]})
        if response.status_code != 200:
            return response
        check = response.json()
        if i < len(initial["questions"])-1:
            response = client.post(f"/agent/understanding/checks/{check['id']}/navigate", json={
                "question_id": question["id"], "action": "next", "revision": check["progress"]["revision"]})
            if response.status_code != 200:
                return response
            check = response.json()
    return client.post(f"/agent/understanding/checks/{check['id']}/navigate", json={
        "question_id": check["progress"]["current_question_id"], "action": "finish", "revision": check["progress"]["revision"]})


def test_pending_hides_keys_and_raw_output(setup_gate):
    client, doc_id, *_ = setup_gate
    _, check = start(client, doc_id)
    assert check["state"] == "pending"
    assert "raw_output" not in check
    assert all("correct" not in q and "quote" not in q for q in check["questions"])
    assert client.get(f"/agent/understanding/checks/{check['id']}/audit").status_code == 409
    assert client.get(f"/agent/understanding/checks/{check['id']}", headers={"X-Operator": "other"}).status_code == 404


@pytest.mark.parametrize("wrong,passed,score", [((), True, 100), ((4,), True, 80),
                                                ((0,), False, 80), ((3, 4), False, 60)])
def test_threshold_and_critical_are_server_rules(setup_gate, wrong, passed, score):
    client, doc_id, *_ = setup_gate
    _, check = start(client, doc_id)
    result = submit(client, check, wrong).json()["result"]
    assert result["passed"] is passed
    assert result["score_percent"] == score
    assert submit(client, check).status_code == 409


def test_gate_rejects_missing_proof_force_and_changed_subject(setup_gate):
    client, doc_id, dispatcher, *_ = setup_gate
    body, check = start(client, doc_id)
    assert client.post("/agent/run", json={**body, "force": True}).status_code == 428
    assert client.post("/agent/run", json={**body, "understanding_check_id": check["id"]}).status_code == 428
    assert submit(client, check).status_code == 200
    for changed in ({"prompt": "另一个需求"}, {"context_memory": "额外修改"}, {"model": "new-model"}):
        assert client.post("/agent/run", json={**body, **changed, "understanding_check_id": check["id"]}).status_code == 428
    assert not dispatcher.calls
    response = client.post("/agent/run", json={**body, "understanding_check_id": check["id"]})
    assert response.status_code == 200, response.text
    assert len(dispatcher.calls) == 1
    assert "核验题" not in (dispatcher.calls[0].get("context_memory") or "")
    audit = client.get(f"/agent/understanding/checks/{check['id']}/audit").json()
    assert audit["task_ids"] == [response.json()["run_id"]]
    assert audit["sources"][-1]["content"] == DOC
    assert audit["model"] == "fake-model" and audit["result"]["passed"]


def test_answers_cannot_be_forged_rewritten_or_concurrently_regraded(setup_gate):
    from concurrent.futures import ThreadPoolExecutor
    client, doc_id, *_ = setup_gate
    _, check = start(client, doc_id)
    assert client.post(f"/agent/understanding/checks/{check['id']}/answers",
                       json={"answers":{"Q1":True}}).status_code == 422
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: submit(client, check), range(2)))
    assert sorted(r.status_code for r in responses) == [200,409]


def test_changed_project_document_invalidates_pass_but_retains_audit(setup_gate):
    client, doc_id, _, knowledge, *_ = setup_gate
    body, check = start(client, doc_id)
    submit(client, check)
    knowledge.update(doc_id, content="项目现在采用新的实现与验收流程")
    assert client.post("/agent/run", json={**body, "understanding_check_id": check["id"]}).status_code == 428
    audit = client.get(f"/agent/understanding/checks/{check['id']}/audit").json()
    assert audit["valid_now"] is False and audit["sources"][-1]["content"] == DOC
    assert client.get("/agent/understanding/checks").json()["checks"][0]["id"] == check["id"]


def test_corrupt_project_document_does_not_destroy_historical_audit(setup_gate):
    client, doc_id, _, knowledge, *_ = setup_gate
    body, check = start(client, doc_id)
    submit(client, check)
    knowledge._path(doc_id).write_text("not JSON", encoding="utf-8")
    audit = client.get(f"/agent/understanding/checks/{check['id']}/audit")
    assert audit.status_code == 200 and not audit.json()["valid_now"]
    assert audit.json()["sources"][-1]["content"] == DOC
    assert client.post("/agent/run", json={**body, "understanding_check_id":check["id"]}).status_code == 428


def test_records_survive_restart_and_cannot_be_rewritten(setup_gate):
    client, doc_id, _, knowledge, _, root = setup_gate
    _, check = start(client, doc_id)
    submit(client, check)
    service = UnderstandingService(root / "records.sqlite3", knowledge, None)
    assert service.get(check["id"], "testclient")["result"]["passed"]
    with service.connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE checks SET actor='forged'")
    with service.connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT OR REPLACE INTO checks SELECT * FROM checks")


def test_expiration_and_unsupported_model_answers(tmp_path):
    from budget_store import BudgetStore
    knowledge = KnowledgeStore(str(tmp_path / "knowledge"))
    doc_id = knowledge.add("资料", DOC)["doc_id"]
    now = [1000]
    service = UnderstandingService(tmp_path / "checks.sqlite3", knowledge,
        BudgetStore(tmp_path / "budget.json"), generated, clock=lambda: now[0])
    from agent_core.service import RunReq as DispatchReq
    body = DispatchReq(prompt="实现导出").model_dump()
    check = service.start(body, [doc_id], "u")
    for q in check["questions"]:
        check = service.answer_question(check["id"], q["id"], q["options"].index(
            "实现导出" if q["category"] == "requirement" else DOC.splitlines()[int(q["id"][1:])-2]),
            check["progress"]["revision"], "u")
        if q["id"] != "Q5":
            check = service.navigate(check["id"], q["id"], "next", None, check["progress"]["revision"], "u")
    service.navigate(check["id"], "Q5", "finish", None, check["progress"]["revision"], "u")
    now[0] += 3600
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        service.authorize(check["id"], body, "u")
    def hallucinated(sources, **kwargs):
        data = generated(sources, **kwargs)
        questions = json.loads(data["raw"])
        questions["questions"][1]["quote"] = "资料中不存在的实现"
        data["raw"] = json.dumps(questions)
        return data
    service.generator = hallucinated
    check = service.start(body, [doc_id], "u")
    assert check["state"] == "failed" and check["questions"] == []


def test_model_interruption_keeps_request_snapshot_and_unknown_cost_hold(tmp_path):
    from budget_store import BudgetStore

    from agent_core.service import RunReq as DispatchReq
    class Crash(BaseException):
        pass
    def interrupted(*args, **kwargs):
        raise Crash()
    knowledge = KnowledgeStore(str(tmp_path / "knowledge"))
    doc_id = knowledge.add("资料", DOC)["doc_id"]
    budget = BudgetStore(tmp_path / "budget.json")
    now = [1000]
    service = UnderstandingService(tmp_path / "audit.sqlite3", knowledge, budget,
                                    interrupted, clock=lambda:now[0])
    with pytest.raises(Crash):
        service.start(DispatchReq(prompt="需求").model_dump(),[doc_id],"u")
    now[0] += 121
    record = service.history("u",20,0)[0]
    assert record["state"] == "failed" and record["result"] is None
    audit = service.get(record["id"],"u",audit=True)
    assert audit["sources"][-1]["content"] == DOC
    assert budget.snapshot()["reserved_usd"] == 0.1


def test_answer_may_quote_a_contiguous_part_but_must_not_paraphrase(tmp_path):
    sources = [{"id":"requirement","content":"实现导出"},
               {"id":"doc:example","content":DOC}]
    service = object.__new__(UnderstandingService)
    data = json.loads(generated(sources)["raw"])
    quote = data["questions"][1]["quote"]
    data["questions"][1]["options"][0] = "SQLite"
    questions = service._questions(json.dumps(data),sources)
    assert questions[1]["options"][questions[1]["correct"]] == "SQLite"
    assert questions[1]["quote"] == quote
    data["questions"][1]["options"][0] = "Oracle 数据库"
    with pytest.raises(ValueError,match="unsupported answer"):
        service._questions(json.dumps(data),sources)


def test_reported_over_budget_cost_is_recorded_but_never_grants_access(tmp_path):
    from budget_store import BudgetStore

    from agent_core.service import RunReq as DispatchReq
    knowledge = KnowledgeStore(str(tmp_path / "knowledge"))
    doc_id = knowledge.add("资料",DOC)["doc_id"]
    budget = BudgetStore(tmp_path / "budget.json")
    def over_budget(sources, **kwargs):
        return {**generated(sources),"cost_usd":0.2}
    service = UnderstandingService(tmp_path / "audit.sqlite3",knowledge,budget,over_budget,max_cost_usd=0.1)
    record = service.start(DispatchReq(prompt="实现导出").model_dump(),[doc_id],"u")
    assert record["state"] == "failed" and record["cost_usd"] == 0.2
    assert record["model"] == "fake-model"
    assert budget.snapshot()["spent_total_usd"] == 0.2


def test_sequential_answer_stays_current_and_skip_requires_revisit(setup_gate):
    client, doc_id, dispatcher, *_ = setup_gate
    body, check = start(client, doc_id)
    base = f"/agent/understanding/checks/{check['id']}"
    assert client.post(base+"/answers", json={"answers": {}}).status_code == 409
    assert client.post(base+"/navigate", json={"question_id":"Q1", "revision":0,"action":"next"}).status_code == 409
    assert client.post(base+"/question-answer", json={"question_id":"Q2","revision":0,"answer":0}).status_code == 409
    check = client.post(base+"/navigate", json={"question_id":"Q1","revision":0,"action":"skip"}).json()
    assert check["progress"]["current_question_id"] == "Q2"
    assert check["progress"]["skipped_question_ids"] == ["Q1"]
    # A stale browser cannot submit an answer after navigation.
    assert client.post(base+"/question-answer", json={"question_id":"Q1","revision":0,"answer":0}).status_code == 409
    for i in range(1,5):
        q = check["questions"][i]
        check = client.post(base+"/question-answer", json={"question_id":q["id"],"revision":check["progress"]["revision"],
            "answer":q["options"].index(DOC.splitlines()[i-1])}).json()
        assert check["progress"]["current_question_id"] == q["id"]
        feedback = check["progress"]["outcomes"][-1]
        assert all(feedback[k] for k in ("summary","explanation","quote","knowledge_points","source_hash"))
        assert check["result"] is None
        if i < 4:
            check = client.post(base+"/navigate", json={"question_id":q["id"],"revision":check["progress"]["revision"],"action":"next"}).json()
    assert client.post(base+"/navigate", json={"question_id":"Q5","revision":check["progress"]["revision"],"action":"finish"}).status_code == 409
    audit = client.get(base+"/audit").json()
    assert audit["partial"] and "raw_output" not in audit
    assert "correct" not in audit["questions"][0]
    assert len(audit["learning_events"]) == check["progress"]["revision"]
    assert client.post("/agent/run",json={**body,"understanding_check_id":check["id"]}).status_code == 428
    check = client.post(base+"/navigate", json={"question_id":"Q5","revision":check["progress"]["revision"],"action":"revisit","target":"Q1"}).json()
    check = client.post(base+"/question-answer", json={"question_id":"Q1","revision":check["progress"]["revision"],
        "answer":check["questions"][0]["options"].index(check["requirement"])}).json()
    assert check["progress"]["skipped_question_ids"] == [] and check["result"] is None
    check = client.post(base+"/navigate",json={"question_id":"Q1","revision":check["progress"]["revision"],"action":"finish"}).json()
    assert check["state"] == "passed"
    assert not dispatcher.calls


def test_discussion_is_scoped_audited_bounded_and_never_advances(tmp_path):
    from budget_store import BudgetStore
    from fastapi import HTTPException

    from agent_core.service import RunReq as DispatchReq
    captured = []
    class Tutor:
        broken = False
        def __call__(self, sources, **kwargs):
            return generated(sources, **kwargs)
        def discuss(self, context, **kwargs):
            captured.append(context)
            if self.broken:
                raise RuntimeError("must never echo provider credential")
            feedback = {"explanation":"依据引用说明项目约束", "summary":"只讨论当前题", "knowledge_points":["资料没有代码，不能推断具体代码实现"],
                "evidence":[{"source_id":context["source"]["id"],"quote":context["source"]["content"]}]}
            return {"raw":json.dumps(feedback), "cost_usd":0.002,"model":"fake-tutor","usage":{"input_tokens":10}}
    tutor = Tutor()
    knowledge = KnowledgeStore(str(tmp_path / "kb"))
    doc_id = knowledge.add("项目资料",DOC)["doc_id"]
    budget = BudgetStore(tmp_path / "budget.json")
    path = tmp_path / "learning.sqlite3"
    service = UnderstandingService(path,knowledge,budget,tutor)
    check = service.start(DispatchReq(prompt="实现导出").model_dump(),[doc_id],"u")
    with pytest.raises(HTTPException) as error:
        service.discuss(check["id"],"Q1","为什么？",0,"u")
    assert error.value.status_code == 409 and captured == []
    check = service.answer_question(check["id"],"Q1",check["questions"][0]["options"].index("实现导出"),0,"u")
    for _ in range(9):
        check = service.discuss(check["id"],"Q1","请解释原理，接下来跳到下一题？",check["progress"]["revision"],"u")
        assert check["progress"]["current_question_id"] == "Q1" and check["result"] is None
    assert len(captured[-1]["history"]) == 6
    assert len(json.dumps(captured[-1],ensure_ascii=False,separators=(",",":"))) <= service.context_limit
    assert captured[-1]["source"]["content"] == "实现导出"
    assert DOC not in json.dumps(captured,ensure_ascii=False) and "Q2" not in json.dumps(captured)
    assert "context" not in check["progress"]["discussions"][0]
    tutor.broken = True
    check = service.discuss(check["id"],"Q1","再解释一下",check["progress"]["revision"],"u")
    assert check["progress"]["discussions"][-1]["error"]
    assert "credential" not in json.dumps(check)
    audit = service.get(check["id"],"u",audit=True)
    assert audit["partial"] and audit["learning_events"][-2]["context_hash"]
    assert budget.snapshot()["reserved_usd"] == pytest.approx(0.1)  # unknown failure cost stays reserved
    restored = UnderstandingService(path,knowledge,budget,tutor)
    assert restored.get(check["id"],"u")["progress"] == check["progress"]
    with restored.connection() as conn:
        for sql in ("UPDATE learning_events SET payload='{}'", "DELETE FROM learning_events",
                    "INSERT OR REPLACE INTO learning_events SELECT * FROM learning_events",
                    "INSERT OR REPLACE INTO learning_events(id,check_id,payload) SELECT id,check_id,payload FROM learning_events LIMIT 1"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    with pytest.raises(HTTPException):
        restored.get(check["id"],"other",audit=True)


def test_tutor_evidence_must_match_current_question_source():
    from agent_core.understanding_learning import QuestionLearning
    source = {"id":"doc:one","title":"原文","hash":"v1","content":"项目采用 SQLite"}
    reply = {"explanation":"说明","summary":"总结","knowledge_points":["知识点"],
             "evidence":[{"source_id":"doc:one","quote":"SQLite"}]}
    assert QuestionLearning._discussion_feedback(json.dumps(reply),source)["evidence"][0]["source_hash"] == "v1"
    for evidence in ([{"source_id":"doc:two","quote":"SQLite"}], [{"source_id":"doc:one","quote":"Redis"}], []):
        with pytest.raises(ValueError):
            QuestionLearning._discussion_feedback(json.dumps({**reply,"evidence":evidence}),source)


@pytest.mark.parametrize("severity,valid_evidence,triggered", [("normal",True,False), ("serious",True,True), ("serious",False,False)])
def test_conditional_gate_only_triggers_for_request_or_evidenced_severe_deviation(tmp_path, severity, valid_evidence, triggered):
    from budget_store import BudgetStore
    from fastapi import HTTPException

    from agent_core.service import RunReq as DispatchReq
    class Assessor:
        def __call__(self, sources, **kwargs):
            return generated(sources)
        def assess(self, context, **kwargs):
            sources = context["sources"]
            return {"raw":json.dumps({"severity":severity,"reason":"资料与分析存在关键实现冲突" if severity == "serious" else "一般疑问，不属于严重偏差",
                "evidence":[{"source_id":sources[0]["id"],"quote":sources[0]["content"]},
                            {"source_id":sources[-1]["id"],"quote":sources[-1]["content"].splitlines()[0] if valid_evidence else "无依据猜测"}]}),
                "cost_usd":0.001,"model":"fake-assessor"}
    knowledge = KnowledgeStore(str(tmp_path / "kb"))
    doc = knowledge.add("项目",DOC)["doc_id"]
    service = UnderstandingService(tmp_path / "audit.sqlite3", knowledge,BudgetStore(tmp_path / "budget.json"),
                                   Assessor(),required=True,trigger_mode="conditional")
    body = DispatchReq(prompt="项目改用 Redis").model_dump()
    assert service.trigger(body,"u")["required"] is False
    result = service.assess(body,[doc],"u")
    assert service.trigger(body,"u")["required"] is triggered
    assert service.trigger(body,"other")["required"] is False
    assert result["severity"] == (severity if valid_evidence else "failed")
    audit = service.assessment_audit(result["id"],"u")
    assert audit["context"]["sources"][-1]["content"] == DOC and audit["raw_output"]
    with pytest.raises(HTTPException):
        service.assessment_audit(result["id"],"other")
    service.start(body,[doc],"u")
    assert service.trigger(body,"u")["required"] is True
    assert service.trigger({**body,"model":"another-model"},"u")["required"] is True
    assert service.trigger({**body,"prompt":"另一个需求"},"u")["required"] is False


def test_corrections_are_inert_until_verified_and_bound_to_document_version(tmp_path):
    from budget_store import BudgetStore
    from fastapi import HTTPException

    from agent_core.service import RunReq as DispatchReq
    knowledge = KnowledgeStore(str(tmp_path / "kb"))
    doc = knowledge.add("实现资料",DOC)["doc_id"]
    service = UnderstandingService(tmp_path / "audit.sqlite3",knowledge,BudgetStore(tmp_path / "budget.json"),generated)
    body = DispatchReq(prompt="实现导出").model_dump()
    check = service.start(body,[doc],"u")
    check = service.answer_question(check["id"],"Q1",0,0,"u")
    check = service.propose_correction(check["id"],"Q1","AI 误解了存储位置，应依据独立存储要求",doc,
        DOC.splitlines()[0],check["progress"]["revision"],"u")
    assert service.verified_references(service._sources(body,[doc]),"u") == []
    proposed = check["progress"]["corrections"][0]
    check = service.verify_correction(check["id"],"Q1",proposed["correction_id"],check["progress"]["revision"],"u")
    references = service.verified_references(service._sources(body,[doc]),"u")
    assert len(references) == 1 and references[0]["claim"] == proposed["claim"]
    assert check["progress"]["current_question_id"] == "Q1" and check["result"] is None
    assert service.verified_references(service._sources(body,[doc]),"other") == []
    with pytest.raises(HTTPException):
        service.verify_correction(check["id"],"Q1",proposed["correction_id"],check["progress"]["revision"],"u")
    knowledge.update(doc,content=DOC+"\n新版本")
    assert service.verified_references(service._sources(body,[doc]),"u") == []




@pytest.mark.parametrize("path",["/agent/run","/agent/run/stream","/agent/run/async"])
def test_all_personal_execution_transports_respect_opt_in_gate(setup_gate,path):
    client,doc_id,kernel,*_=setup_gate
    body={"prompt":"实现导出"}
    assert client.post("/agent/understanding/trigger",json={"dispatch":body}).json()["required"] is False
    _,check=start(client,doc_id,**body)
    assert client.post(path,json=body).status_code==428
    assert kernel.calls==[]
    assert submit(client,check).json()["result"]["passed"]
    response=client.post(path,json={**body,"understanding_check_id":check["id"]})
    assert response.status_code in (200,202),response.text
    if response.status_code==202:
        import time
        for _ in range(100):
            if client.get('/agent/run/'+response.json()['run_id']).json()['status']!='running':
                break
            time.sleep(0.01)
    assert kernel.calls and kernel.calls[0]["prompt"]==body["prompt"]
    assert "核验题" not in kernel.calls[0]["context_memory"]
    assert len(client.get(f"/agent/understanding/checks/{check['id']}/audit").json()["task_ids"])==1


@pytest.mark.parametrize("suffix",["/message","/message/stream"])
def test_live_personal_session_messages_cannot_bypass_gate(setup_gate,monkeypatch,suffix):
    from agent_core.tests.test_service import FakeKernel, _default_events
    client,doc_id,*_=setup_gate
    kernel=FakeKernel(events=_default_events())
    monkeypatch.setattr(personal,"get_kernel",lambda:kernel)
    response=client.post("/agent/session",json={})
    assert response.status_code==200,response.text
    session=response.json();sid=session["session_id"]
    body={"prompt":"实现导出","persona":"executor","cwd":session["cwd"],"session_id":sid}
    _,check=start(client,doc_id,**body)
    assert client.post(f"/agent/session/{sid}"+suffix,json={"prompt":body["prompt"]}).status_code==428
    assert kernel.sessions[0].send_calls==[]
    submit(client,check)
    response=client.post(f"/agent/session/{sid}"+suffix,json={"prompt":body["prompt"],"understanding_check_id":check["id"]})
    assert response.status_code==200,response.text
    assert kernel.sessions[0].send_calls[0][0]==body["prompt"]


def test_workspace_change_isolates_learning_documents_and_audit(setup_gate,tmp_path):
    client,doc_id,*_=setup_gate
    body,check=start(client,doc_id)
    assert client.get("/agent/understanding/documents").json()["docs"]
    second=tmp_path/"another-project";second.mkdir()
    assert client.put("/agent/workspace",json={"path":str(second)}).status_code==200
    assert client.get("/agent/understanding/documents").json()["docs"]==[]
    assert client.get(f"/agent/understanding/checks/{check['id']}").status_code==404
    assert not client.post("/agent/understanding/trigger",json={"dispatch":body}).json()["required"]
    assert client.post("/agent/run",json={**body,"understanding_check_id":check["id"]}).status_code==404

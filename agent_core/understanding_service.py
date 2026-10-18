"""Independent, evidence-bound human comprehension gate and append-only audit."""
import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictInt

from agent_core.understanding_learning import QuestionLearning


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(packed(value).encode("utf-8")).hexdigest()


def binding(body):
    return {k: v for k, v in body.items() if k not in ("understanding_check_id", "force")}


def actor(request):
    # Same operator model as the current shared-key control plane; not a login identity.
    return (request.headers.get("X-Operator") or
            (request.client.host if request.client else "unknown"))[:128]


class UnderstandingService(QuestionLearning):
    threshold = 80
    question_count = 5
    ttl = 3600
    context_limit = 14000

    def __init__(self, db_path, knowledge, budgets, generator=None, *, required=False,
                 max_cost_usd=0.1, clock=time.time, trigger_mode="always"):
        if not isinstance(max_cost_usd, (int, float)) or not math.isfinite(max_cost_usd) or max_cost_usd <= 0:
            raise ValueError("Invalid understanding model budget")
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.knowledge, self.budgets, self.generator = knowledge, budgets, generator
        self.required, self.max_cost, self.clock = required, max_cost_usd, clock
        if trigger_mode not in ("always", "conditional"):
            raise ValueError("understanding trigger mode must be always or conditional")
        self.trigger_mode = trigger_mode
        self.slots = threading.BoundedSemaphore(2)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS generations (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, created REAL NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS checks (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, created REAL NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS answers (
                    check_id TEXT PRIMARY KEY REFERENCES checks(id), payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_links (
                    task_id TEXT PRIMARY KEY, check_id TEXT NOT NULL REFERENCES checks(id),
                    created REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS checks_actor_created ON checks(actor, created);
                CREATE TABLE IF NOT EXISTS learning_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    check_id TEXT NOT NULL REFERENCES checks(id), payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS learning_check ON learning_events(check_id, seq);
                CREATE TABLE IF NOT EXISTS learning_triggers (
                    id TEXT PRIMARY KEY, actor TEXT NOT NULL, created REAL NOT NULL, payload TEXT NOT NULL);
            """)
            for table in ("generations", "checks", "answers", "task_links", "learning_events", "learning_triggers"):
                for action in ("UPDATE", "DELETE"):
                    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
                        BEFORE {action} ON {table} BEGIN
                        SELECT RAISE(ABORT, 'understanding records are append-only'); END""")
                key = {"generations": "id", "checks": "id", "answers": "check_id",
                       "task_links": "task_id", "learning_events": "seq", "learning_triggers": "id"}[table]
                conn.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table}
                    WHEN EXISTS (SELECT 1 FROM {table} WHERE {key}=NEW.{key})
                    BEGIN SELECT RAISE(ABORT, 'understanding records are append-only'); END""")
            conn.execute("""CREATE TRIGGER IF NOT EXISTS learning_events_no_replace_id
                BEFORE INSERT ON learning_events WHEN EXISTS (
                SELECT 1 FROM learning_events WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT, 'understanding records are append-only'); END""")

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def policy(self):
        return {"required": self.required, "available": self.generator is not None,
                "threshold_percent": self.threshold, "question_count": self.question_count,
                "ttl_seconds": self.ttl, "context_max_chars": self.context_limit,
                "max_cost_usd": self.max_cost, "trigger_mode": self.trigger_mode,
                "assessment_available": callable(getattr(self.generator, "assess", None)),
                "discussion_available": callable(getattr(self.generator, "discuss", None)),
                "discussion_context_max_chars": self.context_limit}

    def _sources(self, body, document_ids):
        try:
            packed(binding(body)).encode("utf-8")
        except UnicodeError as error:
            raise HTTPException(400, "核验需求和参数必须是有效 Unicode 文本") from error
        if not body["prompt"].strip() or len(body["prompt"]) > 4000:
            raise HTTPException(400, "核验需求须为 1–4000 字符")
        if not 1 <= len(document_ids) <= 3 or len(set(document_ids)) != len(document_ids):
            raise HTTPException(400, "请选择 1–3 份项目实现及流程资料")
        sources = [{"id": "requirement", "title": "本次需求", "content": body["prompt"],
                    "hash": digest(body["prompt"])}]
        context = body.get("context_memory") or ""
        if context:
            sources.append({"id": "context", "title": "显式上下文", "content": context,
                            "hash": digest(context)})
        for doc_id in document_ids:
            try:
                doc = self.knowledge.get(doc_id, full=True)
                if doc and (not isinstance(doc, dict) or not isinstance(doc.get("title"), str)
                            or not isinstance(doc.get("content"), str) or not doc["content"].strip()):
                    raise ValueError("invalid document")
            except (KeyError, TypeError, ValueError, OSError) as error:
                raise HTTPException(409, "项目资料损坏或为空，请先修复资料") from error
            if not doc:
                raise HTTPException(409, "项目资料不存在，请重新选择")
            sources.append({"id": "doc:" + doc_id, "title": doc["title"],
                            "content": doc["content"][:3000],
                            "hash": digest({"title": doc["title"], "content": doc["content"]})})
        if len(packed(sources)) > self.context_limit:
            raise HTTPException(400, "核验资料超过独立上下文限额，请精简需求、上下文或选用较短资料")
        return sources

    def _questions(self, raw, sources):
        if not isinstance(raw, str) or len(raw) > 32000:
            raise ValueError("invalid model output")
        text = raw.strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        data = json.loads(text)
        questions = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(questions, list) or len(questions) != self.question_count:
            raise ValueError("insufficient grounded questions")
        indexed = {s["id"]: s for s in sources}
        result = []
        for index, q in enumerate(questions):
            if not isinstance(q, dict):
                raise ValueError("invalid question")
            for name, limit in (("question", 500), ("quote", 240), ("explanation", 800)):
                if not isinstance(q.get(name), str) or not q[name].strip() or len(q[name]) > limit:
                    raise ValueError("invalid question text")
                q[name].encode("utf-8")
            category, options, correct = q.get("category"), q.get("options"), q.get("correct")
            if category not in ("requirement", "project", "process") or type(q.get("critical")) is not bool:
                raise ValueError("invalid question category")
            if (not isinstance(options, list) or len(options) != 4
                    or any(not isinstance(o, str) or not o.strip() or len(o) > 240 for o in options)
                    or len(set(options)) != 4 or type(correct) is not int or not 0 <= correct < 4):
                raise ValueError("invalid choices")
            source = indexed.get(q.get("source_id"))
            if (source is None or q["quote"] not in source["content"]
                    or options[correct] not in q["quote"]
                    or (category == "requirement" and source["id"] != "requirement")
                    or (category != "requirement" and not source["id"].startswith("doc:"))):
                raise ValueError("unsupported answer")
            result.append({**{k: q[k] for k in ("question", "category", "critical", "options",
                                               "correct", "source_id", "quote", "explanation")},
                           "id": f"Q{index + 1}"})
            shuffled = list(options)
            secrets.SystemRandom().shuffle(shuffled)
            result[-1]["options"] = shuffled
            result[-1]["correct"] = shuffled.index(options[correct])
            if "summary" in q:
                if not isinstance(q["summary"], str) or not q["summary"].strip() or len(q["summary"]) > 800:
                    raise ValueError("invalid summary")
                result[-1]["summary"] = q["summary"]
                q["summary"].encode("utf-8")
            if "knowledge_points" in q:
                points = q["knowledge_points"]
                if (not isinstance(points, list) or not 1 <= len(points) <= 5 or
                        any(not isinstance(p, str) or not p.strip() or len(p) > 1200 for p in points)):
                    raise ValueError("invalid knowledge points")
                result[-1]["knowledge_points"] = points
                for point in points:
                    point.encode("utf-8")
        if ({q["category"] for q in result} != {"requirement", "project", "process"}
                or not {"requirement", "project"}.issubset(
                    {q["category"] for q in result if q["critical"]})
                or len({q["question"] for q in result}) != len(result)):
            raise ValueError("coverage missing")
        if len({q["quote"].strip().casefold() for q in result}) != len(result):
            raise ValueError("repeated evidence")
        return result

    def start(self, body, document_ids, who):
        if self.generator is None:
            raise HTTPException(503, "独立出题模型未配置，不能自动放行")
        sources = self._sources(body, document_ids)
        if not self.slots.acquire(blocking=False):
            raise HTTPException(429, "核验出题繁忙，请稍后重试")
        check_id = "uc-" + uuid.uuid4().hex
        try:
            self.budgets.reserve(check_id, self.max_cost)
            record = {"id": check_id, "actor": who, "created_at": self.clock(),
                      "expires_at": self.clock() + self.ttl, "binding": binding(body),
                      "binding_hash": digest(binding(body)), "sources": sources,
                      "threshold_percent": self.threshold, "questions": [], "state": "failed",
                      "model": None, "usage": {}, "cost_usd": None,
                      "error": "资料不足、出题失败或证据校验失败，请核查资料后重新发起"}
            record.update({"schema_version": 2, "scoring_version": 1,
                           "trigger_reason": "model_serious_deviation" if self.trigger(body, who)["model_triggered"] else "user_requested",
                           "context_max_chars": self.context_limit, "max_cost_usd": self.max_cost,
                           "input_context_hash": digest(sources)})
            with self.connection() as conn:
                conn.execute("INSERT INTO generations VALUES (?, ?, ?, ?)",
                    (check_id, who, record["created_at"], packed({**record, "state": "generating"})))
            try:
                phase = "model_call"
                generated = self.generator(sources, max_cost_usd=self.max_cost)
                record["raw_output"] = str(generated.get("raw") or "")[:32000]
                record["generation_system_prompt"] = str(generated.get("system_prompt") or "")[:4000]
                record["generator_version"] = generated.get("generator_version", 1)
                record["sdk_result_subtype"] = generated.get("sdk_result_subtype")
                record["sdk_session_id"] = generated.get("sdk_session_id")
                record["usage"] = generated.get("usage") or {}
                record["model"] = generated.get("model")
                cost = generated.get("cost_usd")
                phase = "cost_accounting"
                if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                    self.budgets.settle(check_id, cost)
                    record["cost_usd"] = cost
                    if cost > self.max_cost:
                        record["error"] = "SDK 报告的出题估算费用超出本轮预算，未放行；请核查模型和预算配置"
                        raise ValueError("model cost exceeded check budget")
                # Unknown model cost retains its reservation for explicit reconciliation.
                phase = "model_result"
                if generated.get("is_error"):
                    if generated.get("sdk_result_subtype") == "error_max_budget_usd":
                        record["error"] = "独立出题达到 SDK 预算上限，未放行；请核查费用和模型配置后重试"
                    raise ValueError("model error")
                phase = "evidence_validation"
                record["questions"] = self._questions(generated.get("raw"), sources)
                record["state"], record["error"] = "pending", None
            except Exception as error:  # Only safe class/phase labels, never exception text or credentials.
                record["failure_type"] = type(error).__name__
                record["failure_phase"] = phase
            with self.connection() as conn:
                conn.execute("INSERT INTO checks VALUES (?, ?, ?, ?)",
                             (check_id, who, record["created_at"], packed(record)))
            return self.get(check_id, who)
        finally:
            self.slots.release()

    def _read(self, conn, check_id, who):
        row = conn.execute("SELECT payload FROM checks WHERE id=? AND actor=?", (check_id, who)).fetchone()
        if row is None:
            row = conn.execute("SELECT payload FROM generations WHERE id=? AND actor=?", (check_id, who)).fetchone()
        if row is None:
            raise HTTPException(404, "核验记录不存在")
        record = json.loads(row[0])
        if record["state"] == "generating" and self.clock() > record["created_at"] + 120:
            record["state"] = "failed"
            record["error"] = "出题未完成或服务曾中断，费用预占待核对；可重新发起核验"
        return record

    def _current(self, record):
        if self.clock() >= record["expires_at"]:
            return False
        for source in record["sources"]:
            if source["id"].startswith("doc:"):
                try:
                    doc = self.knowledge.get(source["id"][4:], full=True)
                    matches = doc and digest({"title": doc["title"], "content": doc["content"]}) == source["hash"]
                except (KeyError, TypeError, ValueError, OSError):
                    matches = False
                if not matches:
                    return False
        return True

    def get(self, check_id, who, *, audit=False):
        with self.connection() as conn:
            record = self._read(conn, check_id, who)
            answer = conn.execute("SELECT payload FROM answers WHERE check_id=?", (check_id,)).fetchone()
            result = json.loads(answer[0]) if answer else None
            links = [r[0] for r in conn.execute("SELECT task_id FROM task_links WHERE check_id=?", (check_id,))]
            events = self._events(conn, check_id)
            progress = self._learning(record, events)
        if audit:
            if result is None and record["state"] != "failed" and not events:
                raise HTTPException(409, "完成作答后才能导出完整核验依据")
            if result is None and record["state"] != "failed":
                # Partial audit must not disclose unanswered answer keys via raw model output.
                answered = {o["id"] for o in progress["outcomes"]}
                record = {k: v for k, v in record.items() if k != "raw_output"}
                record["questions"] = [q if q["id"] in answered else {k: q[k] for k in
                    ("id", "question", "category", "critical", "options")} for q in record["questions"]]
            return {**record, "result": result, "task_ids": links, "learning_events": events,
                    "progress": progress, "partial": result is None and record["state"] != "failed",
                    "valid_now": self._current(record)}
        questions = [{k: q[k] for k in ("id", "question", "category", "critical", "options")}
                     for q in record["questions"]]
        return {k: record[k] for k in ("id", "created_at", "expires_at", "threshold_percent",
                                       "model", "usage", "cost_usd", "error")} | {
            "state": "passed" if result and result["passed"] else "failed" if result else record["state"],
            "questions": questions, "result": result, "task_ids": links,
            "progress": {**progress, "discussions": [{k: e[k] for k in
                ("kind", "at", "request_id", "question_id", "message", "feedback", "error", "cost_usd", "model")
                if k in e} for e in progress["discussions"]]},
            "valid_now": self._current(record), "requirement": record["binding"]["prompt"],
            "sources": [{k: s[k] for k in ("id", "title", "hash")} for s in record["sources"]]}

    def answer(self, check_id, answers, who):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record = self._read(conn, check_id, who)
            if record.get("schema_version", 1) >= 2:
                raise HTTPException(409, "本轮须逐题提交并由用户决定下一题，不能批量提交")
            if record["state"] != "pending" or not self._current(record):
                raise HTTPException(409, "核验已失效，请重新发起")
            if conn.execute("SELECT 1 FROM answers WHERE check_id=?", (check_id,)).fetchone():
                raise HTTPException(409, "本轮已评分，不可修改答案；可重新发起核验")
            if (not isinstance(answers, dict) or set(answers) != {q["id"] for q in record["questions"]}
                    or any(type(a) is not int or not 0 <= a < 4 for a in answers.values())):
                raise HTTPException(400, "请为每道题选择一个答案")
            outcomes = [{"id": q["id"], "answer": answers[q["id"]],
                         "correct": answers[q["id"]] == q["correct"],
                         "expected": q["correct"], "critical": q["critical"],
                         "source_id": q["source_id"], "quote": q["quote"],
                         "explanation": q["explanation"]} for q in record["questions"]]
            correct = sum(o["correct"] for o in outcomes)
            critical_ok = all(o["correct"] for o in outcomes if o["critical"])
            result = {"answered_at": self.clock(), "correct_count": correct,
                      "question_count": len(outcomes), "score_percent": correct * 100 // len(outcomes),
                      "critical_passed": critical_ok,
                      "passed": correct * 100 >= record["threshold_percent"] * len(outcomes) and critical_ok,
                      "outcomes": outcomes}
            conn.execute("INSERT INTO answers VALUES (?, ?)", (check_id, packed(result)))
        return self.get(check_id, who)

    def authorize(self, check_id, body, who):
        if not check_id:
            raise HTTPException(428, "请先完成需求理解核验：至少 80%，且关键题全部正确")
        view = self.get(check_id, who)
        with self.connection() as conn:
            record = self._read(conn, check_id, who)
        if (view["state"] != "passed" or not view["valid_now"]
                or digest(binding(body)) != record["binding_hash"]
                or record["created_at"] < self.trigger(body, who)["triggered_at"]):
            raise HTTPException(428, "核验未通过、已过期或需求/资料已变化，请重新核验")

    def link(self, check_id, task_id):
        if check_id and task_id:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                old = conn.execute("SELECT check_id FROM task_links WHERE task_id=?", (task_id,)).fetchone()
                if old:
                    if old[0] != check_id:
                        raise ValueError("task already belongs to another understanding record")
                    return
                conn.execute("INSERT INTO task_links VALUES (?, ?, ?)",
                             (task_id, check_id, self.clock()))

    def authorize_task(self, task_id, prompt, who, overrides=None):
        with self.connection() as conn:
            link = conn.execute("SELECT check_id FROM task_links WHERE task_id=?", (task_id,)).fetchone()
            if not link:
                raise HTTPException(428, "该任务没有有效核验，请在派发台核验后重新提交")
            record = self._read(conn, link[0], who)
        self.authorize(link[0], {**record["binding"], "prompt": prompt, **(overrides or {})}, who)
        return link[0]

    def history(self, who, limit, offset):
        with self.connection() as conn:
            ids = [r[0] for r in conn.execute("""SELECT id FROM (
                SELECT id, actor, created FROM generations UNION SELECT id, actor, created FROM checks
                ) WHERE actor=? ORDER BY created DESC LIMIT ? OFFSET ?""", (who, limit, offset))]
        return [self.get(check_id, who) for check_id in ids]


def create_understanding_router(service, dispatch_model, *, prefix="/agent/understanding"):
    class StartReq(BaseModel):
        dispatch: dispatch_model
        document_ids: list[str] = Field(min_length=1, max_length=3)

    class AnswerReq(BaseModel):
        answers: dict[str, StrictInt] = Field(max_length=5)

    class QuestionAnswerReq(BaseModel):
        question_id: str = Field(max_length=10)
        answer: StrictInt = Field(ge=0, le=3)
        revision: StrictInt = Field(ge=0)

    class NavigateReq(BaseModel):
        question_id: str = Field(max_length=10)
        action: str = Field(max_length=16)
        target: str | None = Field(default=None, max_length=10)
        revision: StrictInt = Field(ge=0)

    class DiscussReq(BaseModel):
        question_id: str = Field(max_length=10)
        message: str = Field(min_length=1, max_length=1500)
        revision: StrictInt = Field(ge=0)

    class AssessReq(StartReq):
        analysis: str = Field(default="", max_length=3000)

    class TriggerReq(BaseModel):
        dispatch: dispatch_model

    class CorrectionReq(BaseModel):
        question_id: str = Field(max_length=10)
        claim: str = Field(min_length=1, max_length=1500)
        document_id: str = Field(min_length=1, max_length=200)
        quote: str = Field(min_length=1, max_length=1000)
        revision: StrictInt = Field(ge=0)

    class VerifyReq(BaseModel):
        question_id: str = Field(max_length=10)
        correction_id: str = Field(max_length=100)
        revision: StrictInt = Field(ge=0)

    router = APIRouter(prefix=prefix, tags=["understanding"])

    @router.get("/policy")
    def policy():
        return service.policy()

    @router.post("/trigger")
    def trigger(req: TriggerReq, request: Request):
        return service.trigger(req.dispatch.model_dump(), actor(request))

    @router.post("/assess")
    def assess(req: AssessReq, request: Request):
        operation = service.assess  # Resolve a lazy personal service before importing its ledger error.
        from budget_store import BudgetExceeded
        try:
            return operation(req.dispatch.model_dump(), req.document_ids, actor(request), req.analysis)
        except BudgetExceeded as error:
            raise HTTPException(429, str(error)) from error

    @router.get("/assessments/{assessment_id}/audit")
    def assessment_audit(assessment_id: str, request: Request):
        return service.assessment_audit(assessment_id, actor(request))

    @router.get("/assessments")
    def assessments(request: Request, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)):
        return {"assessments": service.assessment_history(actor(request), limit, offset)}

    @router.post("/checks")
    def start(req: StartReq, request: Request):
        operation = service.start
        from budget_store import BudgetExceeded
        try:
            return operation(req.dispatch.model_dump(), req.document_ids, actor(request))
        except BudgetExceeded as error:
            raise HTTPException(429, str(error)) from error

    @router.post("/checks/{check_id}/answers")
    def answer(check_id: str, req: AnswerReq, request: Request):
        return service.answer(check_id, req.answers, actor(request))

    @router.post("/checks/{check_id}/question-answer")
    def question_answer(check_id: str, req: QuestionAnswerReq, request: Request):
        return service.answer_question(check_id, req.question_id, req.answer, req.revision, actor(request))

    @router.post("/checks/{check_id}/navigate")
    def navigate(check_id: str, req: NavigateReq, request: Request):
        return service.navigate(check_id, req.question_id, req.action, req.target, req.revision, actor(request))

    @router.post("/checks/{check_id}/corrections")
    def correction(check_id: str, req: CorrectionReq, request: Request):
        return service.propose_correction(check_id, req.question_id, req.claim, req.document_id, req.quote,
                                           req.revision, actor(request))

    @router.post("/checks/{check_id}/verify-correction")
    def verify_correction(check_id: str, req: VerifyReq, request: Request):
        return service.verify_correction(check_id, req.question_id, req.correction_id, req.revision, actor(request))

    @router.post("/checks/{check_id}/discussions")
    def discuss(check_id: str, req: DiscussReq, request: Request):
        operation = service.discuss
        from budget_store import BudgetExceeded
        try:
            return operation(check_id, req.question_id, req.message, req.revision, actor(request))
        except BudgetExceeded as error:
            raise HTTPException(429, str(error)) from error

    @router.get("/checks")
    def history(request: Request, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)):
        return {"checks": service.history(actor(request), limit, offset)}

    @router.get("/checks/{check_id}")
    def detail(check_id: str, request: Request):
        return service.get(check_id, actor(request))

    @router.get("/checks/{check_id}/audit")
    def audit(check_id: str, request: Request):
        return service.get(check_id, actor(request), audit=True)

    return router

"""Personal Agent learning adapter. No Brain, Worker dispatch, or kernel hooks."""
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from agent_core.understanding import QuestionGenerator
from agent_core.understanding_service import UnderstandingService, actor, digest


class PersonalUnderstanding(UnderstandingService):
    def __init__(self, *args, scope, **kwargs):
        self.scope = scope
        super().__init__(*args, **kwargs)

    def canonical(self, body):
        return {**body, "project_scope": self.scope}

    def start(self, body, document_ids, who):
        return super().start(self.canonical(body), document_ids, who)

    def assess(self, body, document_ids, who, analysis=""):
        return super().assess(self.canonical(body), document_ids, who, analysis)

    def trigger(self, body, who):
        return super().trigger(self.canonical(body), who)

    def authorize(self, check_id, body, who):
        return super().authorize(check_id, self.canonical(body), who)

    def policy(self):
        return {**super().policy(), "scope": "personal_agent", "project_scope": self.scope}

    def get(self, check_id, who, *, audit=False):
        value = super().get(check_id, who, audit=audit)
        if not audit:
            with self.connection() as conn:
                record = self._read(conn, check_id, who)
            value["request"] = {k:v for k,v in record["binding"].items() if k != "project_scope"}
        return value


def install_personal_understanding(app, run_model, config_provider, workspace_provider, generator=None):
    """Lazy stores: importing the service must not create operator records."""
    from agent_core.understanding_service import create_understanding_router
    lock = threading.Lock()
    cache = {}

    def service():
        config = config_provider()
        workspace = str(workspace_provider().resolve())
        key = (id(config), workspace)
        with lock:
            if key not in cache:
                # Reuse existing store libraries; no HTTP or connection to a Brain process.
                from agent_core.tools import ensure_runtime_on_path
                ensure_runtime_on_path(config)
                from budget_store import BudgetStore
                from knowledge_store import KnowledgeStore
                root = Path(config.work_dir or config.project_root or ".") / ".understanding" / digest(workspace)[:16]
                budgets = BudgetStore(root / "budget.json")
                budgets.set_limits(global_limit_usd=config.understanding_budget_usd)
                cache[key] = PersonalUnderstanding(root / "records.sqlite3",
                    KnowledgeStore(str(root / "documents")), budgets,
                    generator if generator is not None else QuestionGenerator(config), scope=workspace,
                    required=config.understanding_enabled, trigger_mode="conditional",
                    max_cost_usd=config.understanding_max_cost_usd)
            return cache[key]

    class LazyService:
        def __getattr__(self, name):
            return getattr(service(), name)

    app.state.personal_understanding = service
    app.include_router(create_understanding_router(LazyService(), run_model))
    router = APIRouter(prefix="/agent/understanding", tags=["personal-understanding"])

    class DocumentReq(BaseModel):
        title: str = Field(min_length=1, max_length=200)
        content: str = Field(min_length=1, max_length=12000)

    @router.get("/documents")
    def documents():
        return {"docs": service().knowledge.list_all()}

    @router.post("/documents")
    def document(req: DocumentReq):
        if not req.title.strip() or not req.content.strip():
            raise HTTPException(400, "请填写资料标题和正文")
        return service().knowledge.add(req.title, req.content)

    @router.get("/budget")
    def budget():
        return service().budgets.snapshot()

    @router.get("/panel.js")
    def panel_script():
        return FileResponse(Path(__file__).parent / "panel" / "understanding.js", media_type="application/javascript")

    app.include_router(router)


def authorize_personal_run(request: Request, body):
    service = request.app.state.personal_understanding()
    request.state.personal_understanding = service
    data = body.model_dump()
    who = actor(request)
    if service.required and (body.understanding_check_id or service.trigger(data, who)["required"]):
        service.authorize(body.understanding_check_id, data, who)


def link_personal_run(request: Request, check_id, run_id):
    if check_id:
        request.state.personal_understanding.link(check_id, run_id)

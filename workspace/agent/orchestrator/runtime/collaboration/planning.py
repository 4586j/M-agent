"""Deterministic plan generation, validation, publication and budget reservation.

The planner is a callable seam that only produces a candidate; every rule below
is re-checked against fresh database facts before anything is persisted.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict

from .auth import record_audit
from .budgets import BudgetService
from .models import (
    Conflict, InvalidState, NotFound, Plan, Unavailable, ValidationError,
)
from .store import CollaborationStore

MAX_ITEMS = 256
MAX_TITLE_CHARS = 512
MAX_TEXT_CHARS = 8192
MAX_ESTIMATE = 10 ** 9

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_ITEM_FIELDS = (("title", MAX_TITLE_CHARS), ("objective", MAX_TEXT_CHARS),
                ("scope", MAX_TEXT_CHARS), ("non_goals", MAX_TEXT_CHARS),
                ("duty", MAX_TEXT_CHARS))


def plan_document(plan: Plan) -> dict:
    return {**asdict(plan), "report": json.loads(plan.report)}


def _has_cycle(graph: dict[str, list[str]]) -> bool:
    indegree = {node: 0 for node in graph}
    outgoing: dict[str, list[str]] = {node: [] for node in graph}
    for node, dependencies in graph.items():
        for dependency in dependencies:
            outgoing[dependency].append(node)
            indegree[node] += 1
    ready = [node for node, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for target in outgoing[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    return visited != len(graph)


def validate_candidate(candidate, *, members, bindings, agents,
                       acceptance) -> tuple[list[str], list[dict]]:
    """Return (problems, normalized items) without touching the database."""
    problems: list[str] = []
    if not isinstance(candidate, dict) or not isinstance(candidate.get("items"), list):
        return ["invalid_candidate"], []
    raw_items = candidate["items"]
    if len(raw_items) > MAX_ITEMS:
        return ["too_many_items"], []
    if not raw_items:
        problems.append("empty_plan")
    acceptance_ids = {item.item_id for item in acceptance}
    if not acceptance:
        problems.append("acceptance_items_missing")
    covered: set[str] = set()
    items: list[dict] = []
    seen_keys: set[str] = set()
    for index, raw in enumerate(raw_items):
        label = f"item_{index}"
        if not isinstance(raw, dict):
            problems.append(f"invalid_item:{label}")
            continue
        key = raw.get("key")
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            problems.append(f"invalid_key:{label}")
            continue
        label = key
        if key in seen_keys:
            problems.append(f"duplicate_key:{key}")
            continue
        seen_keys.add(key)
        record = {"key": key, "depends_on": []}
        for name, limit in _ITEM_FIELDS:
            value = raw.get(name)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                problems.append(f"invalid_field:{label}:{name}")
            else:
                record[name] = value
        references = raw.get("acceptance_ids")
        if (not isinstance(references, list) or len(references) > MAX_ITEMS
                or any(not isinstance(value, str) for value in references)):
            problems.append(f"invalid_field:{label}:acceptance_ids")
        else:
            for acceptance_id in references:
                if acceptance_id not in acceptance_ids:
                    problems.append(f"acceptance_unknown:{acceptance_id}")
                else:
                    covered.add(acceptance_id)
        assignee = raw.get("assignee_user_id")
        member = members.get(assignee) if isinstance(assignee, str) else None
        if member is None or member.status != "active":
            problems.append(f"assignee_not_member:{label}")
        binding_id = raw.get("binding_id")
        binding = bindings.get(binding_id) if isinstance(binding_id, str) else None
        if binding is None:
            problems.append(f"binding_not_found:{label}")
        elif binding.status != "active":
            problems.append(f"binding_not_active:{label}")
        else:
            if isinstance(assignee, str) and binding.member_user_id != assignee:
                problems.append(f"binding_member_mismatch:{label}")
            duty = record.get("duty")
            if duty is not None and binding.duty != duty:
                problems.append(f"duty_mismatch:{label}")
            agent = agents.get(binding.agent_id)
            if agent is None or agent.status != "active":
                problems.append(f"agent_not_active:{label}")
            record["assignee_user_id"] = assignee if isinstance(assignee, str) else ""
            record["binding_id"] = binding_id
        estimate = raw.get("estimated_units")
        if type(estimate) is not int or not 0 <= estimate <= MAX_ESTIMATE:
            problems.append(f"invalid_estimate:{label}")
        else:
            record["estimated_units"] = estimate
        depends_on = raw.get("depends_on", [])
        if (not isinstance(depends_on, list)
                or any(not isinstance(value, str) for value in depends_on)):
            problems.append(f"invalid_field:{label}:depends_on")
        else:
            record["depends_on"] = list(dict.fromkeys(depends_on))
        items.append(record)
    for item in items:
        if item["key"] in item["depends_on"]:
            problems.append(f"dependency_self:{item['key']}")
    all_keys = {item["key"] for item in items}
    for item in items:
        for dependency in item["depends_on"]:
            if dependency not in all_keys:
                problems.append(f"dependency_unknown:{item['key']}")
    graph = {item["key"]: [dep for dep in item["depends_on"] if dep in all_keys]
             for item in items}
    if _has_cycle(graph):
        problems.append("dependency_cycle")
    for acceptance_id in sorted(acceptance_ids - covered):
        problems.append(f"acceptance_uncovered:{acceptance_id}")
    return list(dict.fromkeys(problems)), items


class PlanService:
    """Candidate planning runs outside any write transaction; persistence re-checks.

    The planner seam is a callable ``(context) -> dict``; the default build wires
    none, so generation raises Unavailable until a planner is explicitly connected.
    Callers must not hold their own database transaction across ``plan``: it opens
    its own connections, and a held transaction would keep the writer lock busy.
    """

    def __init__(self, database, *, budgets: BudgetService | None = None, clock=time.time):
        self.database = database
        self.budgets = budgets if budgets is not None else BudgetService(clock=clock)
        self.clock = clock
        self.planner = None

    @staticmethod
    def _member(access, project_id: str) -> None:
        member = access.store.get_member(project_id, access.user.user_id)
        if member.status != "active":
            raise NotFound("Resource not found")

    def list_plans(self, access, *, project_id: str, requirement_id: str) -> list[Plan]:
        self._member(access, project_id)
        access.store.get_requirement(project_id, requirement_id)
        return access.store.list_plans(project_id, requirement_id)

    def plan_detail(self, access, *, project_id: str, requirement_id: str, plan_id: str):
        self._member(access, project_id)
        access.store.get_requirement(project_id, requirement_id)
        plan = access.store.get_plan(project_id, plan_id)
        if plan.requirement_id != requirement_id:
            raise NotFound("Resource not found")
        return (plan, access.store.list_work_items(project_id, plan_id),
                access.store.list_work_item_dependencies(project_id, plan_id))

    @staticmethod
    def _applicable(store, project_id: str, requirement, require_approved_only: bool,
                    expected_revision_id: str | None):
        if requirement.approved_revision_id is None or requirement.approved_revision_hash is None:
            return False, InvalidState("Requirement has no approved revision")
        if require_approved_only:
            if requirement.status != "approved":
                return False, None
            if (expected_revision_id is not None
                    and requirement.approved_revision_id != expected_revision_id):
                return False, None
            if store.published_plan(project_id, requirement.requirement_id) is not None:
                return False, None
            return True, None
        if store.published_plan(project_id, requirement.requirement_id) is not None:
            return False, Conflict("A published plan already exists")
        if requirement.status not in ("approved", "planning_blocked"):
            return False, InvalidState("Requirement is not ready for planning")
        return True, None

    @staticmethod
    def _facts(store, project_id: str, requirement_id: str, revision_id: str) -> dict:
        members = {member.user_id: member for member in store.list_members(project_id)}
        display_names = {user_id: store.get_user(user_id).display_name for user_id in members}
        bindings = {binding.binding_id: binding for binding in store.list_bindings(project_id)}
        agents = {}
        for binding in bindings.values():
            try:
                agents[binding.agent_id] = store.get_agent(binding.agent_id, binding.member_user_id)
            except NotFound:
                continue
        acceptance = store.list_acceptance_items(project_id, requirement_id, revision_id)
        return {"members": members, "display_names": display_names, "bindings": bindings,
                "agents": agents, "acceptance": acceptance}

    @staticmethod
    def _context(project_id: str, requirement, revision_id: str, revision_hash: str, content: str,
                 facts: dict) -> dict:
        return {
            "project_id": project_id,
            "requirement_id": requirement.requirement_id,
            "title": requirement.title,
            "revision_id": revision_id,
            "revision_hash": revision_hash,
            "content": content,
            "acceptance_items": [{"item_id": item.item_id, "text": item.text}
                                 for item in facts["acceptance"]],
            "members": [{"user_id": member.user_id,
                         "display_name": facts["display_names"][member.user_id],
                         "access_role": member.access_role, "status": member.status}
                        for member in facts["members"].values()],
            "bindings": [{"binding_id": binding.binding_id, "agent_id": binding.agent_id,
                          "member_user_id": binding.member_user_id, "duty": binding.duty,
                          "persona_version": binding.persona_version, "status": binding.status}
                         for binding in facts["bindings"].values()],
        }

    def plan(self, *, project_id: str, requirement_id: str, actor_id: str | None,
             require_approved_only: bool, expected_revision_id: str | None = None) -> Plan | None:
        with self.database.read() as conn:
            store = CollaborationStore(self.database, connection=conn)
            if not require_approved_only:
                member = (store.get_member(project_id, actor_id)
                          if isinstance(actor_id, str) else None)
                if member is None or member.status != "active":
                    raise NotFound("Resource not found")
            requirement = store.get_requirement(project_id, requirement_id)
            applicable, failure = self._applicable(
                store, project_id, requirement, require_approved_only, expected_revision_id)
            if not applicable:
                if require_approved_only:
                    return None
                raise failure
            revision_id = requirement.approved_revision_id
            revision_hash = requirement.approved_revision_hash
            content = store.get_revision_content(project_id, requirement_id, revision_id)
            if hashlib.sha256(content.encode("utf-8")).hexdigest() != revision_hash:
                raise Conflict("The approved revision does not match its recorded digest")
            facts = self._facts(store, project_id, requirement_id, revision_id)
            context = self._context(project_id, requirement, revision_id, revision_hash,
                                    content, facts)
        planner = self.planner
        if planner is None:
            raise Unavailable("Planner is not configured")
        try:
            candidate = planner(context)
        except Exception:
            raise Unavailable("Planner failed") from None
        now = self.clock()
        with self.database.transaction() as conn:
            store = CollaborationStore(self.database, connection=conn)
            requirement = store.get_requirement(project_id, requirement_id)
            if (requirement.approved_revision_id != revision_id
                    or requirement.approved_revision_hash != revision_hash
                    or requirement.status not in ("approved", "planning_blocked")):
                raise Conflict("The requirement changed while the plan was being generated")
            if store.published_plan(project_id, requirement_id) is not None:
                raise Conflict("A published plan already exists")
            fresh = self._facts(store, project_id, requirement_id, revision_id)
            problems, items = validate_candidate(
                candidate, members=fresh["members"], bindings=fresh["bindings"],
                agents=fresh["agents"], acceptance=fresh["acceptance"])
            total_units = sum(item["estimated_units"] for item in items)
            if not problems:
                problems = self.budgets.limit_problems(
                    store, project_id=project_id, requirement_id=requirement_id,
                    amount=total_units)
            if problems:
                plan = store.create_plan(
                    project_id, requirement_id, revision_id=revision_id,
                    revision_hash=revision_hash, status="blocked",
                    report=json.dumps({"problems": problems, "item_count": len(items)},
                                      ensure_ascii=False),
                    total_units=0, created_by=actor_id, created_at=now)
                updated = store.set_requirement_status(
                    project_id, requirement_id, status="planning_blocked",
                    expected_version=requirement.version, updated_at=now)
                record_audit(conn, now, actor_id, "plan.blocked", plan.plan_id,
                             project_id=project_id, target_version=plan.version)
                record_audit(conn, now, actor_id, "requirement.planning_blocked", requirement_id,
                             project_id=project_id, target_version=updated.version)
                return plan
            plan = store.create_plan(
                project_id, requirement_id, revision_id=revision_id, revision_hash=revision_hash,
                status="published",
                report=json.dumps({"problems": [], "item_count": len(items),
                                   "total_units": total_units,
                                   "keys": [item["key"] for item in items]}, ensure_ascii=False),
                total_units=total_units, created_by=actor_id, created_at=now, items=items)
            self.budgets.reserve(store, project_id=project_id, requirement_id=requirement_id,
                                 plan_id=plan.plan_id, amount=total_units, created_at=now)
            updated = store.set_requirement_status(
                project_id, requirement_id, status="executing",
                expected_version=requirement.version, updated_at=now)
            store.enqueue_event(project_id, "plan.published", {
                "plan_id": plan.plan_id, "requirement_id": requirement_id,
                "revision_id": revision_id}, created_at=now)
            record_audit(conn, now, actor_id, "plan.published", plan.plan_id,
                         project_id=project_id, target_version=plan.version)
            record_audit(conn, now, actor_id, "requirement.executing", requirement_id,
                         project_id=project_id, target_version=updated.version)
            return plan

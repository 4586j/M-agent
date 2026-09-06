"""程序记忆演化：技能定义的提案 → 共识投票 → 自动应用。

程序记忆 = SKILL.md / executor.md / 安全规则等定义文件（CoALA 分类下的
procedural memory）。演化流程对齐大脑 SKILL.md 的共识记忆门控：
1. 从经验库反思报告（brain_api /memory/procedural/reflection）起草修订提案
2. 提案创建时 security 视角由规则引擎自动裁决（不可绕过）
3. brain / executor 视角各投一票（POST vote）
4. 三票全 pass → 自动应用；任一 block → 否决

安全边界：目标路径白名单（仅 skills/ 与 .claude/agents/ 下既有文件）、
original_hash 乐观锁（目标文件漂移即拒应用）、原内容随提案归档永不丢失。
"""
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from safe_id import validate_id

VALID_VOTERS = ("brain", "executor", "security")
VALID_VERDICTS = ("pass", "block")


@dataclass
class ProposalRecord:
    proposal_id: str
    target_path: str  # 相对项目根的路径
    original_content: str  # 应用前内容（历史随提案归档）
    original_hash: str  # 提案时文件内容 sha256（乐观锁）
    content: str  # 提议的新内容
    reason: str
    evidence_exp_ids: list[str] = field(default_factory=list)
    votes: dict = field(default_factory=dict)  # voter -> {"verdict","reason","at"}
    status: str = "pending"  # pending | applied | rejected
    reject_reason: str = ""
    created_at: str = ""
    applied_at: str = ""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProceduralStore:
    """提案持久化与共识应用。security_check 为规则引擎（可选 LLM 增强）。

    allowed_roots 是允许修订的目录白名单（绝对路径）；目标必须是白名单下
    已存在的文件——演化只修订不新建，防止经提案通道植入任意文件。
    """

    def __init__(self, store_dir: str, allowed_roots: list[str], project_root: str,
                 security_check: Callable[[str], dict] | None = None,
                 now_fn: Callable[[], str] | None = None):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_roots = [Path(r).resolve() for r in allowed_roots]
        self.project_root = Path(project_root).resolve()
        self.security_check = security_check
        self._now = now_fn or (lambda: str(int(time.time())))

    # ---- 持久化 ----

    def _path(self, proposal_id: str) -> Path:
        validate_id(proposal_id, "proposal_id")
        return self.store_dir / f"{proposal_id}.json"

    def _dump(self, rec: ProposalRecord) -> None:
        with open(self._path(rec.proposal_id), "w", encoding="utf-8") as f:
            json.dump(asdict(rec), f, ensure_ascii=False, indent=2)

    def get(self, proposal_id: str) -> ProposalRecord | None:
        p = self._path(proposal_id)
        if not p.exists():
            return None
        with open(p, encoding="utf-8") as f:
            return ProposalRecord(**json.load(f))

    def list_all(self, status: str | None = None) -> list[ProposalRecord]:
        out = []
        for p in sorted(self.store_dir.glob("*.json")):
            with open(p, encoding="utf-8") as f:
                out.append(ProposalRecord(**json.load(f)))
        if status:
            out = [r for r in out if r.status == status]
        return sorted(out, key=lambda r: r.created_at, reverse=True)

    def delete(self, proposal_id: str) -> bool:
        p = self._path(proposal_id)
        if p.exists():
            p.unlink()
            return True
        return False

    # ---- 提案与共识 ----

    def _resolve_target(self, target_path: str) -> Path:
        if not target_path or ".." in target_path:
            raise ValueError(f"invalid target_path: {target_path!r}")
        abs_path = (self.project_root / target_path).resolve()
        allowed = any(
            abs_path == root or root in abs_path.parents
            for root in self.allowed_roots
        )
        if not allowed:
            raise ValueError(f"target_path 不在白名单目录内: {target_path}")
        if not abs_path.is_file():
            raise ValueError(f"目标必须是已存在的文件（演化只修订不新建）: {target_path}")
        return abs_path

    def propose(self, target_path: str, content: str, reason: str,
                evidence_exp_ids: list[str] | None = None) -> ProposalRecord:
        abs_path = self._resolve_target(target_path)
        original = abs_path.read_text(encoding="utf-8")
        rec = ProposalRecord(
            proposal_id=f"prop-{uuid.uuid4().hex[:8]}",
            target_path=target_path.replace("\\", "/"),
            original_content=original,
            original_hash=_sha256(original),
            content=content,
            reason=reason,
            evidence_exp_ids=evidence_exp_ids or [],
            created_at=self._now(),
        )
        # security 视角自动裁决（规则引擎，不可绕过）
        if self.security_check:
            v = self.security_check(content)
            rec.votes["security"] = {
                "verdict": v.get("verdict", "block"),
                "reason": v.get("reason", ""),
                "at": self._now(),
            }
        self._evaluate(rec)
        self._dump(rec)
        return rec

    def vote(self, proposal_id: str, voter: str, verdict: str,
             reason: str = "") -> ProposalRecord:
        if voter not in VALID_VOTERS:
            raise ValueError(f"invalid voter: {voter}")
        if verdict not in VALID_VERDICTS:
            raise ValueError(f"invalid verdict: {verdict}")
        rec = self.get(proposal_id)
        if rec is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        if rec.status != "pending":
            raise ValueError(f"提案已终结（{rec.status}），不可再投票")
        rec.votes[voter] = {"verdict": verdict, "reason": reason, "at": self._now()}
        self._evaluate(rec)
        self._dump(rec)
        return rec

    def _evaluate(self, rec: ProposalRecord) -> None:
        """共识判定：任一 block → 否决；三票全 pass → 应用。"""
        verdicts = [v.get("verdict") for v in rec.votes.values()]
        if "block" in verdicts:
            rec.status = "rejected"
            blocked = [f"{k}: {v.get('reason', '')}" for k, v in rec.votes.items()
                       if v.get("verdict") == "block"]
            rec.reject_reason = "; ".join(blocked)
            return
        if set(rec.votes.keys()) >= set(VALID_VOTERS):
            self._apply(rec)

    def _apply(self, rec: ProposalRecord) -> None:
        abs_path = self._resolve_target(rec.target_path)
        current_hash = _sha256(abs_path.read_text(encoding="utf-8"))
        if current_hash != rec.original_hash:
            rec.status = "rejected"
            rec.reject_reason = "target_drift: 目标文件在提案后已被修改，请基于最新内容重新提案"
            return
        abs_path.write_text(rec.content, encoding="utf-8")
        rec.status = "applied"
        rec.applied_at = self._now()

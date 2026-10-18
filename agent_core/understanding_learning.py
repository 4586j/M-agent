"""Append-only, question-scoped learning. Only explicit navigation changes the cursor."""
import json
import math
import uuid

from fastapi import HTTPException


class QuestionLearning:
    def trigger(self, body, who):
        from agent_core.understanding_service import binding, digest
        subject = digest(binding(body))
        with self.connection() as conn:
            triggers = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM learning_triggers WHERE actor=? ORDER BY created DESC", (who,))]
            manual_records = [p for r in conn.execute("SELECT payload FROM generations WHERE actor=?", (who,))
                if (p := json.loads(r[0])).get("binding_hash") == subject or
                p.get("binding", {}).get("prompt") == body.get("prompt")]
            manual = bool(manual_records)
        serious = next((t for t in triggers if (t.get("binding_hash") == subject or
            t.get("prompt") == body.get("prompt")) and t.get("severity") == "serious"), None)
        return {"required": self.required and (self.trigger_mode == "always" or manual or serious is not None),
                "triggered_at": max([p["created_at"] for p in manual_records] +
                                    ([serious["created_at"]] if serious else []) + [0]),
                "model_triggered": serious is not None, "reason": serious["reason"] if serious else
                "用户已主动申请本需求核验" if manual else "尚未触发核验，可主动申请",
                "assessment": {k: serious[k] for k in ("id", "severity", "reason", "cost_usd", "evidence") if k in serious} if serious else None}

    def assessment_audit(self, assessment_id, who):
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM learning_triggers WHERE id=? AND actor=?",
                               (assessment_id, who)).fetchone()
            if row is None:
                raise HTTPException(404, "偏差分析记录不存在")
            return json.loads(row[0])

    def assessment_history(self, who, limit, offset):
        with self.connection() as conn:
            records = [json.loads(r[0]) for r in conn.execute(
                "SELECT payload FROM learning_triggers WHERE actor=? ORDER BY created DESC", (who,))]
        results = {r.get("request_id", r["id"]): r for r in reversed(records)}
        values = sorted(results.values(), key=lambda r:r["created_at"], reverse=True)
        return [{k:r[k] for k in ("id", "created_at", "prompt", "severity", "reason", "cost_usd")}
                for r in values[offset:offset+limit]]

    def verified_references(self, sources, who):
        hashes = {s["id"]: s["hash"] for s in sources}
        with self.connection() as conn:
            rows = conn.execute("""SELECT e.payload FROM learning_events e JOIN checks c ON c.id=e.check_id
                WHERE c.actor=? ORDER BY e.seq DESC""", (who,))
            references = []
            for row in rows:
                event = json.loads(row[0])
                if event["kind"] == "correction_verified" and hashes.get(event["source_id"]) == event["source_hash"]:
                    reference = {k: event[k] for k in ("claim", "source_id", "source_hash", "quote", "at")}
                    if len(json.dumps([*references, reference], ensure_ascii=False)) > 3000:
                        break
                    references.append(reference)
                    if len(references) >= 5:
                        break
        return references

    def assess(self, body, document_ids, who, analysis=""):
        from agent_core.understanding_service import binding, digest, packed
        assessor = getattr(self.generator, "assess", None)
        if assessor is None:
            raise HTTPException(503, "独立偏差分析模型未配置")
        sources = self._sources(body, document_ids)
        if analysis:
            sources.append({"id": "ai_analysis", "title": "待核查的 AI 分析（不视为事实）",
                            "content": analysis[:3000], "hash": digest(analysis[:3000])})
        context = {"sources": sources, "verified_references": self.verified_references(sources, who)}
        if len(packed(context)) > self.context_limit:
            raise HTTPException(400, "偏差分析资料超过独立上下文限额，请精简资料")
        if not self.slots.acquire(blocking=False):
            raise HTTPException(429, "偏差分析繁忙，请稍后重试")
        request_id = "ua-" + uuid.uuid4().hex
        try:
            self.budgets.reserve(request_id, self.max_cost)
            record = {"id": request_id, "actor": who, "created_at": self.clock(),
                "binding_hash": digest(binding(body)), "prompt": body["prompt"], "severity": "pending", "reason": "分析尚未完成",
                "context": context, "context_hash": digest(context), "cost_usd": None}
            with self.connection() as conn:
                conn.execute("INSERT INTO learning_triggers VALUES (?,?,?,?)",
                             (request_id, who, self.clock(), packed(record)))
            result = {**record, "id": request_id+"-result", "request_id": request_id,
                      "severity": "failed", "reason": "偏差分析失败或证据不足，未自动认定严重偏差"}
            try:
                generated = assessor(context, max_cost_usd=self.max_cost)
                result.update({k: generated.get(k) for k in ("model", "usage", "sdk_session_id", "system_prompt")})
                result["raw_output"] = str(generated.get("raw") or "")[:12000]
                cost = generated.get("cost_usd")
                if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                    self.budgets.settle(request_id, cost)
                    result["cost_usd"] = cost
                    if cost > self.max_cost:
                        raise ValueError("assessment exceeded budget")
                if generated.get("is_error"):
                    raise ValueError("assessment model error")
                text = (generated.get("raw") or "").strip()
                if text.startswith("```json") and text.endswith("```"):
                    text = text[7:-3].strip()
                data = json.loads(text)
                if (data.get("severity") not in ("normal", "serious") or not isinstance(data.get("reason"), str)
                        or not data["reason"].strip() or len(data["reason"]) > 1200):
                    raise ValueError("invalid deviation assessment")
                evidence = data.get("evidence", [])
                if not isinstance(evidence, list) or len(evidence) > 4:
                    raise ValueError("invalid deviation evidence")
                indexed = {s["id"]: s for s in sources}
                for e in evidence:
                    source = indexed.get(e.get("source_id")) if isinstance(e, dict) else None
                    if not source or not isinstance(e.get("quote"), str) or not e["quote"].strip() or e["quote"] not in source["content"]:
                        raise ValueError("unsupported deviation evidence")
                if data["severity"] == "serious" and (len(evidence) < 2 or len({(e["source_id"],e["quote"]) for e in evidence}) < 2
                        or not any(e["source_id"].startswith("doc:") for e in evidence)):
                    raise ValueError("serious deviation needs distinct evidence")
                result.update({"severity": data["severity"], "reason": data["reason"],
                               "evidence": [{"source_id":e["source_id"],"quote":e["quote"]} for e in evidence]})
            except Exception as error:
                result["failure_type"] = type(error).__name__
            with self.connection() as conn:
                conn.execute("INSERT INTO learning_triggers VALUES (?,?,?,?)",
                             (result["id"], who, self.clock(), packed(result)))
            return {k: result[k] for k in ("id", "severity", "reason", "cost_usd", "model", "evidence") if k in result}
        finally:
            self.slots.release()

    @staticmethod
    def _events(conn, check_id):
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM learning_events WHERE check_id=? ORDER BY seq", (check_id,))]

    def _event(self, conn, check_id, event):
        from agent_core.understanding_service import packed
        event = {"at": self.clock(), **event}
        conn.execute("INSERT INTO learning_events(id,check_id,payload) VALUES(?,?,?)",
                     (uuid.uuid4().hex, check_id, packed(event)))

    @staticmethod
    def _learning(record, events):
        questions = record["questions"]
        current = questions[0]["id"] if questions else None
        outcomes, skipped = {}, []
        for event in events:
            if event["kind"] == "answer":
                outcomes[event["question_id"]] = event["outcome"]
                skipped = [qid for qid in skipped if qid != event["question_id"]]
            elif event["kind"] == "navigate":
                current = event["current_question_id"]
                if event["action"] == "skip" and event["question_id"] not in skipped:
                    skipped.append(event["question_id"])
        return {"current_question_id": current, "revision": len(events),
                "skipped_question_ids": skipped, "outcomes": list(outcomes.values()),
                "corrections": [e for e in events if e["kind"] in ("correction_proposed", "correction_verified")],
                "discussions": [e for e in events if e["kind"] in ("discussion_request", "discussion_reply")]}

    def propose_correction(self, check_id, question_id, claim, document_id, quote, revision, who):
        if not claim.strip() or len(claim) > 1500 or not quote.strip() or len(quote) > 1000:
            raise HTTPException(400, "请填写纠正内容和资料原文，内容须在长度限制内")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _, progress, _ = self._step(conn, check_id, who, question_id, revision)
            if not any(o["id"] == question_id for o in progress["outcomes"]):
                raise HTTPException(409, "请先作答本题，再记录纠正意见")
            doc = self.knowledge.get(document_id, full=True)
            if not doc or quote not in doc.get("content", ""):
                raise HTTPException(400, "纠正依据必须引用当前知识库文档原文")
            from agent_core.understanding_service import digest
            self._event(conn, check_id, {"kind":"correction_proposed", "question_id":question_id,
                "correction_id":"ur-"+uuid.uuid4().hex, "claim":claim.strip(), "source_id":"doc:"+document_id,
                "source_hash":digest({"title":doc["title"],"content":doc["content"]}), "quote":quote})
        return self.get(check_id, who)

    def verify_correction(self, check_id, question_id, correction_id, revision, who):
        from agent_core.understanding_service import digest
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            _, progress, _ = self._step(conn, check_id, who, question_id, revision)
            proposed = next((e for e in progress["corrections"] if e["kind"] == "correction_proposed" and
                             e["correction_id"] == correction_id and e["question_id"] == question_id), None)
            if not proposed or any(e["kind"] == "correction_verified" and e["correction_id"] == correction_id for e in progress["corrections"]):
                raise HTTPException(409, "纠正意见不存在或已经核实")
            doc = self.knowledge.get(proposed["source_id"][4:], full=True)
            if not doc or digest({"title":doc["title"],"content":doc["content"]}) != proposed["source_hash"]:
                raise HTTPException(409, "纠正依据资料已变化，请重新提交并核查")
            self._event(conn, check_id, {**proposed, "kind":"correction_verified", "at":self.clock(), "verified_by":who})
        return self.get(check_id, who)

    def _step(self, conn, check_id, who, question_id, revision, *, answering=False):
        record = self._read(conn, check_id, who)
        events = self._events(conn, check_id)
        progress = self._learning(record, events)
        if record.get("schema_version", 1) < 2 or not record["questions"]:
            raise HTTPException(409, "本轮不支持逐题学习，请重新发起核验")
        if progress["revision"] != revision or progress["current_question_id"] != question_id:
            raise HTTPException(409, "当前题或记录已变化，请刷新本轮核验后重试")
        completed = {e["request_id"] for e in events if e["kind"] == "discussion_reply"}
        if any(e["kind"] == "discussion_request" and e["request_id"] not in completed
               and self.clock() - e["at"] < 120 for e in events):
            raise HTTPException(409, "本题讲解正在生成，请稍后再操作")
        if answering and (not self._current(record) or
                          conn.execute("SELECT 1 FROM answers WHERE check_id=?", (check_id,)).fetchone()):
            raise HTTPException(409, "本轮已评分或已失效，请重新发起核验")
        question = next(q for q in record["questions"] if q["id"] == question_id)
        return record, progress, question

    @staticmethod
    def _outcome(record, question, answer):
        source = next(s for s in record["sources"] if s["id"] == question["source_id"])
        return {"id": question["id"], "answer": answer, "expected": question["correct"],
                "correct": answer == question["correct"], "critical": question["critical"],
                "source_id": source["id"], "source_title": source["title"], "source_hash": source["hash"],
                "quote": question["quote"], "explanation": question["explanation"],
                "summary": question.get("summary") or f"本题正确结论：{question['options'][question['correct']]}",
                "knowledge_points": question.get("knowledge_points") or [
                    question["explanation"],
                    "核对资料中的目标、实现或验收约束；未提供代码时，不能据此断言具体代码实现。"]}

    def answer_question(self, check_id, question_id, answer, revision, who):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record, progress, question = self._step(conn, check_id, who, question_id, revision, answering=True)
            if any(o["id"] == question_id for o in progress["outcomes"]):
                raise HTTPException(409, "本题已评分，原始答案不可修改；仍可追问和复习")
            if type(answer) is not int or not 0 <= answer < 4:
                raise HTTPException(400, "请选择一个答案")
            self._event(conn, check_id, {"kind": "answer", "question_id": question_id,
                                         "outcome": self._outcome(record, question, answer)})
        return self.get(check_id, who)

    def navigate(self, check_id, question_id, action, target, revision, who):
        from agent_core.understanding_service import packed
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record, progress, _ = self._step(conn, check_id, who, question_id, revision)
            ids = [q["id"] for q in record["questions"]]
            answered = {o["id"] for o in progress["outcomes"]}
            current = question_id
            if action == "finish":
                if len(answered) != len(ids) or not self._current(record):
                    raise HTTPException(409, "请先补答所有暂时跳过的题目，并检查核验是否仍有效")
                if conn.execute("SELECT 1 FROM answers WHERE check_id=?", (check_id,)).fetchone():
                    raise HTTPException(409, "本轮已完成评分")
                outcomes = progress["outcomes"]
                correct = sum(o["correct"] for o in outcomes)
                critical = all(o["correct"] for o in outcomes if o["critical"])
                result = {"answered_at": self.clock(), "correct_count": correct, "question_count": len(ids),
                          "score_percent": correct * 100 // len(ids), "critical_passed": critical,
                          "passed": correct * 100 >= record["threshold_percent"] * len(ids) and critical,
                          "outcomes": sorted(outcomes, key=lambda o: ids.index(o["id"]))}
                conn.execute("INSERT INTO answers VALUES (?,?)", (check_id, packed(result)))
            elif action == "revisit":
                if target not in answered | set(progress["skipped_question_ids"]):
                    raise HTTPException(400, "只能返回已作答或暂时跳过的题目")
                current = target
            elif action in ("next", "skip"):
                if action == "next" and question_id not in answered:
                    raise HTTPException(409, "请先作答本题，或选择暂时跳过")
                if action == "skip" and question_id in answered:
                    raise HTTPException(409, "本题已作答，请选择下一题")
                following = ids[ids.index(question_id)+1:] + ids[:ids.index(question_id)]
                current = next((qid for qid in following if qid not in answered), question_id)
            else:
                raise HTTPException(400, "未知的逐题操作")
            self._event(conn, check_id, {"kind": "navigate", "question_id": question_id,
                "action": action, "current_question_id": current})
        return self.get(check_id, who)

    @staticmethod
    def _discussion_feedback(raw, source):
        if not isinstance(raw, str) or len(raw) > 12000:
            raise ValueError("invalid tutor response")
        text = raw.strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        value = json.loads(text)
        for name, limit in (("explanation", 3000), ("summary", 800)):
            if not isinstance(value.get(name), str) or not value[name].strip() or len(value[name]) > limit:
                raise ValueError("missing feedback")
        points, evidence = value.get("knowledge_points"), value.get("evidence")
        if (not isinstance(points, list) or not 1 <= len(points) <= 5 or
                any(not isinstance(p, str) or not p.strip() or len(p) > 1200 for p in points)):
            raise ValueError("invalid knowledge points")
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 3:
            raise ValueError("missing evidence")
        for item in evidence:
            if (not isinstance(item, dict) or item.get("source_id") != source["id"] or
                    not isinstance(item.get("quote"), str) or not item["quote"].strip() or
                    item["quote"] not in source["content"]):
                raise ValueError("unsupported tutor evidence")
        return {"explanation": value["explanation"], "summary": value["summary"],
                "knowledge_points": points,
                "evidence": [{"source_id": source["id"], "source_title": source["title"],
                              "source_hash": source["hash"], "quote": e["quote"]} for e in evidence]}

    def discuss(self, check_id, question_id, message, revision, who):
        from agent_core.understanding_service import digest, packed
        tutor = getattr(self.generator, "discuss", None)
        if tutor is None:
            raise HTTPException(503, "独立讲解模型不可用，仍可查看本题已有解释和依据")
        try:
            if not isinstance(message, str) or not message.strip() or len(message) > 1500:
                raise HTTPException(400, "追问须为 1–1500 字符")
            message.encode("utf-8")
        except UnicodeError as error:
            raise HTTPException(400, "追问须是有效 Unicode 文本") from error
        if not self.slots.acquire(blocking=False):
            raise HTTPException(429, "讲解繁忙，请稍后重试")
        request_id = "ut-" + uuid.uuid4().hex
        try:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                record, progress, question = self._step(conn, check_id, who, question_id, revision)
                outcome = next((o for o in progress["outcomes"] if o["id"] == question_id), None)
                if outcome is None:
                    raise HTTPException(409, "请先提交本题答案，再针对解释追问")
                source = {"id": question["source_id"], "title": outcome["source_title"],
                          "hash": outcome["source_hash"], "content": question["quote"]}
                # Reconstruct only complete pairs from this question, newest first within a fixed cap.
                events = progress["discussions"]
                replies = {e["request_id"]: e for e in events if e["kind"] == "discussion_reply" and e.get("feedback")}
                history = []
                for event in reversed(events):
                    if event["kind"] == "discussion_request" and event["question_id"] == question_id:
                        reply = replies.get(event["request_id"])
                        if reply:
                            pair = {"user": event["message"], "assistant": reply["feedback"]}
                            if len(history) == 6 or len(packed([pair, *history])) > 8000:
                                break
                            history.insert(0, pair)
                context = {"question": {k: question[k] for k in ("id", "question", "category", "options")},
                           "graded_answer": outcome, "source": source, "history": history, "message": message.strip()}
                while history and len(packed(context)) > self.context_limit:
                    history.pop(0)
                if len(packed(context)) > self.context_limit:
                    raise HTTPException(400, "本题讲解超出独立上下文限额，请缩短追问")
                self.budgets.reserve(request_id, self.max_cost)
                self._event(conn, check_id, {"kind": "discussion_request", "question_id": question_id,
                    "request_id": request_id, "message": message.strip(), "context": context,
                    "context_hash": digest(context), "max_cost_usd": self.max_cost})
            reply = {"kind": "discussion_reply", "request_id": request_id, "question_id": question_id,
                     "feedback": None, "error": "讲解失败或依据校验未通过，请重试；当前题目未切换", "cost_usd": None}
            try:
                generated = tutor(context, max_cost_usd=self.max_cost)
                reply.update({k: generated.get(k) for k in ("model", "usage", "sdk_session_id", "sdk_result_subtype", "system_prompt")})
                reply["raw_output"] = str(generated.get("raw") or "")[:12000]
                cost = generated.get("cost_usd")
                if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                    self.budgets.settle(request_id, cost)
                    reply["cost_usd"] = cost
                    if cost > self.max_cost:
                        raise ValueError("tutor exceeded budget")
                if generated.get("is_error"):
                    raise ValueError("tutor model error")
                reply["feedback"] = self._discussion_feedback(generated.get("raw"), source)
                reply["error"] = None
            except Exception as error:
                reply["failure_type"] = type(error).__name__
            with self.connection() as conn:
                self._event(conn, check_id, reply)
            return self.get(check_id, who)
        finally:
            self.slots.release()

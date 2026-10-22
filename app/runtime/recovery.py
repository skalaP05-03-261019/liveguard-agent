"""Reconcile local inbox from durable checkpoints; never invoke/resume the Graph."""
from pydantic import ValidationError

from app.harness.resume import thread_config
from app.runtime.outcomes import TERMINAL, checkpoint_outcome
from app.schemas import TaskEnvelope


def _bound(task, values, *, require_decision=True):
    try:
        original = TaskEnvelope.model_validate(values.get("task"))
    except (ValidationError, TypeError):
        return False
    keys = ("task_id", "thread_id", "case_id", "snapshot_id", "expected_evidence_version")
    if any(getattr(task, key) != getattr(original, key) for key in keys):
        return False
    refs = {"task_id": str(task.task_id), "thread_id": task.thread_id,
            "case_id": str(task.case_id), "snapshot_id": str(task.snapshot_id),
            "evidence_version": task.expected_evidence_version}
    if any(key in values and values[key] != value for key, value in refs.items()):
        return False
    if task.kind == "RESUME_REVIEW" and require_decision:
        return _decision_bound(task, values)
    return True


def _decision_bound(task, values, *, require_result=True):
    prefix = "context" if task.review_kind == "CONTEXT" else "kick"
    if (values.get(prefix + "_decision_id") != str(task.decision_id)
            or values.get(prefix + "_review_id") != str(task.review_id)):
        return False
    result = values.get(prefix + "_result") or {}
    return not require_result or (
        result.get("decisionId") == str(task.decision_id)
        and result.get("reviewId") == str(task.review_id))


def _waiting_is_bound(task, snapshot):
    # Only the two known human checkpoints can be restored as WAITING_REVIEW.
    if len(snapshot.interrupts) != 1:
        return False
    card = snapshot.interrupts[0].value
    if not isinstance(card, dict):
        return False
    values = snapshot.values
    kind = card.get("reviewKind")
    expected = {"CONTEXT": ("CONTEXT_DECISION_REQUIRED", values.get("context_review_id")),
                "KICK": ("KICK_APPROVAL_REQUIRED", (values.get("kick_review") or {}).get("reviewId"))}
    if kind not in expected:
        return False
    card_type, review_id = expected[kind]
    refs = {"type": card_type, "reviewId": review_id, "taskId": str(task.task_id),
            "threadId": task.thread_id, "caseId": str(task.case_id),
            "snapshotId": str(task.snapshot_id), "evidenceVersion": task.expected_evidence_version,
            "liveId": values.get("live_id"), "policyVersion": values.get("policy_version")}
    return bool(review_id and refs["liveId"] and refs["policyVersion"]) and all(
        card.get(key) == value for key, value in refs.items())


async def reconcile_task(store, graph, task_id, *, apply=False):
    # Hold exclusion for the entire read/compare/update, including async checkpoint read.
    with store.execution_lock(maintenance=True):
        row = store.recovery_candidate(task_id)
        if row is None:
            return {"taskId": str(task_id), "code": "TASK_NOT_FOUND", "applied": False}
        report = {**store.public(row), "dryRun": not apply, "applied": False}
        if row["status"] not in {"RUNNING", "UNKNOWN"}:
            return {**report, "recoveryCode": "RECOVERY_NOT_REQUIRED"}
        task = TaskEnvelope.model_validate_json(row["envelope"])
        proposed, reason, checkpoint_code = "UNKNOWN", "BACKEND_RECONCILIATION_REQUIRED", None
        try:
            snapshot = await graph.aget_state(thread_config(task.thread_id))
        except Exception:
            return {**report, "recoveryCode": "CHECKPOINT_UNAVAILABLE"}
        if snapshot.created_at is None:
            reason = "CHECKPOINT_NOT_FOUND"
        elif not _bound(task, snapshot.values):
            reason = "CHECKPOINT_BINDING_UNVERIFIED"
        elif any(item.error for item in snapshot.tasks):
            reason = "CHECKPOINT_EXECUTION_UNCONFIRMED"
        elif snapshot.interrupts and _waiting_is_bound(task, snapshot):
            proposed, reason = "WAITING_REVIEW", "CHECKPOINT_WAITING_CONFIRMED"
        elif not snapshot.next and not snapshot.interrupts:
            outcome, checkpoint_code = checkpoint_outcome(snapshot)
            if outcome in TERMINAL:
                proposed, reason = outcome, "CHECKPOINT_TERMINAL_CONFIRMED"
        report.update(proposedStatus=proposed, recoveryCode=reason)
        if apply:
            # UNKNOWN never returns to QUEUED. No model calls or business retry here.
            saved_code = checkpoint_code if proposed in TERMINAL else reason
            store.recover(row["operation_id"], row["status"], proposed, saved_code)
            report["previousStatus"] = report["status"]
            report["status"] = proposed
            report["code"] = saved_code
            report["applied"] = True
        return report

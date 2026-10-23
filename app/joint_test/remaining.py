"""Finite, opt-in #96 fault suites layered on BE #76/#78's real joint Compose."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import subprocess
from threading import Event
import time
from uuid import UUID, uuid4

import httpx

from app.joint_test.run import (
    ROOT, JointEnvironment, JointError, checked, decide, job, poll, provenance,
    request, select_review, wait_job,
)


def require(condition, code):
    if not condition:
        raise JointError(code)


def produce(environment, viewer, live, *, marker="NORMAL", pii=False):
    signal = "연락처 010-1234-5678" if pii else "공격 표현 '꺼져'의 합성 인용문"
    message = request(viewer, "POST", f"/api/lives/{UUID(live)}/messages", expected=201,
        body={"clientMessageId": str(uuid4()),
              "text": f"[JOINT96:{marker}] {signal} {uuid4().hex[:12]}"})["data"]
    mid = str(UUID(message["messageId"]))
    raw = environment.query("db",
        "SELECT json_build_object('taskId',s.task_id,'caseId',s.case_id,'threadId',s.thread_id)"
        " FROM guard.live_case_state s JOIN platform.messages m ON m.case_id=s.case_id"
        f" WHERE m.id='{mid}'::uuid")
    require(bool(raw), "REAL_BE_CASE_REQUIRED")
    return {**json.loads(raw), "liveId": live, "messageId": mid}


def outbox(environment, task):
    return json.loads(environment.query("db",
        "SELECT json_build_object('status',status,'attempts',attempt_count,"
        "'uncertain',delivery_uncertain,'code',last_error_code) FROM guard.agent_task_outbox "
        f"WHERE operation_id='run:{UUID(task)}'"))


def metrics(fixture):
    return request(fixture, "GET", "/control")


def arm(fixture, fault):
    return request(fixture, "POST", "/control", body={"fault": fault})


def release(fixture, gate):
    return request(fixture, "POST", "/control", body={"release": gate})


def single_count(environment, case):
    task, cid = UUID(case["taskId"]), UUID(case["caseId"])
    return {
        "jobs": int(environment.query("agent-db",
            f"SELECT count(*) FROM agent_inbox.jobs WHERE operation_id='run:{task}'")),
        "contextReviews": int(environment.query("db",
            f"SELECT count(*) FROM guard.reviews WHERE case_id='{cid}'::uuid AND kind='CONTEXT'")),
    }


def receipt_loss(environment, viewer, operator, fixture, lives):
    arm(fixture, "receipt-loss")
    case = produce(environment, viewer, lives[0], pii=True)
    task = case["taskId"]
    operation = "run:" + task
    poll(lambda: any(row["operationId"] == operation for row in metrics(fixture)["receiptGets"]),
         timeout=60)
    final_outbox = poll(lambda: value if
        (value := outbox(environment, task))["status"] == "ACCEPTED" else None)
    execution = wait_job(environment, task, "SUCCEEDED")
    observed = metrics(fixture)
    posts = [row for row in observed["receipts"] if row["operationId"] == operation]
    gets = [row for row in observed["receiptGets"] if row["operationId"] == operation]
    require(len(posts) == 1 and len(gets) >= 1, "RECEIPT_RECONCILIATION_MUST_NOT_REPOST")
    # BE retains delivery_uncertain as history (OR update), including after ACCEPTED.
    require(final_outbox["attempts"] >= 2 and final_outbox["uncertain"],
            "BE_DID_NOT_RECONCILE_UNCERTAIN_RECEIPT")
    require(single_count(environment, case)["jobs"] == 1, "RECEIPT_LOSS_DUPLICATED_JOB")
    return {"case": case, "agent": execution, "outbox": final_outbox,
            "intakePosts": len(posts), "statusGets": len(gets)}


def early_resume(environment, viewer, operator, fixture, lives):
    arm(fixture, "judgment-gate")
    before = metrics(fixture)["judgmentBlocked"]
    case = produce(environment, viewer, lives[0])
    try:
        poll(lambda: metrics(fixture)["judgmentBlocked"] > before)
        analysis = job(environment, case["taskId"])
        require(analysis["status"] == "RUNNING", "ANALYZE_NOT_RUNNING_AT_DECISION")
        review = select_review(operator, lives[0], case["caseId"], "CONTEXT")
        decision = decide(operator, review, "CONFIRM_NORMAL", case["messageId"])
        # Take envelopes only into memory. Never include them in logs/reports.
        poll(lambda: job(environment, case["taskId"], decision_id=decision))
        envelopes = json.loads(environment.query("db",
            "SELECT json_agg(payload) FROM guard.agent_task_outbox "
            f"WHERE task_id='{UUID(case['taskId'])}'::uuid"))
        require(len(envelopes) == 2, "RUN_AND_RESUME_ENVELOPES_REQUIRED")
        queued = job(environment, case["taskId"], decision_id=decision)
        require(queued["status"] == "QUEUED", "EARLY_RESUME_OVERTOOK_ANALYZE")
        with httpx.Client(base_url=environment.url("agent-api", 8001), timeout=10, trust_env=False,
                         headers={"Authorization": "Bearer " + environment.values["AGENT_SERVICE_TOKEN"]}) as api:
            def resend(envelope):
                suffix = "resume" if envelope["kind"] == "RESUME_REVIEW" else "run"
                result = request(api, "POST", f"/agent/tasks/{case['taskId']}/{suffix}", body=envelope)
                require(result["duplicate"] is True, "CONCURRENT_RECEIPT_NOT_DUPLICATE")
                return result["operationId"]
            with ThreadPoolExecutor(max_workers=4) as pool:
                operations = list(pool.map(resend, envelopes * 4))
        # Eight HTTP duplicates must complete while the original run is still blocked.
        require(job(environment, case["taskId"], decision_id=decision)["status"] == "QUEUED",
                "GATE_RELEASED_BEFORE_RACE_OBSERVED")
    finally:
        release(fixture, "judgment")
    execution = wait_job(environment, case["taskId"], "SUCCEEDED", decision_id=decision)
    counts = single_count(environment, case)
    require(counts == {"jobs": 1, "contextReviews": 1}, "RACE_DUPLICATED_BUSINESS_STATE")
    total = int(environment.query("agent-db",
        "SELECT count(*) FROM agent_inbox.jobs j JOIN agent_inbox.threads t USING(thread_id) "
        f"WHERE t.task_id='{UUID(case['taskId'])}'"))
    require(total == 2, "RACE_DUPLICATED_OPERATION")
    return {"case": case, "analysisAtDecision": analysis, "earlyResume": queued,
            "finalResume": execution, "duplicateRequests": len(operations),
            "distinctOperations": len(set(operations)), "jobCount": total, **counts}


def stable_state(environment, case):
    task = UUID(case["taskId"])
    return {
        "run": job(environment, str(task)),
        "checkpoint": environment.query("agent-db",
            "SELECT md5(string_agg(row_to_json(c)::text,'' ORDER BY checkpoint_ns,checkpoint_id)) "
            "FROM agent.checkpoints c WHERE thread_id=(SELECT thread_id FROM agent_inbox.threads "
            f"WHERE task_id='{task}')"),
        "pendingWrites": environment.query("agent-db",
            "SELECT md5(string_agg(row_to_json(w)::text,'' ORDER BY checkpoint_ns,checkpoint_id,task_id,idx)) "
            "FROM agent.checkpoint_writes w WHERE thread_id=(SELECT thread_id FROM agent_inbox.threads "
            f"WHERE task_id='{task}')"),
        "blobs": environment.query("agent-db",
            "SELECT md5(string_agg(row_to_json(b)::text,'' ORDER BY checkpoint_ns,channel,version)) "
            "FROM agent.checkpoint_blobs b WHERE thread_id=(SELECT thread_id FROM agent_inbox.threads "
            f"WHERE task_id='{task}')"),
        **single_count(environment, case),
    }


def stale_worker(environment, viewer, operator, fixture, lives):
    before = metrics(fixture)
    arm(fixture, "model-gate")
    case = produce(environment, viewer, lives[0])
    owner = None
    try:
        poll(lambda: metrics(fixture)["modelBlocked"] > before["modelBlocked"])
        original = job(environment, case["taskId"])
        require(original["status"] == "RUNNING", "MODEL_GATE_NOT_RUNNING")
        owner = {"integration-worker-1": "worker-1",
                 "integration-worker-2": "worker-2"}[original["workerId"]]
        # Unlike SIGKILL, SIGSTOP preserves the old process, sockets and pending result.
        environment.compose("kill", "-s", "SIGSTOP", owner)
        recovered = wait_job(environment, case["taskId"], {"WAITING_REVIEW", "UNKNOWN", "NEEDS_REVIEW"})
        require(recovered["workerId"] != original["workerId"] and recovered["attempts"] >= 2,
                "LEASE_TRANSFER_REQUIRED")
        state_before = stable_state(environment, case)
        returned_before_release = metrics(fixture)["modelReturned"]
        release(fixture, "model")
        poll(lambda: metrics(fixture)["modelReturned"] > returned_before_release)
        environment.compose("kill", "-s", "SIGCONT", owner)
        time.sleep(6)
        state_after = stable_state(environment, case)
        require(state_before == state_after, "LATE_OLD_OWNER_OVERWROTE_STATE")
        container = environment.compose("ps", "-q", owner)
        require(checked(["docker", "inspect", "--format", "{{.State.Running}}", container]) == "true",
                "OLD_WORKER_DIED_INSTEAD_OF_FENCING")
    finally:
        release(fixture, "model")
        if owner:
            environment.compose("kill", "-s", "SIGCONT", owner)
    if recovered["status"] == "WAITING_REVIEW":
        review = select_review(operator, lives[0], case["caseId"], "CONTEXT")
        decision = decide(operator, review, "CONFIRM_NORMAL", case["messageId"])
        final = wait_job(environment, case["taskId"], "SUCCEEDED", decision_id=decision)
    else:
        require(bool(recovered["code"]), "UNCERTAIN_WITHOUT_CODE")
        final = recovered
    return {"case": case, "previousWorker": original["workerId"], "recovery": recovered,
            "lateResultChangedState": False, "oldProcessAlive": True, "final": final}


def load(environment, viewer, operator, fixture, lives):
    count = environment.args.input_count
    require(8 <= count <= 120, "FINITE_LOAD_REQUIRES_8_TO_120_CASES")
    pattern = getattr(environment.args, "load_pattern", "burst")
    require(pattern in {"burst", "steady"}, "INVALID_LOAD_PATTERN")
    started = time.monotonic()
    cases = []
    stopped = Event()
    def emit():
        # Automatic PII normal route releases slots without human review wait.
        for index in range(count):
            if stopped.is_set():
                break
            cases.append(produce(environment, viewer, lives[index % 2], pii=True))
            if pattern == "steady" and index < count - 1:
                stopped.wait(1)
    if pattern == "burst":
        environment.compose("stop", "worker-1", "worker-2", timeout=100)
        try:
            emit()
            # BE delivery is asynchronous; prove durable admission before execution starts.
            poll(lambda: int(environment.query("agent-db", "SELECT count(*) FROM agent_inbox.jobs"))
                 == count, timeout=60)
        finally:
            environment.compose("start", "worker-1", "worker-2")
    samples = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        emission = pool.submit(emit) if pattern == "steady" else None
        try:
            deadline = time.monotonic() + 480
            while time.monotonic() < deadline:
                if emission and emission.done():
                    emission.result()  # Propagate producer errors, never hide missing input.
                sample = json.loads(environment.query("agent-db",
                    "SELECT json_build_object('queued',count(*) FILTER(WHERE status='QUEUED'),"
                    "'running',count(*) FILTER(WHERE status='RUNNING'),"
                    "'succeeded',count(*) FILTER(WHERE status='SUCCEEDED'),"
                    "'errors',count(*) FILTER(WHERE status IN ('FAILED','UNKNOWN','NEEDS_REVIEW')),"
                    "'maxPerWorker',(SELECT coalesce(max(n),0) FROM "
                    "(SELECT count(*) n FROM agent_inbox.jobs WHERE status='RUNNING' GROUP BY worker_id) w)) "
                    "FROM agent_inbox.jobs"))
                samples.append(sample)
                require(sample["running"] <= 4 and sample["maxPerWorker"] <= 2,
                        "GRAPH_CONCURRENCY_EXCEEDED")
                require(sample["errors"] == 0, "LOAD_HAS_EXPLICIT_FAILURES")
                if sample["succeeded"] == count:
                    break
                time.sleep(1)
            else:
                raise JointError("LOAD_DRAIN_DEADLINE")
            if emission:
                emission.result()
        finally:
            stopped.set()
    rows = json.loads(environment.query("agent-db",
        "SELECT json_agg(row_to_json(s)) FROM (SELECT t.task_id,j.live_id,j.status,"
        "extract(epoch FROM j.updated_at-j.created_at) AS latency,"
        "extract(epoch FROM j.admitted_at-j.created_at) AS wait,"
        "j.admitted_at FROM agent_inbox.jobs j JOIN agent_inbox.threads t USING(thread_id)"
        " ORDER BY j.admitted_at) s"))
    require(len(cases) == len(rows) == count and {row["live_id"] for row in rows[:4]} == set(lives),
            "LOAD_FAIR_ADMISSION_NOT_OBSERVED")
    accepted = int(environment.query("db",
        "SELECT count(*) FROM guard.agent_task_outbox WHERE status='ACCEPTED'"))
    require(accepted == count, "LOAD_OUTBOX_NOT_ACCEPTED")
    model = metrics(fixture)
    require(model["modelPeak"] <= 4, "MODEL_CONCURRENCY_EXCEEDED")
    latencies = sorted(float(row["latency"]) for row in rows)
    elapsed = time.monotonic() - started
    return {"pattern": pattern, "input": count, "completed": count,
            "elapsedSeconds": round(elapsed, 3),
            "throughputPerSecond": round(count / elapsed, 4),
            "latencyP50Seconds": round(statistics.median(latencies), 3),
            "latencyP95Seconds": round(latencies[max(0, (95 * count + 99) // 100 - 1)], 3),
            "maxWaitSeconds": round(max(float(row["wait"]) for row in rows), 3),
            "peakRunning": max(sample["running"] for sample in samples),
            "peakPerWorker": max(sample["maxPerWorker"] for sample in samples),
            "modelPeak": model["modelPeak"], "samples": samples, "admissions": rows,
            "allOutboxAccepted": True, "firstFourCoverBothLives": True}


def deadlines(environment, viewer, operator, fixture, lives):
    require(environment.args.be_scenario == "fallback", "DEFAULT_3M_SCENARIO_REQUIRED")
    violations = [produce(environment, viewer, lives[0], marker="VIOLATION") for _ in range(3)]
    for violation in violations:
        wait_job(environment, violation["taskId"], "WAITING_REVIEW")
        review = select_review(operator, lives[0], violation["caseId"], "CONTEXT")
        decision = decide(operator, review, "CONFIRM_VIOLATION", violation["messageId"])
        expected = "WAITING_REVIEW" if violation is violations[-1] else "SUCCEEDED"
        wait_job(environment, violation["taskId"], expected, decision_id=decision)
    kick = select_review(operator, lives[0], violations[-1]["caseId"], "KICK")
    # Real wall-clock policy; do not mutate clock, DB timestamps, or deadline configuration.
    environment.compose("stop", "worker-1", "worker-2", timeout=100)
    approval = decide(operator, kick, "APPROVE", violations[-1]["messageId"])
    case = produce(environment, viewer, lives[1])
    undecided = produce(environment, viewer, lives[1])
    began = time.monotonic()
    for pending in (case, undecided):
        poll(lambda: outbox(environment, pending["taskId"])["status"] == "ACCEPTED")
        require(job(environment, pending["taskId"])["status"] == "QUEUED",
                "ACCEPTANCE_MUST_NOT_EXECUTE")
    require(environment.query("agent-db",
        "SELECT count(*) FROM agent_inbox.jobs WHERE status='RUNNING'") == "0",
        "WAITING_DEADLINE_OCCUPIED_EXECUTION_SLOT")
    def kick_result():
        raw = environment.query("db",
            "SELECT json_build_object('actionId',a.id,'status',a.status,"
            "'executions',(SELECT count(*) FROM guard.live_kicks k WHERE k.action_id=a.id),"
            "'startedAfterDecisionSeconds',"
            "extract(epoch FROM (a.result->>'startedAt')::timestamptz-d.decided_at)) "
            "FROM guard.live_actions a JOIN guard.decisions d ON d.id=a.decision_id "
            f"WHERE a.decision_id='{UUID(approval)}' AND a.kind='KICK'")
        return json.loads(raw) if raw else None
    pending_kick = kick_result()
    # An approval awaiting Agent revalidation has no action until BE takes over.
    require(pending_kick is None or (
            pending_kick["status"] == "PENDING" and pending_kick["executions"] == 0),
            "KICK_HANDOFF_BEFORE_DEADLINE")
    def fallback_review():
        rows = request(operator, "GET", f"/api/reviews?liveId={lives[1]}&kind=CONTEXT")["data"]
        return next((row for row in rows if row["caseId"] == case["caseId"]), None)
    time.sleep(2)
    require(not fallback_review(), "FALLBACK_BEFORE_DEADLINE")
    review = poll(fallback_review, timeout=210)
    waited = time.monotonic() - began
    detail = request(operator, "GET", f"/api/cases/{case['caseId']}")["data"]
    fallback_delay = float(environment.query("db",
        "SELECT extract(epoch FROM r.created_at-c.created_at) FROM guard.reviews r "
        "JOIN guard.cases c ON c.id=r.case_id "
        f"WHERE c.id='{UUID(case['caseId'])}' AND r.kind='CONTEXT'"))
    require(fallback_delay >= 180 and not detail["findings"],
            "DEFAULT_DEADLINE_OR_NO_AI_NOT_PROVEN")
    undecided_review = poll(lambda: next((row for row in request(operator, "GET",
        f"/api/reviews?liveId={lives[1]}&kind=CONTEXT")["data"]
        if row["caseId"] == undecided["caseId"]), None))
    require(not request(operator, "GET", f"/api/cases/{undecided['caseId']}")["data"]["findings"],
            "SECOND_FALLBACK_ALREADY_HAS_AI")
    handed_off = poll(lambda: value if (value := kick_result())
                      and value["status"] == "SUCCEEDED" else None, timeout=30)
    require(handed_off["executions"] == 1
            and handed_off["startedAfterDecisionSeconds"] >= 180,
            "KICK_HANDOFF_DEADLINE_OR_SINGLE_EFFECT_NOT_PROVEN")
    decide(operator, review, "CONFIRM_NORMAL", case["messageId"])
    environment.compose("start", "worker-1", "worker-2")
    outcome = wait_job(environment, case["taskId"], {"FAILED", "UNKNOWN", "NEEDS_REVIEW"})
    detail = request(operator, "GET", f"/api/cases/{case['caseId']}")["data"]
    require(detail["confirmationStatus"] == "CONFIRMED_NORMAL" and not detail["findings"],
            "LATE_AGENT_CHANGED_OPERATOR_RESULT")
    require(bool(outcome["code"]), "LATE_AGENT_NEEDS_EXPLICIT_CODE")
    kick_resume = wait_job(environment, violations[-1]["taskId"],
                           {"SUCCEEDED", "NEEDS_REVIEW", "UNKNOWN"}, decision_id=approval)
    require(kick_result() == handed_off, "LATE_KICK_WORKER_CHANGED_HANDOFF_RESULT")
    late_analysis = wait_job(environment, undecided["taskId"], "WAITING_REVIEW")
    late_detail = request(operator, "GET", f"/api/cases/{undecided['caseId']}")["data"]
    require(bool(late_detail["findings"]) and
            all(row["verdict"] == "NO_VIOLATION" for row in late_detail["findings"]),
            "LATE_AI_BEFORE_DECISION_NOT_ATTACHED")
    still_pending = select_review(operator, lives[1], undecided["caseId"], "CONTEXT")
    require(still_pending["reviewId"] == undecided_review["reviewId"]
            and single_count(environment, undecided)["contextReviews"] == 1,
            "LATE_AI_DUPLICATED_FALLBACK_REVIEW")
    final_decision = decide(operator, still_pending, "CONFIRM_NORMAL", undecided["messageId"])
    late_resume = wait_job(environment, undecided["taskId"], "SUCCEEDED",
                           decision_id=final_decision)
    return {"case": case, "wallWaitSeconds": round(waited, 3),
            "fallbackAfterCaseSeconds": fallback_delay,
            "outboxAcceptedBeforeExecution": True, "fallbackHadAi": False,
            "lateAgent": outcome, "confirmationStatus": detail["confirmationStatus"],
            "kickHandoff": handed_off, "lateKickResume": kick_resume,
            "kickEffectUnchangedAfterLateResume": True,
            "lateBeforeDecision": {"case": undecided, "analysis": late_analysis,
                                   "resume": late_resume, "reusedFallbackReview": True}}


SUITES = {"receipt": receipt_loss, "race": early_resume, "stale": stale_worker,
          "load": load, "deadline": deadlines}


def verify(environment, report):
    started = time.monotonic()
    report.update(mode="controlled-real-spring-remaining96", realModelExecuted=False,
                  suite=environment.args.suite, transport=environment.args.transport,
                  beScenario=environment.args.be_scenario, provenance=provenance(environment))
    for worker in ("worker-1", "worker-2"):
        poll(lambda: environment.worker_ready(worker), timeout=40)
    with ExitStack() as stack:
        clients = []
        for username in ("viewer", "moderator"):
            client = stack.enter_context(httpx.Client(
                base_url=environment.url("backend", 8080), timeout=15, trust_env=False))
            request(client, "GET", "/api/auth/me", expected=401)
            request(client, "POST", "/api/auth/login",
                    body={"username": username, "password": "liveguard-local"})
            clients.append(client)
        viewer, operator = clients
        fixture = stack.enter_context(httpx.Client(
            base_url=environment.url("controlled-fixture", 8090), timeout=15, trust_env=False,
            headers={"Authorization": "Bearer " + environment.values["MODEL_API_KEY"]}))
        lives = [str(UUID(request(viewer, "GET",
            f"/api/channels/20000000-0000-4000-8000-{index:012d}/lives/current")["data"]["liveId"]))
            for index in (1, 2)]
        require(environment.query("db", "SELECT count(*) FROM platform.messages") == "0",
                "FRESH_JOINT_PROJECT_REQUIRED")
        report["result"] = SUITES[environment.args.suite](
            environment, viewer, operator, fixture, lives)
        report["fixture"] = metrics(fixture)
    report.update(status="PASS", elapsedSeconds=round(time.monotonic() - started, 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--be-repo", type=Path, default=ROOT.parent / "liveguard-be")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--suite", choices=SUITES, required=True)
    parser.add_argument("--transport", choices=["db", "rabbit"], default="db")
    parser.add_argument("--be-scenario", choices=["normal", "fallback"], default="normal")
    parser.add_argument("--input-count", type=int, default=24)
    parser.add_argument("--load-pattern", choices=["burst", "steady"], default="burst")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    args.action_loss = False
    args.extended_faults = True
    report = {"status": "FAILED", "startedAt": datetime.now(timezone.utc).isoformat()}
    try:
        require(not args.report.exists(), "REPORT_ALREADY_EXISTS")
        environment = JointEnvironment(args)
        verify(environment, report)
        print("JOINT96_REMAINING_PASS")
        return 0
    except (JointError, httpx.HTTPError, OSError, ValueError, KeyError,
            subprocess.SubprocessError) as error:
        report["errorCode"] = str(error) if isinstance(error, JointError) else "JOINT96_REMAINING_UNAVAILABLE"
        print("JOINT96_REMAINING_FAILED: " + report["errorCode"])
        return 1
    finally:
        # Do not overwrite prior evidence even when preflight refuses a rerun.
        if not args.report.exists():
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())

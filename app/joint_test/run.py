"""Reuse BE #76 Compose; do not invent a second joint deployment or business DB."""
import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from uuid import UUID, uuid4

import httpx

from app.clients.business_mcp import AgentToolError, BusinessMcpReadClient, BusinessMcpSettings


ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = [
    ("context-normal", "NORMAL", "CONFIRM_NORMAL"),
    ("context-defer-final", "DEFER", "CONFIRM_NORMAL"),
    ("context-violation-1", "VIOLATION", "CONFIRM_VIOLATION"),
    ("context-violation-2", "VIOLATION", "CONFIRM_VIOLATION"),
    ("context-violation-3-kick", "VIOLATION", "CONFIRM_VIOLATION"),
]


class JointError(Exception):
    """Only a bounded code, never subprocess output or HTTP response text."""


def checked(command, *, env=None, cwd=None, timeout=120):
    result = subprocess.run(command, env=env, cwd=cwd, timeout=timeout,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        stage = "COMPOSE" if command[:2] == ["docker", "compose"] else "SUBPROCESS"
        raise JointError(stage + "_COMMAND_FAILED")
    return result.stdout.strip()


def read_env(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines()
                if line and not line.startswith("#"))


class JointEnvironment:
    def __init__(self, args):
        self.args = args
        self.be = args.be_repo.resolve()
        self.setup = self.be / "deploy/integration"
        self.values = read_env(args.env_file)
        if (not args.project.startswith("liveguard-joint96-")
                or self.values.get("MODEL_PROVIDER") != "openai-compatible"
                or self.values.get("MODEL_BASE_URL") != "http://controlled-fixture:8090/v1"
                or self.values.get("RAG_MCP_URL") != "http://controlled-fixture:8090/mcp"):
            raise JointError("ISOLATED_CONTROLLED_ENVIRONMENT_REQUIRED")
        self.environment = {**os.environ, **read_env(self.setup / "versions.env"), **self.values}

    def compose(self, *arguments, timeout=120):
        scenario = getattr(self.args, "be_scenario", "normal")
        transport = getattr(self.args, "transport", "db")
        if scenario not in {"normal", "fallback"} or transport not in {"db", "rabbit"}:
            raise JointError("INVALID_JOINT_COMPOSITION")
        files = [self.setup / "compose.yaml", self.setup / f"{scenario}.yaml",
                 ROOT / "deploy/joint-controlled.yaml"]
        if transport == "rabbit":
            files.append(self.setup / "rabbit.yaml")
        if self.args.action_loss:
            files.append(ROOT / "deploy/joint-action-loss.yaml")
        if getattr(self.args, "extended_faults", False):
            files.append(ROOT / "deploy/joint-extended-faults.yaml")
        command = ["docker", "compose", "--project-name", self.args.project,
                   "--env-file", str(self.setup / "versions.env"),
                   "--env-file", str(self.args.env_file.resolve())]
        for file in files:
            command += ["-f", str(file)]
        return checked(command + list(arguments), env=self.environment, timeout=timeout)

    def query(self, service, sql):
        user = "agent" if service == "agent-db" else "liveguard"
        # Every query is a fixed SELECT; all runtime identifiers are UUID-validated.
        if not sql.lstrip().startswith("SELECT "):
            raise JointError("READ_ONLY_QUERY_REQUIRED")
        return self.compose("exec", "-T", service, "psql", "-U", user, "-d", user,
                            "-At", "-v", "ON_ERROR_STOP=1", "-c", sql)

    def url(self, service, port):
        return "http://" + self.compose("port", service, str(port))

    def worker_ready(self, service):
        container = self.compose("ps", "-q", service)
        if not container:
            return False
        running = checked(["docker", "inspect", "--format", "{{.State.Running}}", container])
        started = checked(["docker", "inspect", "--format", "{{.State.StartedAt}}", container])
        logs = checked(["docker", "logs", "--since", started, container])
        return running == "true" and "WORKER_READY" in logs.splitlines()

    def service_ip(self, service):
        container = self.compose("ps", "-q", service)
        return checked(["docker", "inspect", "--format",
                        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container])


def request(client, method, path, *, body=None, expected=200):
    headers = {}
    if method == "POST" and client.cookies.get("XSRF-TOKEN"):
        headers["X-XSRF-TOKEN"] = client.cookies["XSRF-TOKEN"]
    response = client.request(method, path, json=body, headers=headers)
    if response.status_code != expected:
        raise JointError(f"HTTP_{response.status_code}_EXPECTED_{expected}")
    return response.json()


def poll(function, *, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = function()
        if value:
            return value
        time.sleep(0.5)
    raise JointError("POLL_DEADLINE_EXCEEDED")


def job(environment, task_id, *, decision_id=None):
    condition = f"t.task_id='{UUID(task_id)}'"
    if decision_id:
        condition += f" AND operation_id='resume:{UUID(decision_id)}'"
    raw = environment.query("agent-db",
        "SELECT json_build_object('operationId',operation_id,'status',status,'code',code,"
        "'workerId',last_worker_id,'attempts',attempts,'modelCalls',model_calls,"
        "'createdAt',j.created_at,'updatedAt',j.updated_at,"
        "'waitSeconds',extract(epoch FROM admitted_at-j.created_at),"
        "'lastAttemptSeconds',extract(epoch FROM j.updated_at-admitted_at)) FROM agent_inbox.jobs j "
        "JOIN agent_inbox.threads t ON t.thread_id=j.thread_id "
        f"WHERE {condition} ORDER BY sequence DESC LIMIT 1")
    return json.loads(raw) if raw else None


def wait_job(environment, task_id, expected, *, decision_id=None):
    allowed = {expected} if isinstance(expected, str) else set(expected)
    def observe():
        value = job(environment, task_id, decision_id=decision_id)
        if not value:
            return None
        if value["status"] in {"FAILED", "NEEDS_REVIEW", "UNKNOWN"} and value["status"] not in allowed:
            raise JointError("AGENT_" + value["status"] + "_" + (value["code"] or "NO_CODE"))
        return value if value["status"] in allowed else None
    return poll(observe)


def select_review(operator, live_id, case_id, kind):
    reviews = request(operator, "GET", f"/api/reviews?liveId={UUID(live_id)}&kind={kind}")["data"]
    matching = [review for review in reviews if review["caseId"] == case_id and review["status"] == "PENDING"]
    if len(matching) != 1:
        raise JointError("ONE_PENDING_REVIEW_REQUIRED")
    return matching[0]


def decide(operator, review, decision, message_id):
    body = {"clientRequestId": str(uuid4()), "expectedReviewVersion": review["version"],
            "decision": decision, "reason": "Synthetic joint integration operator decision."}
    if decision == "CONFIRM_VIOLATION":
        body.update(categories=["HARASSMENT"], evidenceMessageIds=[message_id])
    result = request(operator, "POST",
        f"/api/{review['kind'].lower()}-reviews/{review['reviewId']}/decision",
        body=body, expected=202)["data"]
    return str(UUID(result["decisionId"]))


def provenance(environment):
    def image(value):
        raw = checked(["docker", "image", "inspect", "--format",
                       '{{json .Id}}', value])
        label = checked(["docker", "image", "inspect", "--format",
                         '{{index .Config.Labels "liveguard.test.source-sha256"}}', value])
        return {"tag": value, "id": json.loads(raw), "sourceFingerprint": label}
    return {
        "agentRevision": checked(["git", "rev-parse", "HEAD"], cwd=ROOT),
        "agentDirty": bool(checked(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT)),
        "backendRevision": environment.environment["BE_REVISION"],
        "agentImage": image(environment.environment["AGENT_IMAGE"]),
        "backendImage": image(environment.environment["BE_IMAGE"]),
        "backendTestAssetsRevision": checked(["git", "rev-parse", "HEAD"], cwd=environment.be),
        "policySha256": hashlib.sha256(
            (ROOT / "app/rag_mcp/policies/policy-v3.1.md").read_bytes()).hexdigest(),
    }


def source_fingerprint():
    files = [ROOT / name for name in ("Dockerfile", "pyproject.toml", "uv.lock")]
    for folder in ("app", "prompts"):
        files += [path for path in (ROOT / folder).rglob("*") if path.is_file()
                  and "__pycache__" not in path.parts and path.suffix != ".pyc"
                  and not path.is_relative_to(ROOT / "app/business_mcp/liveguard-agent")]
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def execute(environment, report):
    started = time.monotonic()
    report.update(mode="controlled-model-real-spring", realModelExecuted=False,
                  backendReplaced=False,
                  deliveryTransport=getattr(environment.args, "transport", "db"),
                  provenance=provenance(environment), results=[],
                  limits={"scenarioDeadlineSeconds": 90,
                          "normalBeDeadline": "3m" if getattr(environment.args, "be_scenario", "normal") == "fallback" else "15m"})
    for worker in ("worker-1", "worker-2"):
        poll(lambda: environment.worker_ready(worker), timeout=40)
    with ExitStack() as stack:
        backend_url = environment.url("backend", 8080)
        viewer = stack.enter_context(httpx.Client(base_url=backend_url, trust_env=False, timeout=15))
        operator = stack.enter_context(httpx.Client(base_url=backend_url, trust_env=False, timeout=15))
        fixture = stack.enter_context(httpx.Client(
            base_url=environment.url("controlled-fixture", 8090), trust_env=False, timeout=15,
            headers={"Authorization": "Bearer " + environment.values["MODEL_API_KEY"]}))
        for client, username in ((viewer, "viewer"), (operator, "moderator")):
            request(client, "GET", "/api/auth/me", expected=401)
            request(client, "POST", "/api/auth/login",
                    body={"username": username, "password": "liveguard-local"})
        live = request(viewer, "GET",
            "/api/channels/20000000-0000-4000-8000-000000000001/lives/current")["data"]["liveId"]
        existing = environment.query("db",
            f"SELECT count(*) FROM platform.messages WHERE live_id='{UUID(live)}'::uuid")
        if existing != "0":
            raise JointError("UNUSED_SEEDED_LIVE_REQUIRED")
        if environment.args.model_fault:
            request(fixture, "POST", "/control", body={"fault": environment.args.model_fault})
        cases = SCENARIOS if environment.args.suite == "full" else SCENARIOS[:1]
        # Submit all inputs before any sanction; do not bypass a real BE timeout later.
        for name, marker, decision in cases:
            result = {"scenario": name, "decision": decision, "startedAt": time.monotonic()}
            report["results"].append(result)
            text = f"[JOINT96:{marker}] 공격 표현 '꺼져'를 분석하는 합성 인용문 {uuid4().hex[:8]}"
            message = request(viewer, "POST", f"/api/lives/{live}/messages",
                body={"clientMessageId": str(uuid4()), "text": text}, expected=201)["data"]
            message_id = str(UUID(message["messageId"]))
            raw = environment.query("db",
                "SELECT json_build_object('caseId',s.case_id,'taskId',s.task_id,'threadId',s.thread_id) "
                "FROM guard.live_case_state s JOIN platform.messages m ON m.case_id=s.case_id "
                f"WHERE m.id='{message_id}'::uuid")
            if not raw:
                raise JointError("MESSAGE_DID_NOT_CREATE_REAL_BE_CASE")
            result.update(json.loads(raw), messageId=message_id, liveId=live)
        for result in report["results"]:
            task, case_id = result["taskId"], result["caseId"]
            analysis = wait_job(environment, task, "WAITING_REVIEW")
            result["analysis"] = analysis
            result["outboxStatus"] = poll(lambda: "ACCEPTED" if environment.query("db",
                f"SELECT status FROM guard.agent_task_outbox WHERE operation_id='run:{UUID(task)}'"
            ) == "ACCEPTED" else None)
            detail = request(operator, "GET", f"/api/cases/{case_id}")["data"]
            findings = detail.get("findings", [])
            expected_verdict = {"CONFIRM_NORMAL": "NO_VIOLATION",
                                "CONFIRM_VIOLATION": "VIOLATION"}[result["decision"]]
            if result["scenario"] == "context-defer-final":
                expected_verdict = "NEEDS_REVIEW"
            if not findings or any(finding["verdict"] != expected_verdict for finding in findings):
                raise JointError("STORED_CONTROLLED_JUDGMENT_MISMATCH")
            result["storedAiVerdict"] = expected_verdict
            review = select_review(operator, live, case_id, "CONTEXT")
            if result["scenario"] == "context-defer-final":
                deferred = decide(operator, review, "DEFER", result["messageId"])
                result["defer"] = wait_job(environment, task, "WAITING_REVIEW", decision_id=deferred)
                review = select_review(operator, live, case_id, "CONTEXT")
            stopped = None
            try:
                if environment.args.handoff:
                    owner = job(environment, task)["workerId"]
                    stopped = {"integration-worker-1": "worker-1",
                               "integration-worker-2": "worker-2"}.get(owner)
                    if stopped is None:
                        raise JointError("EXPECTED_WORKER_OWNER_REQUIRED")
                    environment.compose("stop", stopped, timeout=100)
                    result["previousWorkerId"] = owner
                decision = decide(operator, review, result["decision"], result["messageId"])
                expected = "WAITING_REVIEW" if result["scenario"].endswith("-kick") else "SUCCEEDED"
                result["contextResume"] = wait_job(environment, task, expected, decision_id=decision)
                if stopped and result["contextResume"]["workerId"] == result["previousWorkerId"]:
                    raise JointError("WORKER_HANDOFF_NOT_PROVEN")
            finally:
                if stopped:
                    environment.compose("start", stopped)
                    poll(lambda: environment.worker_ready(stopped), timeout=40)
            detail = request(operator, "GET", f"/api/cases/{case_id}")["data"]
            expected_confirmation = ("CONFIRMED_NORMAL" if result["decision"] == "CONFIRM_NORMAL"
                                     else "CONFIRMED_VIOLATION")
            if detail["confirmationStatus"] != expected_confirmation:
                raise JointError("BE_CONFIRMATION_MISMATCH")
            result["confirmationStatus"] = detail["confirmationStatus"]
            if result["scenario"].endswith("-kick"):
                kick = select_review(operator, live, case_id, "KICK")
                if len(set(kick["basisCaseIds"])) < 3:
                    raise JointError("KICK_REQUIRES_THREE_REAL_CASES")
                if environment.args.action_loss:
                    request(fixture, "POST", "/control", body={"fault": "action-response-loss"})
                kick_decision = decide(operator, kick, "APPROVE", result["messageId"])
                expected = {"UNKNOWN", "NEEDS_REVIEW"} if environment.args.action_loss else "SUCCEEDED"
                result["kickResume"] = wait_job(environment, task, expected, decision_id=kick_decision)
                if (environment.args.action_loss and result["kickResume"]["status"] == "NEEDS_REVIEW"
                        and result["kickResume"]["code"] != "ACTION_CONFIRMED_REQUIRES_REVIEW"):
                    raise JointError("UNEXPECTED_MANUAL_REVIEW_REASON")
                raw = environment.query("db",
                    "SELECT json_build_object('actionId',id,'status',status) FROM guard.live_actions "
                    f"WHERE decision_id='{UUID(kick_decision)}'::uuid AND kind='KICK'")
                action = json.loads(raw)
                result["kickAction"] = action
                if action["status"] not in {"APPLIED", "SUCCEEDED"}:
                    raise JointError("BE_KICK_NOT_APPLIED")
                client = BusinessMcpReadClient(BusinessMcpSettings(
                    environment.url("agent-api", 8002) + "/mcp",
                    environment.values["BUSINESS_MCP_AGENT_TOKEN"]))
                stopped_mcp = None
                try:
                    if environment.args.action_loss:
                        metrics = request(fixture, "GET", "/control")
                        serving_ip = metrics["executions"][0]["mcpIp"]
                        stopped_mcp = next(service for service in ("mcp-1", "mcp-2")
                                           if environment.service_ip(service) == serving_ip)
                        environment.compose("stop", stopped_mcp, timeout=40)

                    def observe():
                        try:
                            return asyncio.run(client.call("get_action_status", {
                                "actionId": action["actionId"], "traceId": str(uuid4()),
                            }))
                        except AgentToolError as error:
                            if error.retryable:
                                return None
                            raise
                    observed = poll(observe, timeout=20)
                    if stopped_mcp:
                        reads = request(fixture, "GET", "/control")["actionReads"]
                        if not reads or reads[-1]["mcpIp"] == serving_ip:
                            raise JointError("OTHER_MCP_OBSERVATION_NOT_PROVEN")
                        result["actionObservedThroughOtherMcp"] = True
                finally:
                    if stopped_mcp:
                        environment.compose("start", stopped_mcp)
                if observed["data"]["status"] not in {"APPLIED", "SUCCEEDED"}:
                    raise JointError("ACTION_STATUS_NOT_CONFIRMED")
                result["observedActionStatus"] = observed["data"]["status"]
                action_count = environment.query("db",
                    "SELECT count(*) FROM guard.live_actions "
                    f"WHERE decision_id='{UUID(kick_decision)}'::uuid AND kind='KICK'")
                if action_count != "1":
                    raise JointError("ONE_KICK_ACTION_REQUIRED")
                result["kickActionCount"] = int(action_count)
            result["elapsedSeconds"] = round(time.monotonic() - result.pop("startedAt"), 3)
            result["status"] = "PASS"
        report["fixture"] = request(fixture, "GET", "/control")
        if environment.args.model_fault:
            counter = "rateLimits" if environment.args.model_fault == "model429" else "timeouts"
            if report["fixture"][counter] != 1:
                raise JointError("MODEL_FAULT_NOT_OBSERVED")
        if environment.args.action_loss:
            if report["fixture"]["lostResponses"] != 1 or len(report["fixture"]["executions"]) != 1:
                raise JointError("ONE_LOST_RESPONSE_AND_EXECUTION_REQUIRED")
    report["elapsedSeconds"] = round(time.monotonic() - started, 3)
    report["status"] = "PASS"


def init(args):
    if args.env_file.exists():
        raise JointError("ENV_FILE_ALREADY_EXISTS")
    versions = read_env(args.be_repo / "deploy/integration/versions.env")
    values = {key: secrets.token_hex(24) for key in (
        "BE_DB_PASSWORD", "AGENT_DB_PASSWORD", "INTERNAL_SERVICE_TOKEN", "AGENT_SERVICE_TOKEN",
        "BUSINESS_MCP_AGENT_TOKEN", "RAG_MCP_AGENT_TOKEN", "MODEL_API_KEY", "RABBITMQ_PASSWORD")}
    revision = checked(["git", "rev-parse", "HEAD"], cwd=ROOT)
    values.update(
        AGENT_IMAGE=f"liveguard-agent:joint96-{revision[:12]}-{secrets.token_hex(3)}",
        BE_IMAGE=versions["BE_IMAGE"], MODEL_PROVIDER="openai-compatible",
        MODEL_BASE_URL="http://controlled-fixture:8090/v1", MODEL_NAME="joint-controlled-v1",
        MODEL_QUOTA_GROUP="joint96-controlled", RAG_MCP_URL="http://controlled-fixture:8090/mcp",
        BE_PORT="18080", AGENT_PORT="18001", BUSINESS_MCP_PORT="18002", JOINT_FIXTURE_PORT="18004")
    args.env_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(args.env_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "build", "up", "run", "down", "fault", "observe"])
    parser.add_argument("--be-repo", type=Path, default=ROOT.parent / "liveguard-be")
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--project", default="liveguard-joint96-local")
    parser.add_argument("--suite", choices=["smoke", "full"], default="smoke")
    parser.add_argument("--handoff", action="store_true")
    parser.add_argument("--action-loss", action="store_true")
    parser.add_argument("--extended-faults", action="store_true")
    parser.add_argument("--transport", choices=["db", "rabbit"], default="db")
    parser.add_argument("--be-scenario", choices=["normal", "fallback"], default="normal")
    parser.add_argument("--model-fault", choices=["model429", "model-timeout"])
    parser.add_argument("--target", choices=["worker-1", "worker-2", "mcp-1", "mcp-2"])
    parser.add_argument("--fault", choices=["kill", "stop", "start"])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = {"status": "BLOCKED", "startedAt": datetime.now(timezone.utc).isoformat()}
    try:
        if args.command == "run" and args.action_loss and args.suite != "full":
            raise JointError("ACTION_LOSS_REQUIRES_FULL_SUITE")
        if args.command == "init":
            init(args)
        else:
            environment = JointEnvironment(args)
            if args.command == "build":
                revision = checked(["git", "rev-parse", "HEAD"], cwd=ROOT)
                checked(["docker", "build", "--label", f"org.opencontainers.image.revision={revision}",
                         "--label", "liveguard.test.source=worktree",
                         "--label", f"liveguard.test.source-sha256={source_fingerprint()}",
                         "-t", environment.values["AGENT_IMAGE"], "."], cwd=ROOT, timeout=600)
            elif args.command == "up":
                environment.compose("config", "--quiet")
                environment.compose("up", "-d", "--wait", "--wait-timeout", "240", timeout=300)
            elif args.command == "down":
                # Never delete DB evidence automatically. Explicit owner cleanup is documented.
                environment.compose("down")
            elif args.command == "fault":
                if not args.target or not args.fault:
                    raise JointError("FAULT_AND_TARGET_REQUIRED")
                arguments = ["kill", "-s", "SIGKILL"] if args.fault == "kill" else [args.fault]
                environment.compose(*arguments, args.target)
            elif args.command == "observe":
                # Intentionally no payload, model inputs, exception body, DSN or lease token.
                print(environment.query("agent-db",
                    "SELECT coalesce(json_agg(row_to_json(s)),'[]'::json) FROM "
                    "(SELECT operation_id,status,code,last_worker_id,attempts,model_calls,"
                    "created_at,updated_at FROM agent_inbox.jobs ORDER BY sequence DESC LIMIT 50) s"))
            else:
                if not args.report:
                    raise JointError("REPORT_PATH_REQUIRED")
                if args.report.exists():
                    raise JointError("REPORT_ALREADY_EXISTS")
                execute(environment, report)
        print("JOINT96_" + ("PASS" if args.command != "run" else report["status"]))
        return 0
    except (JointError, httpx.HTTPError, OSError, ValueError, KeyError,
            subprocess.SubprocessError) as error:
        report["errorCode"] = str(error) if isinstance(error, JointError) else "JOINT_ENVIRONMENT_UNAVAILABLE"
        report["status"] = "FAILED"
        print("JOINT96_FAILED: " + report["errorCode"])
        return 1
    finally:
        if args.command == "run" and args.report and not args.report.exists():
            args.report.parent.mkdir(parents=True, exist_ok=True)
            with args.report.open("x") as file:
                file.write(json.dumps(report, indent=2, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())

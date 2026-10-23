import asyncio
from argparse import Namespace
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from mcp import Client
from mcp.server import MCPServer
from langgraph.checkpoint.memory import InMemorySaver

from app.case_loader import load_case_data
from app.harness.checkpoint import state_serializer
from app.integration.workflow import build_moderation_graph
from app.investigation.model import ModelTurn, ToolCall
from app.joint_test.fixture import controlled_call, create_app, PolicyFixture, NoEmbedding
from app.joint_test.run import init, JointEnvironment, JointError, wait_job, poll, provenance
from app.joint_test.remaining import receipt_loss, load, deadlines
from app.rag_mcp.tools import register_tools
from app.schemas import TaskEnvelope
from test_agent_read import FIXTURE, FixtureTools
from test_workflow_integration import Backend, LIVE, USER
from test_investigation import Policies


class ControlledTests(unittest.TestCase):
    def test_model_requires_only_synthetic_targets_and_no_invented_ids(self):
        for marker, verdict in (("NORMAL", "NO_VIOLATION"), ("VIOLATION", "VIOLATION"),
                                ("DEFER", "NEEDS_REVIEW")):
            target = {"messageId": str(uuid4()), "text": f"[JOINT96:{marker}] synthetic"}
            messages = [{"role": "user", "content": json.dumps({"targetMessages": [target]})}]
            self.assertEqual(controlled_call(messages), ("get_policy_section", {"policyId": "CP-3.2"}))
            messages.append({"role": "tool"})
            name, result = controlled_call(messages)
            self.assertEqual(name, "finish_investigation")
            self.assertEqual(result["messageFindings"][0]["verdict"], verdict)
            self.assertEqual(result["messageFindings"][0]["messageId"], target["messageId"])
        with self.assertRaises(ValueError):
            controlled_call([{"role": "user", "content": json.dumps(
                {"targetMessages": [{"messageId": str(uuid4()), "text": "real chat"}]})}])

    def test_fixture_requires_explicit_opt_in_and_authenticates(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            create_app()
        with patch.dict(os.environ, {
            "JOINT_TEST_ENABLED": "synthetic-only", "JOINT_TEST_TOKEN": "private-model-token",
            "RAG_MCP_AGENT_TOKEN": "private-rag-token",
        }):
            with TestClient(create_app()) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/control").status_code, 401)
                headers = {"Authorization": "Bearer private-model-token"}
                self.assertEqual(client.post("/control", headers=headers,
                                            json={"fault": "unknown"}).status_code, 400)
                self.assertEqual(client.post("/control", headers=headers,
                                            json={"release": "unknown"}).status_code, 400)
                client.post("/control", headers=headers, json={"fault": "model429"})
                message = {"role": "user", "content": json.dumps({"targetMessages": [
                    {"messageId": str(uuid4()), "text": "[JOINT96:NORMAL] synthetic"},
                ]})}
                for expected in (429, 200):
                    response = client.post("/v1/chat/completions", headers=headers,
                                           json={"messages": [message]})
                    self.assertEqual(response.status_code, expected)
                metrics = client.get("/control", headers=headers).json()
                self.assertEqual(metrics["rateLimits"], 1)
                self.assertNotIn("private-", json.dumps(metrics))

    def test_init_private_file_no_real_keys_and_isolation_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            setup = base / "be/deploy/integration"
            setup.mkdir(parents=True)
            (setup / "versions.env").write_text("BE_IMAGE=test-be:fixed\n")
            args = Namespace(env_file=base / "private.env", be_repo=base / "be",
                             project="liveguard-joint96-unit", action_loss=False)
            init(args)
            self.assertEqual(args.env_file.stat().st_mode & 0o777, 0o600)
            environment = JointEnvironment(args)
            self.assertEqual(environment.values["MODEL_BASE_URL"], "http://controlled-fixture:8090/v1")
            with self.assertRaises(JointError):
                init(args)
            args.project = "production"
            with self.assertRaises(JointError):
                JointEnvironment(args)

    def test_wait_job_reports_only_explicit_reconciliation_states(self):
        safe = {"status": "NEEDS_REVIEW", "code": "ACTION_CONFIRMED_REQUIRES_REVIEW"}
        with patch("app.joint_test.run.job", return_value=safe):
            self.assertEqual(wait_job(None, str(uuid4()), {"UNKNOWN", "NEEDS_REVIEW"}), safe)
            with self.assertRaises(JointError):
                wait_job(None, str(uuid4()), "SUCCEEDED")

    def test_provenance_uses_running_container_images_and_rejects_mixed_replicas(self):
        service_containers = {
            "controlled-fixture": ["fixture-container"],
            "agent-api": ["api-container"],
            "worker-1": ["worker-1-container"],
            "worker-2": ["worker-2-container"],
            "mcp-1": ["mcp-1-container"],
            "mcp-2": ["mcp-2-container"],
            "backend": ["backend-container"],
        }
        container_images = {
            container: "sha256:backend" if service == "backend" else "sha256:agent"
            for service, containers in service_containers.items()
            for container in containers
        }
        environment = MagicMock()
        environment.compose.side_effect = lambda *args: "\n".join(service_containers[args[-1]])
        environment.environment = {"BE_REVISION": "backend-revision"}
        environment.be = Path(".")

        def inspect(command, **kwargs):
            if command[:2] == ["git", "rev-parse"]:
                return "revision"
            if command[:2] == ["git", "status"]:
                return ""
            if command[:2] == ["docker", "inspect"]:
                return container_images[command[-1]]
            return "fingerprint-" + command[-1]

        with patch("app.joint_test.run.checked", side_effect=inspect) as checked_command:
            result = provenance(environment)
        self.assertEqual(set(result["services"]), set(service_containers))
        self.assertEqual(result["services"]["worker-1"][0]["id"], "sha256:agent")
        self.assertEqual(result["services"]["backend"][0]["id"], "sha256:backend")
        image_inspections = [call.args[0] for call in checked_command.call_args_list
                             if call.args[0][:3] == ["docker", "image", "inspect"]]
        self.assertTrue(image_inspections)
        self.assertTrue(all(command[-1].startswith("sha256:") for command in image_inspections))

        container_images["worker-2-container"] = "sha256:mixed"
        with patch("app.joint_test.run.checked", side_effect=inspect):
            with self.assertRaisesRegex(JointError, "MIXED_WORKER_IMAGES"):
                provenance(environment)

    def test_real_intake_proxy_loses_only_committed_receipt_and_preserves_auth(self):
        with patch.dict(os.environ, {
            "JOINT_TEST_ENABLED": "synthetic-only", "JOINT_TEST_TOKEN": "model-token",
            "RAG_MCP_AGENT_TOKEN": "rag-token", "AGENT_SERVICE_TOKEN": "agent-token",
        }):
            app = create_app()
            upstream = AsyncMock()
            upstream.request.side_effect = [
                httpx.Response(202, json={"operationId": "run:test", "duplicate": False}),
                httpx.Response(200, json={"operationId": "run:test", "state": "QUEUED"}),
                httpx.Response(503, headers={"Retry-After": "10"},
                               json={"detail": {"code": "INBOX_UNAVAILABLE"}}),
            ]
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=upstream)
            context.__aexit__ = AsyncMock()
            with patch("app.joint_test.fixture.httpx.AsyncClient", return_value=context):
                with TestClient(app, raise_server_exceptions=False) as client:
                    model = {"Authorization": "Bearer model-token"}
                    agent = {"Authorization": "Bearer agent-token", "X-Trace-Id": "trace"}
                    self.assertEqual(client.post("/agent/tasks/test/run", headers=model).status_code, 401)
                    client.post("/control", headers=model, json={"fault": "receipt-loss"})
                    broken = client.post("/agent/tasks/test/run", headers=agent, json={"kind": "ANALYZE"})
                    with self.assertRaises(ValueError):
                        broken.json()
                    self.assertEqual(client.get("/agent/tasks/test", headers=agent).status_code, 200)
                    unavailable = client.post("/agent/tasks/test/run", headers=agent)
                    self.assertEqual(unavailable.status_code, 503)
                    self.assertEqual(unavailable.headers["Retry-After"], "10")
                    observed = client.get("/control", headers=model).json()
            self.assertEqual(len(observed["receipts"]), 1)
            self.assertEqual(len(observed["receiptGets"]), 1)
            self.assertEqual(len(observed["intakePosts"]), 2)
            self.assertEqual([(row["taskId"], row["operation"])
                              for row in observed["intakePosts"]],
                             [("test", "run"), ("test", "run")])
            self.assertEqual(upstream.request.call_count, 3)
            headers = upstream.request.call_args_list[0].kwargs["headers"]
            self.assertEqual(headers["authorization"], "Bearer agent-token")
            self.assertEqual(headers["x-trace-id"], "trace")
            self.assertNotIn("token", json.dumps(observed))

    def test_new_compose_options_preserve_existing_callers(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            setup = base / "be/deploy/integration"
            setup.mkdir(parents=True)
            (setup / "versions.env").write_text("BE_IMAGE=test-be:fixed\n")
            args = Namespace(env_file=base / "private.env", be_repo=base / "be",
                             project="liveguard-joint96-unit", action_loss=False)
            init(args)
            environment = JointEnvironment(args)
            with patch("app.joint_test.run.checked", return_value="") as command:
                environment.compose("config", "--quiet")
                baseline = command.call_args.args[0]
                self.assertIn(str(setup.resolve() / "normal.yaml"), baseline)
                self.assertNotIn(str(setup.resolve() / "rabbit.yaml"), baseline)
                args.transport, args.be_scenario, args.extended_faults = "rabbit", "fallback", True
                environment.compose("config", "--quiet")
                extended = command.call_args.args[0]
                self.assertIn(str(setup.resolve() / "rabbit.yaml"), extended)
                self.assertIn(str(setup.resolve() / "fallback.yaml"), extended)
                self.assertTrue(any(item.endswith("joint-extended-faults.yaml") for item in extended))

    def test_model_gate_returns_only_after_explicit_release(self):
        with patch.dict(os.environ, {
            "JOINT_TEST_ENABLED": "synthetic-only", "JOINT_TEST_TOKEN": "model-token",
            "RAG_MCP_AGENT_TOKEN": "rag-token",
        }), TestClient(create_app()) as client:
            headers = {"Authorization": "Bearer model-token"}
            client.post("/control", headers=headers, json={"fault": "model-gate"})
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(client.post, "/v1/chat/completions", headers=headers,
                    json={"messages": [{"role": "user", "content": json.dumps({"targetMessages": [
                        {"messageId": str(uuid4()), "text": "[JOINT96:NORMAL] synthetic"},
                    ]})}]})
                try:
                    poll(lambda: client.get("/control", headers=headers).json()["modelBlocked"] == 1,
                         timeout=3)
                    self.assertFalse(pending.done())
                finally:
                    client.post("/control", headers=headers, json={"release": "model"})
                self.assertEqual(pending.result(timeout=3).status_code, 200)
            observed = client.get("/control", headers=headers).json()
            self.assertEqual((observed["modelActive"], observed["modelReturned"]), (0, 1))

    def test_receipt_suite_preserves_uncertainty_history_and_rejects_repost(self):
        case = {"taskId": str(uuid4())}
        operation = "run:" + case["taskId"]
        observed = {"receiptGets": [{"operationId": operation}],
                    "intakePosts": [{"taskId": case["taskId"], "operation": "run"}],
                    "receipts": [{"operationId": operation}]}
        with patch("app.joint_test.remaining.arm"), \
             patch("app.joint_test.remaining.produce", return_value=case), \
             patch("app.joint_test.remaining.metrics", return_value=observed), \
             patch("app.joint_test.remaining.poll", side_effect=lambda function, **kw: function()), \
             patch("app.joint_test.remaining.outbox", return_value={
                 "status": "ACCEPTED", "attempts": 2, "uncertain": True}), \
             patch("app.joint_test.remaining.wait_job", return_value={"status": "SUCCEEDED"}), \
             patch("app.joint_test.remaining.single_count", return_value={"jobs": 1}):
            result = receipt_loss(None, None, None, None, ["live"])
            self.assertEqual((result["intakePosts"], result["statusGets"]), (1, 1))
            observed["intakePosts"].append({"taskId": case["taskId"], "operation": "run"})
            with self.assertRaisesRegex(JointError, "MUST_NOT_REPOST"):
                receipt_loss(None, None, None, None, ["live"])

    def test_remaining_suites_reject_invalid_load_and_short_deadline_composition(self):
        environment = MagicMock()
        for count in (0, 7, 121):
            environment.args.input_count = count
            with self.assertRaisesRegex(JointError, "FINITE_LOAD"):
                load(environment, None, None, None, [])
        environment.args.be_scenario = "normal"
        with self.assertRaisesRegex(JointError, "DEFAULT_3M"):
            deadlines(environment, None, None, None, [])
        environment.compose.assert_not_called()


class ControlledGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_policy_is_real_versioned_text_and_resource(self):
        repository = PolicyFixture()
        server = MCPServer("controlled-test")
        register_tools(server, lambda: repository, NoEmbedding)
        async with Client(server) as client:
            result = await client.call_tool("get_policy_section", {
                "policyId": "CP-3.2", "policyVersion": "v3.1",
            })
            self.assertFalse(result.is_error)
            section = result.structured_content["data"]
            resource = await client.read_resource(section["resourceUri"])
            self.assertEqual(resource.contents[0].text, section["text"])
            self.assertEqual(section["category"], "HARASSMENT")

    async def test_controlled_model_finishes_genuine_investigation_not_manual_fallback(self):
        fixture = deepcopy(FIXTURE)
        fixture["case"].update(liveId=LIVE, userId=USER, handlingRoute="CONTEXT_REVIEW")
        for message in fixture["context"]["messages"]:
            message["visibility"] = "HELD"
            message["text"] = "[JOINT96:VIOLATION] synthetic quoted expression"
        business = FixtureTools(fixture)
        task = TaskEnvelope.model_validate(fixture["task"])
        state = await load_case_data(task, business)
        backend = Backend(state)
        policy = Policies()
        section = await PolicyFixture().get_section("CP-3.2", "v3.1")
        # This unit fixture case uses a separate policy label; real BE uses exact v3.1.
        policy.sections = [{"policyId": section["policy_id"], "title": section["title"],
                            "text": section["text"], "category": section["category"],
                            "version": state["policy_version"],
                            "resourceUri": f"policy://{state['policy_version']}/operating-policy/CP-3.2"}]

        class Model:
            async def complete(self, messages, tools, *, trace_id):
                name, arguments = controlled_call(messages)
                return ModelTurn([ToolCall(str(uuid4()), name, arguments)],
                                 {"inputTokens": 100, "outputTokens": 100})

        graph = build_moderation_graph(model=Model(), business=business, policy=policy,
            backend=backend, checkpointer=InMemorySaver(serde=state_serializer()))
        result = await graph.ainvoke({"task": task}, {"configurable": {"thread_id": task.thread_id}})
        self.assertEqual(result["message_findings"][0].verdict, "VIOLATION")
        self.assertNotIn("investigation_error", result)
        self.assertEqual(result["harness_budget"]["modelCalls"], 2)

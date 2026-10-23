import asyncio
from argparse import Namespace
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from mcp import Client
from mcp.server import MCPServer
from langgraph.checkpoint.memory import InMemorySaver

from app.case_loader import load_case_data
from app.harness.checkpoint import state_serializer
from app.integration.workflow import build_moderation_graph
from app.investigation.model import ModelTurn, ToolCall
from app.joint_test.fixture import controlled_call, create_app, PolicyFixture, NoEmbedding
from app.joint_test.run import init, JointEnvironment, JointError, wait_job
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

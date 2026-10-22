"""BE #45 transport/model-input regression; scripted output is not LLM quality evidence."""

from copy import deepcopy
import json
import unittest

import httpx

from app.business_mcp.backend import BackendApi
from app.business_mcp.config import Settings
from app.case_loader import load_case_data
from app.clients.business_mcp import BusinessMcpReadClient, BusinessMcpSettings
from app.investigation.model import ToolCall
from app.investigation.runner import run_investigation
from app.schemas import TaskEnvelope
from test_agent_read import FIXTURE, FixtureTools, TOKEN, local_mcp
from test_investigation import Policies, ScriptedModel, finding, finish


class UnknownRoleContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_mcp_preserves_unknown_and_existing_roles(self):
        settings = Settings("http://backend.test", "service-token", TOKEN, "127.0.0.1", 8002)
        current = {}
        paths = []

        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, json={"data": current})

        task = FIXTURE["task"]
        async with httpx.AsyncClient(base_url=settings.backend_base_url,
                                     transport=httpx.MockTransport(handler)) as http:
            backend = BackendApi(settings, http)
            async with local_mcp(backend) as url:
                client = BusinessMcpReadClient(BusinessMcpSettings(url, TOKEN))
                for role in ("UNKNOWN", "VIEWER", "STREAMER", "OFFICIAL_STAFF", "MODERATOR"):
                    current = {"authorRole": role,
                               "isOfficialOperator": role in {"STREAMER", "OFFICIAL_STAFF", "MODERATOR"}}
                    for tool in ("get_stream_info", "get_official_links"):
                        with self.subTest(role=role, tool=tool):
                            result = await client.call(tool, {"caseId": task["caseId"],
                                                              "traceId": task["traceId"]})
                            self.assertEqual(result["data"], current)
                            self.assertEqual(result["traceId"], task["traceId"])
                            self.assertEqual(paths[-1], f'/internal/cases/{task["caseId"]}/channel-info')

    async def test_role_and_security_facts_remain_separate_in_model_input(self):
        state = await load_case_data(TaskEnvelope.model_validate(FIXTURE["task"]),
                                     FixtureTools(deepcopy(FIXTURE)))
        channel = {"authorRole": "UNKNOWN", "isOfficialOperator": False}
        original = deepcopy(state)
        # Current membership and verified facts can describe different times/subjects.
        # Do not infer a conflict, a viewer role or a verdict from these fields alone.
        for value in ("TRUE", "FALSE", "UNKNOWN"):
            facts = {"facts": [{"name": "isOfficialContact", "value": value,
                                "sourceRefs": [] if value == "UNKNOWN" else ["fixture:verified-contact"]}]}

            class Business:
                async def call(self, name, arguments):
                    data = channel if name == "get_stream_info" else facts
                    return {"data": deepcopy(data), "traceId": arguments["traceId"]}

            with self.subTest(securityFact=value):
                uncertain = {**finding(state, "NEEDS_REVIEW"), "policyIds": [],
                             "missingFacts": ["사건 당시 역할과 공식 여부의 검증 근거 필요"]}
                model = ScriptedModel([ToolCall("role", "get_stream_info", {}),
                                       ToolCall("facts", "get_security_facts", {}),
                                       finish([uncertain], n="finish")])
                result = await run_investigation(state, model=model, business=Business(), policy=Policies())
                delivered = [json.loads(m["content"])["data"]
                             for m in model.inputs[-1][0] if m["role"] == "tool"]
                self.assertEqual(delivered, [channel, facts])
                self.assertEqual(state, original)
                self.assertEqual(result["message_findings"][0].verdict, "NEEDS_REVIEW")
                self.assertNotIn("action_id", result)

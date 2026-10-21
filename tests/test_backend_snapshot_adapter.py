"""BE InternalCase targets[] compatibility without inventing missing scope or visibility."""
from copy import deepcopy
import unittest
from uuid import UUID

from app.case_loader import CaseSnapshot, load_case_data
from app.clients.business_mcp import AgentToolError
from app.schemas import TaskEnvelope
from test_agent_read import FIXTURE, FixtureBackend, FixtureTools, local_mcp
from app.clients.business_mcp import BusinessMcpReadClient, BusinessMcpSettings
from test_agent_read import TOKEN


def backend_fixture():
    fixture = deepcopy(FIXTURE)
    case = fixture["case"]
    ids = case.pop("targetMessageIds")
    versions = case.pop("targetVersions")
    case["targets"] = [{"messageId": mid, "visibility": "PUBLIC", "version": versions[mid]} for mid in ids]
    case["channelId"] = "00000000-0000-4000-8000-000000000011"
    # BE masked messages deliberately lack visibility. It is not a substitute for context metadata.
    case["messages"] = [{k: v for k, v in message.items() if k != "visibility"}
                        for message in fixture["context"]["messages"]]
    return fixture


class BackendSnapshotAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_targets_load_over_real_mcp_transport_without_fabricating_scope(self):
        fixture = backend_fixture()
        backend = FixtureBackend(fixture)
        async with local_mcp(backend) as url:
            business = BusinessMcpReadClient(BusinessMcpSettings(url, TOKEN))
            state = await load_case_data(TaskEnvelope.model_validate(fixture["task"]), business)
        self.assertEqual(state["case_snapshot"]["targetMessageIds"], FIXTURE["case"]["targetMessageIds"])
        self.assertEqual(state["case_snapshot"]["targetVersions"], FIXTURE["case"]["targetVersions"])
        self.assertEqual(len(state["target_messages"]), 1)
        self.assertEqual(len(state["context_messages"]), 1)
        self.assertNotIn("live_id", state)
        self.assertNotIn("user_id", state)
        self.assertNotIn("handling_route", state)

    async def test_backend_provenance_is_preserved_without_fabricating_missing_fields(self):
        fixture=backend_fixture()
        fixture["case"].update(ruleId="BACKEND_RULE",filterVersion="backend-filter-7",matchingTags=["LINK"])
        state=await load_case_data(TaskEnvelope.model_validate(fixture["task"]),FixtureTools(fixture))
        self.assertEqual(state["case_snapshot"]["ruleId"],"BACKEND_RULE")
        self.assertEqual(state["case_snapshot"]["filterVersion"],"backend-filter-7")
        self.assertEqual(state["case_snapshot"]["matchingTags"],["LINK"])
        state=await load_case_data(TaskEnvelope.model_validate(FIXTURE["task"]),FixtureTools(FIXTURE))
        self.assertIsNone(state["case_snapshot"]["ruleId"])
        self.assertIsNone(state["case_snapshot"]["filterVersion"])

    async def test_equivalent_dual_formats_are_accepted_without_changing_input(self):
        fixture = backend_fixture()
        fixture["case"].update({key: deepcopy(FIXTURE["case"][key])
                               for key in ("targetMessageIds", "targetVersions")})
        original = deepcopy(fixture)
        snapshot = CaseSnapshot.model_validate(fixture["case"])
        self.assertEqual(snapshot.targets[0].message_id, UUID(FIXTURE["case"]["targetMessageIds"][0]))
        self.assertEqual(fixture, original)

    async def test_malformed_or_conflicting_targets_fail_before_context_read(self):
        for mode in ("empty", "object", "duplicate", "no_visibility", "no_version", "zero_version",
                     "string_version", "boolean_version", "invalid_visibility", "conflicting_ids", "conflicting_versions"):
            fixture = backend_fixture()
            case = fixture["case"]
            target = case["targets"][0]
            if mode == "empty": case["targets"] = []
            elif mode == "object": case["targets"] = target
            elif mode == "duplicate": case["targets"].append(deepcopy(target))
            elif mode == "no_visibility": del target["visibility"]
            elif mode == "no_version": del target["version"]
            elif mode == "zero_version": target["version"] = 0
            elif mode == "string_version": target["version"] = "1"
            elif mode == "boolean_version": target["version"] = True
            elif mode == "invalid_visibility": target["visibility"] = "UNKNOWN"
            elif mode == "conflicting_ids": case["targetMessageIds"] = [fixture["context"]["messages"][1]["messageId"]]
            elif mode == "conflicting_versions": case["targetVersions"] = {target["messageId"]: 2}
            tools = FixtureTools(fixture)
            with self.subTest(mode=mode), self.assertRaises(AgentToolError) as raised:
                await load_case_data(TaskEnvelope.model_validate(fixture["task"]), tools)
            self.assertEqual(raised.exception.code, "INVALID_TOOL_RESPONSE")
            self.assertEqual(tools.calls, ["get_moderation_case"])

    async def test_explicit_visibility_conflict_is_stale_evidence(self):
        fixture = backend_fixture()
        fixture["case"]["targets"][0]["visibility"] = "HELD"
        with self.assertRaises(AgentToolError) as raised:
            await load_case_data(TaskEnvelope.model_validate(fixture["task"]), FixtureTools(fixture))
        self.assertEqual(raised.exception.code, "STALE_EVIDENCE")

    async def test_missing_context_metadata_is_never_filled_from_case(self):
        for key in ("snapshotId", "caseId", "evidenceVersion", "visibility"):
            fixture = backend_fixture()
            if key == "visibility":
                del fixture["context"]["messages"][0][key]
            else:
                del fixture["context"][key]
            with self.subTest(key=key), self.assertRaises(AgentToolError) as raised:
                await load_case_data(TaskEnvelope.model_validate(fixture["task"]), FixtureTools(fixture))
            self.assertEqual(raised.exception.code, "INVALID_TOOL_RESPONSE")

    async def test_snapshot_binding_still_precedes_context_read(self):
        fixture = backend_fixture()
        fixture["case"]["evidenceVersion"] = 2
        tools = FixtureTools(fixture)
        with self.assertRaises(AgentToolError) as raised:
            await load_case_data(TaskEnvelope.model_validate(fixture["task"]), tools)
        self.assertEqual(raised.exception.code, "STALE_EVIDENCE")
        self.assertEqual(tools.calls, ["get_moderation_case"])

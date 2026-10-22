"""#6 실행 한도·오류 복구: 한도 초과 / 일시 오류 후 성공 / 재시도 소진 / UNKNOWN 조회 / 재시작 후 예산 유지."""

from copy import deepcopy
import unittest

from app import nodes
from app.case_loader import load_case_data
from app.clients.business_mcp import AgentToolError
from app.harness.execution import (
    EXECUTION_LIMIT, ExecutionLimits, HarnessGate, guarded_investigate, resolve_unknown_action, run_guarded,
)
from app.investigation.model import ToolCall
from app.schemas import TaskEnvelope
from test_agent_read import FIXTURE, FixtureTools
from test_investigation import Policies, ScriptedModel, finding, finish, search

TRACE = "00000000-0000-4000-8000-0000000000e1"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def gate(**kwargs) -> HarnessGate:
    clock = kwargs.pop("clock", FakeClock())

    async def no_sleep(seconds: float) -> None:
        clock.now += seconds

    return HarnessGate(trace_id=TRACE, clock=clock, sleep=no_sleep, **kwargs)


def ok(value="ok"):
    async def operation():
        return value
    return operation


class Flaky:
    """처음 failures번은 일시 오류, 그 뒤 성공."""

    def __init__(self, failures: int, *, retryable: bool = True) -> None:
        self.failures, self.retryable, self.calls = failures, retryable, 0

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise AgentToolError("TEMPORARY_UNAVAILABLE", trace_id=TRACE, retryable=self.retryable)
        return "recovered"


class LimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_calls_stop_after_eight(self) -> None:
        g = gate()
        for _ in range(8):
            await g.run("model", ok())
        with self.assertRaises(AgentToolError) as raised:
            await g.run("model", ok())
        self.assertEqual(raised.exception.code, EXECUTION_LIMIT)
        self.assertEqual(g.snapshot()["exhausted"], "MODEL_CALLS")

    async def test_same_tool_stops_after_three_but_other_tools_continue(self) -> None:
        g = gate()
        for _ in range(3):
            await g.run("search_policy", ok())
        await g.run("get_chat_context", ok())
        with self.assertRaises(AgentToolError):
            await g.run("search_policy", ok())
        self.assertEqual(g.snapshot()["exhausted"], "TOOL_CALLS:search_policy")
        # 한 번 소진되면 같은 작업의 다른 호출도 막는다.
        with self.assertRaises(AgentToolError):
            await g.run("get_chat_context", ok())

    async def test_active_time_limit_counts_only_running_operations(self) -> None:
        clock = FakeClock()
        g = gate(clock=clock)

        async def slow():
            clock.now += 70
            return "done"

        await g.run("get_chat_context", slow)
        clock.now += 3600  # 운영자 승인 대기: 실행 중인 연산이 없으므로 세지 않는다
        await g.run("search_policy", slow)  # 누적 140초 > 120초지만 시작 시점엔 50초 남음
        with self.assertRaises(AgentToolError):
            await g.run("model", ok())
        self.assertEqual(g.snapshot()["exhausted"], "ACTIVE_TIME")
        self.assertEqual(g.snapshot()["activeSeconds"], 140.0)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_error_recovers_within_two_retries(self) -> None:
        g, op = gate(), Flaky(2)
        self.assertEqual(await g.run("model", op), "recovered")
        snap = g.snapshot()
        self.assertEqual((op.calls, snap["retryCount"], snap["modelCalls"]), (3, 2, 3))
        self.assertGreaterEqual(snap["activeSeconds"], 6)  # 2 + 4 plus jitter
        self.assertLessEqual(snap["activeSeconds"], 8)
        self.assertEqual(snap["modelRetryCount"], 2)

    async def test_retries_exhausted_raises_last_error(self) -> None:
        g, op = gate(), Flaky(5)
        with self.assertRaises(AgentToolError) as raised:
            await g.run("search_policy", op)
        self.assertEqual(raised.exception.code, "TEMPORARY_UNAVAILABLE")
        self.assertEqual((op.calls, g.snapshot()["retryCount"]), (3, 2))
        self.assertEqual(g.snapshot()["lastError"]["code"], "TEMPORARY_UNAVAILABLE")

    async def test_non_retryable_error_is_not_retried(self) -> None:
        g, op = gate(), Flaky(1, retryable=False)
        with self.assertRaises(AgentToolError):
            await g.run("search_policy", op)
        self.assertEqual((op.calls, g.snapshot()["retryCount"]), (1, 0))

    async def test_retry_attempts_consume_same_tool_budget(self) -> None:
        g = gate()
        await g.run("search_policy", ok())
        op = Flaky(5)
        with self.assertRaises(AgentToolError) as raised:
            await g.run("search_policy", op)  # 1회 사용 후 남은 2회만 시도 가능
        self.assertEqual(raised.exception.code, EXECUTION_LIMIT)
        self.assertEqual(op.calls, 2)


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_budget_survives_restart_through_state(self) -> None:
        first = gate()
        for _ in range(7):
            await first.run("model", ok())
        state = {"trace_id": TRACE, "harness_budget": first.snapshot()}

        restarted = HarnessGate.from_state(state)  # 재시작·재전달 후 새 gate
        await restarted.run("model", ok())
        with self.assertRaises(AgentToolError):
            await restarted.run("model", ok())
        self.assertEqual(restarted.snapshot()["modelCalls"], 8)

    async def test_run_guarded_records_budget_on_success_and_failure(self) -> None:
        async def step_ok(g):
            await g.run("model", ok())
            return {"turn_count": 1}

        patch = await run_guarded({"trace_id": TRACE}, step_ok, gate=gate())
        self.assertEqual(patch["turn_count"], 1)
        self.assertEqual(patch["harness_budget"]["modelCalls"], 1)

        async def step_over_limit(g):
            for _ in range(9):
                await g.run("model", ok())

        failed = await run_guarded({"trace_id": TRACE}, step_over_limit, gate=gate())
        self.assertEqual(failed["task_outcome"], "NEEDS_REVIEW")
        self.assertEqual(failed["error"]["code"], EXECUTION_LIMIT)
        self.assertEqual(failed["harness_budget"]["modelCalls"], 8)
        self.assertNotIn("message_findings", failed)

        async def step_fail(g):
            await g.run("search_policy", Flaky(5))

        final = await run_guarded({"trace_id": TRACE}, step_fail, gate=gate())
        self.assertEqual(final["task_outcome"], "FAILED")
        self.assertEqual(final["error"], {"code": "TEMPORARY_UNAVAILABLE", "retryable": True, "traceId": TRACE})


class UnknownActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_result_is_confirmed_by_status_not_reexecuted(self) -> None:
        reads: list[str] = []

        async def read_status(action_id):
            reads.append(action_id)
            return {"actionId": action_id, "status": "APPLIED"}

        confirmed = await resolve_unknown_action({"actionId": "act-1", "status": "UNKNOWN"},
                                                 read_status=read_status)
        self.assertEqual((confirmed["status"], confirmed["confirmedBy"], reads), ("APPLIED", "get_action_status", ["act-1"]))
        untouched = await resolve_unknown_action({"actionId": "act-2", "status": "APPLIED"},
                                                 read_status=read_status)
        self.assertEqual((untouched["status"], reads), ("APPLIED", ["act-1"]))


class InvestigationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """실제 조사 루프(#9)에 공유 gate를 꽂아 한도·재시도·예산 기록을 확인한다."""

    async def asyncSetUp(self) -> None:
        self.business = FixtureTools(deepcopy(FIXTURE))
        self.state = await load_case_data(TaskEnvelope.model_validate(FIXTURE["task"]), self.business)

    async def test_investigation_records_budget_in_state(self) -> None:
        model = ScriptedModel([search(), finish([finding(self.state)])])
        patch = await guarded_investigate(self.state, model=model, business=self.business, policy=Policies())
        budget = patch["harness_budget"]
        self.assertEqual(budget["modelCalls"], 2)
        self.assertEqual(budget["toolCalls"]["search_policy"], 1)
        self.assertEqual(len(patch["message_findings"]), 1)

    async def test_looping_model_stops_at_tool_limit_without_findings(self) -> None:
        model = ScriptedModel([ToolCall(str(n), "get_stream_info", {}) for n in range(8)])
        patch = await guarded_investigate(self.state, model=model, business=self.business, policy=Policies())
        self.assertEqual(patch["task_outcome"], "NEEDS_REVIEW")
        self.assertEqual(patch["harness_budget"]["exhausted"], "TOOL_CALLS:get_stream_info")
        self.assertNotIn("message_findings", patch)

    async def test_transient_policy_error_is_retried_inside_investigation(self) -> None:
        policies = Policies()
        original = policies.call
        failures = {"left": 1}

        async def flaky_call(name, args):
            if name == "search_policy" and failures["left"]:
                failures["left"] -= 1
                raise AgentToolError("TEMPORARY_UNAVAILABLE", trace_id=args["traceId"], retryable=True)
            return await original(name, args)

        policies.call = flaky_call
        model = ScriptedModel([search(), finish([finding(self.state)])])
        g = HarnessGate.from_state(self.state, limits=ExecutionLimits(backoff_seconds=(0.0,)))
        patch = await run_guarded(self.state, lambda gate_: nodes.investigate(
            self.state, gate=gate_, model=model, business=self.business, policy=policies), gate=g)
        self.assertEqual(patch["harness_budget"]["retryCount"], 1)
        self.assertEqual(patch["harness_budget"]["toolCalls"]["search_policy"], 2)
        self.assertEqual(len(patch["message_findings"]), 1)


if __name__ == "__main__":
    unittest.main()

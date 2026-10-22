"""Opt-in real HAProxy/MCP container test; creates and removes only its own project."""
import asyncio
from collections import Counter
import os
from pathlib import Path
import subprocess
import time
import unittest
from uuid import uuid4

import httpx

from app.clients.business_mcp import AgentToolError, BusinessMcpReadClient, BusinessMcpSettings
from app.integration.http_gateway import HttpModerationGateway
from test_business_mcp_cluster import ARGS, COMMAND


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.getenv("LIVEGUARD_TEST_DOCKER") == "1", "Opt-in Docker integration")
class DockerClusterTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def compose(cls, *args, timeout=120):
        result = subprocess.run(
            ["docker", "compose", "-p", cls.project,
             "-f", str(ROOT / "deploy/business-mcp.compose.yaml"),
             "-f", str(ROOT / "tests/fixtures/business_mcp.compose.yaml"), *args],
            cwd=ROOT, env=cls.environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
        )
        if result.returncode:
            raise RuntimeError(f"Compose {' '.join(args)} failed:\n{result.stdout[-8000:]}")
        return result.stdout.strip()

    @classmethod
    def cleanup(cls):
        try:
            cls.compose("down", "--volumes", "--remove-orphans")
        finally:
            image = cls.environment["MCP_TEST_IMAGE"]
            exists = subprocess.run(["docker", "image", "inspect", image],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if exists.returncode == 0:
                subprocess.run(["docker", "image", "rm", image], check=True,
                               stdout=subprocess.DEVNULL)

    @classmethod
    def setUpClass(cls):
        cls.project = f"liveguard-mcp94-test-{uuid4().hex[:10]}"
        cls.environment = {
            **os.environ,
            "BUSINESS_BACKEND_BASE_URL": "http://backend-fixture:8080",
            "BUSINESS_MCP_AGENT_TOKEN": uuid4().hex,
            "BUSINESS_MCP_SERVICE_TOKEN": uuid4().hex,
            "MCP_TEST_IMAGE": f"{cls.project}:local",
            "MCP_TEST_FIXTURES": str(ROOT / "tests/fixtures"),
        }
        cls.addClassCleanup(cls.cleanup)
        cls.compose("build", "mcp-1", timeout=600)
        cls.compose("up", "-d", "--wait", "--wait-timeout", "90", timeout=180)
        cls.url = "http://" + cls.compose("port", "business-mcp", "8002") + "/mcp"
        cls.backend_url = "http://" + cls.compose("port", "backend-fixture", "8080")
        cls.settings = BusinessMcpSettings(cls.url, cls.environment["BUSINESS_MCP_AGENT_TOKEN"])

    async def wait_distribution(self, expected):
        deadline = time.monotonic() + 30
        samples = []
        read = BusinessMcpReadClient(self.settings)
        while time.monotonic() < deadline:
            try:
                result = await read.call("get_moderation_case", ARGS)
                samples.append(result["data"]["servedBy"])
                if len(samples) >= 12 and set(samples[-12:]) == expected:
                    return Counter(samples[-12:])
            except AgentToolError:
                # Health checks need time to converge; no production retry policy is changed.
                samples.clear()
            await asyncio.sleep(0.1)
        self.fail(f"Distribution did not converge to {expected}: {Counter(samples)}")

    def instance_ips(self):
        result = {}
        for name in ("mcp-1", "mcp-2"):
            container = self.compose("ps", "-q", name)
            ip = subprocess.check_output(
                ["docker", "inspect", "--format",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
                text=True,
            ).strip()
            result[name] = ip
        return result

    async def test_distribution_kill_lost_mutation_recovery_and_total_outage(self):
        ips = await asyncio.to_thread(self.instance_ips)
        counts = await self.wait_distribution(set(ips.values()))
        self.assertEqual(sorted(counts.values()), [6, 6])
        print(f"Docker initial distribution: {dict(counts)}", flush=True)

        async with httpx.AsyncClient(base_url=self.backend_url, trust_env=False, timeout=3) as http:
            gateway = HttpModerationGateway(http, service_token="unused", business_settings=self.settings)
            operation = asyncio.create_task(gateway.kick(COMMAND, trace_id=ARGS["traceId"]))
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    state = (await http.get("/test/state")).json()
                    if state["commands"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    self.fail("Mutation never reached the fixture Backend")
                serving_ip = state["commands"][0]["servedBy"]
                victim = next(name for name, ip in ips.items() if ip == serving_ip)
                survivor = next(name for name in ips if name != victim)
                await asyncio.to_thread(self.compose, "kill", "-s", "SIGKILL", victim)
                result = await asyncio.wait_for(operation, 12)
                self.assertEqual(result, {"actionId": COMMAND["actionId"], "status": "UNKNOWN"})
                counts = await self.wait_distribution({ips[survivor]})
                self.assertEqual(list(counts.values()), [12])
                observed = await gateway.read_action(
                    action_id=COMMAND["actionId"], trace_id=ARGS["traceId"],
                )
                self.assertEqual(observed["status"], "APPLIED")
                state = (await http.get("/test/state")).json()
                self.assertEqual(len(state["commands"]), 1)
                self.assertEqual(state["commands"][0]["idempotencyKey"], COMMAND["idempotencyKey"])
                print(f"Docker {victim} SIGKILL: UNKNOWN, survivor 12/12, "
                      "action APPLIED, execute POST count=1", flush=True)
            finally:
                await http.post("/test/release")
                if not operation.done():
                    operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)

        await asyncio.to_thread(self.compose, "up", "-d", "--no-deps", "--force-recreate",
                                "--wait", victim)
        ips = await asyncio.to_thread(self.instance_ips)
        counts = await self.wait_distribution(set(ips.values()))
        self.assertEqual(sorted(counts.values()), [6, 6])
        print(f"Docker recreate recovery: {dict(counts)}", flush=True)

        async with httpx.AsyncClient(base_url=self.backend_url, trust_env=False, timeout=3) as http:
            await http.post("/test/block-read")
            active = asyncio.create_task(BusinessMcpReadClient(self.settings).call(
                "get_moderation_case", ARGS,
            ))
            stopping = None
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    state = (await http.get("/test/state")).json()
                    if state["waitingRead"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    self.fail("Read never reached the fixture Backend")
                draining = next(name for name, ip in ips.items() if ip == state["waitingRead"])
                remaining = next(name for name in ips if name != draining)
                stopping = asyncio.create_task(asyncio.to_thread(
                    self.compose, "stop", "-t", "25", draining,
                ))
                await asyncio.sleep(1)
                self.assertFalse(stopping.done(), "Shutdown did not wait for the active request")
                self.assertFalse(active.done())
                await http.post("/test/release-read")
                self.assertEqual((await active)["data"]["servedBy"], ips[draining])
                await stopping
            finally:
                await http.post("/test/release-read")
                await asyncio.gather(active, return_exceptions=True)
                if stopping is not None:
                    await stopping
        counts = await self.wait_distribution({ips[remaining]})
        self.assertEqual(list(counts.values()), [12])
        print("Docker graceful stop: active call completed; survivor handled 12/12 calls", flush=True)
        await asyncio.to_thread(self.compose, "stop", "-t", "25", remaining)
        await asyncio.sleep(3)
        with self.assertRaises(AgentToolError) as raised:
            await BusinessMcpReadClient(self.settings).call("get_moderation_case", ARGS)
        self.assertEqual(raised.exception.code, "TEMPORARY_UNAVAILABLE")
        print("Docker total outage: explicit TEMPORARY_UNAVAILABLE", flush=True)

        logs = await asyncio.to_thread(self.compose, "logs", "--no-color", "mcp-1", "mcp-2", "business-mcp")
        for secret in (self.settings.token, self.environment["BUSINESS_MCP_SERVICE_TOKEN"],
                       COMMAND["idempotencyKey"]):
            self.assertNotIn(secret, logs)

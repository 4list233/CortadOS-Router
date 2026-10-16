# PURPOSE: Independent permission acceptance through public MCP and observable upstream effects.
# CONTRACT: RT-MCP-001 AC-MCP-07; read-only to the production builder.
# DEPENDS: Official MCP client, protected native argv capture double, and local MCP fixture.
# CEILING: Native argv assertions complement real sandbox captures; the double is not a sandbox.
import asyncio
import copy
import json
import subprocess
import sys
import time
import tomllib

from .support import HERE, WorkerCase, process_alive

# Seconds: local HTTP startup/cleanup gets five seconds to tolerate scheduler load;
# less risks fixture flakes, more delays reporting a broken local transport.
HTTP_WAIT_S = 5


def config_assignments(argv):
    """Decode the standard TOML values visible at the native CLI boundary."""
    return [(argv[index + 1], tomllib.loads(argv[index + 1]))
            for index, value in enumerate(argv[:-1]) if value in ("-c", "--config")]


def config_value(assignments, *path):
    for _, value in reversed(assignments):
        for key in path:
            if not isinstance(value, dict) or key not in value:
                break
            value = value[key]
        else:
            return value
    raise AssertionError("permissions: native configuration omitted " + ".".join(path))


class Permissions(WorkerCase):
    def setUp(self):
        super().setUp()
        self.events = self.temp / "upstream-events.jsonl"
        self.server_env.update(MCP_FIXTURE_SECRET="SYNTHETIC_GRANTED_CREDENTIAL",
                               MCP_UNDECLARED_SECRET="SYNTHETIC_UNDECLARED_CREDENTIAL",
                               OPENAI_BASE_URL="https://forbidden.example.invalid")

    def register_fixture(self, *, fail_start=False):
        arguments = [str(HERE / "fake_mcp_server.py"), str(self.events)]
        if fail_start:
            arguments.append("--fail-start")
        self.config["connector_registry"] = {"fixture": {
            "transport": "stdio", "command": sys.executable, "args": arguments,
            "env_vars": ["MCP_FIXTURE_SECRET"], "tools": ["visible_marker", "hidden_marker"]}}
        self.write_config()

    def grants(self):
        return {"network": False, "mcp_servers": [{"name": "fixture", "tools": ["visible_marker"]}]}

    def upstream_events(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    async def test_ac07_proxy_exposes_only_granted_tool_and_blocks_other_mcp_surfaces(self):
        self.register_fixture()
        self.configure_native(mcp_probe=True)
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request(permissions=self.grants()))
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "succeeded")
        probes = self.native_calls("proxy_probe")
        self.assertEqual(len(probes), 1, "permissions: native must actually interrogate the proxy")
        self.assertEqual(len(probes[0]["servers"]), 1)
        observed = probes[0]["servers"][0]
        self.assertEqual(observed["name"], "fixture", "permissions: preserve the granted registry identity")
        self.assertEqual(observed["tools"], ["visible_marker"], "permissions: tool catalog exceeded the job grant")
        self.assertFalse(observed["visible_call"]["is_error"])
        self.assertIn("GRANTED_TOOL_REACHED", json.dumps(observed["visible_call"]["content"]))
        self.assertTrue(observed["hidden_call"]["is_error"], "permissions: guessed ungranted tool was accepted")
        for surface in ("resources", "templates", "prompts"):
            self.assertEqual(observed[surface], [], "permissions: upstream " + surface + " catalog escaped")
        for operation in ("resource_read", "prompt_get"):
            self.assertIn("error", observed[operation], "permissions: ungranted " + operation + " succeeded")
        rows = self.upstream_events()
        self.assertEqual([row["kind"] for row in rows if row["kind"] != "started"], ["visible_tool"],
                         "permissions: proxy forwarded an ungranted operation")
        starts = [row for row in rows if row["kind"] == "started"]
        self.assertTrue(starts, "permissions: fixture must reach the real upstream process")
        for row in starts:
            self.assertEqual(row["marker_environment"], {"MCP_FIXTURE_SECRET": "SYNTHETIC_GRANTED_CREDENTIAL"})
            self.assertFalse(process_alive(row["pid"]), "permissions: terminal job retained its upstream connector")
        native = self.native_calls("exec")[0]
        self.assertNotIn("MCP_FIXTURE_SECRET", native["env_names"])
        self.assertNotIn("MCP_UNDECLARED_SECRET", native["env_names"])
        self.assertNotIn(str(HERE / "fake_mcp_server.py"), json.dumps(native["argv"]),
                         "permissions: native received the upstream command instead of only its proxy")
        assignments = config_assignments(native["argv"])
        servers = config_value(assignments, "mcp_servers")
        self.assertEqual(set(servers), {"fixture"})
        self.assertIs(servers["fixture"]["required"], True)
        self.assertEqual(servers["fixture"]["tools"]["visible_marker"]["approval_mode"], "approve")
        self.assertNotIn("hidden_marker", servers["fixture"].get("tools", {}),
                         "permissions: native approval exceeded the requested grant")
        self.assertNotIn(str(HERE / "fake_mcp_server.py"), json.dumps(result),
                         "permissions: public result exposed the private connector definition")
        for file in self.state.rglob("*"):
            if file.is_file():
                self.assertNotIn(b"SYNTHETIC_GRANTED_CREDENTIAL", file.read_bytes(),
                                 "permissions: credential persisted in " + str(file.relative_to(self.state)))

    async def test_ac07_omitted_grants_start_no_proxy_or_upstream(self):
        self.register_fixture()
        self.configure_native(mcp_probe=True)
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request())
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "succeeded")
        self.assertEqual(self.native_calls("proxy_probe")[0]["servers"], [],
                         "permissions: operator registration alone must not grant worker access")
        self.assertEqual(self.upstream_events(), [])

    async def test_ac07_denied_server_tool_and_network_refuse_before_native_execution(self):
        self.register_fixture()
        denied = [
            {"network": True, "mcp_servers": []},
            {"network": False, "mcp_servers": [{"name": "unknown", "tools": ["visible_marker"]}]},
            {"network": False, "mcp_servers": [{"name": "fixture", "tools": ["never_registered"]}]},
        ]
        async with self.session() as client:
            for permissions in denied:
                with self.subTest(permissions=permissions):
                    await self.call(client, "delegate", self.request(permissions=permissions), error="permission_refused")
        self.assertEqual(self.native_calls("exec"), [])
        self.assertEqual(self.upstream_events(), [])

    async def test_ac07_requests_cannot_replace_registry_endpoints_or_native_settings(self):
        self.register_fixture()
        attempts = [self.request(mcp_servers={"fixture": {"url": "https://forbidden.example.invalid"}}),
                    self.request(config={"approval_policy": "never"}), self.request(command="/bin/sh")]
        for field, value in (("url", "https://forbidden.example.invalid"), ("command", "/bin/sh"),
                             ("env", {"TOKEN": "request-must-not-grant-this"}),
                             ("bearer_token", "request-must-not-grant-this")):
            permissions = copy.deepcopy(self.grants())
            permissions["mcp_servers"][0][field] = value
            attempts.append(self.request(permissions=permissions))
        async with self.session() as client:
            for request in attempts:
                with self.subTest(request=request):
                    await self.call(client, "delegate", request, error="invalid_request")
        self.assertEqual(self.native_calls("exec"), [])
        self.assertEqual(self.upstream_events(), [])

    async def test_ac07_native_boundary_keeps_cwd_tmp_and_environment_scope(self):
        work = self.workspace / "folder.with.periods"
        work.mkdir()
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request(cwd=str(work)))
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "succeeded")
        native = self.native_calls("exec")[0]
        self.assertEqual(native["cwd"], str(work.resolve()))
        argv = native["argv"]
        for flag in ("--ignore-user-config", "--ignore-rules", "--strict-config"):
            self.assertIn(flag, argv, "permissions: omitted native isolation flag " + flag)
        self.assertFalse(any("dangerously" in item or item == "--full-auto" for item in argv))
        sandbox = next((argv[i + 1] for i, item in enumerate(argv[:-1]) if item in ("-s", "--sandbox")), None)
        self.assertEqual(sandbox, "workspace-write")
        assignments = config_assignments(argv)
        self.assertEqual(config_value(assignments, "approval_policy"), "never")
        for field, expected in (("exclude_slash_tmp", True), ("exclude_tmpdir_env_var", True),
                                ("writable_roots", []), ("network_access", False)):
            self.assertEqual(config_value(assignments, "sandbox_workspace_write", field), expected)
        self.assertEqual(config_value(assignments, "web_search"), "disabled")
        for feature in ("apps", "plugins", "hooks", "multi_agent", "browser_use", "browser_use_external",
                        "browser_use_full_cdp_access", "computer_use", "image_generation", "skill_search"):
            disabled = any(item == "--disable" and argv[i + 1] == feature for i, item in enumerate(argv[:-1]))
            self.assertTrue(disabled or config_value(assignments, "features", feature) is False)
        trust_maps = [(raw, value["projects"]) for raw, value in assignments if "projects" in value]
        self.assertEqual(len(trust_maps), 1, "permissions: trust must use one whole TOML map")
        self.assertTrue(trust_maps[0][0].split("=", 1)[0].strip() == "projects")
        self.assertEqual(trust_maps[0][1][str(work.resolve())]["trust_level"], "untrusted")
        allowed = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
                   "SSL_CERT_FILE", "SSL_CERT_DIR", "CODEX_HOME"}
        self.assertEqual(set(native["env_names"]) - allowed, set(), "permissions: native inherited undeclared environment")
        self.assertEqual(native["marker_environment"], {"CODEX_HOME": str(self.native_home)})

    async def test_ac07_operator_allowed_network_reaches_native_policy(self):
        self.config["allow_network"] = True
        self.write_config()
        async with self.session() as client:
            started = await self.call(client, "delegate", self.request(permissions={"network": True, "mcp_servers": []}))
            result = await self.terminal(client, started["job"]["job_id"])
            self.assertEqual(result["job"]["state"], "succeeded")
        assignments = config_assignments(self.native_calls("exec")[0]["argv"])
        self.assertIs(config_value(assignments, "sandbox_workspace_write", "network_access"), True)
        self.assertEqual(config_value(assignments, "web_search"), "disabled", "permissions: shell network grant must not enable native web search")

    async def test_ac07_required_upstream_failure_is_visible_not_success(self):
        self.register_fixture(fail_start=True)
        self.configure_native(mcp_probe=True)
        async with self.session() as client:
            reply = await client.call_tool("delegate", self.request(permissions=self.grants()))
            value = reply.structuredContent or json.loads(next(item.text for item in reply.content if item.type == "text"))
            if reply.isError:
                self.assertFalse(value["ok"])
                self.assertEqual(value["error"]["code"], "connector_unavailable")
            else:
                self.assertTrue(value["ok"])
                result = await self.terminal(client, value["job"]["job_id"])
                self.assertEqual(result["job"]["state"], "failed")
                self.assertEqual(result["result"]["error"]["code"], "connector_unavailable")

    async def test_ac07_http_grant_uses_bearer_reference_without_exposing_it_to_native(self):
        ready = self.temp / "http-ready.json"
        errors = self.temp / "http-fixture-stderr.log"
        environment = {key: self.server_env[key] for key in ("PATH", "HOME", "LANG") if key in self.server_env}
        environment["MCP_FIXTURE_SECRET"] = "SYNTHETIC_GRANTED_CREDENTIAL"
        with errors.open("w") as output:
            upstream = subprocess.Popen([sys.executable, "-B", str(HERE / "fake_mcp_server.py"),
                str(self.events), "--http", str(ready)], env=environment, stdout=output, stderr=output)
            try:
                deadline = time.monotonic() + HTTP_WAIT_S
                while not ready.exists() and upstream.poll() is None and time.monotonic() < deadline:
                    await asyncio.sleep(0.03)
                self.assertTrue(ready.exists(), "permissions fixture: HTTP server failed to become ready")
                self.config["connector_registry"] = {"fixture": {"transport": "http",
                    "url": json.loads(ready.read_text())["url"], "bearer_token_env_var": "MCP_FIXTURE_SECRET",
                    "tools": ["visible_marker", "hidden_marker"]}}
                self.write_config()
                self.configure_native(mcp_probe=True)
                async with self.session() as client:
                    started = await self.call(client, "delegate", self.request(permissions=self.grants()))
                    result = await self.terminal(client, started["job"]["job_id"])
                    self.assertEqual(result["job"]["state"], "succeeded")
                observed = self.native_calls("proxy_probe")[0]["servers"]
                self.assertEqual(len(observed), 1)
                self.assertEqual(observed[0]["tools"], ["visible_marker"])
                self.assertFalse(observed[0]["visible_call"]["is_error"])
                self.assertIn("GRANTED_TOOL_REACHED", json.dumps(observed[0]["visible_call"]["content"]))
                kinds = [row["kind"] for row in self.upstream_events()]
                self.assertIn("http_authorized", kinds, "permissions: fixture never received the bearer credential")
                self.assertNotIn("http_rejected", kinds, "permissions: proxy omitted or changed the credential")
                self.assertEqual(kinds.count("visible_tool"), 1)
                self.assertFalse(any(kind.startswith("hidden_") for kind in kinds))
                native = self.native_calls("exec")[0]
                self.assertNotIn("MCP_FIXTURE_SECRET", native["env_names"])
                self.assertNotIn("SYNTHETIC_GRANTED_CREDENTIAL", json.dumps(native))
                self.assertNotIn(self.config["connector_registry"]["fixture"]["url"], json.dumps(native["argv"]))
            finally:
                upstream.terminate()
                try:
                    await asyncio.to_thread(upstream.wait, timeout=HTTP_WAIT_S)
                except subprocess.TimeoutExpired:
                    upstream.kill()
                    await asyncio.to_thread(upstream.wait, timeout=HTTP_WAIT_S)

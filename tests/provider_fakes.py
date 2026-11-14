"""Shared by the test modules that run the provider against the fake CLIs (tests/fixtures/fake_claude_cli.py,
fake_codex_app_server.py, the search lane's fake).

The provider starts every backend CLI on an allowlisted environment (cortados_router/provider/child_env.py, #106), so
the FAKE_* variables these fakes are configured by would no longer reach them. A module that imports setUpModule /
tearDownModule from here lets FAKE_* through, and nothing else: production code is unchanged, and the allowlist's own
tests (tests/test_child_env.py) do not import this.
"""
from unittest import mock

from cortados_router.provider import child_env

_patch = mock.patch.object(child_env, "BASE_PREFIXES", child_env.BASE_PREFIXES + ("FAKE_",))


def setUpModule():
    _patch.start()


def tearDownModule():
    _patch.stop()

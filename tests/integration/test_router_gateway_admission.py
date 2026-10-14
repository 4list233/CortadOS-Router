"""Exercise installed Hermes admission, with temporary homes and no model/Context calls.

Run with Hermes scripts/run_tests.sh (absolute path to this file).
"""
import importlib.util as _guard_util
import unittest as _guard_unittest
if _guard_util.find_spec("pytest") is None or _guard_util.find_spec("hermes_cli") is None:
    raise _guard_unittest.SkipTest("Hermes integration test: needs pytest and an importable Hermes host (hermes_cli). "
                                   "Run with pytest inside the Hermes venv; see tests/integration/README.md")
import asyncio
import threading
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_constants import (get_hermes_home, reset_hermes_home_override,
                              set_hermes_home_override)


def runner():
    obj = object.__new__(GatewayRunner)
    obj.config = NS(multiplex_profiles=False)
    obj._scale_to_zero_note_real_inbound = lambda: None
    obj._is_user_authorized_for_source = lambda source: True
    obj._admit_bot_message_for_source = lambda source: True
    return obj


def event(face):
    return MessageEvent(text="inspect attached image", message_id=face,
                        source=SessionSource(platform=Platform.DISCORD, chat_id=face,
                                             user_id="test-user", profile=face))


@pytest.mark.asyncio
async def test_slow_hooks_keep_loop_live_and_two_profile_homes_isolated(tmp_path):
    obj = runner()
    entered = {face: threading.Event() for face in ("donna", "harvey")}
    release = threading.Event()
    seen = []
    loop_thread = threading.get_ident()

    def hook(name, **payload):
        e = payload["event"]
        face = e.source.profile
        entered[face].set()
        released = release.wait(3)
        seen.append((face, get_hermes_home(), threading.get_ident(), released))
        return [{"action": "rewrite", "text": "converted " + face}]

    async def admit(face):
        token = set_hermes_home_override(tmp_path / face)
        try:
            return await obj._hm_admit_event(event(face))
        finally:
            reset_hermes_home_override(token)

    with patch("hermes_cli.lifecycle.invoke_hook", side_effect=hook):
        tasks = [asyncio.create_task(admit(face)) for face in entered]
        try:
            async with asyncio.timeout(2):
                while not all(x.is_set() for x in entered.values()):
                    await asyncio.sleep(0.01)
            assert all(not task.done() for task in tasks)
        finally:
            release.set()
            outcomes = await asyncio.gather(*tasks)
    assert [result[0].text for result in outcomes] == ["converted donna", "converted harvey"]
    assert all(home == tmp_path / face and thread != loop_thread and released
               for face, home, thread, released in seen)


@pytest.mark.asyncio
async def test_cancellation_drains_hook_and_preserves_skip_and_error_contracts():
    obj = runner()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_hook(name, **payload):
        entered.set()
        release.wait(3)
        finished.set()
        return [{"action": "skip", "reason": "test policy"}]

    with patch("hermes_cli.lifecycle.invoke_hook", side_effect=slow_hook):
        task = asyncio.create_task(obj._hm_admit_event(event("donna")))
        try:
            async with asyncio.timeout(2):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    with patch("hermes_cli.lifecycle.invoke_hook", return_value=[{"action": "skip"}]):
        assert await obj._hm_admit_event(event("harvey")) is None
    e = event("harvey")
    with patch("hermes_cli.lifecycle.invoke_hook", side_effect=RuntimeError("test failure")):
        assert (await obj._hm_admit_event(e))[0] is e

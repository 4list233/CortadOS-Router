"""core._scarcity_module() under concurrent first use.

The S1 outcome loop's first cycle in provider mode loads scarcity.py for the first time inside its two run workers
(core.resolve -> lane_health), since provider.status() never touches it. The lazy loader published the module before
executing it, so the second worker got a half-executed module: "module 'cortados_router_scarcity' has no attribute
'lane_scarcity'" (Studio, 2026-11-04)."""
import importlib.machinery
import importlib.util
import threading
import time
import unittest
from unittest import mock

from cortados_router import core


class SlowLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        time.sleep(0.3)                    # widen the window a real first import has while scarcity.py executes
        super().exec_module(module)


class ConcurrentFirstUseTest(unittest.TestCase):
    def setUp(self):
        self._keep = core._sc_mod
        core._sc_mod = None

    def tearDown(self):
        core._sc_mod = self._keep

    def test_every_thread_gets_a_fully_executed_module_or_none_at_all(self):
        real = importlib.util.spec_from_file_location

        def slow_spec(name, path, *a, **kw):
            return real(name, path, loader=SlowLoader(name, str(path)))
        got, errors = [], []

        def use():
            try:
                got.append(core._scarcity_module().lane_scarcity)
            except Exception as exc:       # recorded and asserted below, never swallowed
                errors.append(exc)
        with mock.patch.object(importlib.util, "spec_from_file_location", side_effect=slow_spec):
            threads = [threading.Thread(target=use) for _ in range(4)]
            for t in threads:
                t.start()
                time.sleep(0.02)           # the later threads arrive while the first is still executing the module
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(got), 4)
        self.assertEqual(len({id(core._sc_mod)}), 1)

    def test_a_failed_load_is_not_published(self):
        real = importlib.util.spec_from_file_location

        class FailingLoader(importlib.machinery.SourceFileLoader):
            def exec_module(self, module):
                raise RuntimeError("scarcity.py failed to import")
        with mock.patch.object(importlib.util, "spec_from_file_location",
                               side_effect=lambda name, path, *a, **kw: real(name, path,
                                                                             loader=FailingLoader(name, str(path)))):
            with self.assertRaises(RuntimeError):
                core._scarcity_module()
        self.assertIsNone(core._sc_mod)    # the next call retries instead of returning a half-loaded module
        self.assertTrue(callable(core._scarcity_module().lane_scarcity))

if __name__ == "__main__":
    unittest.main()

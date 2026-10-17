"""Run only a process-local mutation; never edit a candidate source file."""
import ast
import inspect
import unittest
from acceptance.quota_sources.test_final_capture_deadline import FinalCaptureDeadline

original = FinalCaptureDeadline.setUp


def setup(self):
    original(self)
    owner = self.fixture.policy
    tree = ast.parse(inspect.getsource(owner.check_actual))
    class RemoveGuard(ast.NodeTransformer):
        count = 0
        def visit_If(self, node):
            if '_usable' in ast.unparse(node.test):
                self.count += 1
                return None
            return self.generic_visit(node)
    mutation = RemoveGuard()
    tree = mutation.visit(tree)
    assert mutation.count == 1
    ast.fix_missing_locations(tree)
    exec(compile(tree, '<QA mutant: omit final proof validity guard>', 'exec'), owner.__dict__)


FinalCaptureDeadline.setUp = setup
result = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.loadTestsFromTestCase(FinalCaptureDeadline))
assert result.testsRun == 2 and len(result.failures) == 2 and not result.errors and not result.skipped

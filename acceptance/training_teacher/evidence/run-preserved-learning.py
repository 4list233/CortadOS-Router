# PURPOSE: Preserve every 003 exam/assertion under 007's required installed label interpreter.
# Only the current label fixture gains the real writer context and inert native quota boundary.
from contextlib import contextmanager, ExitStack
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

# Load installed package search roots before any legacy fixture inserts the checkout.
import cortados_router
import integrations
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from acceptance.mcp_learning import pipeline_support as support
from cortados_router import teacher, teacher_attempts
from integrations.mcp import config

_original = support.pipeline
SITE = Path(sys.prefix) / 'lib/python3.14/site-packages'
assert Path(teacher.__file__).resolve() == SITE / 'cortados_router/teacher.py'

@contextmanager
def pipeline(name, data, *, source=None, tokenizer_missing=False):
    if name != 'label' or source is not None:
        with _original(name, data, source=source, tokenizer_missing=tokenizer_missing) as value:
            yield value
        return
    inherited = '/Users/velox/.local/share/cortados-workers/releases/quota-sources-v1-20261017/venv/lib/python3.14/site-packages'
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {'CORTADOS_TRAINING_TEACHER_V1': '0',
            'CORTADOS_MCP_LEARNING_V1': '1', 'CORTADOS_MCP_LEARNING_SITE_PACKAGES': inherited}))
        # Existing teacher transcript assertions still observe the real argv/payload builder.
        stack.enter_context(patch.object(config, 'load_config', return_value={}))
        stack.enter_context(patch.object(teacher.shutil, 'which', return_value='/synthetic/claude'))
        stack.enter_context(patch.object(teacher.quota_admission, 'prepare_actual', return_value=object()))
        stack.enter_context(patch.object(teacher.quota_admission, 'check_actual', return_value={'status': 'available'}))
        stack.enter_context(patch.object(teacher.scarcity, 'current', return_value={'lanes': {'claude': {'scarcity': 0.0}}}))
        module, traces = stack.enter_context(_original(name, data))
        original_work = module.work
        def work(job):
            if module._STORE is not None:
                return original_work(job)
            with teacher_attempts.LabelAttempts(data, validate_answers=module.valid_answers, questions=module.QUESTIONS) as store:
                module._STORE = store
                module._SNAPSHOT = module._MCP_LEARNING.bind_snapshot(data)
                try:
                    return original_work(job)
                finally:
                    module._STORE = None
        stack.enter_context(patch.object(module, 'work', work))
        yield module, traces

support.pipeline = pipeline
suite = unittest.defaultTestLoader.discover(str(ROOT / 'acceptance/mcp_learning'), top_level_dir=str(ROOT))
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1)

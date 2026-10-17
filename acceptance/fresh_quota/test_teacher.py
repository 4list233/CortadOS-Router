# PURPOSE: Exercise the actual staged nightly teacher subprocess boundary.
# CONTRACT: RT-FRESH-QUOTA-005 AC-FQ06/09; both learning modes require final quota.
# DEPENDS: Frozen label.py, relocated bridge, explicit installed-owner fixture.
# CEILING: Inert executable and subprocess double; no labels, credentials or models.
import ast
import importlib
import json
import os
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import Mock, patch

from .stage_support import STAGE, StageCase, load


class TeacherDispatch(StageCase):
    def setUp(self):
        super().setUp()
        common = ModuleType('common')
        common.DATA, common.REPO = self.root, self.root
        common.QUESTIONS = ['steps']
        common.core = NS(SYSTEM='Inert teacher fixture')
        common.read_jsonl = lambda path: []
        common.valid_answers = lambda row: True
        sys.modules['common'] = common
        (self.install / 'cortados_router/learning_data.py').write_text(
            "TEACHER_EVIDENCE_NOTE='inert fixture'\ndef teacher_payload(batch):\n"
            " return [dict(i=i, request=row['request'], unit_kind=row['unit_kind']) for i,row in enumerate(batch)]\n")
        self.binary = self.root / 'bin/claude'
        self.binary.parent.mkdir()
        self.binary.write_text('#!/bin/sh\nexit 99\n')
        self.binary.chmod(0o700)
        self.environment = {'HOME': str(self.root), 'PATH': str(self.binary.parent)}
        self.batch = [{'id': 'exam', 'request': 'inert request', 'unit_kind': 'interactive'}]

    def module(self, enabled):
        source = (STAGE / 'train/label.py').read_text()
        # Relocate only the guarded release literal in this private fixture copy.
        literals = {node.value for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Constant)
                    and isinstance(node.value, str) and node.value.endswith('/site-packages')}
        self.assertEqual(len(literals), 1)
        source = source.replace(next(iter(literals)), str(self.install))
        target = self.root / ('label_enabled.py' if enabled else 'label_disabled.py')
        target.write_text(source)
        with patch.dict(os.environ, {'CORTADOS_MCP_LEARNING_V1': '1' if enabled else '0',
                                     'CORTADOS_MCP_LEARNING_SITE_PACKAGES': str(self.install)}):
            module = load('fresh_teacher_' + str(enabled), target)
        module.STATE, module.LABELS = self.root / 'private-state', self.root / 'labels.jsonl'
        module._env = Mock(return_value=self.environment)
        return module

    def test_unknown_and_spent_have_zero_teacher_processes_in_both_learning_modes(self):
        for enabled in (False, True):
            module = self.module(enabled)
            for status in ('unknown', 'spent'):
                with self.subTest(enabled=enabled, status=status):
                    self.write_quota(status)
                    with patch.object(module.subprocess, 'run', return_value=NS(stdout=json.dumps({'result': '[]'}))) as run:
                        with self.assertRaisesRegex(RuntimeError, 'quota_' + status):
                            module.call_teacher('claude-sonnet-5', self.batch)
                        run.assert_not_called()
                    self.assertFalse(module.LABELS.exists())

    def test_known_source_uses_exact_environment_and_resolved_actual_argv(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.write_quota('available')
                module = self.module(enabled)
                owner = importlib.import_module('cortados_router.quota_admission')
                with patch.object(owner, 'auth_scope', wraps=owner.auth_scope) as canonicalize, \
                        patch.object(module.subprocess, 'run', return_value=NS(stdout=json.dumps({'result': '[]'}))) as run:
                    answers, _ = module.call_teacher('claude-sonnet-5', self.batch)
                self.assertEqual(answers, [])
                module._env.assert_called_once()
                run.assert_called_once()
                canonicalize.assert_called_once()
                self.assertEqual(run.call_args.args[0][0], str(self.binary.resolve()))
                self.assertIs(run.call_args.kwargs['env'], self.environment)
                self.assertIs(canonicalize.call_args.kwargs['environment'], self.environment)
                self.assertEqual(canonicalize.call_args.kwargs['argv'], tuple(run.call_args.args[0]))
                self.assertEqual(canonicalize.call_args.kwargs['executable'], str(self.binary.resolve()))
                self.assertEqual(canonicalize.call_args.args[0], 'claude-cli')

    def test_missing_teacher_executable_refuses_before_process(self):
        module = self.module(False)
        module._env.return_value = {'HOME': str(self.root), 'PATH': str(self.root / 'absent')}
        with patch.object(module.subprocess, 'run', return_value=NS(stdout=json.dumps({'result': '[]'}))) as run:
            with self.assertRaisesRegex(RuntimeError, 'quota_unknown'):
                module.call_teacher('claude-sonnet-5', self.batch)
            run.assert_not_called()

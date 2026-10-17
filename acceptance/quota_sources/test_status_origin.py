# PURPOSE: File-loaded shared status must bind its exact sibling quota bridge.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS03/07; no ambient module policy substitution.
import importlib
from pathlib import Path
import sys
from types import ModuleType
from unittest.mock import Mock, patch

from acceptance.fresh_quota.stage_support import StageCase, load


class SharedStatusOrigin(StageCase):
    def _owner_and_scarcity(self):
        source = self.install / 'cortados_router/quota_observations.py'
        source.write_text("def status_record(lane, *, config, environment=None):\n return {'source':'native fixture','windows':[]}\n")
        owner = importlib.import_module('cortados_router.quota_observations')
        # train/common.py -> core.py -> scarcity.py uses file loaders, not a
        # plugin-package import. Preserve that maintained caller's geometry.
        sys.path[:] = [entry for entry in sys.path
                       if Path(entry).name != 'cortados-router']
        scarcity = load('quota_sources_origin_scarcity',
                        self.root / 'plugins/cortados-router/scarcity.py')
        return owner, scarcity

    def test_file_loaded_cursor_status_does_not_require_plugin_on_sys_path(self):
        owner, scarcity = self._owner_and_scarcity()
        sys.modules.pop('quota_policy', None)
        with patch.object(owner, 'status_record', wraps=owner.status_record) as status:
            self.assertEqual(scarcity._cursor(), {'source': 'native fixture', 'windows': []})
        status.assert_called_once()

    def test_cursor_status_ignores_unrelated_cached_quota_policy(self):
        owner, scarcity = self._owner_and_scarcity()
        unrelated = ModuleType('quota_policy')
        unrelated.cursor_status = Mock(return_value={'source': 'unrelated policy', 'windows': []})
        with patch.dict(sys.modules, {'quota_policy': unrelated}), \
             patch.object(owner, 'status_record', wraps=owner.status_record) as status:
            self.assertEqual(scarcity._cursor(), {'source': 'native fixture', 'windows': []})
        unrelated.cursor_status.assert_not_called()
        status.assert_called_once()

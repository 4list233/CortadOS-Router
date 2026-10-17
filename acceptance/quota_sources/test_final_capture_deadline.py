# PURPOSE: Private native validity must survive final source recapture itself.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS04/06; fresh quota cannot extend token proof.
import time
import unittest
from unittest.mock import patch

from acceptance.quota_sources import test_actual_protocol as protocol_fixture


class FinalCaptureDeadline(unittest.TestCase):
    def setUp(self):
        self.fixture = protocol_fixture.ActualSourceProtocol('test_prepare_captures_without_permission_or_child_io')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def _cross_deadline_during_final_capture(self, cached):
        f = self.fixture
        wall = [time.time()]
        f.control['valid_until_epoch'] = wall[0] + f.config['limits']['startup_timeout_s'] + 1
        f.write_control()
        with patch.object(time, 'time', side_effect=lambda: wall[0]):
            actual = f.prepare()
            if cached:
                self.assertEqual(f.check(actual)['status'], 'available')
            capture = f.capture.side_effect
            count = [0]
            def recapture(*args, **kwargs):
                result = capture(*args, **kwargs)
                count[0] += 1
                if count[0] == 2:
                    # Final source hashing crosses proof validity minus startup;
                    # quota itself is only two seconds old (under the 30s cap).
                    wall[0] += 2
                return result
            f.capture.side_effect = recapture
            result = f.check(actual)
        self.assertEqual(count[0], 2, 'Exam must reach both real source rechecks')
        self.assertEqual(result['status'], 'unknown', 'Final recapture crossed native validity')
        self.assertEqual(len(f.launches), 1, 'Cached case must reuse the prior source read')

    def test_new_observation_expiring_during_final_capture_refuses(self):
        self._cross_deadline_during_final_capture(cached=False)

    def test_cached_observation_expiring_during_final_capture_refuses(self):
        self._cross_deadline_during_final_capture(cached=True)

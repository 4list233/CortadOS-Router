# PURPOSE: Pin private source lifetime and exact native proof boundaries.
# CONTRACT: RT-QUOTA-SOURCES-006; no native request, credential access or live writes.
# DEPENDS: Existing source/admission/collector owners; synthetic in-memory source seam.
import pickle
import unittest
from unittest.mock import patch
from cortados_router import quota_admission as admission, quota_observations as collector, quota_sources as sources


class PrivateSourceLifetime(unittest.TestCase):
    def setUp(self):
        self.generation = b'private-source-one'
        self.config = {'limits': {'startup_timeout_s': 3, 'metadata_max_age_s': 30}}
        self.capture = self.enterContext(patch.object(sources, 'capture_source', side_effect=self.source))
        self.collect = self.enterContext(patch.object(collector, 'collect_actual', side_effect=AssertionError('Refusal must precede transport')))

    def source(self, *args, **kwargs):
        return sources.SourceCapture(profile=sources.PROFILE, lane='cursor', source_argv=('--model', 'auto'),
                                     material=self.generation, files=())

    def prepare(self):
        return admission.prepare_actual('cursor-cli', environment={'HOME': '/fixture', 'PRIVATE': 'sentinel'},
            executable='/fixture/cursor-agent', argv=('/fixture/cursor-agent', '-p', '--model', 'auto'),
            cwd='/fixture', config=self.config)

    def test_changed_source_retires_original_context_even_if_old_bytes_return(self):
        actual = self.prepare()
        self.generation = b'private-source-two'
        self.assertEqual(admission.check_actual('cursor-cli', 'auto', actual=actual, config=self.config)['status'], 'unknown')
        self.generation = b'private-source-one'
        self.assertEqual(admission.check_actual('cursor-cli', 'auto', actual=actual, config=self.config)['status'], 'unknown')
        self.collect.assert_not_called()

    def test_actual_model_mismatch_refuses_before_source_query(self):
        actual = self.prepare()
        self.assertEqual(admission.check_actual('cursor-cli', 'other-model', actual=actual, config=self.config)['status'], 'unknown')
        self.collect.assert_not_called()

    def test_context_and_capture_cannot_serialize_private_material(self):
        for value in (self.prepare(), self.source()):
            self.assertNotIn('private-source', repr(value))
            self.assertNotIn('/fixture', repr(value))
            with self.assertRaises(TypeError):
                pickle.dumps(value)


class NativeProofBoundary(unittest.TestCase):
    def test_validity_deadline_includes_exact_configured_startup_allowance(self):
        config = {'limits': {'startup_timeout_s': 3, 'metadata_max_age_s': 30}}
        row = {'observed_at': 100, 'observed_monotonic': 200, 'error': None, '_valid_until_epoch': 103}
        with patch('time.time', return_value=100), patch('time.monotonic', return_value=200):
            self.assertTrue(collector._usable(row, config))
            self.assertFalse(collector._usable(dict(row, _valid_until_epoch=102.999), config))
            self.assertFalse(collector._usable(dict(row, _valid_until_epoch=True), config))

    def test_cursor_keeps_zero_fractional_percent_and_native_cycle_geometry(self):
        payload = {'billingCycleStart': '-1000', 'billingCycleEnd': '1000',
                   'planUsage': {'totalPercentUsed': 0, 'autoPercentUsed': 0, 'apiPercentUsed': 12.3456789}}
        windows = sources.cursor_record(payload, observed_at=0)['windows']
        self.assertEqual([w['used_pct'] for w in windows], [0, 0, 12.3456789])
        self.assertTrue(all(w['reset_epoch'] == 1 and w['window_s'] == 2 for w in windows))


class ProbeAcquisitionCleanup(unittest.TestCase):
    def test_unregistered_probe_never_signals_an_already_reaped_child(self):
        from cortados_router import probe_reaper
        with patch.object(probe_reaper, 'untrack', return_value=None), \
             patch.object(probe_reaper.os, 'waitpid', side_effect=ChildProcessError), \
             patch.object(probe_reaper.os, 'kill') as kill, \
             patch.object(probe_reaper.os, 'close') as close:
            with self.assertRaisesRegex(RuntimeError, 'ownership is unavailable'):
                probe_reaper.reap(123, 456, None)
        kill.assert_not_called()
        close.assert_called_once_with(456)

    def test_direct_child_reap_has_finite_cleanup_allowance(self):
        from cortados_router import probe_reaper
        with patch.object(probe_reaper.os, 'waitpid', return_value=(0, 0)) as wait, \
             patch.object(probe_reaper.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'cleanup allowance'):
                probe_reaper._wait_child(123)
        self.assertEqual(wait.call_count, 30)
        self.assertEqual(sleep.call_count, 30)


if __name__ == '__main__':
    unittest.main()

# PURPOSE: Pin exact native Cursor cycle and pool projection independently.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS01/QS03; profile enablement is a separate gate.
# DEPENDS: Captured native response and the existing typed WorkerError owner.
import unittest

from acceptance.quota_sources.cursor_projection_cases import CursorProjectionCases
from integrations.mcp.schemas import WorkerError


class CursorNativeProjection(CursorProjectionCases, unittest.TestCase):
    def project(self, payload, now):
        from cortados_router.quota_sources import cursor_record
        return cursor_record(payload, observed_at=now)

    def assert_unknown(self, payload, now):
        with self.assertRaises(WorkerError) as caught:
            self.project(payload, now)
        self.assertEqual(caught.exception.code, 'quota_unknown')
        self.assertEqual(str(caught.exception), 'Native quota data is invalid')

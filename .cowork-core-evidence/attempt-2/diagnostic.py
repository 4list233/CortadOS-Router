# PRIVATE DIAGNOSTIC ONLY: synthetic boot identity replaces sandbox-denied sysctl.
# Unchanged exams invoked selectively; not acceptance or host containment evidence.
import sys, unittest
from unittest.mock import patch
sys.path.insert(0, '.')
names = [
'acceptance.cowork_mcp.test_tasks.BuildLifecycleRegressionExam',
'acceptance.cowork_mcp.test_recovery.BuildIntegrityRegressionExam.test_remote_job_audit_failure_latches_admission_and_emits_fatal_signal',
'acceptance.cowork_mcp.test_recovery.BuildIntegrityRegressionExam.test_runtime_ready_and_disable_each_have_one_durable_transition',
'acceptance.cowork_mcp.test_recovery.BuildIntegrityRegressionExam.test_missed_monitor_deadline_closes_admission_after_scheduler_stall',
'acceptance.cowork_mcp.test_recovery.BuildIntegrityRegressionExam.test_timely_monitor_control_preserves_admission',
'acceptance.cowork_mcp.test_recovery.BuildIntegrityRegressionExam.test_one_cleanup_integrity_failure_cannot_leave_another_descriptor_open',
'acceptance.cowork_mcp.test_recovery.BuildIntegrityRegressionExam.test_acceptance_audit_failure_is_fatal_without_job',
'acceptance.cowork_mcp.test_recovery.BuildIntegrityRegressionExam.test_lease_audit_failure_is_fatal_without_descriptor',
'acceptance.cowork_mcp.test_recovery.BuildIntegrityRegressionExam.test_malformed_frame_audit_failure_closes_endpoint_and_latches',
'acceptance.cowork_mcp.test_compatibility.BuildHttpDeadlineRegressionExam',
]
with patch('integrations.mcp.native_process.host_boot_id', return_value='private-diagnostic-boot'):
 result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
sys.exit(not result.wasSuccessful())

# PRIVATE DIAGNOSTIC ONLY. No process/native execution or containment evidence.
# Synthetic boot identity and run_worker; real acceptance, stores, snapshot and IPC.
import sys, unittest, json, copy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, '.')
from acceptance.cowork_mcp.support import FIXTURE, mod
async def synthetic_worker(broker, record):
    runtime = broker.core_runtime
    binding = runtime.binding(record)
    task = runtime.task(binding.seed.task_id)
    mod('task_workspace').prepare(task, binding)
    control = json.loads((Path(broker.config['codex_home']) / 'fixture-control.json').read_text())
    for name, encoded in control.get('output_hex', {}).items():
        (Path(record['cwd']) / 'outputs' / name).write_bytes(bytes.fromhex(encoded))
    broker.store.transition(record['job_id'], 'running')
    result = mod('job_store').failure_result(record, 'native_failed', 'Private synthetic result')
    broker.store.transition(record['job_id'], 'succeeded', result=result)
    await runtime.publish(record['job_id'])
names = [
 'acceptance.cowork_mcp.test_native_policy.BuildPublicationRegressionExam',
 'acceptance.cowork_mcp.test_recovery.BuildIntegrityRegressionExam.test_binding_derived_task_index_must_equal_immutable_seed',
]
with patch('integrations.mcp.native_process.host_boot_id', return_value='private-diagnostic-boot'), patch('integrations.mcp.broker.Broker.run_worker', new=synthetic_worker):
 result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
sys.exit(not result.wasSuccessful())

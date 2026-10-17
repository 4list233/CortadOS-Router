# PURPOSE: Preserve raw prebuild failures and exact discovered methods without counting skips as success.
# CONTRACT: RT-QUOTA-SOURCES-006 independent exam freeze; not a candidate acceptance runner.
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

def identities(suite):
    result = []
    for test in suite:
        result.extend(identities(test) if isinstance(test, unittest.TestSuite) else [test.id()])
    return result

suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), top_level_dir=str(ROOT))
methods = identities(suite)
result = unittest.TextTestRunner(verbosity=2).run(suite)
report = {'baseline_revision': subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
          'methods': methods, 'tests':result.testsRun, 'failure_records':len(result.failures),
          'error_records':len(result.errors), 'skips':len(result.skipped),
          'failing_cases':[test.id() for test,_ in result.failures],
          'errored_cases':[test.id() for test,_ in result.errors],
          'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in sorted(Path(__file__).parent.rglob('*')) if p.is_file() and p.suffix in ('.py','.cjs','.json') and 'evidence' not in p.parts},
          'purpose':'Prebuild red gate. Missing owner APIs are errors; behavioral regressions are failures. No passing implementation claim.'}
Path(sys.argv[1]).write_text(json.dumps(report,indent=2)+'\n')
sys.exit(0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1)

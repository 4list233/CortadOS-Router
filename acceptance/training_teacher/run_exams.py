# PURPOSE: Count every independent teacher exam, including errors and skips, and preserve raw ground truth.
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern='test_*.py', top_level_dir=str(ROOT))
result = unittest.TextTestRunner(verbosity=2).run(suite)
report = {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
          'skips': len(result.skipped), 'pass': result.wasSuccessful() and not result.skipped,
          'failure_ids': [test.id() for test, _ in result.failures],
          'error_ids': [test.id() for test, _ in result.errors]}
if len(sys.argv) > 1:
    Path(sys.argv[1]).write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report))
sys.exit(0 if report['pass'] else 1)

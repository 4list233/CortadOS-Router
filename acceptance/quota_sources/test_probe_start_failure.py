# PURPOSE: Identity-acquisition failure must contain the freshly forked probe.
# CONTRACT: RT-QUOTA-SOURCES-006 AC-QS05; refusal never abandons owned processes/fds.
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import patch

from cortados_router import scarcity
from integrations.mcp import native_process


class ProbeStartFailure(unittest.TestCase):
    def _identity_failure(self, error):
        children = []
        real_fork = scarcity.pty.fork
        def fork():
            pid, fd = real_fork()
            if pid:
                children.append((pid, fd))
            return pid, fd
        with tempfile.TemporaryDirectory(prefix='quota-probe-start-failure-') as folder:
            try:
                identity_failure = (patch.object(native_process, 'process_identity', side_effect=error)
                                    if error else patch.object(native_process, 'process_identity', return_value=None))
                with patch.object(scarcity, 'PROBE_DIR', Path(folder)), \
                     patch.object(scarcity.pty, 'fork', side_effect=fork), \
                     identity_failure:
                    with self.assertRaises((RuntimeError, OSError)):
                        scarcity.tui_capture([sys.executable, '-I', '-c', 'import time; time.sleep(60)'],
                            '/usage', 'DONE', dict(os.environ), boot_s=.01, run_s=.01)
                self.assertEqual(len(children), 1)
                pid, fd = children[0]
                # No polling grace is needed: a failed start must finish cleanup before returning.
                alive = native_process.process_identity(pid) is not None
                try:
                    os.fstat(fd)
                    fd_open = True
                except OSError:
                    fd_open = False
            finally:
                # An unreaped direct fork owns this PID; independent cleanup keeps red contained.
                for pid, fd in children:
                    try:
                        waited, _ = os.waitpid(pid, os.WNOHANG)
                        if not waited:
                            os.kill(pid, signal.SIGKILL)
                            os.waitpid(pid, 0)
                    except (ChildProcessError, ProcessLookupError):
                        pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self.assertFalse(alive, 'Identity failure leaked the freshly forked quota probe')
            self.assertFalse(fd_open, 'Identity failure leaked the quota PTY descriptor')

    def test_missing_native_identity_contains_probe_and_closes_fd(self):
        self._identity_failure(None)

    def test_native_identity_error_contains_probe_and_closes_fd(self):
        self._identity_failure(OSError('Synthetic identity unavailable'))

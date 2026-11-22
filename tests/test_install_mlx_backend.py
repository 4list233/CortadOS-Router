"""scripts/install_mlx_backend.sh reloading its LaunchAgent (#130): it waits, bounded, for launchd to release the
booted-out job and retries a bootstrap that fails with launchd's transient error 5. Run end to end in a throwaway HOME
with a fake launchctl (CORTADOS_LAUNCHCTL), a fake venv python (its guard --check exits 0) and an empty model dir: no
real launchctl, vllm-mlx or model."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "scripts" / "install_mlx_backend.sh"

# Stateful: FAKE_PRINT_LOADED = how many `print` calls still find the job; FAKE_BOOTSTRAP_FAILS = how many bootstraps
# fail, with FAKE_BOOTSTRAP_RC (default 5, "Input/output error").
FAKE_LAUNCHCTL = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
count() { n=$(cat "$FAKE_LOG.$1" 2>/dev/null || echo 0); n=$((n + 1)); echo "$n" > "$FAKE_LOG.$1"; echo "$n"; }
case "$1" in
  print)
    [ "$(count print)" -le "${FAKE_PRINT_LOADED:-0}" ] && { echo "gui/501/com.cortados.vllm-mlx = {"; exit 0; }
    echo "Could not find service" >&2; exit 113 ;;
  bootstrap)
    if [ "$(count bootstrap)" -le "${FAKE_BOOTSTRAP_FAILS:-0}" ]; then
      rc="${FAKE_BOOTSTRAP_RC:-5}"
      [ "$rc" = 5 ] && echo "Bootstrap failed: 5: Input/output error" >&2 || echo "Bootstrap failed: $rc: other" >&2
      exit "$rc"
    fi ;;
esac
exit 0
"""


@unittest.skipUnless(shutil.which("bash") and os.path.exists("/usr/bin/python3"), "needs bash and /usr/bin/python3")
class InstallMlxBackendReloadTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.fake = self.home / "fake-launchctl"
        self.fake.write_text(FAKE_LAUNCHCTL)
        os.chmod(self.fake, 0o755)
        self.log = self.home / "launchctl.log"
        venv_bin = self.home / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").write_text("#!/bin/sh\nexit 0\n")          # the guard's --check: accepted
        os.chmod(venv_bin / "python", 0o755)
        (self.home / "model").mkdir()
        cfg = json.loads((REPO / "tables" / "local-backend.example.json").read_text())
        cfg["vllm_mlx"]["venv"] = str(self.home / "venv")
        cfg["vllm_mlx"]["models"][cfg["vllm_mlx"]["model"]]["path"] = str(self.home / "model")
        self.config = self.home / "local-backend.json"
        self.config.write_text(json.dumps(cfg))

    def install(self, **fake):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "CORTADOS_LAUNCHCTL": str(self.fake),
               "CORTADOS_LAUNCHD_POLL_S": "0.01", "FAKE_LOG": str(self.log), **fake}
        return subprocess.run(["/bin/bash", str(INSTALL), "--config", str(self.config)], capture_output=True,
                              text=True, env=env, timeout=60)

    def calls(self):
        return [c.split()[0] for c in self.log.read_text().splitlines()]

    def test_a_bootstrap_that_fails_with_5_is_retried_and_the_install_succeeds(self):
        r = self.install(FAKE_BOOTSTRAP_FAILS="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), ["bootout", "print", "bootstrap", "bootstrap"])
        self.assertIn("launchctl bootstrap exited 5", r.stderr)
        self.assertIn("retrying (2 of 3)", r.stderr)
        self.assertIn("loaded com.cortados.vllm-mlx", r.stdout)

    def test_bootstrap_waits_until_launchd_has_released_the_old_job(self):
        r = self.install(FAKE_PRINT_LOADED="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), ["bootout", "print", "print", "print", "print", "bootstrap"])
        self.assertEqual(r.stderr, "")

    def test_the_wait_is_bounded_and_then_bootstraps_anyway(self):
        r = self.install(FAKE_PRINT_LOADED="1000")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls().count("print"), 21)                 # the first poll, then 20 waits
        self.assertEqual(self.calls()[-1], "bootstrap")
        self.assertIn("still loaded after bootout; bootstrapping anyway", r.stderr)

    def test_retries_are_bounded(self):
        r = self.install(FAKE_BOOTSTRAP_FAILS="100")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.calls().count("bootstrap"), 3)
        self.assertIn("failed (exit 5, try 3 of 3)", r.stderr)
        self.assertNotIn("loaded com.cortados.vllm-mlx", r.stdout)

    def test_another_bootstrap_error_is_not_retried(self):
        r = self.install(FAKE_BOOTSTRAP_FAILS="1", FAKE_BOOTSTRAP_RC="37")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.calls().count("bootstrap"), 1)
        self.assertIn("failed (exit 37, try 1 of 3)", r.stderr)
        self.assertNotIn("retrying", r.stderr)


if __name__ == "__main__":
    unittest.main()

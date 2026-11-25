import os
import tempfile

# #126 review round 6: every gate a test starts (a vllm-mlx text node) takes its epoch from a throwaway file, never the
# node's own ~/.config/cortados/local-gate-epoch (cortados_router/local_gate.py next_epoch).
os.environ["CORTADOS_LOCAL_GATE_EPOCH_FILE"] = os.path.join(tempfile.mkdtemp(prefix="cortados-test-epoch-"),
                                                            "local-gate-epoch")

# #114: no test reads the owner's API credit grants file or sees a real credit key. The config points at a throwaway
# path that does not exist (the lane is off unless a test writes its own), and any credit key in the environment the
# suite was started from is removed before a test runs.
os.environ["CORTADOS_API_CREDIT_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="cortados-test-api-credit-"),
                                                        "api-credit.json")
for _name in [n for n in os.environ if n.startswith("CORTADOS_ANTHROPIC_API_KEY")]:
    del os.environ[_name]

# Codex transcripts (cortados_router/provider/transcripts.py) are on by default in production and would be written
# under $CORTADOS_HOME, which defaults to the owner's real ~/.cortados. Off for the suite; tests/test_transcripts.py
# turns them on per test with a throwaway CORTADOS_HOME.
os.environ["CORTADOS_TRANSCRIPTS"] = "0"

# Nested child tasks (docs/NESTED-TASKS.md): a task's depth is read from the nearest child-task runner among the caller's
# process ancestors. When the suite itself runs inside a child task, that runner is the real one: only runners under the
# test's own tasks dir count here, and the child's own claim variables are removed, so an in-process start_task is a
# depth-0 start as it is on the owner's machine.
from cortados_router import child_tasks as _child_tasks  # noqa: E402

_child_tasks.ANCESTRY_SCOPE = "tasks_dir"
for _name in ("CORTADOS_TASK_ID", "CORTADOS_TASK_DEPTH", "CORTADOS_TASK_DELEGATE"):
    os.environ.pop(_name, None)

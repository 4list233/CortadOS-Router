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

import os
import tempfile

# #126 review round 6: every gate a test starts (a vllm-mlx text node) takes its epoch from a throwaway file, never the
# node's own ~/.config/cortados/local-gate-epoch (cortados_router/local_gate.py next_epoch).
os.environ["CORTADOS_LOCAL_GATE_EPOCH_FILE"] = os.path.join(tempfile.mkdtemp(prefix="cortados-test-epoch-"),
                                                            "local-gate-epoch")

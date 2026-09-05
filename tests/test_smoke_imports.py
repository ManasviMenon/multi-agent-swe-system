"""Confirms the core modules load without error -- zero API cost, catches import-time
mistakes (typos, missing dependencies, syntax errors) without needing a Gemini API key,
a marshmallow clone, or any real ticket data.
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src" / "agents"))
sys.path.insert(0, str(ROOT / "scripts"))

CORE_MODULES = [
    "eval.run_eval",
    "agent_runtime",
    "planner",
    "tester",
    "coder",
    "judge",
    "orchestrator",
    "curate_tickets",
]


def test_core_modules_import_cleanly():
    for module_name in CORE_MODULES:
        importlib.import_module(module_name)

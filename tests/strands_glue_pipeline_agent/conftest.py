import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agents" / "strands_glue_pipeline_agent"

# Each agent dir has its own top-level `utils` package; evict any cached
# version so this test session imports the strands_glue_pipeline_agent one.
for mod in [m for m in sys.modules if m == "utils" or m.startswith("utils.")]:
    del sys.modules[mod]

sys.path[:] = [p for p in sys.path if p != str(AGENT_ROOT)]
sys.path.insert(0, str(AGENT_ROOT))

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STRANDS_AGENT_ROOT = REPO_ROOT / "agents" / "strands_agent"

if str(STRANDS_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(STRANDS_AGENT_ROOT))


# Puts the repo root (for "from picomp import ...") and firmware/ (for "import proto")
# on sys.path. firmware/ goes last so nothing in it can shadow an installed package.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.append(str(ROOT / "firmware"))

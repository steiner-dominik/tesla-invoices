import sys
from pathlib import Path

# The application package lives one level up, in the repository root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

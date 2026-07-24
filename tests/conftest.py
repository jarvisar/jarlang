import sys
from pathlib import Path

# Make jarlang and support importable without installing
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

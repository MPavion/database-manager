import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent / "Notion-Claude Optimizer"
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.main import main

if __name__ == "__main__":
    main()

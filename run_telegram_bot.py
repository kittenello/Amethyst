import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[0]
sys.path.insert(0, str(ROOT))

from telegram.bot import main

if __name__ == "__main__":
    asyncio.run(main())

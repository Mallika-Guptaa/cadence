from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

# .env must load before cadence modules read SCHEDULER_TZ / CADENCE_* at import
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from cadence.slack_app import main  # noqa: E402


if __name__ == "__main__":
    main()

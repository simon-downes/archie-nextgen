"""Archie — Personal AI agent harness.

This is the top-level package init. It runs once when any module in the package
is first imported, making it the right place for process-wide setup like logging.
"""

import logging
from pathlib import Path

# Log to a file so we can debug crashes even when the TUI swallows stderr.
# The file lives alongside sessions in ~/.archie/ for easy access.
_log_dir = Path.home() / ".archie"
_log_dir.mkdir(parents=True, exist_ok=True)
_log_file = _log_dir / "archie.log"

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_file),
    ],
)

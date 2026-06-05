"""Archie — Personal AI agent harness.

This is the top-level package init. It runs once when any module in the package
is first imported, making it the right place for process-wide setup like logging.
"""

import logging

# Configure logging for the entire application.
# Level is WARNING by default — only throttle retries and errors show up.
# Set to DEBUG via code or env var when you need to trace what's happening.
# The format includes time, logger name (module path), level, and message.
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

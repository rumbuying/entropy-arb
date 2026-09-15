"""Ring buffer of recent log lines for UI event panels.

Deliberately rich-free: both the terminal dashboard and the (dependency-free
path of the) web overview attach one of these to the root logger.
"""
from __future__ import annotations

import logging
from collections import deque


class BufferLogHandler(logging.Handler):
    """Keeps the last `maxlen` formatted lines as (levelno, text) tuples."""

    def __init__(self, maxlen: int = 200) -> None:
        super().__init__()
        self.lines: deque = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:
            return
        if "[status]" in msg:
            return  # dashboards render the status line's content themselves
        self.lines.append((record.levelno, msg))

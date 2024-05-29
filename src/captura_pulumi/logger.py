# =========================================================================== #
import json
import logging
from typing import List, Set

LOG_RECORD_BUILTIN_ATTRS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
    "taskName",
}


# Thanks mCoding: https://www.youtube.com/watch?v=9L77QExPmI0
class JSONFormatter(logging.Formatter):
    fmt_keys: Set[str]

    @property
    def fmt_keys_default(self) -> Set[str]:
        return {
            "levelname",
            "message",
            "timestamp",
            "name",
            "module",
            "funcName",
            "lineno",
            "threadName",
        }

    def __init__(self, *, fmt_keys: List[str] | None = None):
        super().__init__()
        self.fmt_keys = (
            (set(fmt_keys) & LOG_RECORD_BUILTIN_ATTRS)
            if fmt_keys is not None
            else self.fmt_keys_default
        )

    def format(self, record: logging.LogRecord) -> str:
        line = {key: getattr(record, key) for key in self.fmt_keys}
        if record.exc_info is not None:
            line.update(exc_info=self.formatException(record.exc_info))
        if record.stack_info is not None:
            line.update(stack_info=self.formatStack(record.stack_info))

        return json.dumps(line, default=str)

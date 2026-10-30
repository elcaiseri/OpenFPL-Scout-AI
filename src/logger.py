import json
import logging
import os
from datetime import datetime, timezone
from typing import Mapping, Optional

TEXT_FORMAT = "[%(asctime)s] %(levelname)s %(filename)s - %(message)s"


class CloudLoggingFormatter(logging.Formatter):
    """Write one JSON object per line, as Cloud Logging expects.

    Cloud Run parses each line into a structured entry, so ``severity`` is
    filterable and an exception's traceback stays in one entry (which Error
    Reporting groups) instead of one DEFAULT entry per line.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message = f"{message}\n{self.formatException(record.exc_info)}"
        if record.stack_info:
            message = f"{message}\n{self.formatStack(record.stack_info)}"
        return json.dumps(
            {
                "severity": record.levelname,
                "message": message,
                "time": datetime.fromtimestamp(
                    record.created, timezone.utc
                ).isoformat(),
                "logger": record.name,
                "logging.googleapis.com/sourceLocation": {
                    "file": record.pathname,
                    "line": record.lineno,
                    "function": record.funcName,
                },
            },
            ensure_ascii=False,
            default=str,
        )


def uses_json_logs(environment: Optional[Mapping[str, str]] = None) -> bool:
    """JSON on Cloud Run (or when asked for); readable text everywhere else."""
    values = os.environ if environment is None else environment
    configured = values.get("OPENFPL_LOG_FORMAT", "").strip().lower()
    if configured in {"json", "text"}:
        return configured == "json"
    return bool(values.get("K_SERVICE"))


def get_logger(name: Optional[str] = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = (
            CloudLoggingFormatter()
            if uses_json_logs()
            else logging.Formatter(TEXT_FORMAT)
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

"""Cloud Run logs must carry a severity Cloud Logging can filter on."""

import json
import logging
import sys
import unittest
from unittest.mock import patch

from src.logger import CloudLoggingFormatter, get_logger, uses_json_logs


def record(
    level=logging.WARNING, message="Skipping player %d", args=(26,), exc_info=None
):
    return logging.LogRecord(
        name="src.official_fpl",
        level=level,
        pathname="/app/src/official_fpl.py",
        lineno=795,
        msg=message,
        args=args,
        exc_info=exc_info,
        func="player_history",
    )


class CloudLoggingFormatterTests(unittest.TestCase):
    def test_a_line_is_one_json_entry_with_its_severity(self):
        entry = json.loads(CloudLoggingFormatter().format(record()))

        self.assertEqual(entry["severity"], "WARNING")
        self.assertEqual(entry["message"], "Skipping player 26")
        self.assertEqual(entry["logger"], "src.official_fpl")
        self.assertEqual(
            entry["logging.googleapis.com/sourceLocation"],
            {
                "file": "/app/src/official_fpl.py",
                "line": 795,
                "function": "player_history",
            },
        )
        self.assertTrue(entry["time"].endswith("+00:00"))

    def test_a_traceback_stays_in_the_same_entry(self):
        try:
            raise ValueError("bad payload")
        except ValueError:
            exc_info = sys.exc_info()

        line = CloudLoggingFormatter().format(
            record(logging.ERROR, "Scout inference failed", (), exc_info)
        )

        self.assertEqual(len(line.splitlines()), 1)
        entry = json.loads(line)
        self.assertEqual(entry["severity"], "ERROR")
        self.assertTrue(entry["message"].startswith("Scout inference failed\n"))
        self.assertIn("Traceback (most recent call last)", entry["message"])
        self.assertIn("ValueError: bad payload", entry["message"])


class LogFormatChoiceTests(unittest.TestCase):
    def test_cloud_run_logs_json(self):
        self.assertTrue(uses_json_logs({"K_SERVICE": "openfpl-scout-ai"}))

    def test_local_runs_keep_readable_text(self):
        self.assertFalse(uses_json_logs({}))

    def test_the_environment_variable_overrides_the_default(self):
        self.assertFalse(
            uses_json_logs(
                {"K_SERVICE": "openfpl-scout-ai", "OPENFPL_LOG_FORMAT": "text"}
            )
        )
        self.assertTrue(uses_json_logs({"OPENFPL_LOG_FORMAT": "JSON"}))

    def test_get_logger_uses_the_chosen_format(self):
        with patch.dict("os.environ", {"K_SERVICE": "openfpl-scout-ai"}):
            logger = get_logger("tests.cloud-run-format")
        with patch.dict("os.environ", {"OPENFPL_LOG_FORMAT": "text"}):
            local = get_logger("tests.local-format")

        self.assertIsInstance(logger.handlers[0].formatter, CloudLoggingFormatter)
        self.assertNotIsInstance(local.handlers[0].formatter, CloudLoggingFormatter)


if __name__ == "__main__":
    unittest.main()

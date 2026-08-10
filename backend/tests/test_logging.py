import json
import logging

from cti_app.logging import JsonFormatter


def test_json_formatter_emits_structured_record() -> None:
    record = logging.LogRecord("cti.test", logging.INFO, __file__, 1, "service ready", (), None)
    record.correlation_id = "corr-123"
    record.http_status = 200

    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["message"] == "service ready"
    assert payload["correlation_id"] == "corr-123"
    assert payload["http_status"] == 200

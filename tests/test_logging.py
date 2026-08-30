"""Structured logging.

The reserved-attribute check exists because the collision it catches is invisible until
logging is actually turned on: ``Logger.info`` never builds a record while the level is
above INFO, so a bad ``extra`` key passes every test that does not configure logging and
then raises on the first real start. It cost one startup to find, and one test to never
find again.
"""

from __future__ import annotations

import ast
import io
import json
import logging
import pathlib

import pytest

import localplane
from localplane.log import JsonFormatter, configure_logging

PACKAGE_ROOT = pathlib.Path(localplane.__file__).parent
RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None)))


def _extra_keys() -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg == "extra" and isinstance(keyword.value, ast.Dict):
                    for key in keyword.value.keys:
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            found.append(
                                (str(path.relative_to(PACKAGE_ROOT)), key.lineno, key.value)
                            )
    return found


def test_no_log_field_collides_with_a_reserved_record_attribute():
    collisions = [entry for entry in _extra_keys() if entry[2] in RESERVED]
    assert collisions == [], f"logging.extra keys that would raise at runtime: {collisions}"


def test_there_are_structured_log_fields_to_check():
    assert len(_extra_keys()) > 10, "the audit above would pass vacuously"


def test_records_render_as_one_json_object_per_line():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    logging.getLogger("localplane.test").info("something happened", extra={"host_id": "host_x"})
    line = stream.getvalue().strip()
    payload = json.loads(line)
    assert "\n" not in line
    assert payload["message"] == "something happened"
    assert payload["level"] == "info"
    assert payload["host_id"] == "host_x"
    assert payload["logger"] == "localplane.test"


def test_exceptions_are_carried_in_the_record():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    try:
        raise ValueError("the actual problem")
    except ValueError:
        logging.getLogger("localplane.test").exception("it failed")
    assert "the actual problem" in json.loads(stream.getvalue().strip())["exception"]


def test_configuring_twice_does_not_duplicate_output():
    stream = io.StringIO()
    configure_logging("INFO", stream)
    configure_logging("INFO", stream)
    logging.getLogger("localplane.test").info("once")
    assert len(stream.getvalue().strip().splitlines()) == 1


def test_the_formatter_never_raises_on_an_unserialisable_field():
    record = logging.LogRecord("t", logging.INFO, "f", 1, "m", (), None)
    record.thing = object()
    assert json.loads(JsonFormatter().format(record))["thing"]


@pytest.fixture
def restore_logging():
    yield
    configure_logging("WARNING")

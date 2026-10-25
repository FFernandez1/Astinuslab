"""Unit tests for output_parsers — all pure functions, zero dependencies."""

from __future__ import annotations

import pytest

from astinuslab.core.output_parsers import (
    parse_keyed_output,
    parse_metadata,
    parse_required_keyed_output,
)


class TestParseMetadata:
    def test_extracts_json_from_clean_output(self):
        output = '{"distro": "ubuntu", "version": "22.04"}'
        result = parse_metadata(output)
        assert result == {"distro": "ubuntu", "version": "22.04"}

    def test_extracts_json_embedded_in_noise(self):
        output = 'some prefix\n{"key": "val"}\nsome suffix'
        result = parse_metadata(output)
        assert result == {"key": "val"}

    def test_raises_when_no_json_braces(self):
        with pytest.raises(RuntimeError, match="Could not parse guest metadata JSON"):
            parse_metadata("no json here")

    def test_raises_on_empty_string(self):
        with pytest.raises(RuntimeError):
            parse_metadata("")


class TestParseKeyedOutput:
    def test_parses_single_key_value(self):
        assert parse_keyed_output("KEY=value") == {"KEY": "value"}

    def test_parses_multiple_lines(self):
        output = "DISTRO=ubuntu\nVERSION=22.04\nARCH=x86_64"
        result = parse_keyed_output(output)
        assert result == {"DISTRO": "ubuntu", "VERSION": "22.04", "ARCH": "x86_64"}

    def test_skips_lines_without_equals(self):
        output = "not-a-key-value\nKEY=val"
        assert parse_keyed_output(output) == {"KEY": "val"}

    def test_value_can_contain_equals(self):
        output = "URL=http://example.com/path?a=1"
        assert parse_keyed_output(output) == {"URL": "http://example.com/path?a=1"}

    def test_strips_whitespace_from_keys_and_values(self):
        assert parse_keyed_output("  KEY  =  value  ") == {"KEY": "value"}

    def test_empty_string_returns_empty_dict(self):
        assert parse_keyed_output("") == {}


class TestParseRequiredKeyedOutput:
    def test_returns_dict_when_all_required_keys_present(self):
        result = parse_required_keyed_output(
            "DISTRO=ubuntu\nVERSION=22.04",
            required_keys={"DISTRO", "VERSION"},
            context="test",
        )
        assert result["DISTRO"] == "ubuntu"

    def test_raises_when_required_key_missing(self):
        with pytest.raises(RuntimeError, match="missing required keys"):
            parse_required_keyed_output(
                "DISTRO=ubuntu",
                required_keys={"DISTRO", "VERSION"},
                context="test-context",
            )

    def test_error_message_includes_context(self):
        with pytest.raises(RuntimeError, match="test-context"):
            parse_required_keyed_output("", required_keys={"KEY"}, context="test-context")

    def test_error_message_includes_missing_key_names(self):
        with pytest.raises(RuntimeError, match="MISSING_KEY"):
            parse_required_keyed_output("OTHER=x", required_keys={"MISSING_KEY"}, context="ctx")

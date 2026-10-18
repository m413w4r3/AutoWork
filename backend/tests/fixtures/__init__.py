"""MWCP parser package used only by safe integration tests."""

from pathlib import Path

config = str(Path(__file__).with_name("mwcp_safe_fixture_parsers.yml"))

"""Harmless parser fixture used to verify the confined MWCP invocation path."""

import mwcp


class AutoworkSafeFixture(mwcp.Parser):  # type: ignore[misc]  # mwcp ships no type information
    DESCRIPTION = "AutoWork safe invocation fixture"
    AUTHOR = "AutoWork tests"
    PARSER_VERSION = "fixture-v1"

    @classmethod
    def identify(cls, _file_object):
        return True

    def run(self):
        self.report.add_metadata("other", {"safe_fixture": "invoked"})

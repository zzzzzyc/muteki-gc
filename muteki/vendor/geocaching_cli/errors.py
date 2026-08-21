"""Vendored error types from geocaching-cli.

Source: https://github.com/zzzzzyc/geocaching-cli
Commit: 4273699aa1dfc7da09e75eb4211ad923e4fd0bd3

Minimal subset required by ``coord.py``. Messages and class names match
upstream ``src/geocaching_cli/errors.py`` at that commit.
"""


class GeoCLIError(Exception):
    """Base error for expected, reportable failures."""


class CoordError(GeoCLIError):
    """Coordinate parse or computation failure."""

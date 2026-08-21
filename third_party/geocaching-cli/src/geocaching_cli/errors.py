"""User-facing error types. Messages may be localized at the CLI boundary."""


class GeoCLIError(Exception):
    """Base error for expected, reportable failures."""


class CoordError(GeoCLIError):
    """Coordinate parse or computation failure."""


class ImportError_(GeoCLIError):
    """GPX / Pocket Query import failure."""


class StoreError(GeoCLIError):
    """Local database failure."""


class LiveError(GeoCLIError):
    """Optional live (geocaching.com) adapter failure."""


class LiveLoginError(LiveError):
    """Login failed (bad credentials, CAPTCHA, network, ToS challenge)."""

    def __init__(self, message: str, *, captcha: bool = False) -> None:
        super().__init__(message)
        self.captcha = captcha

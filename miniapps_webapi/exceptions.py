"""MiniApps API exceptions."""


class MiniAppsError(Exception):
    """Raised when the API returns an error status."""

    def __init__(self, status_code: int, message: str, body: dict | None = None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = status_code
        self.message = message
        self.body = body or {}


class InsufficientCreditsError(MiniAppsError):
    """Raised when the account has run out of credits (HTTP 412)."""

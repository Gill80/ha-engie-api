"""ENGIE Particuliers client exceptions."""


class EngieError(Exception):
    """Base error for the ENGIE Particuliers client."""


class AuthenticationError(EngieError):
    """Login or token validation failed."""


class MfaRequiredError(AuthenticationError):
    """Okta primary auth succeeded but a second factor is required."""

    def __init__(self, state_token: str, factors: list[dict]):
        self.state_token = state_token
        self.factors = factors
        kinds = ", ".join(
            str(f.get("factorType") or f.get("id") or "?") for f in factors
        ) or "unknown"
        super().__init__(f"MFA required ({kinds}). Call login() again with mfa_code.")


class LockedOutError(AuthenticationError):
    """Too many failed login attempts."""


class PasswordExpiredError(AuthenticationError):
    """The account password must be changed before login can complete."""


class ApiError(EngieError):
    """HTTP call succeeded at the transport layer but the backend rejected it."""

    def __init__(self, status_code: int, message: str = "", body: object | None = None):
        self.status_code = status_code
        self.api_message = message
        self.body = body
        detail = message or str(body) or ""
        super().__init__(f"{status_code}: {detail}" if detail else str(status_code))

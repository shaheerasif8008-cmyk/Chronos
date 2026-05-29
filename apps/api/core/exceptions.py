class ChronosError(Exception):
    """Base class for all Chronos application errors."""


class RateLimitExceeded(ChronosError):
    """Raised when an org exceeds the per-minute action rate limit."""

    def __init__(self, org_id: str, count: int, limit: int) -> None:
        self.org_id = org_id
        self.count = count
        self.limit = limit
        super().__init__(f"Rate limit exceeded for org {org_id}: {count}/{limit} actions/minute")


class LoopDetected(ChronosError):
    """Raised when the same tool+args is called >= 10 times in 5 minutes."""

    def __init__(self, tool: str, count: int) -> None:
        self.tool = tool
        self.count = count
        super().__init__(f"Loop detected: {tool} called {count} times in 5-minute window")


class SafetyLimitViolation(ChronosError):
    """Raised when a tool call violates a hard safety limit."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Safety limit violated: {reason}")


class ApprovalRequired(ChronosError):
    """Raised when a tool call requires a human approval record before executing."""

    def __init__(self, tool: str, reason: str = "") -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(f"Approval required for {tool}" + (f": {reason}" if reason else ""))


class VaultError(ChronosError):
    """Raised when credential vault operations fail."""


class PermissionDenied(ChronosError):
    """Raised when an authorization check denies an action (OpenFGA enforcement)."""

    def __init__(self, actor_id: str, action: str, resource: str) -> None:
        self.actor_id = actor_id
        self.action = action
        self.resource = resource
        super().__init__(f"Permission denied: {actor_id} cannot {action} on {resource}")


class ConnectorNotFound(ChronosError):
    """Raised when no connector record matches the requested org/provider."""

    def __init__(self, org_id: str, provider: str) -> None:
        self.org_id = org_id
        self.provider = provider
        super().__init__(f"No active connector found for org={org_id} provider={provider}")

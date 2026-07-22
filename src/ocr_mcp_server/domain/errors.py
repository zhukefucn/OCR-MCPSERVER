"""Safe, stable errors shared across service layers."""

from __future__ import annotations


class DomainError(Exception):
    """Base error whose string representation is safe for clients and logs."""

    code = "domain_error"
    safe_message = "The operation could not be completed."

    def __init__(self, *, cause: BaseException | None = None) -> None:
        self.cause = cause
        super().__init__(self.safe_message)


class ConfigurationError(DomainError):
    """Raised when deployment configuration cannot be loaded or validated."""

    code = "configuration_invalid"
    safe_message = "Service configuration is invalid."


class InputValidationError(DomainError):
    """Raised when an incoming business input violates the service contract."""

    code = "input_invalid"
    safe_message = "Input validation failed."


class StateTransitionError(DomainError):
    """Raised when a requested task state change violates domain rules."""

    code = "state_transition_invalid"
    safe_message = "Task state transition is invalid."


class LeaseConflictError(DomainError):
    """Raised when a task lease is absent, expired, or does not match."""

    code = "lease_conflict"
    safe_message = "Task lease is invalid or expired."


class PersistenceError(DomainError):
    """Raised when task persistence fails without exposing database details."""

    code = "persistence_error"
    safe_message = "Task persistence operation failed."

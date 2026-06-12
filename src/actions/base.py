"""Action exceptions (ADR-043, Phase 2.2).

TaskNoop / TaskDeferred, the control-flow signals raised by scheduler actions,
moved out of src/builtin_actions.py into this dependency-free leaf module so the
src/actions/* submodules can import them without a circular import back to
builtin_actions. Re-imported into builtin_actions so existing
`from src.builtin_actions import TaskNoop` / `TaskDeferred` callers are unchanged.
"""


class TaskNoop(BaseException):
    """Raised by an action when it determined there's nothing to do.

    Inherits from BaseException (not Exception) so the standard
    `except Exception` wrappers each action uses for real error handling
    don't accidentally catch it. The scheduler explicitly catches TaskNoop,
    drops the queued TaskRun row, advances last_run / next_run, and exits
    silently. Nothing appears in the Activity log; the message is logged
    server-side only.
    """


class TaskDeferred(BaseException):
    """Raised when a task should run later without recording a skipped run."""

    def __init__(self, reason: str, delay_seconds: int = 20 * 60):
        super().__init__(reason)
        self.reason = reason
        self.delay_seconds = delay_seconds

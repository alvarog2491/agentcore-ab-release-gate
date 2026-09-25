"""Logging that emits GitHub Actions workflow commands (::error::, ::warning::) to stdout."""

import logging
import sys

LOGGER_NAME = "agentcore_release_gate"


class _WorkflowCommandHandler(logging.Handler):
    """Writes to the CURRENT sys.stdout on every emit.

    A plain logging.StreamHandler binds to whatever stream object exists when
    it's constructed, which is the real terminal at module-import time -- so it
    silently bypasses pytest's capsys (and any other stdout redirection) once
    installed. Resolving sys.stdout fresh on each emit, the same way the
    builtin print() does, keeps GitHub Actions' ::error::/::warning:: workflow
    commands visible to both the terminal and tests.
    """

    def emit(self, record: logging.LogRecord) -> None:
        print(self.format(record), file=sys.stdout, flush=True)


def get_workflow_logger() -> logging.Logger:
    """Return the action's logger, configured to write workflow commands to stdout.

    Safe to call repeatedly: the handler is installed only once.
    """
    logger = logging.getLogger(LOGGER_NAME)
    if not any(isinstance(handler, _WorkflowCommandHandler) for handler in logger.handlers):
        logger.setLevel(logging.INFO)
        logger.addHandler(_WorkflowCommandHandler())
        logger.propagate = False
    return logger

"""Retry reads in place, preserving registration and Solver state in memory."""
import time

from .gateway import READ_ONLY_TOOLS, McpResponseError, McpReadDeadline, _transient_transport_error


ReadDeadline = McpReadDeadline


class ReadRecovery:
    def __init__(self, gateway, sleep):
        self.gateway, self.sleep = gateway, sleep
        self.deadline = None

    def call(self, name, arguments=None):
        delay = 60
        while True:
            if self.deadline is not None and time.time() >= self.deadline:
                raise ReadDeadline("Read deadline reached")
            try:
                previous = getattr(self.gateway, "read_deadline", None)
                self.gateway.read_deadline = self.deadline
                try:
                    result = self.gateway.call(name, arguments) if arguments is not None else self.gateway.call(name)
                finally:
                    self.gateway.read_deadline = previous
                if name in READ_ONLY_TOOLS and isinstance(result, dict) and (result.get("isError") or "error" in result):
                    raise McpResponseError(result)
                if self.deadline is not None and time.time() >= self.deadline:
                    raise ReadDeadline("Read deadline reached")
                return result
            except (OSError, RuntimeError) as error:
                transient = error.retryable and not error.rejected if isinstance(error, McpResponseError) else _transient_transport_error(error)
                if name not in READ_ONLY_TOOLS or not transient:
                    raise
                wait = max(delay, getattr(error, "retry_after", 0))
                if self.deadline is not None and time.time() + wait >= self.deadline:
                    raise ReadDeadline("Read deadline reached") from None
                self.sleep(wait)
                delay = min(delay * 2, 300)

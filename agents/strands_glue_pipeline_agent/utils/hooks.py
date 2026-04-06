"""Custom Strands hooks used by the Glue pipeline agent."""

import logging
import time
from threading import Lock
from typing import Any

from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent

LOGGER = logging.getLogger(__name__)


class GlueJobRunPollThrottleHook(HookProvider):
    """
    Throttle repeated Glue `get-job-run` MCP calls for the same job run.

    This acts like middleware for tool invocations: if the model attempts to call
    `athena_manage_aws_glue_jobs` with `operation='get-job-run'` on the same
    `(job_name, job_run_id)` too quickly, the hook pauses until the minimum
    interval has elapsed.
    """

    def __init__(self, min_interval_seconds: float = 20.0):
        """
        Initialize the poll throttle hook.

        Args:
            min_interval_seconds: Minimum gap between identical get-job-run calls.
        """
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._last_call_ts_by_key: dict[tuple[str, str], float] = {}
        self._lock = Lock()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        """
        Register hook callbacks with the Strands hook registry.

        Args:
            registry: Hook registry used by the agent.
            **kwargs: Reserved for compatibility with hook provider interface.
        """
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        """
        Enforce a minimum interval for repeated `get-job-run` tool calls.

        Args:
            event: Hook event emitted before each tool invocation.
        """
        tool_use = event.tool_use
        if tool_use.get("name") != "athena_manage_aws_glue_jobs":
            return

        tool_input = tool_use.get("input")
        if not isinstance(tool_input, dict):
            return
        if tool_input.get("operation") != "get-job-run":
            return

        job_name = tool_input.get("job_name")
        job_run_id = tool_input.get("job_run_id")
        if not isinstance(job_name, str) or not job_name:
            return
        if not isinstance(job_run_id, str) or not job_run_id:
            return

        key = (job_name, job_run_id)
        now = time.monotonic()

        with self._lock:
            last_ts = self._last_call_ts_by_key.get(key)
            remaining = 0.0 if last_ts is None else self.min_interval_seconds - (now - last_ts)

            if remaining > 0:
                LOGGER.debug(
                    "Throttling repeated get-job-run call for %s/%s by %.2fs",
                    job_name,
                    job_run_id,
                    remaining,
                )
                time.sleep(remaining)
                now = time.monotonic()

            self._last_call_ts_by_key[key] = now

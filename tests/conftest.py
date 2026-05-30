"""Test-session configuration for the harness test suite.

MAF's observability layer sets a ContextVar token in the synchronous pre-flight
of _trace_agent_invocation() and then resets it inside the coroutine. When the
coroutine is scheduled via asyncio.run(), Python copies the current Context for
the root Task; the token therefore lives in a different Context object than the
one where reset() is called, raising ValueError. Disabling instrumentation causes
MAF to skip the telemetry wrapper entirely (line 1679-1680 in observability.py),
so asyncio.run(agent.run(...)) works correctly in synchronous test functions.
"""

from agent_framework.observability import disable_instrumentation

disable_instrumentation()

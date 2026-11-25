"""Tool contract: Pydantic-validated args, retry on transient errors, and a uniform
{ok, data, error} result. Failures are returned, never swallowed or raised into the graph."""

from __future__ import annotations

import os
import random
import time
from typing import Any, Callable

import psycopg
from pydantic import BaseModel, ValidationError


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: str | None = None
    attempts: int = 1


class ToolError(Exception):
    """A business error (not found, invalid state). Not retried."""


TRANSIENT = (psycopg.OperationalError, TimeoutError, ConnectionError)


def _injected_fault(tool: str, attempt: int) -> bool:
    """FAULT_INJECT="transaction_lookup:fail_first=1" or "transaction_lookup:rate=0.3".
    Demo/test only: shows retries in the trace without breaking a real dependency."""
    for spec in filter(None, os.environ.get("FAULT_INJECT", "").split(";")):
        name, _, rule = spec.partition(":")
        if name.strip() != tool:
            continue
        key, _, val = rule.partition("=")
        if key == "fail_first" and attempt <= int(val):
            return True
        if key == "rate" and random.random() < float(val):
            return True
    return False


def run_tool(fn: Callable[..., Any], args_model: type[BaseModel], raw_args: dict[str, Any],
             *, retries: int = 2, backoff_s: float = 0.2, name: str | None = None) -> ToolResult:
    tool = name or fn.__name__.lstrip("_")
    try:
        args = args_model.model_validate(raw_args)
    except ValidationError as e:
        return ToolResult(ok=False, error=f"invalid_args: {e.errors()[0]['msg']}")
    attempt = 0
    while True:
        attempt += 1
        try:
            if _injected_fault(tool, attempt):
                raise ConnectionError(f"injected fault in {tool}")
            return ToolResult(ok=True, data=fn(args), attempts=attempt)
        except ToolError as e:
            return ToolResult(ok=False, error=str(e), attempts=attempt)
        except TRANSIENT as e:
            if attempt > retries:
                return ToolResult(ok=False, error=f"transient_failure: {type(e).__name__}", attempts=attempt)
            time.sleep(backoff_s * 2 ** (attempt - 1))

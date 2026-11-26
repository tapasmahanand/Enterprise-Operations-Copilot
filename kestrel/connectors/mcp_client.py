"""Client side of the bank connector (D57): the chat graph's tool node calls through here.

One stdio MCP session is kept open in a background event-loop thread (spawning a Python
process per lookup would add about a second to every answer on a live call). Calls are
synchronous for the graph, with a hard timeout. A timeout, a crashed connector or a refusal
comes back as an ordinary failed ToolResult with a plain reason, so the graph says what
happened and drafts nothing; the session is restarted on the next call.

TOOL_TRANSPORT=direct bypasses MCP and calls the Python functions (unit tests of other parts,
emergency fallback). The default is mcp.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import sys
import threading
from contextlib import AsyncExitStack
from typing import Any

from kestrel.connectors import scope as scope_mod
from kestrel.tools.base import ToolResult

log = logging.getLogger("mcp_client")
TIMEOUT_S = float(os.environ.get("MCP_TOOL_TIMEOUT_S", "5"))


class _Connector:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: Any = None
        self._stack: AsyncExitStack | None = None
        self._lock = threading.Lock()
        self._alock: asyncio.Lock | None = None
        self._env_key: tuple | None = None

    # --- lifecycle (runs on the background loop) ---
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            threading.Thread(target=self._loop.run_forever, name="mcp-connector", daemon=True).start()
        return self._loop

    async def _open(self) -> Any:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        scope_mod.scope_key()   # make sure the child inherits the signing key
        params = StdioServerParameters(command=sys.executable, args=["-m", "kestrel.connectors.mcp_server"],
                                       env=dict(os.environ))
        stack = AsyncExitStack()
        read, write = (await stack.enter_async_context(stdio_client(params)))[:2]
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._stack, self._session = stack, session
        return session

    async def _close(self) -> None:
        stack, self._stack, self._session = self._stack, None, None
        if stack is not None:
            try:
                await stack.aclose()
            except BaseException:  # noqa: BLE001 - tearing down a broken session
                log.debug("connector close raised", exc_info=True)

    async def _session_ready(self) -> Any:
        if self._alock is None:
            self._alock = asyncio.Lock()   # created on the loop that uses it
        async with self._alock:
            # The child inherits the environment at spawn; if the database it should read
            # changed (tests use one database per module), start a fresh one.
            key = (os.environ.get("DATABASE_URL"), os.environ.get("KESTREL_MCP_FAULT"))
            if self._session is not None and key != self._env_key:
                await self._close()
            if self._session is None:
                self._env_key = key
            return self._session or await self._open()

    async def _call(self, tool: str, args: dict[str, Any]) -> Any:
        return await (await self._session_ready()).call_tool(tool, args)

    async def _list(self) -> Any:
        return await (await self._session_ready()).list_tools()

    # --- sync API for the graph ---
    def run(self, coro_fn, *a, timeout: float) -> Any:
        loop = self._ensure_loop()
        with self._lock:   # one session open at a time
            fut = asyncio.run_coroutine_threadsafe(coro_fn(*a), loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            fut.cancel()
            asyncio.run_coroutine_threadsafe(self._close(), loop)   # a stuck session is discarded
            raise TimeoutError
        except Exception:
            asyncio.run_coroutine_threadsafe(self._close(), loop)
            raise

    def reset(self) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._close(), self._loop).result(10)


_connector = _Connector()


def reset() -> None:
    _connector.reset()


def list_tools(timeout: float | None = None) -> Any:
    return _connector.run(_connector._list, timeout=timeout or TIMEOUT_S * 3)


def call(tool: str, args: dict[str, Any], timeout: float | None = None) -> ToolResult:
    """Call a connector tool. Never raises: failures are ToolResult(ok=False) with a reason."""
    t = timeout if timeout is not None else float(os.environ.get("MCP_TOOL_TIMEOUT_S", TIMEOUT_S))
    try:
        r = _connector.run(_connector._call, tool, {k: v for k, v in args.items() if v is not None}, timeout=t)
    except TimeoutError:
        return ToolResult(ok=False, error=f"connector_timeout: the bank connector did not answer {tool} within {t:g}s")
    except Exception as e:  # noqa: BLE001 - connector crashed or could not start
        return ToolResult(ok=False, error=f"connector_unavailable: {type(e).__name__}: {e}"[:300])
    if r.is_error:
        text = " ".join(getattr(c, "text", "") for c in r.content).strip()
        return ToolResult(ok=False, error=text or f"{tool} failed")
    return ToolResult(ok=True, data=r.structured_content)


def transaction_lookup(*, scope_token: str, account_id: str | None, card_last4: str | None,
                       as_of: Any = None, amount_inr: int | None = None) -> ToolResult:
    """Same contract as tools.bank.transaction_lookup (list of row dicts with dates), over MCP."""
    from kestrel.connectors.mcp_server import Transaction
    if os.environ.get("TOOL_TRANSPORT", "mcp") == "direct":
        from kestrel.tools import bank
        return bank.transaction_lookup(account_id=account_id, card_last4=card_last4, as_of=as_of, amount_inr=amount_inr)
    res = call("transaction_lookup", {"scope_token": scope_token, "account_id": account_id, "card_last4": card_last4,
                                      "amount_inr": amount_inr, "as_of": as_of.isoformat() if as_of else None})
    if res.ok:
        res.data = [Transaction.model_validate(t).model_dump() for t in res.data["transactions"]]
    return res

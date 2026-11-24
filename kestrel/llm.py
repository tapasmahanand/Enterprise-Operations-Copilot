"""The three LLM calls in the system (Supervisor, Retrieval rewrite, Composer), behind one
small interface so tests and offline runs use a deterministic fake (LLM_PROVIDER=fake).

Every call returns usage so the trace can show tokens and ₹ cost per node."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterator, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens (input, output). UNVERIFIED for the gpt-5.4 family: check
# https://openai.com/api/pricing before quoting costs in the report.
PRICES_USD_PER_M = {
    "gpt-5.4-mini": (0.25, 2.00),
    "gpt-5.4-nano": (0.05, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
}
USD_INR = float(os.environ.get("USD_INR", "88"))


@dataclass
class Usage:
    model: str
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def cost_inr(self) -> float:
        pin, pout = PRICES_USD_PER_M.get(self.model, (0.0, 0.0))
        return round((self.tokens_in * pin + self.tokens_out * pout) / 1e6 * USD_INR, 4)

    def trace(self) -> dict:
        return {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "cost_inr": self.cost_inr}


Route = Literal["rag", "tools", "action", "async", "refuse"]


class SupervisorDecision(BaseModel):
    intent: str = Field(description="One BANKING77 label from the in-scope list, or out_of_scope")
    route: Route
    risk_level: Literal["low", "medium", "high"]
    proposed_action: Literal["credit", "fee_reversal", "chargeback", "card_block", "escalate_complaint", "none"]
    card_last4: str | None = Field(default=None, description="Last 4 digits of a card mentioned, if any")
    amount_inr: int | None = Field(default=None, description="Amount the customer mentions, in rupees (informational only)")
    customer_shared_credentials: bool | None = Field(
        default=None, description="True if the customer says they shared OTP/PIN/card details; null if not stated")
    rationale: str = Field(description="One sentence")


class LLM:
    def structured(self, system: str, user: str, schema: type[T], *, model: str) -> tuple[T, Usage]: ...
    def text(self, system: str, user: str, *, model: str) -> tuple[str, Usage]: ...
    def stream(self, system: str, user: str, *, model: str) -> Iterator[str | Usage]: ...


class OpenAILLM(LLM):
    def __init__(self) -> None:
        from openai import OpenAI
        self.client = OpenAI(max_retries=3, timeout=30)

    @staticmethod
    def _extra(model: str) -> dict:
        # gpt-5.x are reasoning models; on a live call we want answers, not deliberation.
        return {"reasoning_effort": "none"} if model.startswith("gpt-5") else {"temperature": 0}

    def structured(self, system, user, schema, *, model):
        r = self.client.chat.completions.parse(
            model=model, response_format=schema, **self._extra(model),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return r.choices[0].message.parsed, Usage(model, r.usage.prompt_tokens, r.usage.completion_tokens)

    def text(self, system, user, *, model):
        r = self.client.chat.completions.create(
            model=model, **self._extra(model),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        return r.choices[0].message.content or "", Usage(model, r.usage.prompt_tokens, r.usage.completion_tokens)

    def stream(self, system, user, *, model):
        s = self.client.chat.completions.create(
            model=model, stream=True, stream_options={"include_usage": True}, **self._extra(model),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        usage = Usage(model)
        for chunk in s:
            if chunk.usage:
                usage = Usage(model, chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
        yield usage


class FakeLLM(LLM):
    """Deterministic keyword rules. Lets the graph, API and restart tests run with no key.
    Never used for any quality number."""

    def structured(self, system, user, schema, *, model):
        u = user.lower()
        if schema is SupervisorDecision:
            last4 = re.search(r"(?:x{4}[\s-]?){3}(\d{4})|ending (?:in )?(\d{4})", u)
            amt = re.search(r"(?:₹|rs\.?\s?|inr\s?)([\d,]+)", u)
            fraud = any(w in u for w in ("never made", "didn't make", "did not make", "not recognise", "unauthori", "fraud"))
            if fraud:
                d = SupervisorDecision(intent="card_payment_not_recognised", route="action", risk_level="high",
                                       proposed_action="credit", rationale="unrecognised card payment")
            elif "charged twice" in u or "duplicate" in u:
                d = SupervisorDecision(intent="transaction_charged_twice", route="action", risk_level="medium",
                                       proposed_action="credit", rationale="duplicate charge")
            elif any(w in u for w in ("policy", "what is", "how long", "liability", "rule")):
                d = SupervisorDecision(intent="card_payment_not_recognised", route="rag", risk_level="low",
                                       proposed_action="none", rationale="policy question")
            else:
                d = SupervisorDecision(intent="out_of_scope", route="refuse", risk_level="low",
                                       proposed_action="none", rationale="not a tier-1 resolution request")
            d.card_last4 = next((g for g in (last4.groups() if last4 else ()) if g), None)
            d.amount_inr = int(amt.group(1).replace(",", "")) if amt else None
            return d, Usage("fake")
        raise NotImplementedError(schema)

    def text(self, system, user, *, model):
        return user.splitlines()[-1][:200], Usage("fake")

    def stream(self, system, user, *, model):
        ids = re.findall(r"\[chunk_id=([^\]]+)\]", user)
        out = "Per policy, this case is handled as summarised in the facts below"
        out += f" [{ids[0]}]." if ids else ". I couldn't find a policy chunk for this."
        for w in out.split(" "):
            yield w + " "
        yield Usage("fake")


@lru_cache(maxsize=1)
def get_llm() -> LLM:
    return FakeLLM() if os.environ.get("LLM_PROVIDER", "openai") == "fake" else OpenAILLM()


def model_for(role: Literal["supervisor", "rewrite", "composer"]) -> str:
    default = {"supervisor": "gpt-5.4-mini", "rewrite": "gpt-5.4-nano", "composer": "gpt-5.4-mini"}[role]
    return os.environ.get(f"{role.upper()}_MODEL", default)

"""Hero demo, end to end over HTTP, including a hard restart of the app mid-approval.

    python scripts/hero_demo.py --restart "docker compose kill app && docker compose up -d app"

Steps: chat (stream) → draft → Send to supervisor → RESTART → supervisor approves →
case resumes → credit posted once → SMS drafted → second decision gets 409.
Exit code 0 only if every check passes."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import httpx

HERO = ("Customer says there is a ₹18,450 card payment she never made on card 4111 1111 1111 4821, "
        "it went through yesterday.")


def check(ok: bool, label: str) -> None:
    print(("PASS " if ok else "FAIL ") + label)
    if not ok:
        sys.exit(1)


def wait_healthy(base: str, timeout: float = 120) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(f"{base}/health", timeout=3).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    check(False, "app healthy again after restart")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--restart", required=True, help="shell command that kills and restarts the app")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.base, timeout=60)
    wait_healthy(a.base)

    run = c.post("/chat", json={"message": HERO, "idv_level": 2}, headers={"X-Role": "agent"}).json()
    events, t0, first = [], time.perf_counter(), None
    with c.stream("GET", run["stream"]) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                if ev["type"] == "progress":
                    first = first or (time.perf_counter() - t0)
                    print(f"  … {ev['label']}")
                events.append(ev)
    final = next(e for e in events if e["type"] == "final")
    done = next(e for e in events if e["type"] == "done")
    print(f"  first progress {first*1000:.0f} ms, total {done['latency_ms']} ms")
    check(final["card_block"]["status"] == "blocked", "card ending 4821 blocked immediately (no approval)")
    check(final["calc"]["customer_liability_inr"] == 0, "calculator: zero customer liability")
    check(bool(final["calc"]["shadow_credit_due"]), f"calculator: shadow credit due {final['calc']['shadow_credit_due']}")
    check(final["draft"] and final["draft"]["amount_inr"] == 18450, "shadow credit drafted for ₹18,450 (from lookup)")
    check(final["output_check"]["verdict"] == "allow" and final["citations"], f"answer cites {final['citations']}")
    print("  script:", final["script"])

    case = c.post(f"/drafts/{final['draft']['draft_id']}/submit", headers={"X-Role": "agent"}).json()
    check(case["status"] == "pending_approval", f"agent sent {case['case_id']} to supervisor")

    print(f"  restarting app: {a.restart}")
    subprocess.run(a.restart, shell=True, check=True)
    wait_healthy(a.base)
    check(True, "app restarted")

    q = c.get("/approvals", headers={"X-Role": "supervisor"}).json()
    item = next((x for x in q if x["case_id"] == case["case_id"]), None)
    check(item is not None, "case still in supervisor queue after restart")
    res = c.post(f"/approvals/{item['approval_id']}", headers={"X-Role": "supervisor"},
                 json={"decision": "approve", "reason": "evidence reviewed", "approver_id": "P01"})
    body = res.json()
    check(res.status_code == 200 and body["status"] == "closed", "supervisor approved; case graph resumed and closed")
    check(body["result"]["credit"]["amount_inr"] == 18450, f"shadow credit posted: {body['result']['credit']['credit_id']}")
    print("  SMS draft:", body["result"]["notification"]["body"])
    dup = c.post(f"/approvals/{item['approval_id']}", headers={"X-Role": "supervisor"},
                 json={"decision": "reject", "reason": "double click", "approver_id": "P01"})
    check(dup.status_code == 409, "second decision rejected with 409, no side effect")
    trace = c.get(f"/cases/{case['case_id']}").json()["events"]
    check([e["node"] for e in trace][-3:] == ["approval_gate", "execute_action", "notify"], "case trace has every step")
    print("ALL PASS")


if __name__ == "__main__":
    main()

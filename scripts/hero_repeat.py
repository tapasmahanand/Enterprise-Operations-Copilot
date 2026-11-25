"""Run the hero chat N times and report output-check failures and latency (chat only)."""
import json, statistics, sys, time
import httpx
HERO = "Customer says there is a ₹18,450 card payment she never made on card 4111 1111 1111 4821, it went through yesterday."
n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
c = httpx.Client(base_url="http://localhost:8000", timeout=60)
lat, blocked = [], []
for i in range(n):
    run = c.post("/chat", json={"message": HERO}).json()
    final = done = None
    with c.stream("GET", run["stream"]) as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                final = ev if ev["type"] == "final" else final
                done = ev if ev["type"] == "done" else done
    lat.append(done["latency_ms"])
    if final["output_check"]["verdict"] != "allow":
        blocked.append(final["output_check"]["violations"])
print(f"runs={n} blocked={len(blocked)} p50={statistics.median(lat)} ms max={max(lat)} ms")
for b in blocked:
    print("  ", b)

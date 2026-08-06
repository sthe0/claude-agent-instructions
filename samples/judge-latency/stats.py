import json
import statistics as st

LIMITS = {"outage": 20, "defer": 20, "feedback": 30}


def show(path, limit_map=None):
    try:
        d = json.load(open(path))
    except Exception as exc:
        print(path, "unavailable:", exc)
        return
    for k, rows in d.items():
        lat = sorted(r["latency_s"] for r in rows)
        lim = (limit_map or {}).get(k)
        over = sum(1 for x in lat if lim is not None and x >= lim)
        ver = [r.get("verdict") if "verdict" in r else (r.get("ans") or [""])[0] for r in rows]
        line = "%-14s n=%d min=%5.2f med=%5.2f p90=%5.2f max=%5.2f" % (
            k, len(lat), lat[0], st.median(lat),
            lat[int(round(0.9 * (len(lat) - 1)))], lat[-1])
        if lim is not None:
            line += "  at-or-over-%ds=%d/%d" % (lim, over, len(lat))
        print(line)
        print("               sorted:", lat)
        print("               verdicts:", ver)


print("=== sonnet (the model the advisor actually uses) ===")
show("/tmp/cc-scratch/live-run/latency-sample.json", LIMITS)
print()
print("=== haiku probe ===")
show("/tmp/cc-scratch/live-run/haiku-sample.json")

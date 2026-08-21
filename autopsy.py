#!/usr/bin/env python3
"""
claude-code-autopsy — find out where your Claude Code tokens actually go.

Reads the transcripts Claude Code already writes to ~/.claude/projects and reports
what you are paying for. Everything is local; nothing is uploaded anywhere.

    python3 autopsy.py                  # analyze everything
    python3 autopsy.py --days 30        # only the last 30 days
    python3 autopsy.py --json out.json  # machine-readable, for feeding to an agent

The headline number is the burn ratio: tokens spent per token of useful output,
bucketed by how full the context window was at the time. It rises steeply, which is
why long-lived agents are expensive even when they are doing nothing wrong.
"""

import argparse
import collections
import datetime as dt
import glob
import json
import os
import re
import sys

ROOT = os.path.expanduser("~/.claude/projects")

# Context-size buckets. Cost per unit of output rises sharply across these.
BUCKETS = [
    (0, 50_000, "<50k"),
    (50_000, 100_000, "50-100k"),
    (100_000, 200_000, "100-200k"),
    (200_000, 400_000, "200-400k"),
    (400_000, 600_000, "400-600k"),
    (600_000, 10**9, ">600k"),
]

# Error signatures worth attributing downstream burn to. Ordered most to least specific
# so the first match wins.
ERROR_PATTERNS = [
    ("zsh unmatched glob", r"no matches found:|\(eval\):\d+:"),
    ("vitest failure", r"FAIL\s+\S+|Tests?\s+\d+\s+failed"),
    ("npm/pnpm script failure", r"ELIFECYCLE|command failed with exit code"),
    ("typecheck error", r"error TS\d{4}"),
    ("docker/testcontainers", r"testcontainer|docker.*(daemon|not running)|Could not find a working container"),
    ("file not found", r"No such file or directory"),
    ("command timed out", r"command timed out|timed out after"),
    ("permission denied", r"[Pp]ermission denied|requires approval|classifier"),
    ("git worktree exists", r"already exists|is already checked out"),
    ("edit target missing", r"String to replace not found|has not been read yet"),
]


def bucket_of(ctx):
    for lo, hi, label in BUCKETS:
        if lo <= ctx < hi:
            return label
    return ">600k"


def normalize_command(cmd):
    """Collapse a shell command to its shape so retries of the same thing group together."""
    cmd = cmd.strip().split("\n")[0]
    head = cmd.split()[:1]
    if not head:
        return "(empty)"
    verb = os.path.basename(head[0])
    # Keep the subcommand for multiplexers like git/gh/pnpm, drop everything else.
    parts = cmd.split()
    if verb in {"git", "gh", "pnpm", "npm", "yarn", "docker", "cargo"} and len(parts) > 1:
        return f"{verb} {parts[1]}"
    return verb


def iter_records(days=None):
    """Yield every transcript record. Transcripts nest several levels deep, so recurse."""
    cutoff = None
    if days:
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()
    files = glob.glob(os.path.join(ROOT, "**", "*.jsonl"), recursive=True)
    if not files:
        sys.exit(f"No transcripts found under {ROOT}. Is Claude Code installed for this user?")
    for path in files:
        try:
            with open(path, errors="ignore") as fh:
                for line in fh:
                    if '"usage"' not in line and '"tool_use"' not in line and '"tool_result"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if cutoff and (rec.get("timestamp") or "") < cutoff:
                        continue
                    yield rec
        except OSError:
            continue


def analyze(days=None):
    totals = collections.Counter()
    side_totals = collections.Counter()
    by_bucket = collections.Counter()
    out_by_bucket = collections.Counter()
    turns_by_bucket = collections.Counter()
    by_model = collections.Counter()

    agents = collections.defaultdict(lambda: {"tokens": 0, "turns": 0, "peak": 0, "out": 0, "model": ""})
    spawn_costs = []

    tool_bytes = collections.Counter()
    tool_calls = collections.Counter()
    tool_names = {}          # tool_use_id -> name
    tool_inputs = {}         # tool_use_id -> input dict

    reads_per_agent = collections.defaultdict(collections.Counter)
    read_no_range = read_ranged = 0

    cmd_failures = collections.Counter()
    cmd_sessions = collections.defaultdict(set)
    error_hits = collections.Counter()
    error_sessions = collections.defaultdict(set)

    first_seen_ts = last_seen_ts = None

    for rec in iter_records(days):
        msg = rec.get("message") or {}
        sid = rec.get("sessionId", "?")
        agent_key = (sid, rec.get("agentId") or "-")
        is_side = bool(rec.get("isSidechain"))
        ts = rec.get("timestamp") or ""
        if ts:
            if first_seen_ts is None or ts < first_seen_ts:
                first_seen_ts = ts
            if last_seen_ts is None or ts > last_seen_ts:
                last_seen_ts = ts

        usage = msg.get("usage")
        if isinstance(usage, dict):
            inp = usage.get("input_tokens") or 0
            out = usage.get("output_tokens") or 0
            cr = usage.get("cache_read_input_tokens") or 0
            cw = usage.get("cache_creation_input_tokens") or 0
            ctx = inp + cr + cw
            spent = ctx + out

            for key, val in (("in", inp), ("out", out), ("cache_read", cr), ("cache_write", cw)):
                totals[key] += val
                if is_side:
                    side_totals[key] += val
            by_model[msg.get("model", "?")] += spent

            label = bucket_of(ctx)
            by_bucket[label] += spent
            out_by_bucket[label] += out
            turns_by_bucket[label] += 1

            if is_side:
                rec_agent = agents[agent_key]
                if rec_agent["turns"] == 0:
                    spawn_costs.append(ctx)
                rec_agent["tokens"] += spent
                rec_agent["turns"] += 1
                rec_agent["out"] += out
                rec_agent["peak"] = max(rec_agent["peak"], ctx)
                rec_agent["model"] = msg.get("model", "")

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "tool_use":
                name = block.get("name", "?")
                tool_names[block.get("id")] = name
                tool_inputs[block.get("id")] = block.get("input") or {}
                tool_calls[name] += 1
                if name == "Read":
                    params = block.get("input") or {}
                    path = params.get("file_path", "?")
                    reads_per_agent[agent_key][path] += 1
                    if params.get("limit") or params.get("offset"):
                        read_ranged += 1
                    else:
                        read_no_range += 1

            elif btype == "tool_result":
                tid = block.get("tool_use_id")
                name = tool_names.get(tid, "?")
                raw = block.get("content")
                text = ""
                if isinstance(raw, str):
                    text = raw
                elif isinstance(raw, list):
                    text = "".join(x.get("text", "") or "" for x in raw if isinstance(x, dict))
                tool_bytes[name] += len(text)

                is_error = block.get("is_error") or False
                if is_error and name == "Bash":
                    shape = normalize_command((tool_inputs.get(tid) or {}).get("command", ""))
                    cmd_failures[shape] += 1
                    cmd_sessions[shape].add(sid)
                if text:
                    head = text[:4000]
                    for label, pattern in ERROR_PATTERNS:
                        if re.search(pattern, head):
                            error_hits[label] += 1
                            error_sessions[label].add(sid)
                            break

    reread = sum(sum(c - 1 for c in files.values() if c > 1) for files in reads_per_agent.values())
    total_reads = sum(sum(files.values()) for files in reads_per_agent.values())

    return {
        "window": {"from": (first_seen_ts or "")[:10], "to": (last_seen_ts or "")[:10]},
        "totals": dict(totals),
        "sidechain_totals": dict(side_totals),
        "by_bucket": dict(by_bucket),
        "out_by_bucket": dict(out_by_bucket),
        "turns_by_bucket": dict(turns_by_bucket),
        "by_model": dict(by_model),
        "agents": agents,
        "spawn_costs": sorted(spawn_costs),
        "tool_bytes": dict(tool_bytes),
        "tool_calls": dict(tool_calls),
        "reads": {
            "total": total_reads,
            "reread": reread,
            "whole_file": read_no_range,
            "ranged": read_ranged,
        },
        "cmd_failures": dict(cmd_failures),
        "cmd_sessions": {k: len(v) for k, v in cmd_sessions.items()},
        "error_hits": dict(error_hits),
        "error_sessions": {k: len(v) for k, v in error_sessions.items()},
    }


def fmt(n):
    return f"{int(n):,}"


def report(data):
    t = data["totals"]
    grand = sum(t.values())
    if not grand:
        sys.exit("No token usage found. Nothing to report.")
    side = sum(data["sidechain_totals"].values())
    w = data["window"]

    print("=" * 74)
    print(f"  CLAUDE CODE AUTOPSY    {w['from']} to {w['to']}")
    print("=" * 74)

    print(f"\nTotal tokens processed: {fmt(grand)}")
    print(f"  cache reads   {fmt(t.get('cache_read', 0)):>16}  {100*t.get('cache_read',0)/grand:5.1f}%")
    print(f"  cache writes  {fmt(t.get('cache_write', 0)):>16}  {100*t.get('cache_write',0)/grand:5.1f}%")
    print(f"  fresh input   {fmt(t.get('in', 0)):>16}  {100*t.get('in',0)/grand:5.1f}%")
    print(f"  output        {fmt(t.get('out', 0)):>16}  {100*t.get('out',0)/grand:5.1f}%")

    cr, cw = t.get("cache_read", 0), t.get("cache_write", 0)
    if cr + cw:
        print(f"\nCache hit rate: {100*cr/(cr+cw):.1f}%  (above ~85% means caching is not your problem)")
    print(f"Subagents:      {100*side/grand:.0f}% of all tokens")

    # The headline.
    print("\n" + "-" * 74)
    print("  BURN RATIO — tokens spent per token of output, by context size")
    print("-" * 74)
    print(f"  {'context':<12}{'turns':>9}{'tokens spent':>17}{'output':>13}{'burn':>10}")
    ratios = {}
    for _, _, label in BUCKETS:
        spent = data["by_bucket"].get(label, 0)
        if not spent:
            continue
        out = data["out_by_bucket"].get(label, 0)
        ratio = spent / max(out, 1)
        ratios[label] = ratio
        print(f"  {label:<12}{data['turns_by_bucket'].get(label,0):>9,}{fmt(spent):>17}{fmt(out):>13}{ratio:>9.0f}x")

    if len(ratios) > 1:
        cheap = min(ratios.values())
        dear = max(ratios.values())
        print(f"\n  Same work costs {dear/cheap:.0f}x more at the top of this table than the bottom.")
    high = sum(v for k, v in data["by_bucket"].items() if k in ("200-400k", "400-600k", ">600k"))
    high_out = sum(v for k, v in data["out_by_bucket"].items() if k in ("200-400k", "400-600k", ">600k"))
    total_out = sum(data["out_by_bucket"].values())
    if high:
        print(f"  Turns above 200k context: {100*high/grand:.0f}% of spend, "
              f"{100*high_out/max(total_out,1):.0f}% of output.")

    # Agents.
    agents = data["agents"]
    if agents:
        ranked = sorted(agents.values(), key=lambda a: -a["tokens"])
        top = ranked[:10]
        top_share = sum(a["tokens"] for a in top) / max(side, 1)
        print("\n" + "-" * 74)
        print(f"  SUBAGENTS — {len(agents)} total, top 10 are {100*top_share:.0f}% of subagent spend")
        print("-" * 74)
        print(f"  {'tokens':>16}{'turns':>8}{'peak ctx':>12}{'output':>11}   model")
        for a in top[:6]:
            print(f"  {fmt(a['tokens']):>16}{a['turns']:>8,}{fmt(a['peak']):>12}{fmt(a['out']):>11}   {a['model']}")
        over = sum(1 for a in agents.values() if a["peak"] > 200_000)
        print(f"\n  {over} of {len(agents)} agents ({100*over/len(agents):.0f}%) grew past 200k context.")
        if data["spawn_costs"]:
            sc = data["spawn_costs"]
            median_spawn = sc[len(sc)//2]
            worst_peak = max(a["peak"] for a in agents.values())
            print(f"  Median cold start: {fmt(median_spawn)} tokens.")
            print(f"  One turn of your largest agent: {fmt(worst_peak)} tokens "
                  f"({worst_peak/max(median_spawn,1):.0f}x a fresh spawn).")

    # Waste.
    r = data["reads"]
    print("\n" + "-" * 74)
    print("  WASTE SIGNALS")
    print("-" * 74)
    if r["total"]:
        print(f"  Re-reads of a file the same agent already read: {r['reread']:,} of {r['total']:,} "
              f"({100*r['reread']/r['total']:.0f}%)")
        print(f"  Reads with no offset/limit (whole file):        {r['whole_file']:,} of "
              f"{r['whole_file']+r['ranged']:,} ({100*r['whole_file']/max(r['whole_file']+r['ranged'],1):.0f}%)")

    if data["cmd_failures"]:
        print("\n  Failing command shapes:")
        for shape, n in sorted(data["cmd_failures"].items(), key=lambda kv: -kv[1])[:8]:
            print(f"    {n:>5} failures across {data['cmd_sessions'].get(shape,0):>3} sessions   {shape}")

    if data["error_hits"]:
        print("\n  Error signatures:")
        for label, n in sorted(data["error_hits"].items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {n:>5} hits across {data['error_sessions'].get(label,0):>3} sessions   {label}")

    # Recommendations, thresholded off the numbers above.
    print("\n" + "=" * 74)
    print("  WHAT TO DO")
    print("=" * 74)
    recs = []
    if high and 100 * high / grand > 30:
        recs.append(
            f"Cap agent context. {100*high/grand:.0f}% of your spend happens above 200k context, "
            f"where each token of output costs several times what it does early in a session. "
            f"Have long-running agents checkpoint their state to a file, branch, or PR at ~200k "
            f"and hand off to a fresh one. This is almost always the single biggest lever.")
    if agents and sum(1 for a in agents.values() if a["peak"] > 600_000):
        n = sum(1 for a in agents.values() if a["peak"] > 600_000)
        recs.append(f"{n} agent(s) exceeded 600k context. Check what they were doing; agents that "
                    f"run for hundreds of turns usually needed to be several agents.")
    if r["total"] and 100 * r["reread"] / r["total"] > 25:
        recs.append(f"{100*r['reread']/r['total']:.0f}% of reads re-open a file the agent already read. "
                    f"Usually means the file is too big to answer one question, so it gets re-read "
                    f"instead of remembered. Split the files that show up most.")
    if r["whole_file"] and 100 * r["whole_file"] / max(r["whole_file"] + r["ranged"], 1) > 50:
        recs.append("Most reads pull whole files. Adding a table of contents with line ranges to "
                    "your largest files lets an agent ask for the part it needs.")
    if data["error_hits"].get("zsh unmatched glob", 0) > 20:
        recs.append(f"{data['error_hits']['zsh unmatched glob']} commands died on zsh's unmatched-glob "
                    f"error (`grep --include=*.ts` and friends). Fix it at the shell, not in a docs "
                    f"file: add `unsetopt nomatch` to ~/.zshrc.")
    if data["error_hits"].get("docker/testcontainers", 0) > 50:
        recs.append("Testcontainer failures are frequent. Document a fast test lane that does not "
                    "boot containers; agents default to the slowest command you gave them.")
    if data["error_hits"].get("permission denied", 0) > 30:
        recs.append("Permission friction is costing real tokens. Add an `allow` list for read-only "
                    "and routine dev commands to .claude/settings.json.")
    cheap_models = sum(v for k, v in data["by_model"].items() if "haiku" in k or "sonnet" in k)
    if grand and cheap_models / grand < 0.2:
        recs.append(f"Only {100*cheap_models/grand:.0f}% of your tokens run on Sonnet or Haiku. "
                    f"Read-only exploration rarely needs your largest model.")
    if not recs:
        recs.append("Nothing above the alarm thresholds. Your setup looks healthy.")
    for i, rec in enumerate(recs, 1):
        print(f"\n  {i}. {rec}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Find out where your Claude Code tokens actually go.")
    ap.add_argument("--days", type=int, help="only look at the last N days")
    ap.add_argument("--json", metavar="PATH", help="also write raw findings as JSON")
    args = ap.parse_args()

    data = analyze(args.days)
    report(data)

    if args.json:
        serializable = dict(data)
        serializable["agents"] = [
            {k: v for k, v in a.items()}
            for a in sorted(data["agents"].values(), key=lambda x: -x["tokens"])[:50]
        ]
        with open(args.json, "w") as fh:
            json.dump(serializable, fh, indent=2)
        print(f"  Raw findings written to {args.json}\n")


if __name__ == "__main__":
    main()

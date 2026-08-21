#!/usr/bin/env python3
"""
sediment — find out where your Claude Code tokens actually go.

Reads the transcripts Claude Code already writes to ~/.claude/projects and reports
what you are paying for. Everything is local; nothing is uploaded anywhere.

    python3 sediment.py                  # analyze everything
    python3 sediment.py --days 30        # only the last 30 days
    python3 sediment.py --json out.json  # machine-readable, for feeding to an agent

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
# so the first match wins. Deliberately spans several ecosystems: the point is to work on
# somebody else's stack, not only the one this was written against.
ERROR_PATTERNS = [
    # Shell and environment
    ("shell glob not expanded", r"no matches found:|nomatch|bad pattern:"),
    ("command not found", r"command not found|is not recognized as an internal"),
    ("permission / approval", r"[Pp]ermission denied|requires approval|EACCES|not permitted"),
    ("file not found", r"No such file or directory|ENOENT"),
    ("command timed out", r"command timed out|timed out after|ETIMEDOUT"),
    ("port already in use", r"EADDRINUSE|address already in use|port .* already"),
    # Test runners, several ecosystems
    ("test failure (js)", r"FAIL\s+\S+|Tests?:\s+\d+\s+failed|✕ |● .*›"),
    ("test failure (python)", r"=+ FAILURES =+|\d+ failed,|E\s+assert |pytest"),
    ("test failure (go)", r"^--- FAIL|FAIL\s+\S+\s+\d+\.\d+s"),
    ("test failure (rust/other)", r"test result: FAILED|thread '.*' panicked"),
    # Build and type systems
    ("type error", r"error TS\d{4}|mypy: |error\[E\d+\]|type error:"),
    ("lint failure", r"\d+ problems? \(\d+ error|eslint|ruff|rubocop.*offense"),
    ("package script failure", r"ELIFECYCLE|command failed with exit code|npm ERR!|make: \*\*\*"),
    ("dependency resolution", r"ERESOLVE|could not resolve dependency|version solving failed"),
    # Infra
    ("container / docker", r"testcontainer|docker.*(daemon|not running)|Cannot connect to the Docker"),
    ("database connection", r"ECONNREFUSED.*(5432|3306|27017)|could not connect to server|connection refused"),
    # Version control
    ("git conflict or dirty tree", r"CONFLICT|would be overwritten|Your local changes"),
    ("git worktree/branch exists", r"already exists|is already checked out"),
    # Agent-harness specific
    ("edit target missing", r"String to replace not found|has not been read yet|old_string"),
]

# Tool calls that change something. Used to tell real progress from spinning.
PRODUCTIVE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# Files nobody can restructure: scratch output, generated dumps, vendored code. Reading these
# repeatedly may still be wasteful, but "split this file" is not the fix, so they are excluded
# from split candidates rather than offered as advice that cannot be taken.
THROWAWAY_DIRS = ("/scratchpad/", "/tmp/", "/.git/", "/node_modules/", "/dist/", "/build/",
                  "/vendor/", "/.venv/", "/site-packages/", "/coverage/", "/__pycache__/")
THROWAWAY_EXTS = (".diff", ".patch", ".log", ".lock", ".min.js", ".map", ".snap", ".csv")


def is_throwaway(path):
    lowered = path.lower()
    return (any(d in lowered for d in THROWAWAY_DIRS)
            or lowered.endswith(THROWAWAY_EXTS))


def group_key(path):
    """Collapse the same file checked out in several worktrees into one entry.

    Git worktrees give one source file many absolute paths, which would otherwise split its
    cost across a dozen rows and hide how expensive it really is. The last two path components
    identify it well enough in practice.
    """
    parts = [p for p in path.split(os.sep) if p]
    return os.sep.join(parts[-2:]) if len(parts) >= 2 else path


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

    agents = collections.defaultdict(
        lambda: {"tokens": 0, "turns": 0, "peak": 0, "out": 0, "model": "", "final_chars": 0})
    spawn_costs = []

    tool_bytes = collections.Counter()
    tool_calls = collections.Counter()
    tool_names = {}          # tool_use_id -> name
    tool_inputs = {}         # tool_use_id -> input dict

    reads_per_agent = collections.defaultdict(collections.Counter)
    read_no_range = read_ranged = 0
    read_tokens_by_file = collections.Counter()   # path -> tokens returned across all reads
    read_counts_by_file = collections.Counter()   # path -> how many reads returned content

    cmd_failures = collections.Counter()
    cmd_sessions = collections.defaultdict(set)
    error_hits = collections.Counter()
    error_sessions = collections.defaultdict(set)

    # Loop detection. For each agent we keep a rolling trace of what it did, so we can spot
    # an agent repeating itself and an agent that stopped changing anything.
    trace = collections.defaultdict(list)     # agent -> [(signature, productive)]
    loops = []                                # (agent, signature, repeat count)
    stall_runs = collections.Counter()        # agent -> longest run of unproductive calls

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
                body = msg.get("content")
                if isinstance(body, list):
                    rec_agent["final_chars"] = sum(
                        len(b.get("text", "") or "")
                        for b in body if isinstance(b, dict) and b.get("type") == "text")

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

                # Signature of what this call actually does, so an identical repeat is visible.
                params = block.get("input") or {}
                if name == "Bash":
                    sig = f"Bash:{(params.get('command') or '')[:160]}"
                elif name in ("Read", "Edit", "Write"):
                    sig = f"{name}:{params.get('file_path','?')}"
                elif name in ("Grep", "Glob"):
                    sig = f"{name}:{params.get('pattern','?')}:{params.get('path','')}"
                else:
                    sig = f"{name}:{json.dumps(params, sort_keys=True, default=str)[:160]}"
                # Editing the same file repeatedly is normal iterative work, not a loop, so
                # productive tools carry no signature and are only used to break a stall run.
                trace[agent_key].append((None if name in PRODUCTIVE_TOOLS else sig,
                                         name in PRODUCTIVE_TOOLS))

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
                if name == "Read" and text:
                    path = (tool_inputs.get(tid) or {}).get("file_path", "?")
                    read_tokens_by_file[path] += len(text) // 4
                    read_counts_by_file[path] += 1

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

    # Split candidates. A file that many DIFFERENT agents open in full, repeatedly, is a file
    # whose structure forces a wide read for a narrow question. Rank by tokens spent re-reading,
    # because that is the part splitting the file actually recovers.
    per_file = collections.defaultdict(
        lambda: {"views": 0, "rereads": 0, "agents": set(), "tokens": 0, "avg": 0})
    for agent, files in reads_per_agent.items():
        for path, count in files.items():
            entry = per_file[path]
            entry["views"] += count
            entry["rereads"] += count - 1
            entry["agents"].add(agent)
    for path, entry in per_file.items():
        entry["avg"] = int(read_tokens_by_file[path] / max(read_counts_by_file[path], 1))
        entry["tokens"] = entry["avg"] * entry["rereads"]
    # Two corrections before ranking. Throwaway artifacts cannot be "split", and the same source
    # file checked out in several worktrees is one file whose costs belong added together.
    grouped = collections.defaultdict(
        lambda: {"views": 0, "rereads": 0, "agents": set(), "tokens": 0, "avg": 0, "copies": set()})
    for path, entry in per_file.items():
        if is_throwaway(path):
            continue
        key = group_key(path)
        g = grouped[key]
        g["views"] += entry["views"]
        g["rereads"] += entry["rereads"]
        g["agents"] |= entry["agents"]
        g["tokens"] += entry["tokens"]
        g["avg"] = max(g["avg"], entry["avg"])
        g["copies"].add(path)
    split_candidates = [
        {"path": key, "views": g["views"], "rereads": g["rereads"],
         "agents": len(g["agents"]), "avg_tokens": g["avg"],
         "wasted_tokens": g["tokens"], "copies": len(g["copies"])}
        for key, g in grouped.items()
        if g["rereads"] >= 3 and len(g["agents"]) >= 2 and g["avg"] >= 400
    ]
    split_candidates.sort(key=lambda d: -d["wasted_tokens"])

    # Dead ends: an agent that ran a long time and handed back almost nothing. Judge it by the
    # size of its FINAL message, not by output summed over every turn — a long run produces plenty
    # of output along the way and still delivers a two-line answer at the end.
    dead_ends = [
        {"turns": a["turns"], "tokens": a["tokens"], "final": a["final_chars"], "peak": a["peak"]}
        for a in agents.values()
        if a["turns"] >= 25 and a["final_chars"] < 800
    ]
    dead_ends.sort(key=lambda d: -d["tokens"])

    # MCP servers cost context on every turn: their tool schemas sit in the prompt whether or not
    # you call them. Calls are visible in transcripts; the schemas are not, so report usage and
    # let the reader decide what is carrying its weight.
    mcp_calls = collections.Counter()
    for name, count in tool_calls.items():
        if name.startswith("mcp__"):
            parts = name.split("__")
            mcp_calls[parts[1] if len(parts) > 1 else name] += count
    total_tool_calls = sum(tool_calls.values())

    # Resolve the traces into loops and stalls.
    LOOP_MIN = 3      # an identical call this many times over is a loop, not a retry
    STALL_MIN = 25    # this many calls in a row without changing a file is spinning
    for agent, steps in trace.items():
        counts = collections.Counter(sig for sig, _ in steps if sig is not None)
        for sig, n in counts.items():
            if n >= LOOP_MIN:
                loops.append((agent, sig, n))
        run = best = 0
        for _, productive in steps:
            run = 0 if productive else run + 1
            best = max(best, run)
        if best >= STALL_MIN:
            stall_runs[agent] = best

    # Group loop signatures so the same mistake made by many agents reads as one finding.
    loop_by_sig = collections.Counter()
    loop_agents_by_sig = collections.defaultdict(set)
    for agent, sig, n in loops:
        loop_by_sig[sig] += n
        loop_agents_by_sig[sig].add(agent)

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
        "split_candidates": split_candidates[:40],
        "dead_ends": {
            "count": len(dead_ends),
            "tokens": sum(d["tokens"] for d in dead_ends),
            "worst": dead_ends[0] if dead_ends else None,
        },
        "mcp": {"by_server": dict(mcp_calls), "total_tool_calls": total_tool_calls},
        "cmd_failures": dict(cmd_failures),
        "cmd_sessions": {k: len(v) for k, v in cmd_sessions.items()},
        "error_hits": dict(error_hits),
        "error_sessions": {k: len(v) for k, v in error_sessions.items()},
        "loops": {
            "signatures": loop_by_sig.most_common(200),
            "agents_by_sig": {k: len(v) for k, v in loop_agents_by_sig.items()},
            "agents_looping": len({a for a, _, _ in loops}),
            "total_repeats": sum(n - 1 for _, _, n in loops),
        },
        "stalls": {
            "agents": len(stall_runs),
            "worst": max(stall_runs.values()) if stall_runs else 0,
        },
        "agent_count": len(trace),
    }


def fmt(n):
    return f"{int(n):,}"


def report(data, show_all=False):
    t = data["totals"]
    grand = sum(t.values())
    if not grand:
        sys.exit("No token usage found. Nothing to report.")
    side = sum(data["sidechain_totals"].values())
    w = data["window"]

    print("=" * 74)
    print(f"  SEDIMENT    {w['from']} to {w['to']}")
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

    cands = data.get("split_candidates") or []
    if cands:
        print("\n" + "-" * 74)
        print("  SPLIT CANDIDATES — files whose shape forces a wide read for a narrow question")
        print("-" * 74)
        print(f"  {'wasted':>9}{'avg size':>10}{'reads':>7}{'agents':>8}{'copies':>8}   file")
        for c in cands[:10]:
            path = c["path"]
            if len(path) > 40:
                path = "..." + path[-37:]
            print(f"  {fmt(c['wasted_tokens']):>9}{fmt(c['avg_tokens']):>10}"
                  f"{c['views']:>7}{c['agents']:>8}{c['copies']:>8}   {path}")
        total_wasted = sum(c["wasted_tokens"] for c in cands)
        print(f"\n  {len(cands)} files fit the pattern; re-reading them cost ~{fmt(total_wasted)} tokens.")
        print("  'wasted' is tokens spent on reads after the first, which is what a split recovers.")

    if data["cmd_failures"]:
        print("\n  Failing command shapes:")
        for shape, n in sorted(data["cmd_failures"].items(), key=lambda kv: -kv[1])[:8]:
            print(f"    {n:>5} failures across {data['cmd_sessions'].get(shape,0):>3} sessions   {shape}")

    if data["error_hits"]:
        print("\n  Error signatures:")
        for label, n in sorted(data["error_hits"].items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {n:>5} hits across {data['error_sessions'].get(label,0):>3} sessions   {label}")

    # Loops and stalls.
    loops = data["loops"]
    stalls = data["stalls"]
    if loops["signatures"] or stalls["agents"]:
        print("\n" + "-" * 74)
        print("  LOOPS AND STALLS — agents repeating themselves or making no progress")
        print("-" * 74)
        if loops["signatures"]:
            print(f"  {loops['agents_looping']} of {data['agent_count']} agents repeated an identical "
                  f"call 3+ times ({loops['total_repeats']:,} redundant calls total).")
            print("\n  Most-repeated calls:")
            for sig, n in loops["signatures"][:8]:
                n_agents = loops["agents_by_sig"].get(sig, 1)
                shown = sig if len(sig) <= 58 else sig[:55] + "..."
                print(f"    {n:>5}x by {n_agents:>3} agents   {shown}")
        if stalls["agents"]:
            print(f"\n  {stalls['agents']} agents went 25+ consecutive tool calls without editing "
                  f"a file.")
            print(f"  Longest such stretch: {stalls['worst']} calls with nothing changed.")

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
    if cands:
        top = cands[0]
        recs.append(f"Split the files under SPLIT CANDIDATES. The worst is "
                    f"`{top['path']}` — {top['agents']} different agents read it "
                    f"{top['views']} times at ~{fmt(top['avg_tokens'])} tokens a read, burning "
                    f"~{fmt(top['wasted_tokens'])} tokens on repeat reads alone. A file this shape "
                    f"is answering narrow questions with a wide read; splitting it by "
                    f"responsibility, or adding a table of contents with line ranges, lets an agent "
                    f"ask for the part it needs.")
    elif r["total"] and 100 * r["reread"] / r["total"] > 25:
        recs.append(f"{100*r['reread']/r['total']:.0f}% of reads re-open a file the agent already "
                    f"read, but no single file dominates. That points at reading habits rather than "
                    f"file structure.")
    if r["whole_file"] and 100 * r["whole_file"] / max(r["whole_file"] + r["ranged"], 1) > 50:
        recs.append("Most reads pull whole files. Adding a table of contents with line ranges to "
                    "your largest files lets an agent ask for the part it needs.")
    dead = data.get("dead_ends") or {}
    if dead.get("count") and side:
        share = 100 * dead["tokens"] / max(side, 1)
        recs.append(f"{dead['count']} agents ran 25+ turns and signed off with under 800 "
                    f"characters, costing {fmt(dead['tokens'])} tokens ({share:.0f}% of subagent "
                    f"spend). Work that thin either got abandoned or was redone somewhere else. "
                    f"Read a few of them; the usual cause is a task that was never scoped tightly "
                    f"enough to finish.")
    mcp = data.get("mcp") or {}
    if mcp.get("by_server"):
        mcp_total = sum(mcp["by_server"].values())
        share = 100 * mcp_total / max(mcp["total_tool_calls"], 1)
        if share < 2:
            servers = ", ".join(sorted(mcp["by_server"], key=lambda s: -mcp["by_server"][s])[:4])
            recs.append(f"MCP tools were {share:.1f}% of your tool calls ({fmt(mcp_total)} of "
                        f"{fmt(mcp['total_tool_calls'])}), across: {servers}. Every connected "
                        f"server's tool schemas sit in your context on every turn whether you call "
                        f"them or not. Disconnect the ones you are not using per project.")
    if loops["total_repeats"] > 50:
        top = loops["signatures"][0] if loops["signatures"] else ("", 0)
        recs.append(f"{loops['total_repeats']:,} redundant calls came from agents repeating an "
                    f"identical action 3+ times, across {loops['agents_looping']} agents. The most "
                    f"repeated was `{top[0][:60]}`. A repeat loop means the agent could not tell "
                    f"whether the call worked, so make the command's success or failure obvious, or "
                    f"remove the need for it.")
    if stalls["agents"] > 5:
        recs.append(f"{stalls['agents']} agents ran 25+ consecutive tool calls without editing "
                    f"anything (worst: {stalls['worst']}). Long unproductive stretches usually mean "
                    f"the agent lost the thread and kept searching. These are the sessions worth "
                    f"reading by hand.")
    if data["error_hits"].get("shell glob not expanded", 0) > 20:
        recs.append(f"{data['error_hits']['shell glob not expanded']} commands died because the "
                    f"shell tried to expand a glob meant for the program (`--include=*.ts` and "
                    f"friends). zsh treats an unmatched glob as a fatal error where bash passes it "
                    f"through. Fix it in the shell rather than in a docs file: `unsetopt nomatch` "
                    f"in ~/.zshrc (or `setopt +o nomatch`).")
    if data["error_hits"].get("container / docker", 0) > 50:
        recs.append("Container startup failures are frequent. If your default test command boots "
                    "containers, document a fast lane that does not; agents use the slowest command "
                    "you gave them because it is the only one they know about.")
    if data["error_hits"].get("permission / approval", 0) > 30:
        recs.append("Permission friction is costing real tokens. Add an allowlist for read-only and "
                    "routine dev commands to your agent settings, and keep mutating commands out "
                    "of it. Beware prefix matches: allowing `find` also allows `find -delete`.")
    if data["error_hits"].get("port already in use", 0) > 10:
        recs.append("Port conflicts recur. Agents leave dev servers running. Have them start "
                    "long-running processes in the background with a known port and kill them by "
                    "name, or pick a random free port.")
    cheap_models = sum(v for k, v in data["by_model"].items() if "haiku" in k or "sonnet" in k)
    if grand and cheap_models / grand < 0.2:
        recs.append(f"Only {100*cheap_models/grand:.0f}% of your tokens run on Sonnet or Haiku. "
                    f"Read-only exploration rarely needs your largest model.")
    if not recs:
        recs.append("Nothing above the alarm thresholds. Your setup looks healthy.")
    shown = recs if show_all else recs[:6]
    for i, rec in enumerate(shown, 1):
        print(f"\n  {i}. {rec}")
    if len(recs) > len(shown):
        print(f"\n  ...and {len(recs) - len(shown)} smaller findings. Run with --all to see them, "
              f"or fix these first and re-run.")
    print()


def main():
    ap = argparse.ArgumentParser(description="Find out where your Claude Code tokens actually go.")
    ap.add_argument("--days", type=int, help="only look at the last N days")
    ap.add_argument("--json", metavar="PATH", help="also write raw findings as JSON")
    ap.add_argument("--all", action="store_true", help="show every recommendation, not just the top 6")
    args = ap.parse_args()

    data = analyze(args.days)
    report(data, show_all=args.all)

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

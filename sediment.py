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

# Error signatures, matched only against results the harness marked as failures. Ordered most
# to least specific so the first match wins, and deliberately spanning several ecosystems.
#
# Every pattern here matches text that appears only when something failed, never a word that
# also appears in ordinary output about the same subject: "testcontainer" shows up in passing
# test names, "eslint" in directory listings. Matching those inflated the table roughly twenty
# fold before this was tightened.
#
# The table undercounts, deliberately. A command that fails inside a pipeline, or reports
# failures in its output while still exiting zero, is not marked as an error by the harness and
# is not counted here. Overcounting was the worse failure: it made the report confidently wrong.
ERROR_PATTERNS = [
    # Shell and environment
    ("shell glob not expanded", r"no matches found:|zsh: bad pattern:"),
    ("command not found", r"command not found|is not recognized as an internal"),
    ("permission / approval", r"[Pp]ermission denied|requires approval|EACCES|not permitted"),
    ("file not found", r"No such file or directory|ENOENT"),
    ("command timed out", r"command timed out|timed out after|ETIMEDOUT"),
    ("port already in use", r"EADDRINUSE|address already in use|port \d+ is already"),
    # Test runners, several ecosystems
    ("test failure (js)", r"^\s*FAIL\s+\S+|Tests?:?\s+\d+\s+failed|✕ |● .*›"),
    ("test failure (python)", r"=+ FAILURES =+|\d+ failed[,\s]|E\s+assert "),
    ("test failure (go)", r"^--- FAIL|FAIL\s+\S+\s+\d+\.\d+s"),
    ("test failure (rust/other)", r"test result: FAILED|thread '.*' panicked"),
    # Build and type systems
    ("type error", r"error TS\d{4}|mypy: error:|error\[E\d+\]|type error:"),
    ("lint failure", r"\d+ problems? \(\d+ error|✖ \d+ problem|rubocop.*\d+ offense"),
    ("package script failure", r"ELIFECYCLE|command failed with exit code|npm ERR!|make: \*\*\*"),
    ("dependency resolution", r"ERESOLVE|could not resolve dependency|version solving failed"),
    # Infra
    ("container / docker", r"Could not find a working container runtime|"
                           r"docker.*(daemon is not running|Cannot connect)|"
                           r"Cannot connect to the Docker"),
    ("database connection", r"ECONNREFUSED.*(5432|3306|27017)|could not connect to server|connection refused"),
    # Version control
    ("git conflict or dirty tree", r"CONFLICT|would be overwritten|Your local changes"),
    ("git worktree/branch exists", r"fatal:.*already exists|is already checked out"),
    # Agent-harness specific
    ("edit target missing", r"String to replace not found|has not been read yet"),
]

# Commands that exit non-zero to mean "no match" rather than "something broke". The harness
# marks those results as errors, which buries real failures under searches that found nothing.
EXIT_1_IS_NORMAL = {"grep", "rg", "egrep", "fgrep", "find", "diff", "test", "cmp", "ag", "ack"}

# Shapes that say nothing about your setup: shell keywords that are not commands, and calls the
# harness refuses on purpose. Counting them puts noise at the top of the failing-command list.
NOT_YOUR_PROBLEM = {"sleep", "for", "while", "if", "do", "done", "then", "fi", "(empty)"}


# Tool calls that change something. Used to tell real progress from spinning.
PRODUCTIVE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}

# An agent whose last act is one of these handed its work off through the harness rather than
# writing a final message, so a short final message does not mean it produced nothing.
DELIVERY_TOOLS = {"StructuredOutput", "SendMessage", "TaskOutput", "ReportFindings"}

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

    Git worktrees give one source file many absolute paths, which would otherwise split its cost
    across a dozen rows and hide how expensive it really is. Keying on the trailing components
    alone would go too far the other way and merge same-named files from different repositories,
    so the key keeps everything below the checkout directory: for a worktree the checkout name
    varies while the path under it does not, and two repositories that both hold lib/server.ts
    stay apart because the rest of their paths differ.
    """
    parts = [p for p in path.split(os.sep) if p]
    if len(parts) < 2:
        return path
    # Find the deepest directory that looks like a source root and key on everything under it.
    for marker in ("src", "lib", "app", "packages", "tests", "test", "docs", "scripts"):
        if marker in parts[:-1]:
            idx = len(parts) - 1 - parts[::-1].index(marker)
            return os.sep.join(parts[idx:])
    return os.sep.join(parts[-2:])


def bucket_of(ctx):
    for lo, hi, label in BUCKETS:
        if lo <= ctx < hi:
            return label
    return ">600k"


def normalize_command(cmd):
    """Collapse a shell command to its shape so retries of the same thing group together."""
    cmd = cmd.strip().split("\n")[0]
    # Step over wrappers so the shape is the command that failed, not the navigation before it.
    for _ in range(3):
        stripped = re.sub(r"^\s*(cd|export|env|source)\s+[^&;|]*(&&|;)\s*", "", cmd)
        if stripped == cmd:
            break
        cmd = stripped
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
    if days is not None:
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
                    if not isinstance(rec, dict):
                        continue
                    ts = rec.get("timestamp")
                    if cutoff and (ts if isinstance(ts, str) else "") < cutoff:
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
        lambda: {"tokens": 0, "turns": 0, "peak": 0, "out": 0, "model": "", "final_chars": 0,
                 "session": "", "project": "", "started": ""})
    spawn_costs = []

    tool_bytes = collections.Counter()
    tool_calls = collections.Counter()
    tool_names = {}          # tool_use_id -> name
    tool_inputs = {}         # tool_use_id -> input dict

    reads_per_agent = collections.defaultdict(collections.Counter)
    read_no_range = read_ranged = 0
    read_tokens_by_file = collections.Counter()   # path -> tokens returned across all reads
    read_counts_by_file = collections.Counter()   # path -> how many reads returned content

    # Downstream cost. When a finding appears, remember the agent and how much it had spent, then
    # bill it for what that agent spends over its next few turns. This is attribution, not proof:
    # some of that spend would have happened anyway.
    DOWNSTREAM_TURNS = 5
    pending = collections.defaultdict(list)      # agent -> [(label, turns_left, spend_at_start)]
    downstream = collections.Counter()           # label -> tokens spent after it appeared
    billed = collections.defaultdict(set)        # label -> {(agent, turn index) already charged}
    billed_any = set()                           # every (agent, turn) charged by any label
    downstream_total = [0]                       # corpus-wide, each turn counted once
    agent_turn = collections.Counter()           # agent -> turns seen so far
    examples = {}                                # label -> one real sample, for the report
    agent_spend = collections.Counter()          # agent -> running total

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
        ts = rec.get("timestamp")
        ts = ts if isinstance(ts, str) else ""
        if ts:
            if first_seen_ts is None or ts < first_seen_ts:
                first_seen_ts = ts
            if last_seen_ts is None or ts > last_seen_ts:
                last_seen_ts = ts

        usage = msg.get("usage")
        if isinstance(usage, dict):
            def _n(key):
                value = usage.get(key) or 0
                return value if isinstance(value, int) else 0
            inp, out = _n("input_tokens"), _n("output_tokens")
            cr, cw = _n("cache_read_input_tokens"), _n("cache_creation_input_tokens")
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

            agent_spend[agent_key] += spent
            agent_turn[agent_key] += 1
            turn_no = agent_turn[agent_key]
            still = []
            for sig_label, left, at_start in pending[agent_key]:
                # Charge this turn once per signature even when several are open, so overlapping
                # windows cannot bill the same tokens twice and push the total over 100%.
                key = (agent_key, turn_no)
                if key not in billed[sig_label]:
                    billed[sig_label].add(key)
                    downstream[sig_label] += spent
                if key not in billed_any:
                    billed_any.add(key)
                    downstream_total[0] += spent
                if left > 1:
                    still.append((sig_label, left - 1, at_start))
            pending[agent_key] = still

            if is_side:
                rec_agent = agents[agent_key]
                if rec_agent["turns"] == 0:
                    spawn_costs.append(ctx)
                rec_agent["tokens"] += spent
                rec_agent["turns"] += 1
                rec_agent["out"] += out
                rec_agent["peak"] = max(rec_agent["peak"], ctx)
                rec_agent["model"] = msg.get("model", "")
                if not rec_agent["session"]:
                    rec_agent["session"] = sid
                    rec_agent["project"] = os.path.basename(rec.get("cwd") or "") or "?"
                    rec_agent["started"] = ts[:16].replace("T", " ")
                body = msg.get("content")
                if isinstance(body, list):
                    chars = sum(len(b.get("text", "") or "")
                                for b in body if isinstance(b, dict) and b.get("type") == "text")
                    # Reporting through a delivery tool is the normal path, not a dead end.
                    if any(isinstance(b, dict) and b.get("type") == "tool_use"
                           and b.get("name") in DELIVERY_TOOLS for b in body):
                        chars = max(chars, 800)
                    rec_agent["final_chars"] = chars

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
                raw_input = block.get("input")
                tool_inputs[block.get("id")] = raw_input if isinstance(raw_input, dict) else {}
                tool_calls[name] += 1

                # Signature of what this call actually does, so an identical repeat is visible.
                params = tool_inputs[block.get("id")]
                if name == "Bash":
                    sig = f"Bash:{(params.get('command') or '')[:160]}"
                elif name in ("Read", "Edit", "Write"):
                    # Include the range: paging through a file is not repeating yourself.
                    span = f"#{params.get('offset','')}:{params.get('limit','')}"
                    sig = f"{name}:{params.get('file_path','?')}{span if span != '#:' else ''}"
                elif name in ("Grep", "Glob"):
                    sig = f"{name}:{params.get('pattern','?')}:{params.get('path','')}"
                else:
                    sig = f"{name}:{json.dumps(params, sort_keys=True, default=str)[:160]}"
                # Editing the same file repeatedly is normal iterative work, not a loop, so
                # productive tools carry no signature and are only used to break a stall run.
                trace[agent_key].append((None if name in PRODUCTIVE_TOOLS else sig,
                                         name in PRODUCTIVE_TOOLS))

                if name == "Read":
                    path = params.get("file_path", "?")
                    span = (params.get("offset"), params.get("limit"))
                    reads_per_agent[agent_key][(path, span)] += 1
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
                shape = None
                if is_error and name == "Bash":
                    shape = normalize_command((tool_inputs.get(tid) or {}).get("command") or "")
                    body = re.sub(r"^\s*Exit code \d+\s*", "", text).strip()
                    if shape.split()[0] in NOT_YOUR_PROBLEM:
                        shape = None
                    elif shape.split()[0] in EXIT_1_IS_NORMAL and not body:
                        # A search that printed nothing and exited non-zero found nothing.
                        is_error = False
                        shape = None
                    else:
                        cmd_failures[shape] += 1
                        cmd_sessions[shape].add(sid)
                if text and is_error:
                    head = text[:4000]
                    for label, pattern in ERROR_PATTERNS:
                        found = re.search(pattern, head, re.MULTILINE)
                        if found:
                            error_hits[label] += 1
                            error_sessions[label].add(sid)
                            pending[agent_key].append(
                                (label, DOWNSTREAM_TURNS, agent_spend[agent_key]))
                            if label not in examples:
                                # Keep only what the pattern matched. Whole lines carried absolute
                                # paths and, in one real case, a keychain lookup with a wallet
                                # address, and the report is meant to be pasted to an agent.
                                examples[label] = found.group(0)[:80]
                            break


    # Anything still open when the input ends already had every turn it saw charged above, so
    # there is nothing left to flush; the entries simply expire.
    pending.clear()

    reread = sum(sum(c - 1 for c in files.values() if c > 1) for files in reads_per_agent.values())
    total_reads = sum(sum(files.values()) for files in reads_per_agent.values())

    # Split candidates. A file that many DIFFERENT agents open in full, repeatedly, is a file
    # whose structure forces a wide read for a narrow question. Rank by tokens spent re-reading,
    # because that is the part splitting the file actually recovers.
    per_file = collections.defaultdict(
        lambda: {"views": 0, "rereads": 0, "agents": set(), "tokens": 0, "avg": 0})
    for agent, files in reads_per_agent.items():
        for (path, _span), count in files.items():
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
         "wasted_tokens": g["tokens"], "checkouts": len(g["copies"])}
        for key, g in grouped.items()
        # avg is the tokens a single read returned. A small file is cheap to re-read and
        # splitting it would not help, however many checkouts of it exist.
        if g["rereads"] >= 3 and len(g["agents"]) >= 2 and g["avg"] >= 2_000
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
        # An Explore or review agent never edits by design; a long unproductive run is its
        # normal shape, not a stall. Only agents that do edit somewhere can be said to stall.
        if not any(productive for _, productive in steps):
            continue
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
        "downstream": dict(downstream),
        "downstream_total": downstream_total[0],
        "examples": dict(examples),
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


def report(data):
    t = data["totals"]
    grand = sum(t.values())
    side = sum(data["sidechain_totals"].values())
    # A transcript can hold real tool activity with no usage telemetry attached. The token
    # sections are meaningless then, but the loops, errors and re-reads are not, so print what
    # there is rather than throwing the whole report away.
    have_tokens = grand > 0
    w = data["window"]

    print("=" * 74)
    print(f"  SEDIMENT    {w['from']} to {w['to']}")
    print("=" * 74)

    high = high_out = 0
    if not have_tokens:
        print("\nNo token usage recorded in these transcripts. Token sections are skipped;"
              "\nthe behaviour findings below are unaffected.")
    else:
        print(f"\nTotal tokens processed: {fmt(grand)}")
        print(f"  cache reads   {fmt(t.get('cache_read', 0)):>16}  {100*t.get('cache_read',0)/grand:5.1f}%")
        print(f"  cache writes  {fmt(t.get('cache_write', 0)):>16}  {100*t.get('cache_write',0)/grand:5.1f}%")
        print(f"  fresh input   {fmt(t.get('in', 0)):>16}  {100*t.get('in',0)/grand:5.1f}%")
        print(f"  output        {fmt(t.get('out', 0)):>16}  {100*t.get('out',0)/grand:5.1f}%")

        cr, cw = t.get("cache_read", 0), t.get("cache_write", 0)
        if cr + cw:
            print(f"\nCache hit rate: {100*cr/(cr+cw):.1f}%"
                  f"  (above ~85% means caching is not your problem)")
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
            print(f"  {label:<12}{data['turns_by_bucket'].get(label,0):>9,}"
                  f"{fmt(spent):>17}{fmt(out):>13}{ratio:>9.0f}x")

        if len(ratios) > 1:
            print(f"\n  Same work costs {max(ratios.values())/min(ratios.values()):.0f}x more "
                  f"at the top of this table than the bottom.")
        high = sum(v for k, v in data["by_bucket"].items() if k in ("200-400k", "400-600k", ">600k"))
        high_out = sum(v for k, v in data["out_by_bucket"].items()
                       if k in ("200-400k", "400-600k", ">600k"))
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
        print(f"  {'tokens':>16}{'turns':>7}{'peak ctx':>11}   {'started':<16} {'project':<16} session")
        for a in top[:6]:
            print(f"  {fmt(a['tokens']):>16}{a['turns']:>7,}{fmt(a['peak']):>11}   "
                  f"{a['started']:<16} {a['project'][:16]:<16} {a['session'][:8]}")
        print("\n  Find one with: grep -rl <session> ~/.claude/projects")
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
    # A handful of tokens is not worth a section; it reads as a finding when it is not one.
    if sum(c["wasted_tokens"] for c in cands) < 100_000:
        cands = []
    if cands:
        print("\n" + "-" * 74)
        print("  SPLIT CANDIDATES — files whose shape forces a wide read for a narrow question")
        print("-" * 74)
        print(f"  {'wasted':>9}{'avg size':>10}{'reads':>7}{'agents':>8}{'checkouts':>10}   file")
        for c in cands[:10]:
            path = c["path"]
            if len(path) > 40:
                path = "..." + path[-37:]
            print(f"  {fmt(c['wasted_tokens']):>9}{fmt(c['avg_tokens']):>10}"
                  f"{c['views']:>7}{c['agents']:>8}{c['checkouts']:>10}   {path}")
        total_wasted = sum(c["wasted_tokens"] for c in cands)
        noun = "file" if len(cands) == 1 else "files"
        print(f"\n  {len(cands)} {noun} fit the pattern; re-reading them cost ~{fmt(total_wasted)} tokens.")
        print("  'wasted' is tokens spent on reads after the first, which is what a split recovers.")

    if data["cmd_failures"]:
        print("\n  Failing command shapes:")
        for shape, n in sorted(data["cmd_failures"].items(), key=lambda kv: -kv[1])[:8]:
            print(f"    {n:>5} failures across {data['cmd_sessions'].get(shape,0):>3} sessions   {shape}")

    if data["error_hits"]:
        down = data.get("downstream") or {}
        eg = data.get("examples") or {}
        print("\n  Error signatures, by what the agent spent in the 5 turns after each:")
        ranked = sorted(data["error_hits"].items(), key=lambda kv: -down.get(kv[0], 0))
        for label, n in ranked[:10]:
            cost = down.get(label, 0)
            print(f"    {fmt(cost):>14} tok  {n:>5} hits / {data['error_sessions'].get(label,0):>3} "
                  f"sessions   {label}")
            if eg.get(label):
                print(f"                     e.g. {eg[label]}")
        total_down = data.get("downstream_total", 0)
        if grand:
            print(f"\n  All error signatures together: {fmt(total_down)} tokens, "
                  f"{100*total_down/grand:.1f}% of everything. Attribution, not proof.")

    # Loops and stalls.
    loops = data["loops"]
    stalls = data["stalls"]
    if loops["signatures"] or stalls["agents"]:
        print("\n" + "-" * 74)
        print("  LOOPS AND STALLS — agents repeating themselves or making no progress")
        print("-" * 74)
        if loops["signatures"]:
            print(f"  {loops['agents_looping']} of {data['agent_count']} agents and sessions "
                  f"repeated an identical "
                  f"call 3+ times ({loops['total_repeats']:,} redundant calls total).")
            print("\n  Most-repeated calls:")
            for sig, n in loops["signatures"][:8]:
                n_agents = loops["agents_by_sig"].get(sig, 1)
                shown = sig if len(sig) <= 58 else sig[:55] + "..."
                print(f"    {n:>5}x by {n_agents:>3} agents   {shown}")
        if stalls["agents"]:
            print(f"\n  {stalls['agents']} editing agents went 25+ consecutive tool calls without editing "
                  f"a file.")
            print(f"  Longest such stretch: {stalls['worst']} calls with nothing changed.")

    # Recommendations, thresholded off the numbers above.
    print("\n" + "=" * 74)
    print("  WHAT LOOKS WORTH FIXING, LARGEST FIRST")
    print("=" * 74)
    # Each recommendation carries the tokens it plausibly concerns, so the list reads in order of
    # size rather than in the order the checks happen to run. The weights are rough: they say
    # which findings look worth reading first, not what any fix would return.
    down = data.get("downstream") or {}
    over600 = sum(v for k, v in data["by_bucket"].items() if k == ">600k")
    splitw = sum(c["wasted_tokens"] for c in (data.get("split_candidates") or []))
    loopw = 0
    recs = []
    if high and 100 * high / grand > 30:
        recs.append((high, 
            f"{100*high/grand:.0f}% of spend happened above 200k context, where a token of output "
            f"cost several times what it did earlier in a session. Long-running agents that "
            f"checkpoint to a file, branch or PR and hand off to a fresh one would avoid some of "
            f"that. On most corpora this is the largest single line item."))
    if agents and sum(1 for a in agents.values() if a["peak"] > 600_000):
        n = sum(1 for a in agents.values() if a["peak"] > 600_000)
        recs.append((over600, f"{n} agents went past 600k context. Worth checking what they were "
                    f"doing; runs that long are often several tasks in one agent."))
    if cands:
        top = cands[0]
        recs.append((splitw, f"Split the files under SPLIT CANDIDATES. The worst is "
                    f"`{top['path']}` — {top['agents']} different agents read it "
                    f"{top['views']} times at ~{fmt(top['avg_tokens'])} tokens a read, burning "
                    f"~{fmt(top['wasted_tokens'])} tokens on repeat reads alone. A file this shape "
                    f"is answering narrow questions with a wide read; splitting it by "
                    f"responsibility, or adding a table of contents with line ranges, lets an agent "
                    f"ask for the part it needs."))
    elif r["total"] and 100 * r["reread"] / r["total"] > 25:
        recs.append((0, f"{100*r['reread']/r['total']:.0f}% of reads re-open a file the agent already "
                    f"read, but no single file dominates. That points at reading habits rather than "
                    f"file structure."))
    if r["whole_file"] and 100 * r["whole_file"] / max(r["whole_file"] + r["ranged"], 1) > 50:
        recs.append((0, "Most reads pull whole files. A table of contents with line ranges in the "
                    "largest files would let an agent ask for the part it needs."))
    dead = data.get("dead_ends") or {}
    if dead.get("count") and side:
        share = 100 * dead["tokens"] / max(side, 1)
        recs.append((dead['tokens'], f"{dead['count']} agents ran 25+ turns and signed off with under 800 "
                    f"characters, costing {fmt(dead['tokens'])} tokens ({share:.0f}% of subagent "
                    f"spend). Some of that is likely work abandoned or redone elsewhere, though a short "
                    f"sign-off does not prove it. Worth opening a few. "
                    f""))
    mcp = data.get("mcp") or {}
    if mcp.get("by_server"):
        mcp_total = sum(mcp["by_server"].values())
        share = 100 * mcp_total / max(mcp["total_tool_calls"], 1)
        if share < 2:
            servers = ", ".join(sorted(mcp["by_server"], key=lambda s: -mcp["by_server"][s])[:4])
            recs.append((0, f"MCP tools were {share:.1f}% of your tool calls ({fmt(mcp_total)} of "
                        f"{fmt(mcp['total_tool_calls'])}), across: {servers}. Each connected "
                        f"server's tool schemas sit in context on every turn whether or not you "
                        f"call them, so pruning unused ones per project may help."))
    loopw = loops["total_repeats"] * (grand // max(sum(data["turns_by_bucket"].values()), 1))
    if loops["total_repeats"] > 50:
        top = loops["signatures"][0] if loops["signatures"] else ("", 0)
        recs.append((loopw, f"{loops['total_repeats']:,} redundant calls came from agents repeating an "
                    f"identical action 3+ times, across {loops['agents_looping']} agents. The most "
                    f"repeated was `{top[0][:60]}`. Repeats often mean the agent could not tell "
                    f"whether the call worked; making success or failure obvious, or removing the "
                    f"need for the call, tends to help."))
    if stalls["agents"] > 5:
        recs.append((0, f"{stalls['agents']} agents ran 25+ consecutive tool calls without editing "
                    f"anything (worst: {stalls['worst']}). That can mean an agent lost the thread, "
                    f"though read-only work looks the same. Worth a look."))
    if data["error_hits"].get("shell glob not expanded", 0) > 20:
        recs.append((down.get('shell glob not expanded', 0), f"{data['error_hits']['shell glob not expanded']} commands died because the "
                    f"shell tried to expand a glob meant for the program (`--include=*.ts` and "
                    f"friends). zsh treats an unmatched glob as a fatal error where bash passes it "
                    f"through. This is fixable in the shell rather than in a docs file: `unsetopt nomatch` "
                    f"in ~/.zshrc (or `setopt +o nomatch`)."))
    if data["error_hits"].get("container / docker", 0) > 50:
        recs.append((down.get('container / docker', 0), "Container startup failures are frequent. If your default test command boots "
                    "containers, document a fast lane that does not; agents use the slowest command "
                    "you gave them because it is the only one they know about."))
    if data["error_hits"].get("permission / approval", 0) > 30:
        recs.append((down.get('permission / approval', 0), "Permission friction is costing real tokens. Add an allowlist for read-only and "
                    "routine dev commands to your agent settings, and keep mutating commands out "
                    "of it. Beware prefix matches: allowing `find` also allows `find -delete`."))
    if data["error_hits"].get("port already in use", 0) > 10:
        recs.append((down.get('port already in use', 0), "Port conflicts recur. Agents leave dev servers running. Have them start "
                    "long-running processes in the background with a known port and kill them by "
                    "name, or pick a random free port."))
    cheap_models = sum(v for k, v in data["by_model"].items() if "haiku" in k or "sonnet" in k)
    if grand and cheap_models / grand < 0.2:
        recs.append((0, f"Only {100*cheap_models/grand:.0f}% of your tokens run on Sonnet or Haiku. "
                    f"Read-only exploration may not need your largest model."))
    if not recs:
        recs.append((0, "Nothing crossed the thresholds this run."))
    recs.sort(key=lambda pair: -pair[0])
    for weight, rec in recs:
        size = f"~{fmt(weight)} tokens" if weight else "size not measured"
        print(f"\n  [{size}]  {rec}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Find out where your Claude Code tokens actually go.")
    ap.add_argument("--days", type=int, help="only look at the last N days")
    ap.add_argument("--json", metavar="PATH", help="also write raw findings as JSON")
    args = ap.parse_args()
    if args.days is not None and args.days < 1:
        ap.error("--days must be at least 1")

    data = analyze(args.days)
    report(data)

    if args.json:
        serializable = dict(data)
        serializable["agents"] = [
            {k: v for k, v in a.items()}
            for a in sorted(data["agents"].values(), key=lambda x: -x["tokens"])[:50]
        ]
        try:
            with open(args.json, "w") as fh:
                json.dump(serializable, fh, indent=2)
            print(f"  Raw findings written to {args.json}\n")
        except OSError as exc:
            print(f"  Could not write {args.json}: {exc}\n")


if __name__ == "__main__":
    main()

# sediment

Find out where your Claude Code tokens actually go.

Claude Code already writes session transcripts to `~/.claude/projects`. `sediment`
reads those local transcripts and turns them into a token audit: where spend
accumulates, which agents got bloated, which commands keep failing, and which
patterns are likely wasting context.

It is deliberately small: one Python file, no install step, no dependencies, no
network calls, no API key, and no account. It reads transcripts on your machine
and prints a report.

It audits **all** of your Claude Code history, across every project on the
machine, not just the repo you run it from. That is intentional: recurring
failures, long-lived agents, and context habits usually cross repo boundaries.
When you use the report to improve one repo, treat findings from other repos as
context, not as local files to fix.

## Quick Start

Get it and run it:

```bash
git clone https://github.com/vraspar/sediment
cd sediment
python3 sediment.py
```

For a recent window:

```bash
python3 sediment.py --days 14
```

To hand the raw findings to an agent:

```bash
python3 sediment.py --json findings.json
```

If Claude Code has not written any transcripts under `~/.claude/projects`,
`sediment` will say so and exit.

`sediment` needs Python 3.8 or newer. Runtime depends on transcript size; a
large history usually takes a few seconds to a minute, roughly 10 seconds per
gigabyte.

## Copy-Paste Prompt

Paste this into Claude Code from the repo you want to improve:

```text
Audit my Claude Code token history end to end, then find fixes for this repo.

sediment reads all of my Claude Code history across every project, so some
findings will name files and repos other than this one. Use those only as
context. Propose changes only for this repo, and say explicitly when a finding
belongs somewhere else.

Run:
tmp=$(mktemp -d)
git clone https://github.com/vraspar/sediment "$tmp/sediment"
python3 "$tmp/sediment/sediment.py" --json findings.json

Then use findings.json and the printed sediment report to find concrete fixes in
this repo. Do not give me generic agent best practices. Tie every recommendation
to evidence from sediment, then confirm it against this codebase before proposing
a change. Prefer fixing the environment, code, hooks, or lint rules over adding
more agent instructions. If a docs change is still the cheapest working fix,
rewrite the stale instruction in place instead of appending a correction.
```

For a more thorough version, use [`PROMPT.md`](./PROMPT.md). It walks the agent
through confirming findings, checking agent-facing docs, finding oversized files,
and ranking fixes.

## What The Report Shows

`sediment` focuses on the parts of agent work that usually hide in plain sight:

- **Burn ratio:** tokens spent per token of output, grouped by context size.
  This shows how much more expensive a long-lived agent becomes as its context
  fills up.
- **Cache health:** cache reads, cache writes, fresh input, and output share.
  A high cache hit rate means caching is not the bottleneck.
- **Subagent concentration:** how many sidechain agents ran, how expensive the
  largest ones were, and whether a fresh spawn would have been cheaper than
  another turn in a bloated context.
- **Loops and stalls:** repeated identical tool calls, plus long stretches of
  tool use where the agent did not edit anything.
- **Waste signals:** repeated file reads, whole-file reads, recurring failing
  command shapes, and error signatures that show up across sessions.
- **Recommendations:** thresholded suggestions based on your numbers rather
  than a generic checklist.

Example, shortened:

```text
==========================================================================
  SEDIMENT    2026-07-09 to 2026-08-21
==========================================================================

Total tokens processed: 21,500,000,000
  cache reads     20,000,000,000   93.0%
  cache writes     1,440,000,000    6.7%
  fresh input          8,000,000    0.0%
  output              62,000,000    0.3%

Cache hit rate: 93.3%  (above ~85% means caching is not your problem)
Subagents:      66% of all tokens

--------------------------------------------------------------------------
  BURN RATIO — tokens spent per token of output, by context size
--------------------------------------------------------------------------
  context         turns     tokens spent       output      burn
  <50k           13,000      480,000,000    3,400,000      140x
  100-200k       31,000    4,500,000,000   20,000,000      225x
  400-600k        8,000    4,000,000,000    5,200,000      770x
  >600k           4,900    3,700,000,000    2,500,000     1440x

  Turns above 200k context: 68% of spend, 38% of output.

--------------------------------------------------------------------------
  LOOPS AND STALLS — agents repeating themselves or making no progress
--------------------------------------------------------------------------
  117 of 416 agents repeated an identical call 3+ times.
  229 agents went 25+ consecutive tool calls without editing a file.
```

## How To Read It

Start with the biggest pools of wasted spend:

1. **Spend above 200k context.** If this is a large share of total spend,
   lifecycle is probably the main lever. Checkpoint long-running work to a file,
   branch, or PR, then hand off to a fresh agent.
2. **Recurring errors across sessions.** These are usually cheap to fix and
   easy to miss. A command that fails in many sessions is a systems problem, not
   one unlucky run.
3. **Loops and stalls.** Repeated identical calls often mean the agent could not
   tell whether the call succeeded. Long no-edit stretches often mean it lost
   the thread and kept searching.
4. **Re-reads and whole-file reads.** If the same files keep getting read in
   full, they may be too large or poorly structured for narrow questions.

The goal is not to make every number small. Some expensive sessions are worth it.
The goal is to find spend that repeats without buying better work.

## Fix Root Causes

When a finding points to a recurring failure, avoid the reflex to add another
line to `CLAUDE.md` or `AGENTS.md`.

Agent-facing docs are context, not enforcement. They also cost tokens every time
they are loaded. Prefer fixes in this order:

1. Fix the environment.
2. Fix the code.
3. Add a hook.
4. Add a lint or test rule.
5. Rewrite the doc only if documentation is still the cheapest reliable fix.

If you do edit docs, replace stale instructions in place. Do not leave a
correction next to the old mistake.

## Options

```text
--days N     only analyze transcripts from the last N days
--json PATH  write raw findings as JSON as well as printing the report
```

## Limitations

- Downstream burn after an error is attribution, not proof that the error caused
  all later spend.
- Error detection is regex-based. It can miss quiet failures and occasionally
  match text that only discusses an error.
- Several signatures can share one root cause, so estimated savings do not add
  up cleanly.
- Token counts come from Claude Code usage records. `sediment` does not
  re-tokenize transcripts.
- It measures cost, not value. Read the numbers next to what the agent actually
  accomplished.

## License

MIT.

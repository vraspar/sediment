# sediment

Find out where your Claude Code tokens actually go.

Claude Code writes a full transcript of every session to `~/.claude/projects`. Nobody reads
them. `sediment` reads all of them and reports what you paid for, then hands you a prompt that
turns those findings into changes to your repo.

It runs entirely on your machine. **No network calls, no API key, no account, no dependencies.**
It reads your transcripts and prints numbers; it never uploads them anywhere.

## Requirements

- Python 3.8 or newer. Nothing to install.
- Transcripts at `~/.claude/projects`, which Claude Code creates on its own. If that directory is
  missing or empty, `sediment` says so and exits rather than printing an empty report.
- A few seconds to a minute, depending on how much history you have. Roughly 10 seconds per
  gigabyte of transcripts.

## Usage

```bash
python3 sediment.py
```

That prints the whole report. Two flags change what it covers:

| flag | what it does |
|---|---|
| `--days N` | Only look at the last N days. Useful for checking whether a change helped. |
| `--json PATH` | Also write the raw findings to a JSON file, for handing to an agent. |

The report has five parts: a token summary, the burn-ratio table, subagent concentration, waste
signals, and a ranked list of what to do. Abridged sample:

```
==========================================================================
  SEDIMENT    2026-07-09 to 2026-08-21
==========================================================================

Total tokens processed: 21,500,000,000
  cache reads     20,000,000,000   93.0%
  cache writes     1,440,000,000    6.7%
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
  Longest such stretch: 436 calls with nothing changed.
```

(Numbers rounded for the example. Yours will differ; the shape of the burn curve usually
doesn't.)

## Turning findings into fixes

The report tells you what happened. [`PROMPT.md`](./PROMPT.md) turns it into a plan.

```bash
python3 sediment.py --json findings.json
```

Then open `PROMPT.md`, copy the prompt inside it, and paste it into Claude Code **in the repo you
actually work in**, together with `findings.json` and the printed report. The agent can then read
both your findings and the codebase they describe.

What comes back is a ranked list of changes, each tied to the evidence that justifies it, with
the cheapest working fix for each. `PROMPT.md` also makes the agent confirm findings before
acting on them, because some are artifacts of pattern-matching, and acting on a misattributed
finding wastes more time than it saves.

## What it reports

**The burn ratio.** Tokens spent per token of output, bucketed by how full the context window was
at the time. It rises roughly an order of magnitude over a long session, because every turn pays
to re-read everything before it. Long-lived agents get expensive even when they are behaving
correctly.

**Cache health.** Hit rate, and how much of your traffic is cache reads versus fresh input.

**Subagent concentration.** How many agents ran, what the worst ten cost, and how many grew past
200k context. It prints the cost of a cold start next to the cost of one turn of your largest
agent, which is the comparison that decides whether to reuse agents or replace them.

**Loops and stalls.** Agents that made the same call three or more times, and agents that ran long
stretches of tool calls without changing a file. Editing one file repeatedly is normal iterative
work and is not counted as a loop.

**Waste signals.** How often agents re-read a file they had already read, how often they pull a
whole file with no line range, which command shapes keep failing, and which errors recur. An error
across many sessions is systemic; an error in one session is bad luck.

**Recommendations**, thresholded against your own numbers rather than drawn from a list of general
advice.

## Reading the output

In rough order of how much money is usually involved:

1. **Spend above 200k context.** If this is over a third of your total, agent lifecycle is your
   biggest lever and nothing else is close. Have long-running agents checkpoint to a file, a
   branch, or a PR and hand off to a fresh one. A cold start typically costs about 27k tokens; one
   turn of a bloated agent can cost a million.
2. **Errors that span many sessions.** Usually cheap to fix, and almost never fixed, because
   nobody knew they were happening.
3. **Loops and stalls.** A repeated identical call usually means the agent could not tell whether
   the call worked. A long stall usually means it lost the thread and kept searching.
4. **Re-read rate.** Often means a file is too big to answer one question, so it gets re-read
   rather than remembered. Split the files that show up most.
5. **Whole-file reads.** A table of contents with line ranges lets an agent ask for the part it
   needs.

## Fix things at the root, not in a docs file

When you find a recurring failure, the reflex is to add a line to `CLAUDE.md`. Resist it when
something better exists. Anthropic's documentation says these files are treated as "context, not
enforced configuration," with "no guarantee of strict compliance," and recommends hooks when
something must actually happen. A `CLAUDE.md` rule is a probabilistic fix that costs tokens on
every turn, forever.

Prefer, in order: fix the environment, fix the code, add a hook, add a lint rule, then write a doc
line. `PROMPT.md` makes the agent justify going further down that list.

## Limitations

- Downstream burn after an error is **attribution, not causation**. Some of that spend would have
  happened anyway.
- Error detection is regex over tool output. It misses failures that do not announce themselves,
  and occasionally matches source code that merely discusses an error.
- Several signatures often share one root cause, so fixing one thing can move several numbers and
  the savings do not simply add up.
- Token counts come from the usage records Claude Code writes. They are what the API reported, not
  a re-tokenization.
- It measures cost, not value. A session that burned a lot and shipped something important is not a
  problem. Read the numbers next to what you were actually doing.

## License

MIT.

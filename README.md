# claude-code-autopsy

Find out where your Claude Code tokens actually go.

Claude Code already writes a full transcript of every session to `~/.claude/projects`.
Nobody reads them. This reads all of them and tells you what you are paying for.

```bash
python3 autopsy.py
```

No install, no dependencies, no API key. Python 3.8+. Everything stays on your machine.

```bash
python3 autopsy.py --days 30           # just the last month
python3 autopsy.py --json findings.json # machine-readable, for handing to an agent
```

## Why this exists

Most advice about agent cost is about the transport layer: compress the context, cache more,
route to a cheaper model. That advice is fine, and it is also not usually where the money goes.

The money goes into turns that accomplish nothing. An agent hits an error at step 7, fails to
recover, and keeps working for another two hundred steps. Every one of those steps re-reads the
entire accumulated context. Compression makes each wasted step cheaper. It does not remove a
single wasted step.

This tool finds the wasted steps.

## What it reports

**The burn ratio.** Tokens spent per token of output, bucketed by how full the context window
was at the time. It is the headline because it is usually shocking:

```
  context         turns     tokens spent       output      burn
  <50k           13,658      486,193,053    3,460,445      141x
  100-200k       31,527    4,526,094,997   20,291,737      223x
  400-600k        8,404    4,067,387,843    5,213,903      780x
  >600k           4,935    3,672,059,325    2,555,971     1437x
```

Identical work costs an order of magnitude more late in a session than early. Not because the
model got worse, but because every turn pays to re-read everything before it. Long-lived agents
are expensive even when they are behaving perfectly.

**Cache health.** If your hit rate is already above 85%, caching is not your problem and you can
stop reading articles about it.

**Subagent concentration.** How many agents you ran, how much the worst ten cost, and how many
grew past 200k context. It also prints what a cold start actually costs next to what one turn of
your largest agent costs. That comparison tends to settle the "should I reuse agents or respawn
them" argument on its own.

**Waste signals.** How often agents re-read a file they had already read, how often they pull a
whole file with no line range, which command shapes keep failing, and which error signatures keep
recurring across sessions. Errors that show up in many different sessions are systemic; errors
in one session are bad luck.

**Recommendations**, generated from thresholds against your own numbers rather than from a list
of general best practices.

## Reading the output

Roughly, in order of how much money is usually involved:

1. **Spend above 200k context.** If this is over a third of your total, agent lifecycle is your
   biggest lever, and nothing else on this list is close. Have long-running agents checkpoint to
   a file, a branch, or a PR and hand off to a fresh one. A cold start typically costs about 27k
   tokens; one turn of a bloated agent can cost a million.

2. **Error signatures across many sessions.** These are usually cheap to fix and are almost never
   fixed, because nobody knows they are happening. A shell quirk, an undocumented test command, a
   missing permission entry.

3. **Re-read rate.** A high rate usually means a file is too large to answer one question, so it
   gets re-read instead of remembered. The fix is splitting the file, not instructing the agent to
   remember harder.

4. **Whole-file reads.** If most reads pass no line range, a table of contents in your biggest
   files lets an agent ask for the part it needs.

## Fix things at the root, not in a docs file

When you find a recurring failure, the reflex is to add a line to `CLAUDE.md`. Resist it when
something better exists.

Anthropic's own documentation says these files are treated as "context, not enforced
configuration," with "no guarantee of strict compliance," and recommends hooks when something
must actually happen. Practitioners measure roughly 80% adherence, with rules silently dropped as
the file grows. So a `CLAUDE.md` rule is a probabilistic fix that costs tokens on every single
turn, forever.

A worked example. The most common systemic failure in the first corpus this tool was built
against was `grep --include=*.ts`, which fails under zsh with `no matches found` because zsh
treats an unmatched glob as an error where bash passes it through. 552 failures across 336
agents. The `CLAUDE.md` fix would have been a rule about quoting globs, followed ~80% of the
time, costing tokens on every turn. The actual fix was one line in `~/.zshrc`:

```sh
unsetopt nomatch
```

That works 100% of the time and costs nothing. Prefer, in order: fix the environment, fix the
code, add a hook, add a lint rule, and only then write a doc line.

## Limitations

Be honest about what this does and does not show.

- Downstream burn after an error is **attribution, not causation**. The tool counts what was
  spent after an error appeared. Some of that spend would have happened anyway.
- Error detection is regex over tool output. It will miss failures that do not announce
  themselves and will occasionally match source code that merely discusses an error.
- Estimated savings are estimates. Several signatures often share one root cause, so fixing one
  thing can move several numbers, and the savings do not simply add up.
- Token counts come from the usage records Claude Code writes. They are what the API reported,
  not a re-tokenization.
- It measures cost, not value. A session that burned a lot and shipped something important is not
  a problem. Read the numbers next to what you were actually doing.

## License

MIT.

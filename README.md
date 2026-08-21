# sediment

Find out where your Claude Code tokens go, then turn the waste into fixes.

## Copy This Into Claude Code

Open the repo you want to improve, then paste:

```text
Use sediment to audit my Claude Code history and find fixes for this repo.

Run:
tmp=$(mktemp -d)
git clone https://github.com/vraspar/sediment "$tmp/sediment"
python3 "$tmp/sediment/sediment.py" --json findings.json

sediment reads Claude Code history across every project on this machine. Some
findings may belong to other repos. Use those as context only. Propose changes
only for this repo, and say when a finding does not apply here.

Use findings.json and the printed report. For each recommendation, show:
- the sediment evidence
- what you checked in this repo
- the smallest fix that should work

Prefer fixing code, environment, hooks, or lint rules over adding more agent
instructions. If a docs change is still the best fix, replace stale text instead
of appending another warning.
```

That is the fastest path. The agent will clone `sediment`, run it, read the
report, and turn the findings into a repo-specific plan.

For a stricter review workflow, use [`PROMPT.md`](./PROMPT.md).

## Run It Yourself

```bash
git clone https://github.com/vraspar/sediment
cd sediment
python3 sediment.py
```

Recent history only:

```bash
python3 sediment.py --days 14
```

Write JSON for an agent:

```bash
python3 sediment.py --json findings.json
```

`sediment` needs Python 3.8 or newer. It has no dependencies, no API key, no
account, and no network calls after you have the file. It reads local Claude
Code transcripts from `~/.claude/projects`.

If there are no transcripts there, it says so and exits.

## What It Reports

`sediment` audits all Claude Code projects on the machine, not just the repo you
run it from. That is intentional: bad loops, bloated sessions, and recurring
tool failures often show up across projects.

The report includes:

- token spend by context size
- cache reads, cache writes, fresh input, and output
- the most expensive subagents
- repeated tool calls and long no-edit stretches
- repeated file reads and files that may be too large
- failing command shapes and recurring error patterns
- ranked recommendations based on your own numbers

Use cross-repo findings carefully. They can explain your agent habits, but they
are not automatically fixes for the repo in front of you.

## How To Read It

Start with the largest repeated cost:

1. **High spend above 200k context:** checkpoint long-running work and hand off
   to a fresh agent.
2. **Errors across many sessions:** fix the command, environment, or test path
   that keeps failing.
3. **Loops and stalls:** make success or failure obvious, or remove the step
   that keeps trapping the agent.
4. **Repeated whole-file reads:** split oversized files or add a short map with
   line ranges.

The goal is not to make every expensive session disappear. Some expensive work
is worth it. The goal is to find waste that repeats.

## Options

```text
--days N     only analyze transcripts from the last N days
--json PATH  also write raw findings as JSON
```

## Limits

- Error detection is regex-based. Check findings before acting on them.
- Token counts come from Claude Code usage records. `sediment` does not
  re-tokenize transcripts.
- Downstream burn after an error is attribution, not proof that the error caused
  all later spend.
- It measures cost, not value.

## License

MIT.

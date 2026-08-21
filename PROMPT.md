# Interpretation prompt

The script tells you what happened. This prompt turns that into changes to your repo.

Run sediment first, then paste the prompt below into Claude Code in the repo you actually
work in. It works best when the agent can read both the findings and the codebase they describe.

```bash
python3 sediment.py --json findings.json
```

---

## The prompt

> I ran a token audit on my Claude Code history. The findings are in `findings.json`, and the
> summary is below. I want you to turn this into specific changes to this repo. Do not give me
> general best practices — I want fixes tied to the evidence.
>
> Paste the sediment output here.
>
> Work through this in order:
>
> **1. Confirm the findings before acting on them.** For each error signature and failing command
> shape, reproduce it or find it in the codebase. Some will be artifacts of the regex matching.
> Tell me which ones are real and which are noise. Do not skip this step; acting on a
> misattributed finding wastes more time than it saves.
>
> **2. For every confirmed finding, propose the cheapest fix that works, and say why that
> mechanism.** Rank the options in this order and justify going further down the list:
> fix the environment (shell config, tool defaults) · fix the code · add a hook · add a lint rule
> · document it. A documentation line is the last resort, not the first, because agent docs are
> advisory and cost tokens on every turn.
>
> **3. Audit the agent-facing docs for claims that are false.** Read every `CLAUDE.md`,
> `AGENTS.md`, and skill file in this repo. For each concrete claim — a command, a path, a file
> name, an environment variable, a config value, an architectural statement — check it against the
> actual repo. List every claim that is wrong, with file:line, what it says, and what is actually
> true. A wrong instruction costs an agent a failed run plus a recovery loop, so this usually
> matters more than doc length.
>
> **4. Find the files that are too big to answer one question.** Cross-reference the re-read data
> with the repo. A file that many different agents read in full, repeatedly, is a file whose
> structure forces a wide read for a narrow question. For each, say whether it splits cleanly and
> how.
>
> **5. Check for doc churn.** Use `git log` on the agent-facing docs and compare lines added to
> lines removed over the last few months. If docs only grow, look for the specific pattern where a
> step changed and the correction was appended next to the stale instruction instead of replacing
> it. Quote real examples.
>
> **6. Give me a ranked plan.** Each item: the change, the evidence count that justifies it, the
> mechanism, and a rough estimate of what it saves. Put anything about agent lifecycle first if
> the audit shows significant spend above 200k context, because that lever is usually larger than
> everything else combined.
>
> Rules while you work: rewrite docs in place to state current truth, never append a correction
> next to a stale line, and never leave a changelog trail inside a doc. If you find that a doc and
> your own memory disagree, fix the doc.

---

## What good output looks like

A finding that is worth acting on names all four of these. If any is missing, push back.

| | example |
|---|---|
| The evidence | "hundreds of failures, across hundreds of agents" |
| The root cause | "zsh errors on unmatched globs; bash passes them through" |
| The mechanism | "shell config, not a docs line, because docs are ~80% adhered to" |
| The change | "`unsetopt nomatch` in `~/.zshrc`" |

A finding that says "consider adding guidance about shell quoting to CLAUDE.md" has skipped
steps 1 and 2 and should be sent back.

## Re-run afterwards

The point of a measurement tool is the second measurement. Re-run sediment a couple of weeks
after making changes and compare. Watch the share of spend above 200k context, the recurring
error counts, and the re-read rate.

If a number did not move, the fix did not work. That is useful, and it is the part almost nobody
does.

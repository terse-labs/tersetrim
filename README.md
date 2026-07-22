# tersetrim

**Trim your shell output. Save your tokens.**

*The first tool from **Terse Labs** — lean developer tools for the AI-agent era.*

A token-optimizing command wrapper for AI coding agents (Claude Code, Cursor, and friends). It runs
your shell commands and compacts their verbose output so the agent reads far fewer tokens — without
ever corrupting an argument or spraying junk files into your working directory.

## Why

LLM coding agents pay per token and blow through context on noisy tool output. A 20-line `git status`
or a screenful of `git log` is mostly ceremony the model doesn't need. `tersetrim` compacts that output
to the signal, so every command costs the agent less.

## Quick start

```bash
pip install tersetrim         # https://pypi.org/project/tersetrim/
tersetrim git status           # runs it, prints a compact summary + a savings line on stderr
tersetrim git log -n 20        # one line per commit
tersetrim --stats              # cumulative tokens saved so far
```

Prefix `tersetrim` to any command. Commands it doesn't have a compactor for pass through untouched —
it never changes what a command *does*, only what the agent *reads*.

## Example (real output)

`tersetrim git log -n 2` on this repo:

```
[tersetrim] ~110->32 tok (70% saved)
3cbb2eed4 chore: Terse Labs launch
8a1879e9f tersetrim v0.2.0 — argv-safe token-optimizing command wrapper for AI coding agents
```

The full multi-line `git log` (commit / Author / Date / blank / message per commit) becomes one
`<short-hash> <subject>` line each — **70% fewer tokens** even on this tiny 2-commit log (longer logs save more), no commit dropped.

`git status` collapses to `N changed:` + one porcelain line per file; `ls -l` becomes `size  name`
(perms/owner/date dropped); `docker ps` / `docker images` keep only the columns an agent reasons about
(names/status/image/ports, repo/tag/id/size) and drop the id/command/created noise.

## Why argv-safe matters

Some command wrappers **reconstruct your command into a shell string and re-exec it**. A quoted `>`
or `/` inside an argument then becomes a shell redirect, spraying 0-byte junk files and directories
into your cwd — a real bug class we hit in production before building this.

tersetrim runs the **argv list directly** (`shell=False`), so the OS receives your arguments verbatim.
That class of bug is **structurally impossible** — on Windows, macOS, and Linux alike. Its self-check
asserts exactly this: an argument containing `>` and `/` round-trips untouched and creates no file.

## Works with your agent CLI

tersetrim is a plain command wrapper — any agent that runs shell commands can use it. Two
invocations:

```bash
tersetrim git status            # console script (pip's Scripts dir must be on PATH)
python -m tersetrim git status  # always works, zero PATH setup
```

> **Windows note:** `pip install --user` places `tersetrim.exe` in
> `%APPDATA%\Python\PythonXY\Scripts`, which is often *not* on PATH. Use `python -m tersetrim`
> or add that directory once.

### aider — tested (aider 0.86.2)

`/run` output is compacted *before* it enters the chat context:

```
/run python -m tersetrim git status
```

Receipt from a live session: `[tersetrim] ~47->6 tok (87% saved)` on the `/run` output line.
To make it standing policy, put this line in a conventions file you load with
`aider --read CONVENTIONS.md`:

```
When running read-only shell commands (git status/log, ls -l, docker ps),
prefix them with `python -m tersetrim` to keep their output compact.
```

### codex CLI and Cline — one AGENTS.md line covers both

Add the same policy line to your project's `AGENTS.md`. codex reads `AGENTS.md` at session
start, and Cline auto-detects `AGENTS.md` alongside its own `.clinerules/` directory (per
[Cline's rules docs](https://docs.cline.bot/customization/cline-rules), "standard format for
cross-tool compatibility"):

```
Prefix verbose read-only commands (git status, git log, docker ps) with
`python -m tersetrim` — it compacts their output and passes everything else through.
```

The wrapper is shell-level, so there is nothing agent-specific to install. (We publish savings
numbers only from real runs; compaction varies with output shape — the `[tersetrim]` banner on
every wrapped command shows the actual tokens saved, so your receipts are built in.)

## Pro

The free tier is complete and stays free. **Pro** ($19 once, all v0.x) adds:

- **More compactors** — `kubectl get`, `pip list`, `npm ls`, `git diff --stat`, `pytest`
  (failures are never dropped — they're the point of reading test output)
- **Savings analytics** — `--stats` gains a per-command breakdown, a 7-day trend, and a
  dollar translation at *your* token rate (`TERSETRIM_USD_PER_MTOK`; we don't invent prices)

```bash
tersetrim --pro                 # what pro adds + the link
tersetrim --activate YOUR-KEY   # verified once, cached, works offline forever after
```

**The honest print:** this source is MIT and public — including the pro code and its gate.
A license key is how you pay for the work, not DRM. Free never degrades: an unlicensed pro
command runs normally and passes through untouched, with at most one hint a day.

## Roadmap

- **v0.2** — `docker ps` / `docker images` ✓
- **v0.3** — pro tier: kubectl/pip/npm/git-diff/pytest compactors + savings analytics ✓
- **v0.3** — a hook that auto-wraps an agent's commands (no per-command prefix)
- **v0.4** — per-tool profiles + user-defined compactors
- **v1.0** — a compactor plugin ecosystem

## License

MIT.

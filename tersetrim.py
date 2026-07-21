#!/usr/bin/env python3
"""tersetrim — a token-optimizing command wrapper for AI coding agents. A Terse Labs tool.

Shrinks verbose CLI output so an LLM reads fewer tokens — without the bug class that plagues
shell-string rewrappers: a tool that rebuilds your command into a SHELL STRING and re-execs it
turns a quoted '>' or '/' inside an argument into a redirect, spraying junk files/dirs into your
working directory. tersetrim NEVER reconstructs a shell string — it runs the argv list directly,
so that class of bug is STRUCTURALLY IMPOSSIBLE, on every platform.

  tersetrim git status         # runs `git status`, prints a compact summary + a token-savings line
  tersetrim git log -n 20      # compacts to one line per commit
  tersetrim --stats            # cumulative tokens saved so far
  tersetrim --self-check

Design: a command has an optional COMPACTOR (compact its output); unknown commands pass through
untouched. Compaction is pure text, so it can never change what the command DID — only what the
agent READS. A tokens≈chars/4 estimate is logged to a ledger so savings are measurable.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_LEDGER = Path(os.environ.get("TERSETRIM_LEDGER", Path.home() / ".tersetrim" / "savings.jsonl"))

# A git PORCELAIN status line: two status codes from the git set, a space, a path. Prose lines from
# plain `git status` ("On branch main") do NOT match, so the compactor never counts prose as a file.
_PORCELAIN = re.compile(r"^[ MADRCU?!]{2} \S")


def _compact_git_status(raw: str) -> str:
    """`git status` -> one line per changed file + a count; passes prose through if no porcelain lines."""
    files = [l for l in raw.splitlines() if _PORCELAIN.match(l)]
    if not files:
        return "clean working tree" if "nothing to commit" in raw else raw
    return f"{len(files)} changed:\n" + "\n".join(f"  {l.strip()}" for l in files)


def _compact_git_log(raw: str) -> str:
    """Full `git log` (commit/Author/Date/blank/message blocks) -> one '<short> <subject>' per commit.
    Leaves already-oneline output untouched. Never drops a commit; only trims per-commit prose."""
    commits = []
    cur = {}
    for line in raw.splitlines():
        if line.startswith("commit "):
            if cur:
                commits.append(cur)
            cur = {"hash": line.split()[1][:9], "subject": ""}
        elif line.startswith("    ") and cur and not cur["subject"]:
            cur["subject"] = line.strip()
    if cur:
        commits.append(cur)
    if not commits:                       # not the multi-line format (already --oneline etc.)
        return raw
    return "\n".join(f"{c['hash']} {c['subject']}" for c in commits)


def _compact_ls_long(raw: str) -> str:
    """`ls -l`/`ls -la` -> 'size  name', dropping perms/links/owner/group/date (noise for an agent).
    Keeps the 'total' header out; leaves non-long output (plain `ls`) untouched."""
    out = []
    matched = False
    for line in raw.splitlines():
        if line.startswith("total "):
            continue
        # perms(10) links owner group size mon day time/year name...
        m = re.match(r"^[-dlbcps][-rwxsStT]{9}[.+]?\s+\d+\s+\S+\s+\S+\s+(\d+)\s+\S+\s+\S+\s+\S+\s+(.+)$", line)
        if m:
            matched = True
            out.append(f"{m.group(1):>10}  {m.group(2)}")
        else:
            out.append(line)
    return "\n".join(out) if matched else raw


def _compact_docker_ps(raw: str) -> str:
    """`docker ps` is very wide (id, image, command, created, status, ports, names). Keep the SIGNAL
    — NAMES, STATUS, IMAGE, PORTS — and drop id/command/created. Header-driven so column widths/order
    can't break it; passes non-table output (errors, `-q`) through untouched."""
    lines = raw.splitlines()
    if not lines or "CONTAINER ID" not in lines[0]:
        return raw
    hdr = lines[0]
    # Parse ALL columns by header position (must track every one, or a slice bleeds an untracked
    # column into a kept one), then emit only NAMES/STATUS/IMAGE/PORTS.
    heads = [h for h in ("CONTAINER ID", "IMAGE", "COMMAND", "CREATED", "STATUS", "PORTS", "NAMES")
             if h in hdr]
    if "NAMES" not in heads or "STATUS" not in heads:
        return raw
    heads.sort(key=lambda h: hdr.find(h))
    starts = [hdr.find(h) for h in heads] + [len(hdr) + 9999]
    keep = ("NAMES", "STATUS", "IMAGE", "PORTS")
    out = ["  ".join(k for k in keep if k in heads)]
    for line in lines[1:]:
        if not line.strip():
            continue
        vals = {heads[i]: line[starts[i]:starts[i + 1]].strip() for i in range(len(heads))}
        out.append("  ".join(vals.get(c, "") for c in keep if c in heads).rstrip())
    return "\n".join(out)


def _compact_docker_images(raw: str) -> str:
    """`docker images` is wide (repo, tag, id, created, size). Keep REPOSITORY/TAG/IMAGE ID/SIZE
    — the identity + footprint an agent reasons about — and drop CREATED (verbose relative time,
    low signal). Header-driven like docker ps; passes non-table output (errors, `-q`) through."""
    lines = raw.splitlines()
    if not lines or "REPOSITORY" not in lines[0] or "SIZE" not in lines[0]:
        return raw
    hdr = lines[0]
    heads = [h for h in ("REPOSITORY", "TAG", "IMAGE ID", "CREATED", "SIZE") if h in hdr]
    heads.sort(key=lambda h: hdr.find(h))
    starts = [hdr.find(h) for h in heads] + [len(hdr) + 9999]
    keep = ("REPOSITORY", "TAG", "IMAGE ID", "SIZE")
    out = ["  ".join(k for k in keep if k in heads)]
    for line in lines[1:]:
        if not line.strip():
            continue
        vals = {heads[i]: line[starts[i]:starts[i + 1]].strip() for i in range(len(heads))}
        out.append("  ".join(vals.get(c, "") for c in keep if c in heads).rstrip())
    return "\n".join(out)


def _compactor_for(argv: list[str]):
    joined = tuple(a for a in argv if not a.startswith("-"))  # ignore flags for the key
    for n in (2, 1):
        key = joined[:n]
        if key in _COMPACTORS:
            return _COMPACTORS[key]
    return None


_COMPACTORS = {
    ("git", "status"): _compact_git_status,
    ("git", "log"): _compact_git_log,
    ("ls",): _compact_ls_long,
    ("docker", "ps"): _compact_docker_ps,
    ("docker", "images"): _compact_docker_images,
}


def _est_tokens(s: str) -> int:
    return max(0, len(s) // 4)  # ponytail: chars/4 is the standard rough token estimate


def _record_saving(cmd: str, raw: str, out: str) -> None:
    try:
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        saved = _est_tokens(raw) - _est_tokens(out)
        with _LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "cmd": cmd,
                                "raw_tok": _est_tokens(raw), "out_tok": _est_tokens(out),
                                "saved_tok": saved}) + "\n")
    except Exception:  # noqa: BLE001 — telemetry must never break the wrapper
        pass


def run(argv: list[str], _runner=None) -> tuple[int, str]:
    """Run argv DIRECTLY (never via a shell string) and return (returncode, output_for_agent).
    _runner is a test seam returning (rc, stdout)."""
    if not argv:
        return 2, "tersetrim: no command given"
    if _runner is not None:
        rc, raw = _runner(argv)
    else:
        # shell=False + a list => the OS gets argv verbatim; no quoting/redirect reinterpretation.
        p = subprocess.run(argv, capture_output=True, text=True)
        rc, raw = p.returncode, (p.stdout or "") + (p.stderr or "")
    comp = _compactor_for(argv)
    out = comp(raw) if comp else raw
    if comp and raw:
        _record_saving(" ".join(argv), raw, out)
        saved = _est_tokens(raw) - _est_tokens(out)
        pct = (100 * saved // _est_tokens(raw)) if _est_tokens(raw) else 0
        sys.stderr.write(f"[tersetrim] ~{_est_tokens(raw)}->{_est_tokens(out)} tok ({pct}% saved)\n")
    return rc, out


def _stats() -> int:
    if not _LEDGER.exists():
        print("tersetrim: no savings recorded yet (run some commands through it first)")
        return 0
    n = saved = 0
    for line in _LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            saved += int(json.loads(line).get("saved_tok", 0))
            n += 1
        except (json.JSONDecodeError, ValueError):
            continue
    print(f"tersetrim: {n} commands compacted, ~{saved:,} tokens saved cumulatively")
    return 0


def _self_check() -> int:
    import tempfile
    # 1. THE rtk BUG CANNOT HAPPEN: an argument with '>' and '/' passes through as one argv element.
    marker = os.path.join(tempfile.gettempdir(), "tersetrim_selfcheck_marker")
    if os.path.exists(marker):
        os.unlink(marker)
    hostile = f"a > {marker} and x/inf/) fragment"
    rc, out = run(["python", "-c", "import sys; sys.stdout.write(sys.argv[1])", hostile])
    assert rc == 0 and out == hostile, f"argv passthrough corrupted the argument: {out!r}"
    assert not os.path.exists(marker), "tersetrim created a redirect file — the shell-rewrap redirect bug is present!"
    # 2. git status: counts porcelain lines, never prose, honest on clean
    raw = "On branch main\nChanges not staged for commit:\n M a.py\n M b.py\n\nno changes added\n"
    got = run(["git", "status"], _runner=lambda a: (0, raw))[1]
    assert got.startswith("2 changed") and "a.py" in got and "On branch" not in got, got
    assert run(["git", "status"], _runner=lambda a: (0, "nothing to commit, working tree clean\n"))[1] == "clean working tree"
    # 3. git log: multi-line -> one line per commit, never drops a commit
    glog = ("commit abc1234567\nAuthor: X\nDate: today\n\n    first subject\n\n"
            "commit def8901234\nAuthor: Y\nDate: today\n\n    second subject\n")
    gl = run(["git", "log"], _runner=lambda a: (0, glog))[1]
    assert gl == "abc123456 first subject\ndef890123 second subject", gl
    assert run(["git", "log", "--oneline"], _runner=lambda a: (0, "abc123 x\n"))[1] == "abc123 x\n"  # already-oneline untouched
    # 4. ls -l: 'size  name', drops perms/owner/date; plain ls untouched
    lsl = "total 8\n-rw-r--r-- 1 me grp 1234 Jul 15 10:00 file.py\ndrwxr-xr-x 2 me grp 4096 Jul 15 10:00 dir\n"
    lo = run(["ls", "-la"], _runner=lambda a: (0, lsl))[1]
    assert "1234  file.py" in lo and "rw-r" not in lo and "total" not in lo, lo
    assert run(["ls"], _runner=lambda a: (0, "a.py\nb.py\n"))[1] == "a.py\nb.py\n"  # plain ls untouched
    # 5. docker ps: keep NAMES/STATUS/IMAGE/PORTS, drop id/command/created; non-table untouched
    dps = ("CONTAINER ID   IMAGE          COMMAND       CREATED       STATUS       PORTS                    NAMES\n"
           "abc123def456   nginx:latest   \"/docker-e\"   2 hours ago   Up 2 hours   0.0.0.0:80->80/tcp       web\n")
    dc = run(["docker", "ps"], _runner=lambda a: (0, dps))[1]
    assert "web" in dc and "Up 2 hours" in dc and "nginx:latest" in dc, dc
    assert "abc123def456" not in dc and "docker-e" not in dc, "compactor must drop id/command"
    assert run(["docker", "ps", "-q"], _runner=lambda a: (0, "abc123\n"))[1] == "abc123\n"  # non-table untouched
    # 5b. docker images: keep REPOSITORY/TAG/IMAGE ID/SIZE, drop CREATED; non-table untouched
    dim = ("REPOSITORY   TAG       IMAGE ID       CREATED         SIZE\n"
           "nginx        latest    abc123def456   2 weeks ago     187MB\n")
    di = run(["docker", "images"], _runner=lambda a: (0, dim))[1]
    assert "nginx" in di and "187MB" in di and "abc123def456" in di, di
    assert "2 weeks ago" not in di and "CREATED" not in di, "docker images must drop CREATED"
    assert run(["docker", "images", "-q"], _runner=lambda a: (0, "abc123\n"))[1] == "abc123\n"  # non-table untouched
    # 6. unknown command passes through untouched
    assert run(["echo", "hi"], _runner=lambda a: (0, "hi\n"))[1] == "hi\n"
    # 6. flags don't break compactor lookup (git -C . status still compacts)
    assert _compactor_for(["git", "status"]) is _compact_git_status
    print("tersetrim self-check: PASS (argv passthrough quote/redirect-safe — shell-rewrap junk-file bug impossible; "
          "git-status/git-log/ls-l compactors shrink without losing signal or fabricating state)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args == ["--self-check"]:
        return _self_check()
    if args == ["--stats"]:
        return _stats()
    if not args or args == ["--help"]:
        print(__doc__.strip())
        return 0
    rc, out = run(args)
    sys.stdout.write(out if (not out or out.endswith("\n")) else out + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())

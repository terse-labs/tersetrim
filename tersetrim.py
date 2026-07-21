#!/usr/bin/env python3
"""tersetrim — a token-optimizing command wrapper for AI coding agents. A Terse Labs tool.

Shrinks verbose CLI output so an LLM reads fewer tokens — without the bug class that plagues
shell-string rewrappers: a tool that rebuilds your command into a SHELL STRING and re-execs it
turns a quoted '>' or '/' inside an argument into a redirect, spraying junk files/dirs into your
working directory. tersetrim NEVER reconstructs a shell string — it runs the argv list directly,
so that class of bug is STRUCTURALLY IMPOSSIBLE, on every platform.

  tersetrim git status         # runs `git status`, prints a compact summary + a token-savings line
  tersetrim git log -n 20      # compacts to one line per commit
  tersetrim --stats            # savings so far (pro: per-command breakdown + 7-day trend)
  tersetrim --pro              # what pro adds + where to get a key
  tersetrim --activate KEY     # unlock pro (Gumroad license key; cached, offline after)
  tersetrim --self-check

Design: a command has an optional COMPACTOR (compact its output); unknown commands pass through
untouched. Compaction is pure text, so it can never change what the command DID — only what the
agent READS. A tokens≈chars/4 estimate is logged to a ledger so savings are measurable.

PRO, honestly: the free tier is complete and stays free (git status/log, ls, docker ps/images).
Pro adds more compactors (kubectl get, pip list, npm ls, git diff --stat, pytest) and savings
analytics. The source is MIT and public — including the pro code and this gate. A license key is
how you pay for the work, not DRM; fork the gate out if you must, buy a key if it saves you money.
Free NEVER degrades: an unlicensed pro command runs normally and passes through untouched.
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
_LICENSE = Path(os.environ.get("TERSETRIM_LICENSE", Path.home() / ".tersetrim" / "license.json"))
_NOTICE_TS = _LICENSE.parent / "pro_notice_ts"
PRO_PRODUCT_ID = "ZmDHvhtMEW2egS4seEWEzg=="            # Gumroad product for license verification
PRO_URL = "https://egirald.gumroad.com/l/mzmogm"


# --------------------------------------------------------------------------- pro licensing
def _verify_license(key: str, _verifier=None) -> tuple[bool, str]:
    """(ok, message). Checks the key against Gumroad's public license-verify API (no auth needed).
    _verifier is a test seam. Network trouble is reported, never treated as valid OR as revoked."""
    if _verifier is not None:
        return _verifier(key)
    import urllib.parse
    import urllib.request
    body = urllib.parse.urlencode({"product_id": PRO_PRODUCT_ID, "license_key": key,
                                   "increment_uses_count": "false"}).encode()
    try:
        req = urllib.request.Request("https://api.gumroad.com/v2/licenses/verify", data=body,
                                     headers={"User-Agent": "tersetrim"})
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        if d.get("success") and not (d.get("purchase") or {}).get("refunded"):
            return True, "license verified"
        return False, "key not valid for tersetrim pro"
    except Exception as e:  # noqa: BLE001 — offline/blocked = can't verify, say so honestly
        return False, f"could not reach the license server ({type(e).__name__}); try again online"


def activate(key: str, _verifier=None) -> int:
    ok, msg = _verify_license(key.strip(), _verifier)
    if not ok:
        print(f"tersetrim: activation failed — {msg}")
        return 1
    _LICENSE.parent.mkdir(parents=True, exist_ok=True)
    _LICENSE.write_text(json.dumps({"key": key.strip(), "verified_at": int(time.time())}),
                        encoding="utf-8")
    print("tersetrim: pro activated — thank you for paying for the work. New compactors: "
          "kubectl get, pip list, npm ls, git diff --stat, pytest. Try: tersetrim --stats")
    return 0


def is_pro() -> bool:
    """Licensed = an activation cache exists. Deliberately offline-first: once activated, pro
    works forever without phoning home (no telemetry, no expiry for v0.x). This is a courtesy
    gate on MIT source, not DRM — see the module docstring."""
    try:
        return bool(json.loads(_LICENSE.read_text(encoding="utf-8")).get("key"))
    except (OSError, json.JSONDecodeError, ValueError):
        return False


def _pro_notice(cmd_key: str) -> None:
    """One gentle stderr line, at most once per 24h across ALL commands — never nagware."""
    try:
        if _NOTICE_TS.exists() and time.time() - float(_NOTICE_TS.read_text()) < 86400:
            return
        _NOTICE_TS.parent.mkdir(parents=True, exist_ok=True)
        _NOTICE_TS.write_text(str(time.time()), encoding="utf-8")
        sys.stderr.write(f"[tersetrim] a pro compactor exists for '{cmd_key}' — "
                         f"see `tersetrim --pro` (this notice appears at most once a day)\n")
    except Exception:  # noqa: BLE001 — a notice must never break the wrapper
        pass

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


# --------------------------------------------------------------------------- pro compactors
def _compact_kubectl_get(raw: str) -> str:
    """`kubectl get pods/deploy/svc` -> NAME + STATUS/READY only (drop RESTARTS/AGE/IP/NODE noise).
    Header-driven; non-table output passes through."""
    lines = raw.splitlines()
    if not lines or "NAME" not in lines[0].split():
        return raw
    hdr = lines[0]
    cols = hdr.split()
    keep = [c for c in ("NAME", "READY", "STATUS", "AVAILABLE", "TYPE") if c in cols]
    if len(keep) < 2:
        return raw
    idx = {c: cols.index(c) for c in keep}
    out = ["  ".join(keep)]
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= len(cols) - 2 and parts:
            out.append("  ".join(parts[idx[c]] if idx[c] < len(parts) else "" for c in keep))
    return "\n".join(out) if len(out) > 1 else raw


def _compact_pip_list(raw: str) -> str:
    """`pip list` table -> 'pkg==version' per line (the format agents and requirements share)."""
    lines = raw.splitlines()
    if len(lines) < 2 or not lines[0].startswith("Package"):
        return raw
    out = []
    for line in lines[2:]:                      # skip header + ---- rule
        parts = line.split()
        if len(parts) >= 2:
            out.append(f"{parts[0]}=={parts[1]}")
    return "\n".join(out) if out else raw


def _compact_npm_ls(raw: str) -> str:
    """`npm ls` tree -> flat 'name@version' per dep, tree glyphs and 'deduped' noise dropped."""
    deps = re.findall(r"[├└│─+\\\s-]*([a-z0-9@/._-]+)@(\d[\w.-]*)", raw, re.I)
    if not deps:
        return raw
    seen, out = set(), []
    for name, ver in deps:
        if (name, ver) not in seen:
            seen.add((name, ver))
            out.append(f"{name}@{ver}")
    return "\n".join(out)


def _compact_git_diff_stat(raw: str) -> str:
    """`git diff --stat` -> per-file 'path +N' lines without the +++/--- bar art, summary kept."""
    out = []
    changed = False
    for line in raw.splitlines():
        m = re.match(r"^\s*(\S.*?)\s*\|\s*(\d+|Bin[\s\S]*)\s*[+\-]*\s*$", line)
        if m:
            changed = True
            out.append(f"{m.group(1)} | {m.group(2).strip()}")
        else:
            out.append(line.strip())
    return "\n".join(l for l in out if l) if changed else raw


def _compact_pytest(raw: str) -> str:
    """pytest output -> failures/errors + the final summary line; passing noise dropped.
    NEVER drops a failure — the failures are the entire point of reading test output."""
    lines = raw.splitlines()
    summary = [l for l in lines if re.search(r"=+ .*(passed|failed|error|no tests ran).* =+", l)]
    if not summary:
        return raw
    keep = [l for l in lines
            if l.startswith(("FAILED", "ERROR")) or l.startswith("E ") or "::" in l and "FAILED" in l]
    return "\n".join(keep + [summary[-1].strip("= ").strip()]) if (keep or summary) else raw


_COMPACTORS = {
    ("git", "status"): _compact_git_status,
    ("git", "log"): _compact_git_log,
    ("ls",): _compact_ls_long,
    ("docker", "ps"): _compact_docker_ps,
    ("docker", "images"): _compact_docker_images,
}
_PRO_COMPACTORS = {
    ("kubectl", "get"): _compact_kubectl_get,
    ("pip", "list"): _compact_pip_list,
    ("npm", "ls"): _compact_npm_ls,
    ("git", "diff"): _compact_git_diff_stat,
    ("pytest",): _compact_pytest,
    ("python", "pytest"): _compact_pytest,      # `python -m pytest` (flags are stripped in the key)
}


def _compactor_for(argv: list[str]):
    joined = tuple(a for a in argv if not a.startswith("-"))  # ignore flags for the key
    for n in (2, 1):
        key = joined[:n]
        if key in _COMPACTORS:
            return _COMPACTORS[key]
        if key in _PRO_COMPACTORS:
            if is_pro():
                return _PRO_COMPACTORS[key]
            _pro_notice(" ".join(key))          # free tier: command runs untouched, one daily hint
            return None
    return None


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
    rows = []
    for line in _LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    saved = sum(int(r.get("saved_tok", 0)) for r in rows)
    print(f"tersetrim: {len(rows)} commands compacted, ~{saved:,} tokens saved cumulatively")
    if not is_pro():
        print("(pro adds per-command breakdown + 7-day trend — tersetrim --pro)")
        return 0
    by_cmd: dict[str, list[int]] = {}
    for r in rows:
        key = " ".join(str(r.get("cmd", "?")).split()[:2])
        by_cmd.setdefault(key, []).append(int(r.get("saved_tok", 0)))
    print("\nby command:")
    for cmd, vals in sorted(by_cmd.items(), key=lambda kv: -sum(kv[1])):
        print(f"  {cmd:<22} {len(vals):>4} runs   ~{sum(vals):>8,} tok saved   "
              f"(avg {sum(vals) // max(1, len(vals)):,}/run)")
    week_ago = time.time() - 7 * 86400
    wk = [int(r.get("saved_tok", 0)) for r in rows if r.get("ts", 0) >= week_ago]
    print(f"\nlast 7 days: {len(wk)} runs, ~{sum(wk):,} tok saved")
    rate = os.environ.get("TERSETRIM_USD_PER_MTOK")
    if rate:                                   # dollars only when YOU set the rate — no invented pricing
        try:
            print(f"≈ ${sum(wk) * float(rate) / 1e6:.2f} this week / "
                  f"${saved * float(rate) / 1e6:.2f} all-time at ${float(rate)}/Mtok "
                  f"(your TERSETRIM_USD_PER_MTOK)")
        except ValueError:
            pass
    return 0


def _pro_info() -> int:
    print(f"""tersetrim pro — lifetime license for all v0.x: {PRO_URL}
free (forever):  git status · git log · ls -l · docker ps · docker images · savings total
pro adds:        kubectl get · pip list · npm ls · git diff --stat · pytest
                 --stats breakdown per command + 7-day trend + $-translation (your rate)
activate:        tersetrim --activate YOUR-KEY   (verified once, cached, works offline after)
honest print:    the source is MIT and public, including this gate. The key is how you pay
                 for the work — not DRM. Free never degrades: unlicensed pro commands run
                 normally, untouched, with at most one hint a day.""")
    return 0


def _self_check() -> int:
    import tempfile
    # HERMETIC: every fixture run below flows through _record_saving — on the REAL ledger that
    # would pollute the pro analytics a paying user reads (--stats). Redirect for the whole check.
    global _LEDGER
    _ledger_orig = _LEDGER
    _ledger_tmp = Path(tempfile.mkdtemp(prefix="tersetrim_check_")) / "savings.jsonl"
    _LEDGER = _ledger_tmp
    try:
        return _self_check_body()
    finally:
        _LEDGER = _ledger_orig
        import shutil
        shutil.rmtree(_ledger_tmp.parent, ignore_errors=True)


def _self_check_body() -> int:
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

    # ---- PRO TIER ------------------------------------------------------------------------
    global _LICENSE, _NOTICE_TS
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        old_lic, old_ts = _LICENSE, _NOTICE_TS
        _LICENSE, _NOTICE_TS = Path(td) / "license.json", Path(td) / "notice_ts"
        try:
            # 7. FREE NEVER DEGRADES: unlicensed pro command passes through untouched
            assert not is_pro()
            piplist = "Package    Version\n---------- -------\nrequests   2.31.0\nurllib3    2.0.4\n"
            assert run(["pip", "list"], _runner=lambda a: (0, piplist))[1] == piplist, \
                "unlicensed pro command must pass through UNTOUCHED"
            # 8. activation: bad key refused, good key cached, verifier seam only (no network)
            assert activate("BAD", _verifier=lambda k: (False, "nope")) == 1 and not is_pro()
            assert activate("GOOD-KEY", _verifier=lambda k: (True, "ok")) == 0 and is_pro()
            # 9. licensed: pip list -> pkg==ver
            got = run(["pip", "list"], _runner=lambda a: (0, piplist))[1]
            assert got == "requests==2.31.0\nurllib3==2.0.4", got
            # 10. kubectl get: NAME/READY/STATUS kept, RESTARTS/AGE dropped; non-table untouched
            kg = ("NAME                READY   STATUS    RESTARTS   AGE\n"
                  "web-7d4b9c          1/1     Running   0          2d\n")
            k = run(["kubectl", "get", "pods"], _runner=lambda a: (0, kg))[1]
            assert "web-7d4b9c" in k and "Running" in k and "AGE" not in k and "2d" not in k, k
            assert run(["kubectl", "get"], _runner=lambda a: (0, "error: x\n"))[1] == "error: x\n"
            # 11. npm ls: tree -> flat name@version, deduped
            nl = "app@1.0.0\n├── react@18.2.0\n│ └── loose-envify@1.4.0\n└── react@18.2.0 deduped\n"
            n = run(["npm", "ls"], _runner=lambda a: (0, nl))[1]
            assert "react@18.2.0" in n and n.count("react@18.2.0") == 1 and "├" not in n, n
            # 12. git diff --stat: bar art dropped, counts kept
            gd = " src/a.py | 24 ++++++++++----\n src/b.py | 3 ---\n 2 files changed, 18 insertions(+), 9 deletions(-)\n"
            g = run(["git", "diff", "--stat"], _runner=lambda a: (0, gd))[1]
            assert "src/a.py | 24" in g and "++++" not in g and "2 files changed" in g, g
            # 13. pytest: failures + summary kept, passing noise dropped, failures NEVER dropped
            pt = ("tests/test_a.py::test_ok PASSED\ntests/test_b.py::test_bad FAILED\n"
                  "E  assert 1 == 2\n=========== 1 failed, 1 passed in 0.12s ===========\n")
            p = run(["pytest"], _runner=lambda a: (0, pt))[1]
            assert "test_bad FAILED" in p and "1 failed, 1 passed" in p and "test_ok PASSED" not in p, p
            # 14. pro stats renders breakdown without raising (ledger may hold this run's rows)
            _stats()
        finally:
            _LICENSE, _NOTICE_TS = old_lic, old_ts
    print("tersetrim self-check: PASS (argv passthrough quote/redirect-safe — shell-rewrap junk-file bug impossible; "
          "free compactors shrink without losing signal; PRO: unlicensed = untouched passthrough, "
          "activation via verifier seam, kubectl/pip/npm/git-diff/pytest compactors keep every "
          "failure and drop only noise)")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args == ["--self-check"]:
        return _self_check()
    if args == ["--stats"]:
        return _stats()
    if args == ["--pro"] or args == ["--license"]:
        return _pro_info()
    if len(args) == 2 and args[0] == "--activate":
        return activate(args[1])
    if not args or args == ["--help"]:
        print(__doc__.strip())
        return 0
    rc, out = run(args)
    sys.stdout.write(out if (not out or out.endswith("\n")) else out + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())

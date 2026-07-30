#!/usr/bin/env python3
"""
Inventory every launchd job on this Mac and render jobs.html.

  python3 scripts/jobs.py                  # write jobs.html
  python3 scripts/jobs.py --text           # same inventory, as a terminal table
  python3 scripts/jobs.py --include-apple  # also list Apple's own system jobs
  python3 scripts/jobs.py --run LABEL      # start a job now, watch it, show its output
  python3 scripts/jobs.py --remove LABEL   # unload a job, delete its plist, rewrite the page

Why this exists: `launchctl list` prints several hundred labels, almost all of
which are on-demand helpers that only wake when something asks for them. That
makes it useless for the question you actually have -- "what runs on my machine
without me asking, and is it still working?" -- so this filters by the keys that
create a timer and reports liveness next to each one.

Liveness is inferred from two independent signals, because a schedule that has
quietly died looks identical to a healthy one in `launchctl list`:

  * last exit code, from launchctl
  * the mtime of the job's own log, when it declares one -- if a job that runs
    daily has not written anything in three days, it is flagged stale

Apple's ~700 built-in jobs are excluded by default as noise, not because they
are uninteresting; --include-apple adds them and the page says which it dropped.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "scripts" / "jobs.template.html"
OUT = REPO / "jobs.html"
HOME = Path.home()

# (directory, group key, human title, subtitle)
SOURCES = [
    (HOME / "Library/LaunchAgents", "user", "Your user agents",
     "~/Library/LaunchAgents — run when you are logged in"),
    (Path("/Library/LaunchAgents"), "global", "Installed for all users",
     "/Library/LaunchAgents — third-party installers put things here"),
    (Path("/Library/LaunchDaemons"), "daemon", "System daemons",
     "/Library/LaunchDaemons — run as root, from boot, with no login needed"),
]
DAY = 86400

# The job that regenerates this page. Named in the staleness banner, because a
# stale page is the only outward sign that this particular job has died.
SELF_LABEL = "com.jasperlouage.ai-agent-usage.refresh"

# Caps on what gets inlined per job. These bound the page size and are always
# reported in the UI when they bite -- a silent truncation would read as "this is
# the whole script", which is worse than showing nothing.
MAX_TEXT_BYTES = 12_288
MAX_TEXT_LINES = 240
READ_CEILING = 262_144           # never slurp more than this to count lines

# How long `--run` watches a job it started before handing it back to launchd.
RUN_WAIT = 120

# Argv[0] values that mean "the real payload is a later argument".
INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "python", "python2", "python3",
                "perl", "ruby", "node", "osascript", "env", "swift"}


# --------------------------------------------------------------------------- #
# launchd state
# --------------------------------------------------------------------------- #

def launchctl_state() -> dict[str, dict]:
    """label -> {running, last_exit} from `launchctl list`.

    Output is "PID\tStatus\tLabel"; a '-' PID means loaded but not running, and
    Status is the last exit code (negative means it was killed by that signal).
    """
    try:
        out = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return {}

    state: dict[str, dict] = {}
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, status, label = parts[0], parts[1], parts[2].strip()
        state[label] = {
            "running": pid.strip() not in ("-", ""),
            "last_exit": int(status) if re.fullmatch(r"-?\d+", status.strip()) else None,
        }
    return state


# --------------------------------------------------------------------------- #
# triggers
# --------------------------------------------------------------------------- #

def _cal_text(cal: dict) -> str:
    """One StartCalendarInterval dict -> English. Absent keys are wildcards."""
    days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    hour, minute = cal.get("Hour"), cal.get("Minute")
    when = f"{hour:02d}:{minute:02d}" if hour is not None and minute is not None else (
        f"{hour:02d}:00" if hour is not None else
        f":{minute:02d} past the hour" if minute is not None else "every minute")

    if "Weekday" in cal:
        return f"{days[cal['Weekday'] % 7]}s at {when}"
    if "Day" in cal:
        return f"day {cal['Day']} at {when}"
    if hour is not None:
        return f"daily at {when}"
    return when


def _interval_text(secs: int) -> str:
    if secs % 3600 == 0 and secs >= 3600:
        h = secs // 3600
        return f"every {h}h" if h > 1 else "hourly"
    if secs % 60 == 0 and secs >= 60:
        return f"every {secs // 60}m"
    return f"every {secs}s"


def describe(pl: dict) -> tuple[str, bool, int | None]:
    """-> (trigger text, is_on_a_timer, approximate period in seconds)."""
    cal = pl.get("StartCalendarInterval")
    if cal:
        entries = cal if isinstance(cal, list) else [cal]
        texts = [_cal_text(c) for c in entries if isinstance(c, dict)]
        # Period drives the staleness check; assume daily unless it repeats more often.
        period = DAY if len(texts) == 1 else max(DAY // max(len(texts), 1), 3600)
        return ("; ".join(texts) or "calendar", True, period)

    iv = pl.get("StartInterval")
    if isinstance(iv, int) and iv > 0:
        return (_interval_text(iv), True, iv)

    if pl.get("WatchPaths"):
        return ("when files change", False, None)
    if pl.get("QueueDirectories"):
        return ("when a queue fills", False, None)
    if pl.get("KeepAlive"):
        return ("kept alive", False, None)
    if pl.get("RunAtLoad"):
        return ("at login / boot", False, None)
    return ("on demand", False, None)


def next_run(pl: dict, now: datetime) -> datetime | None:
    """Next fire time for calendar jobs. Interval jobs depend on load time, so
    launchd alone knows those -- we return None rather than guess."""
    cal = pl.get("StartCalendarInterval")
    if not cal:
        return None
    entries = [c for c in (cal if isinstance(cal, list) else [cal]) if isinstance(c, dict)]

    best = None
    for c in entries:
        # An absent field is a wildcard, so "Minute: 15" alone fires every hour.
        hours = [c["Hour"]] if "Hour" in c else list(range(24))
        minutes = [c["Minute"]] if "Minute" in c else list(range(60))
        found = None
        for delta in range(0, 400):                      # ~13 months is enough
            day = (now + timedelta(days=delta)).date()
            # launchd counts Sunday as 0 or 7; isoweekday() counts it as 7.
            if "Weekday" in c and day.isoweekday() % 7 != c["Weekday"] % 7:
                continue
            if "Day" in c and day.day != c["Day"]:
                continue
            if "Month" in c and day.month != c["Month"]:
                continue
            for h in hours:
                for m in minutes:
                    cand = datetime(day.year, day.month, day.day, h, m, tzinfo=now.tzinfo)
                    if cand > now:
                        found = cand
                        break
                if found:
                    break
            if found:
                break
        if found and (best is None or found < best):
            best = found
    return best


# --------------------------------------------------------------------------- #

def human_ago(ts: float, now: float) -> str:
    d = now - ts
    if d < 90:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)}m ago"
    if d < 2 * DAY:
        return f"{int(d // 3600)}h ago"
    return f"{int(d // DAY)}d ago"


def plain(value):
    """plistlib types -> JSON-safe, without pretending binary blobs are strings."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes of binary data>"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def script_target(args: list[str]) -> str | None:
    """The script an interpreter was pointed at, e.g. /bin/sh -> refresh.sh.

    Without this the detail view of every wrapper job would just say "/bin/sh",
    which is exactly the dead end that makes these plists opaque.
    """
    if not args:
        return None
    if Path(str(args[0])).name not in INTERPRETERS:
        return None
    for a in args[1:]:
        a = str(a)
        if a.startswith("-"):
            continue
        if Path(a).is_file():
            return a
    return None


_sig_cache: dict[str, str] = {}


def signature(path: str) -> str:
    """Who signed the binary. The honest answer to "is this really Adobe?"."""
    if path in _sig_cache:
        return _sig_cache[path]
    result = "unknown"
    try:
        r = subprocess.run(["/usr/bin/codesign", "-dv", "--verbose=2", path],
                           capture_output=True, text=True, timeout=10)
        out = r.stderr or ""
        auth = team = None
        for line in out.splitlines():
            if line.startswith("Authority=") and auth is None:
                auth = line.split("=", 1)[1].strip()
            elif line.startswith("TeamIdentifier="):
                team = line.split("=", 1)[1].strip()
        if auth:
            result = f"{auth}" + (f" · team {team}" if team and team != "not set" else "")
        elif "not signed" in out or "code object is not signed" in out:
            result = "not signed"
    except (OSError, subprocess.SubprocessError):
        pass
    _sig_cache[path] = result
    return result


def inspect_file(path_str: str, want_signature: bool) -> dict:
    """What is this file, and if it is readable text, what does it say?"""
    p = Path(path_str)
    info: dict = {"path": path_str}
    try:
        st = p.stat()
    except OSError:
        info["missing"] = True
        return info
    info["bytes"] = st.st_size

    try:
        with open(p, "rb") as fh:
            raw = fh.read(min(st.st_size, READ_CEILING) or 1)
    except OSError:
        info["unreadable"] = True
        return info

    if b"\x00" in raw[:8192]:
        info["kind"] = "binary"
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            info["kind"] = "binary"
        else:
            info["kind"] = "text"
            lines = text.splitlines()
            shown, notes = lines, []
            if len(lines) > MAX_TEXT_LINES:
                shown = lines[:MAX_TEXT_LINES]
                notes.append(f"first {MAX_TEXT_LINES} of "
                             f"{len(lines)}{'+' if st.st_size > READ_CEILING else ''} lines")
            body = "\n".join(shown)
            if len(body.encode("utf-8")) > MAX_TEXT_BYTES:
                body = body.encode("utf-8")[:MAX_TEXT_BYTES].decode("utf-8", "ignore")
                notes.append(f"first {MAX_TEXT_BYTES // 1024} KB of {st.st_size // 1024 or 1} KB")
            info["text"] = body
            if notes:
                info["truncated"] = "showing " + ", and ".join(notes)

    if want_signature and info.get("kind") == "binary":
        info["signature"] = signature(path_str)
    return info


def command_of(pl: dict) -> str:
    args = pl.get("ProgramArguments")
    if isinstance(args, list) and args:
        return " ".join(str(a) for a in args)
    return str(pl.get("Program") or "—")


def collect(include_apple: bool) -> tuple[list[dict], int]:
    state = launchctl_state()
    now_dt = datetime.now().astimezone()
    now = now_dt.timestamp()
    jobs, skipped = [], 0

    for directory, group, _, _ in SOURCES:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.plist")):
            try:
                with open(path, "rb") as fh:
                    pl = plistlib.load(fh)
            except Exception:
                continue
            label = str(pl.get("Label") or path.stem)
            if label.startswith("com.apple.") and not include_apple:
                skipped += 1
                continue

            trigger, timed, period = describe(pl)
            cmd = command_of(pl)
            st = state.get(label, {})
            loaded = label in state

            log = pl.get("StandardOutPath") or pl.get("StandardErrorPath")
            last_output, stale = None, False
            if log:
                try:
                    mtime = os.stat(log).st_mtime
                    last_output = human_ago(mtime, now)
                    if timed and period:
                        stale = (now - mtime) > max(3 * period, 3 * DAY)
                except OSError:
                    last_output = "no log yet"

            nxt = next_run(pl, now_dt)
            mine = str(REPO) in cmd

            uninstall = (f"/usr/bin/python3 {REPO / 'scripts' / 'jobs.py'} "
                         f"--remove {label}")
            run_cmd = (f"/usr/bin/python3 {REPO / 'scripts' / 'jobs.py'} "
                       f"--run {label}")

            # What the job actually is: its own declaration, the executable it
            # invokes, and -- when that executable is only a wrapper -- the script
            # holding the real logic.
            argv = pl.get("ProgramArguments")
            argv = [str(a) for a in argv] if isinstance(argv, list) else \
                   ([str(pl["Program"])] if pl.get("Program") else [])
            files = []
            if argv:
                files.append(inspect_file(argv[0], want_signature=True))
                target = script_target(argv)
                if target:
                    files.append(inspect_file(target, want_signature=False))

            jobs.append({
                "label": label, "path": str(path).replace(str(HOME), "~"),
                "group": group, "trigger": trigger, "timed": timed,
                "next": nxt.strftime("%a %d %b, %H:%M") if nxt else None,
                "last_output": last_output, "stale": stale,
                "loaded": loaded, "running": bool(st.get("running")),
                "last_exit": st.get("last_exit"),
                # System daemons live in the root domain, which `launchctl list`
                # cannot see without sudo. Absence there says nothing about them,
                # so don't let it be reported as "not loaded".
                "status_unknown": group == "daemon" and not loaded,
                "command": cmd, "mine": mine,
                "uninstall": uninstall, "run": run_cmd,
                "plist": json.dumps(plain(pl), indent=1, sort_keys=True),
                "plist_path": str(path),
                "files": files,
                "log": log or None,
            })

    # Yours first, then anything unhealthy, then alphabetical.
    jobs.sort(key=lambda j: (not j["mine"], not (j["stale"] or (j["last_exit"] or 0) > 0),
                             j["label"].lower()))
    return jobs, skipped


# --------------------------------------------------------------------------- #
# acting on one job
# --------------------------------------------------------------------------- #

def find_job(label: str) -> tuple[Path, dict, str] | None:
    """-> (plist path, parsed plist, group key) for `label`, searched in the same
    order the page lists them."""
    for directory, group, _, _ in SOURCES:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.plist")):
            try:
                with open(path, "rb") as fh:
                    pl = plistlib.load(fh)
            except Exception:
                continue
            if str(pl.get("Label") or path.stem) == label:
                return path, pl, group
    return None


def unload_and_delete(label: str, assume_yes: bool) -> int:
    """Actually remove a job: unload it, then delete its plist.

    Two details that a hand-written one-liner gets wrong, and did:

      * /Library/LaunchAgents and /Library/LaunchDaemons are root:wheel, so
        deleting from them needs sudo even though unloading a *user* agent does
        not. Mixing the two in one command silently half-works.
      * The steps must not be chained with &&. Once a job is unloaded, bootout
        exits non-zero, which would stop the delete from ever running -- so the
        job appears stuck as "not loaded" no matter how often you retry.
    """
    found = find_job(label)
    if not found:
        print(f"  no job found with label {label}")
        return 1
    path, _pl, group = found

    if label.startswith("com.apple."):
        print(f"  refusing to remove an Apple system job: {label}")
        return 1

    uid = os.getuid()
    domain = f"system/{label}" if group == "daemon" else f"gui/{uid}/{label}"
    root_owned = HOME not in path.parents
    boot = (["sudo"] if group == "daemon" else []) + \
           ["/bin/launchctl", "bootout", domain]
    delete = (["sudo"] if root_owned else []) + ["/bin/rm", str(path)]

    print(f"\n  {label}")
    print(f"    unload:  {' '.join(boot)}")
    print(f"    delete:  {' '.join(delete)}")
    if root_owned:
        print("    (sudo required: this plist is owned by root)")
    if not assume_yes:
        try:
            if input("\n  Remove it? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  left alone")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\n  left alone")
            return 1

    # Output is deliberately not captured: sudo writes its password prompt to
    # stderr, and swallowing that would leave the user staring at a hung command
    # with no idea it wants a password.
    #
    # Flushing matters for the same reason. Our stdout is block-buffered when
    # piped, while the child writes straight to the terminal, so without this
    # launchctl's errors surface before the plan they belong to.
    print()
    sys.stdout.flush()
    # Unloading may legitimately fail when the job is already unloaded; that must
    # not stop the delete, which is the part that makes it stick.
    if subprocess.run(boot).returncode == 0:
        print("  unloaded")
    else:
        print("  not unloaded (already inactive?) — continuing to the delete")

    sys.stdout.flush()
    if subprocess.run(delete).returncode != 0:
        print("  FAILED to delete the plist — the job will load again at "
              "next login while the file exists")
        return 1
    print(f"  deleted {path}")
    return 0


def service_state(label: str, use_sudo: bool):
    """-> {"pid", "last_exit"} | None if not loaded | "unknown" if we can't tell.

    "Unknown" is a real third answer and must not collapse into "not loaded":
    root-domain daemons are invisible to a plain `launchctl list`, and the sudo
    that would reveal them only works while its credentials are still cached.
    """
    argv = (["/usr/bin/sudo", "-n"] if use_sudo else []) + \
           ["/bin/launchctl", "list", label]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if r.returncode != 0:
        return "unknown" if use_sudo else None
    pid = re.search(r'"PID"\s*=\s*(\d+)', r.stdout)
    exit_code = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', r.stdout)
    return {"pid": int(pid.group(1)) if pid else None,
            "last_exit": int(exit_code.group(1)) if exit_code else None}


def watch(label: str, daemon: bool, prev_exit: int | None) -> int:
    """Poll until the job stops running, then report how it ended.

    kickstart returns as soon as the job is launched, so without this a run
    reports success before the job has done anything -- and a job that fails in
    two seconds would look identical to one that worked.

    `prev_exit` guards the other end: for the moment between a job exiting and
    launchd recording that, `launchctl list` still shows the *previous* run's
    status. Reporting that as this run's result is worse than waiting, so an
    unchanged code is not believed until the job has been seen running or the
    grace period is up.
    """
    start = time.monotonic()
    deadline, grace = start + RUN_WAIT, start + 2.0
    announced = False
    while True:
        st = service_state(label, use_sudo=daemon)
        if not isinstance(st, dict):
            print("  started — launchctl will not report a root-domain job's "
                  "progress without sudo, so its log below is the only witness")
            return 0

        if st.get("pid"):
            if not announced:
                print(f"  running (pid {st['pid']})…")
                announced = True
        else:
            code = st.get("last_exit")
            if announced or code != prev_exit or time.monotonic() > grace:
                if code is None:
                    print("  finished — launchctl reported no exit code")
                    return 0
                print(f"  finished — exit {code}")
                return 0 if code == 0 else 1

        if time.monotonic() > deadline:
            print(f"  still running after {RUN_WAIT}s — left to finish in the "
                  f"background; check its log for the rest")
            return 0
        time.sleep(0.4)


def tail_new_log(log: str | None, offset: int, max_lines: int = 40) -> None:
    """Show only what this run appended, from a size recorded before it started.

    The whole point of a manual run is seeing what happened; a job's log is
    usually months long, so the tail of the file is not the same question.
    """
    if not log:
        print("  it declares no log, so there is nothing more to show")
        return
    try:
        size = os.stat(log).st_size
    except OSError:
        print(f"  it wrote no log ({log} does not exist)")
        return
    if size == offset:
        print("  it wrote nothing to its log")
        return

    # A file that shrank was rotated or truncated mid-run, so the old offset
    # points past the end of what is now a different file: start over from 0.
    with open(log, "rb") as fh:
        fh.seek(offset if size > offset else 0)
        raw = fh.read(READ_CEILING)
    lines = raw.decode("utf-8", "replace").strip("\n").splitlines()

    print(f"\n  {log}")
    if len(lines) > max_lines:
        print(f"  … showing the last {max_lines} of {len(lines)} new lines")
        lines = lines[-max_lines:]
    for line in lines:
        print("  | " + line)


def run_now(label: str) -> int:
    """Fire a job now, on its own schedule's terms, and show what it did."""
    found = find_job(label)
    if not found:
        print(f"  no job found with label {label}")
        return 1
    path, pl, group = found

    uid = os.getuid()
    daemon = group == "daemon"
    target = f"system/{label}" if daemon else f"gui/{uid}/{label}"
    kick = (["sudo"] if daemon else []) + \
           ["/bin/launchctl", "kickstart", "-p", target]

    state = service_state(label, use_sudo=daemon)
    if state is None:
        # kickstart only starts a job launchd already knows about, so this is a
        # different problem with a different fix -- say so instead of failing.
        boot = ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(path)]
        print(f"\n  {label} is not loaded, so there is nothing to start.")
        print(f"    load it first:  {' '.join(boot)}")
        return 1
    if isinstance(state, dict) and state.get("pid"):
        print(f"\n  {label} is already running (pid {state['pid']}) — left alone.")
        return 0

    # Recorded before the run so the output below is this run's, not the file's.
    log = pl.get("StandardOutPath") or pl.get("StandardErrorPath")
    try:
        offset = os.stat(log).st_size if log else 0
    except OSError:
        offset = 0

    print(f"\n  {label}")
    print(f"    start:  {' '.join(kick)}")
    if daemon:
        print("    (sudo required: this job runs in the root domain)")
    print()
    sys.stdout.flush()

    # Uncaptured for the same reason removal is: sudo's password prompt and
    # launchctl's own errors are the output that matters when this goes wrong.
    if subprocess.run(kick).returncode != 0:
        print("  launchctl refused to start it — its message is above")
        return 1

    rc = watch(label, daemon, state.get("last_exit"))
    tail_new_log(log, offset)
    return rc


# --------------------------------------------------------------------------- #

def render_text(jobs: list[dict]) -> None:
    timed = [j for j in jobs if j["timed"]]
    print(f"\n  {len(timed)} job(s) on a timer, {len(jobs) - len(timed)} on demand\n")
    for j in timed:
        flag = ("!" if j["stale"] or (j["last_exit"] or 0) > 0 else
                "*" if j["mine"] else " ")
        print(f"  {flag} {j['label'][:52]:<52} {j['trigger'][:22]:<22} "
              f"{(j['last_output'] or '—'):>9}")
    print("\n  * yours   ! needs a look\n")


def render_html(jobs: list[dict], skipped: int) -> None:
    if not TEMPLATE.exists():
        sys.exit(f"Missing template: {TEMPLATE}")
    tpl = TEMPLATE.read_text()
    marker = "/*__JOBS_DATA__*/"
    if marker not in tpl:
        sys.exit(f"Template has no {marker} placeholder")

    note = ("Triggers come from each job's plist; status and last exit code from "
            "launchctl; “last output” is the mtime of the log the job declares, "
            "which is the only honest way to tell a working timer from a dead one. "
            "A job is flagged stale when it has not written to its log in three "
            "times its own period. System daemons show “unknown” because they run "
            "in the root domain, which launchctl will not report on without sudo — "
            "that is a gap in what this page can see, not a dormant job.")
    if skipped:
        note += f" {skipped} Apple system jobs are hidden — rerun with --include-apple to list them."

    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "self_label": SELF_LABEL,
        "jobs": jobs,
        "groups": [{"key": k, "title": t, "sub": s} for _, k, t, s in SOURCES],
        "note": note,
    }
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.replace(marker, "window.JOBS_DATA=" + blob + ";")
    html = html.replace("<!-- SOURCE TEMPLATE. Rendered to ../jobs.html by scripts/jobs.py.\n"
                        "     Edit this file; jobs.html is generated. -->\n", "")
    OUT.write_text(html)
    print(f"  wrote jobs.html  ({OUT.stat().st_size // 1024} KB, self-contained)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", action="store_true", help="print a terminal table instead")
    ap.add_argument("--include-apple", action="store_true",
                    help="also list com.apple.* jobs (hundreds of them)")
    act = ap.add_mutually_exclusive_group()
    act.add_argument("--run", metavar="LABEL",
                     help="start a job now, wait for it, and show what it logged")
    act.add_argument("--remove", metavar="LABEL",
                     help="unload a job and delete its plist, then rewrite jobs.html")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if args.remove or args.run:
        rc = (unload_and_delete(args.remove, args.yes) if args.remove
              else run_now(args.run))
        # Rewrite the page either way, so it reflects reality after a partial
        # failure -- and so a run's new exit code and log time land on it.
        jobs, skipped = collect(args.include_apple)
        render_html(jobs, skipped)
        sys.exit(rc)

    jobs, skipped = collect(args.include_apple)
    if args.text:
        render_text(jobs)
        return
    render_html(jobs, skipped)

    timed = sum(1 for j in jobs if j["timed"])
    bad = [j for j in jobs if j["stale"] or (j["last_exit"] or 0) > 0]
    print(f"\n  {timed} on a timer  |  {sum(1 for j in jobs if j['mine'])} yours  |  "
          f"{len(bad)} needing a look")
    for j in bad:
        why = "stale" if j["stale"] else f"exit {j['last_exit']}"
        print(f"  ! {j['label']}  ({why})")


if __name__ == "__main__":
    main()

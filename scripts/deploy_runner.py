#!/usr/bin/env python3
"""Deploy runner for the 5 tartufi-pazzi agents. Only Leo's Mac runs it.

Watches GitHub main, queues one job per changed agent (newest commit wins),
deploys each job from a pinned git worktree (never the shared working folder),
gates it (compile, unit tests, `city run` smoke), picks the next version from
the live one, retries City Hall's flaky roll-outs, honours the team quota, and
watches front-door agents after release, rolling back to the last certified
version if one goes down.

Usage (from the repo root):
  python3 scripts/deploy_runner.py run [--dry-run] [--hold-lines 300]
  python3 scripts/deploy_runner.py status
  python3 scripts/deploy_runner.py enqueue <agent> [sha]
  python3 scripts/deploy_runner.py approve <agent>

State and log live in .deploy/ (git-ignored): state.json, deploy-log.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEPLOY_DIR = Path(os.environ.get("DEPLOY_DIR", REPO / ".deploy"))
STATE = DEPLOY_DIR / "state.json"
LOG = DEPLOY_DIR / "deploy-log.md"
LOCK = DEPLOY_DIR / "runner.lock"
AGENTS = ("booking_centre", "gp", "family_counseling", "hospital", "specialist")
FRONT_DOORS = {"booking_centre", "gp", "family_counseling"}
CITY_PY = Path.home() / ".local/share/uv/tools/city-cli/bin/python"
SMOKE = {
    "gp": "/need symptom=SYM-PALPITATIONS",
    "booking_centre": "book prescription_number=prescription_number-LOCAL lang=it",
    "family_counseling": "need.request lang=it text=vorrei_una_visita",
    "hospital": "checkin booking_id=bk-local-1",
    "specialist": "checkin booking_id=bk-local-1",
}
POLL_SECONDS = 60
RETRIES = 3
HEALTH_SECONDS = int(os.environ.get("DEPLOY_HEALTH_SECONDS", "150"))


# --- pure helpers (unit-tested) ---------------------------------------------------------------


def bump(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def vkey(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def next_version(*seen: str) -> str:
    """One patch above the highest version seen (live, card, or attempted)."""

    known = [v for v in seen if v and re.fullmatch(r"\d+\.\d+\.\d+", v)]
    return bump(max(known, key=vkey)) if known else "1.0.1"


def changed_agents(paths: list[str]) -> list[str]:
    """Agents whose brain changed. Only asst/<agent>/agent/ counts (cards and Dockerfile do not)."""

    found = []
    for agent in AGENTS:
        prefix = f"asst/{agent}/agent/"
        if any(path.startswith(prefix) for path in paths) and agent not in found:
            found.append(agent)
    return found


def classify(output: str) -> tuple[str, int]:
    """Outcome of one `city deploy` run: (promoted|quota|timeout|failed, seconds to wait)."""

    if "Status promoted" in output:
        return "promoted", 0
    if "QUOTA_EXCEEDED" in output:
        match = re.search(r"try again in (\d+) min", output)
        return "quota", (int(match.group(1)) * 60 + 10) if match else 60
    if "UPSTREAM_TIMEOUT" in output:
        return "timeout", 0
    return "failed", 60


def enqueue(queue: list[dict], agent: str, sha: str, lines: int = 0) -> list[dict]:
    """Newest commit wins: a queued job for the same agent is replaced, keeping its place."""

    for job in queue:
        if job["agent"] == agent and job.get("status") in ("queued", "held"):
            job.update(sha=sha, lines=lines, status="queued")
            return queue
    queue.append({"agent": agent, "sha": sha, "lines": lines, "status": "queued"})
    return queue


# --- io helpers -------------------------------------------------------------------------------


def sh(cmd: list[str], *, cwd: Path = REPO, timeout: int = 120, stdin: str | None = None) -> tuple[int, str]:
    try:
        done = subprocess.run(cmd, cwd=cwd, input=stdin, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        return 124, (err.stdout or "") if isinstance(err.stdout, str) else "timeout"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def log(line: str) -> None:
    DEPLOY_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    with LOG.open("a", encoding="utf-8") as out:
        out.write(f"- {stamp} {line}\n")
    print(f"{stamp} {line}", flush=True)


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"last_seen": "", "queue": [], "certified": {}, "attempted": {}}


def save(state: dict) -> None:
    DEPLOY_DIR.mkdir(exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE)


def live(agent: str, cwd: Path) -> tuple[str, str]:
    code, out = sh(["city", "--json", "status", agent], cwd=cwd, timeout=60)
    try:
        data = json.loads(out[out.index("{"):])
        return str(data.get("version", "")), str(data.get("visibility", ""))
    except ValueError:
        return "", ""


def card_version(card: Path) -> str:
    match = re.search(r"^version:\s*(\S+)", card.read_text(), re.M)
    return match.group(1) if match else ""


def set_card_version(card: Path, version: str) -> None:
    text = card.read_text()
    card.write_text(re.sub(r"^version:.*$", f"version: {version}", text, count=1, flags=re.M))


def worktree(sha: str) -> Path:
    path = DEPLOY_DIR / f"wt-{sha[:10]}"
    if not path.exists():
        code, out = sh(["git", "worktree", "add", "--detach", str(path), sha])
        if code:
            raise RuntimeError(f"worktree failed: {out.strip()[:200]}")
    return path


def drop_worktree(path: Path) -> None:
    sh(["git", "worktree", "remove", "--force", str(path)])


# --- gate, deploy, health ---------------------------------------------------------------------


def gate(agent: str, wt: Path) -> str | None:
    """None if the snapshot is fit to deploy, else the reason."""

    brain = wt / "asst" / agent / "agent" / "agent.py"
    code, out = sh([sys.executable, "-I", "-m", "py_compile", str(brain)], cwd=wt)
    if code:
        return f"compile: {out.strip()[-200:]}"
    for cache in wt.glob("asst/*/agent/__pycache__"):
        sh(["rm", "-rf", str(cache)])
    if (wt / "tests").exists():
        python = str(CITY_PY) if CITY_PY.exists() else sys.executable
        code, out = sh([python, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=wt, timeout=300)
        for cache in wt.rglob("__pycache__"):
            sh(["rm", "-rf", str(cache)])
        if code:
            return f"tests: {out.strip()[-300:]}"
    script = f"citizen\n{SMOKE[agent]}\n/quit\n"
    code, out = sh(["city", "run", agent], cwd=wt / "asst", timeout=120, stdin=script)
    for cache in wt.glob("asst/*/agent/__pycache__"):
        sh(["rm", "-rf", str(cache)])
    if "Traceback" in out or code not in (0,):
        return f"smoke: exit {code} {out.strip()[-300:]}"
    return None


def wait_live(agent: str, target: str, cwd: Path, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if live(agent, cwd)[0] == target:
            return True
        time.sleep(30)
    return False


def health(agent: str, target: str, state: dict, cwd: Path, dry: bool) -> None:
    """Watch a released agent; roll a front door back to its last certified version if it drops."""

    deadline = time.time() + HEALTH_SECONDS
    while time.time() < deadline:
        version, visibility = live(agent, cwd)
        if version and version != target and (version == "1.0.0" or visibility != "visible"):
            log(f"ALERT {agent} dropped to {version or '?'} ({visibility}) after {target}")
            if agent not in FRONT_DOORS:
                return
            previous = [v for v in state["certified"].get(agent, []) if v != target]
            if not previous:
                log(f"no certified version to roll {agent} back to")
                return
            back = max(previous, key=vkey)
            log(f"rolling {agent} back to {back}")
            if not dry:
                code, out = sh(["city", "rollback", agent, "--to", back], cwd=cwd, timeout=600)
                log(f"rollback {agent} -> {back}: {'ok' if code == 0 else out.strip()[-200:]}")
            return
        time.sleep(30)
    log(f"{agent} {target} healthy for {HEALTH_SECONDS // 60} min")


def deploy(job: dict, state: dict, dry: bool) -> None:
    agent, sha = job["agent"], job["sha"]
    wt = worktree(sha)
    asst = wt / "asst"
    log(f"{agent} @ {sha[:7]}: gate")
    reason = gate(agent, wt)
    if reason:
        job["status"] = "rejected"
        log(f"{agent} @ {sha[:7]} REJECTED ({reason.splitlines()[0][:160]})")
        drop_worktree(wt)
        return
    current, _ = live(agent, asst)
    card = asst / agent / "card.yaml"
    target = next_version(current, card_version(card), state["attempted"].get(agent, ""))
    set_card_version(card, target)
    state["attempted"][agent] = target
    save(state)
    failures = 0
    while True:
        log(f"{agent} @ {sha[:7]}: deploy {target} (live {current})")
        if dry:
            outcome, wait, out = "promoted", 0, "dry-run"
        else:
            code, out = sh(["city", "deploy", agent], cwd=asst, timeout=1800)
            outcome, wait = classify(out)
        if outcome == "quota":
            log(f"quota: waiting {wait}s")
            time.sleep(wait)
            continue
        if outcome == "promoted":
            state["certified"].setdefault(agent, []).append(target)
            job["status"] = "live"
            log(f"{agent} {target} PROMOTED (certified) from {sha[:7]}")
            break
        if outcome == "timeout" and wait_live(agent, target, asst, 900):
            job["status"] = "live"
            log(f"{agent} {target} live after CLI timeout (not certified)")
            break
        failures += 1
        last = [line for line in out.splitlines() if "✗" in line][-1:] or [out.strip()[-160:]]
        log(f"{agent} {target} failed ({failures}/{RETRIES}): {last[0][:160]}")
        if failures >= RETRIES:
            job["status"] = "failed"
            break
        time.sleep(wait)
    save(state)
    if job["status"] == "live":
        health(agent, target, state, asst, dry)
    drop_worktree(wt)


# --- commands ---------------------------------------------------------------------------------


def scan(state: dict, hold_lines: int) -> None:
    sh(["git", "fetch", "-q", "origin"])
    _, head = sh(["git", "rev-parse", "origin/main"])
    head = head.strip()
    if not state["last_seen"]:
        state["last_seen"] = head
        log(f"watching from {head[:7]}")
        return
    if head == state["last_seen"]:
        return
    _, names = sh(["git", "diff", "--name-only", state["last_seen"], head])
    for agent in changed_agents(names.split()):
        _, stat = sh(["git", "diff", "--numstat", state["last_seen"], head, "--", f"asst/{agent}/agent/"])
        lines = sum(int(a) + int(b) for a, b, *_ in (row.split("\t") for row in stat.splitlines()) if a.isdigit())
        enqueue(state["queue"], agent, head, lines)
        if hold_lines and lines > hold_lines:
            for job in state["queue"]:
                if job["agent"] == agent and job["status"] == "queued":
                    job["status"] = "held"
            log(f"{agent} @ {head[:7]} HELD ({lines} lines > {hold_lines}): run `approve {agent}`")
        else:
            log(f"{agent} @ {head[:7]} queued ({lines} lines)")
    state["last_seen"] = head


def run(args: argparse.Namespace) -> None:
    DEPLOY_DIR.mkdir(exist_ok=True)
    if LOCK.exists():
        pid = int(LOCK.read_text() or 0)
        try:
            os.kill(pid, 0)
            sys.exit(f"another runner is active (pid {pid})")
        except OSError:
            pass
    LOCK.write_text(str(os.getpid()))
    try:
        while True:
            state = load()
            scan(state, args.hold_lines)
            save(state)
            job = next((j for j in state["queue"] if j["status"] == "queued"), None)
            if job is None:
                time.sleep(POLL_SECONDS)
                continue
            job["status"] = "deploying"
            save(state)
            try:
                deploy(job, state, args.dry_run)
            except Exception as err:  # keep the runner alive; the job is marked failed
                job["status"] = "failed"
                log(f"{job['agent']} error: {err}")
            state["queue"] = [j for j in state["queue"] if j["status"] in ("queued", "held", "deploying")]
            save(state)
    finally:
        LOCK.unlink(missing_ok=True)


def status(_: argparse.Namespace) -> None:
    state = load()
    print(f"last seen: {state['last_seen'][:7] or '-'}")
    for job in state["queue"]:
        print(f"  {job['status']:<9} {job['agent']:<18} {job['sha'][:7]} ({job.get('lines', 0)} lines)")
    for agent in AGENTS:
        version, visibility = live(agent, REPO / "asst")
        certified = ", ".join(state["certified"].get(agent, [])[-3:])
        print(f"{agent:<18} live {version:<7} {visibility:<8} certified [{certified}]")
    if LOG.exists():
        print("--- last log lines:")
        print("".join(LOG.read_text().splitlines(keepends=True)[-8:]), end="")


def cmd_enqueue(args: argparse.Namespace) -> None:
    state = load()
    sha = args.sha
    if not sha:
        sh(["git", "fetch", "-q", "origin"])
        sha = sh(["git", "rev-parse", "origin/main"])[1].strip()
    enqueue(state["queue"], args.agent, sha)
    save(state)
    log(f"{args.agent} @ {sha[:7]} queued by hand")


def approve(args: argparse.Namespace) -> None:
    state = load()
    for job in state["queue"]:
        if job["agent"] == args.agent and job["status"] == "held":
            job["status"] = "queued"
            log(f"{args.agent} approved")
    save(state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.add_argument("--hold-lines", type=int, default=0)
    p_run.set_defaults(fn=run)
    sub.add_parser("status").set_defaults(fn=status)
    p_enq = sub.add_parser("enqueue")
    p_enq.add_argument("agent", choices=AGENTS)
    p_enq.add_argument("sha", nargs="?", default="")
    p_enq.set_defaults(fn=cmd_enqueue)
    p_app = sub.add_parser("approve")
    p_app.add_argument("agent", choices=AGENTS)
    p_app.set_defaults(fn=approve)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

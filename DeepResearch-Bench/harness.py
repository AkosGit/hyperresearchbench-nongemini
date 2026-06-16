#!/usr/bin/env python3
"""DeepResearch-Bench harness for hyperresearch on Claude Code.

Runs the 100 benchmark queries through `claude -p` invoking the
`/hyperresearch` skill on its full tier (16-step pipeline with
adversarial review), captures the resulting research reports, and
writes JSONL ready for RACE/FACT evaluation.

This is the canonical reproduction harness for hyperresearch's
DeepResearch-Bench leaderboard score. To reproduce: run
`bash setup.sh` then `python harness.py` (or `bash run.sh` which
wraps both). Each query runs on Opus 4.7 (orchestrator + critics +
synthesizer + patcher), Sonnet 4.6 (loci-analysts, depth-investigators,
draft sub-orchestrators, source-analyst), and Haiku 4.5 (fetchers).

Usage:
    python harness.py --setup                     # Download benchmark queries
    python harness.py --query 67                  # Single-query smoke test
    python harness.py                             # Full 100-query run (~1.5-2.5h per query)
    python harness.py --resume                    # Resume from last checkpoint
    python harness.py --lang en                   # English queries only (50)
    python harness.py --timeout 10800             # 3-hour per-query cap

Prerequisites (all handled by `bash setup.sh`):
    - Claude Code CLI installed and authenticated (`claude --version`)
    - hyperresearch installed globally (`pip install hyperresearch && hyperresearch install --global`)
    - Benchmark queries downloaded
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Windows cp1252 can't encode Unicode characters that agents emit in progress
# messages. Reconfigure stdout/stderr to UTF-8 with replacement so the
# harness never crashes mid-run on a print statement.
if hasattr(sys.stdout, "reconfigure"):
    # line_buffering=True so progress reaches a redirected log in real time.
    # Block buffering (the default to a pipe/file) is what made a dead run look
    # identical to a busy one — the log simply stopped, with no way to tell why.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
QUERY_FILE = DATA_DIR / "query.jsonl"
QUERY_URL = (
    "https://raw.githubusercontent.com/Ayanami0730/deep_research_bench/main/"
    "data/prompt_data/query.jsonl"
)

DEFAULT_RUNS_DIR = HERE / "runs"
DEFAULT_RESULTS_DIR = HERE / "results"

# Prompt explicitly invokes /hyperresearch on the full tier so step 1's
# tier classifier doesn't downgrade short-prompt queries. Save path is
# the v0.8.5+ vault_tag-suffixed form; the harness reads the most-
# recently-modified file matching `final_report*.md`.
RESEARCH_PROMPT = """\
Use the `/hyperresearch` skill on the FULL tier (the 16-step pipeline with adversarial review) to research this topic and write a comprehensive report with inline citations:

{prompt}

When step 1 (decompose) classifies the query, override `pipeline_tier` to `"full"` regardless of the query length — this is a benchmark run that requires the full pipeline.

Save your final report to `research/notes/final_report_<vault_tag>.md` (relative to the current working directory). The harness reads the most-recently-modified file matching `research/notes/final_report*.md`.
"""


def download_queries() -> None:
    """Pull the upstream query.jsonl (100 PhD-level prompts, 50 zh + 50 en)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if QUERY_FILE.exists():
        print(f"[OK] {QUERY_FILE} already exists ({QUERY_FILE.stat().st_size:,} bytes)")
        return
    print(f"[FETCH] Downloading benchmark queries from {QUERY_URL}")
    urllib.request.urlretrieve(QUERY_URL, QUERY_FILE)
    print(f"[OK] Saved to {QUERY_FILE}")


def load_queries(
    limit: int | None = None,
    lang_filter: str | None = None,
    only_id: int | None = None,
) -> list[dict]:
    """Load benchmark queries from query.jsonl with optional filters."""
    queries: list[dict] = []
    with QUERY_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            q = json.loads(line)
            if lang_filter and q.get("language") != lang_filter:
                continue
            if only_id is not None and int(q.get("id", 0)) != only_id:
                continue
            queries.append(q)
            if limit and len(queries) >= limit:
                break
    return queries


def _setup_run_dir(parent_project: Path, run_dir: Path) -> None:
    """Mirror the parent project's .claude/ and CLAUDE.md into the run dir.

    The harness runs each query in an isolated subdir so per-query state
    (research/, .hyperresearch/, etc.) doesn't pollute the parent. But
    the subdir needs to inherit whatever Claude Code skills + agents +
    hooks the parent project has installed — that's what determines the
    research workflow under test.
    """
    run_dir.mkdir(parents=True, exist_ok=True)

    for name in (".claude", "CLAUDE.md"):
        src = parent_project / name
        dst = run_dir / name
        if not src.exists():
            continue
        if dst.exists():
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)


def _read_report(run_dir: Path) -> str | None:
    """Read the most-recently-modified final_report*.md from research/notes/."""
    notes_dir = run_dir / "research" / "notes"
    if not notes_dir.exists():
        return None
    candidates = sorted(
        notes_dir.glob("final_report*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None
    return candidates[0].read_text(encoding="utf-8")


HEARTBEAT_INTERVAL_S = 20  # how often the heartbeat thread refreshes status + log


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM then SIGKILL the subprocess's whole process group.

    claude spawns subagents/fetchers as child processes; killing only the
    leader on timeout would orphan them. start_new_session=True puts the whole
    tree in its own process group so killpg reaps all of it.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    for _ in range(30):  # ~3s grace before the hammer
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _write_status(run_dir: Path, status: dict) -> None:
    """Atomically write the per-query heartbeat/status JSON.

    An external watcher (or a human) reads harness-status.json to know whether a
    run is alive WITHOUT guessing from vault mtimes: a frozen `heartbeat_iso`
    means the harness process is gone; a frozen `last_event_iso` with a fresh
    `heartbeat_iso` means claude is alive but stalled.
    """
    status_path = run_dir / "harness-status.json"
    tmp = status_path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(status_path)
    except OSError:
        pass


def run_query(
    query: dict,
    run_dir: Path,
    parent_project: Path,
    timeout: int = 3600,
    model: str = "opus",
) -> tuple[str | None, int, int, float]:
    """Run a single benchmark query through `claude -p`, streaming its output.

    Unlike a buffered subprocess.run(), this streams the claude stream-json
    events line-by-line so that:
      * harness-query.log shows real-time, timestamped progress,
      * a heartbeat thread refreshes harness-status.json every
        HEARTBEAT_INTERVAL_S seconds — a stall or death is then visible
        immediately instead of looking identical to a long-running step,
      * the full raw event stream is persisted to claude-stream.jsonl for
        post-hoc debugging + token accounting,
      * a timeout kills the whole claude process group (subagents included)
        and is recorded as a distinct `timeout` state.

    Returns (article, prompt_tokens, completion_tokens, duration_seconds);
    article is None if the agent didn't write a report.
    """
    _setup_run_dir(parent_project, run_dir)

    prompt_text = RESEARCH_PROMPT.format(prompt=query["prompt"])
    cmd = [
        "claude", "-p", prompt_text,
        "--model", model,
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--output-format", "stream-json",
        "--verbose",
    ]

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    qid = int(query.get("id", 0))
    qlog = (run_dir / "harness-query.log").open("a", encoding="utf-8", buffering=1)
    raw = (run_dir / "claude-stream.jsonl").open("a", encoding="utf-8", buffering=1)

    def qprint(msg: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        qlog.write(f"[{ts}] {msg}\n")

    state = {
        "query_id": qid, "state": "starting", "model": model, "pid": None,
        "start_iso": datetime.now(timezone.utc).isoformat(), "start_mono": time.time(),
        "last_event_iso": None, "last_event_type": None, "last_tool": None,
        "events": 0, "tool_calls": 0, "tool_counts": {},
        "prompt_tokens": 0, "completion_tokens": 0, "timeout_s": timeout,
    }
    lock = threading.Lock()
    stop_heartbeat = threading.Event()
    deadline = time.time() + timeout
    timed_out = {"flag": False}

    def snapshot() -> dict:
        with lock:
            s = dict(state)
            s["tool_counts"] = dict(state["tool_counts"])
        s["elapsed_s"] = round(time.time() - state["start_mono"], 1)
        s["heartbeat_iso"] = datetime.now(timezone.utc).isoformat()
        report = _read_report(run_dir)
        s["report_found"] = report is not None
        s["report_chars"] = len(report) if report else 0
        s["tool_counts"] = dict(sorted(s["tool_counts"].items(), key=lambda kv: -kv[1])[:6])
        s.pop("start_mono", None)
        return s

    def heartbeat_loop(proc: subprocess.Popen) -> None:
        # Wake frequently (<=5s) so the timeout is enforced promptly even when a
        # blocked read would otherwise outlive the deadline, but only emit a
        # heartbeat line / status refresh every HEARTBEAT_INTERVAL_S.
        last_hb = 0.0
        tick = min(5.0, HEARTBEAT_INTERVAL_S)
        while not stop_heartbeat.wait(tick):
            now = time.time()
            if now >= deadline and proc.poll() is None:
                timed_out["flag"] = True
                qprint(f"TIMEOUT after {timeout}s — killing claude process group")
                _kill_group(proc)
                return
            if now - last_hb >= HEARTBEAT_INTERVAL_S:
                last_hb = now
                s = snapshot()
                _write_status(run_dir, s)
                qprint(f"HEARTBEAT elapsed={s['elapsed_s']:.0f}s events={s['events']} "
                       f"tools={s['tool_calls']} tok={s['prompt_tokens']}p+{s['completion_tokens']}c "
                       f"last={s['last_event_type']}/{s['last_tool']} "
                       f"report={'Y' if s['report_found'] else 'n'}")

    start = time.time()
    qprint(f"START query {qid} model={model} timeout={timeout}s")
    _write_status(run_dir, snapshot())

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, cwd=str(run_dir), env=env,
            start_new_session=True,  # own process group → clean group-kill on timeout
        )
    except FileNotFoundError:
        qprint("ERROR: `claude` not found on PATH — generation cannot start")
        with lock:
            state["state"] = "error_no_claude"
        _write_status(run_dir, snapshot())
        qlog.close(); raw.close()
        return None, 0, 0, 0.0

    with lock:
        state["pid"], state["state"] = proc.pid, "generating"
    _write_status(run_dir, snapshot())

    hb = threading.Thread(target=heartbeat_loop, args=(proc,), daemon=True)
    hb.start()

    try:
        for line in proc.stdout:
            raw.write(line)
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = rec.get("type")
            with lock:
                state["events"] += 1
                state["last_event_type"] = etype
                state["last_event_iso"] = datetime.now(timezone.utc).isoformat()
                usage = rec.get("usage") or rec.get("message", {}).get("usage")
                if usage:
                    state["prompt_tokens"] += usage.get("input_tokens", 0)
                    state["completion_tokens"] += usage.get("output_tokens", 0)
                if etype == "assistant":
                    for block in rec.get("message", {}).get("content", []) or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            name = block.get("name", "?")
                            state["tool_calls"] += 1
                            state["tool_counts"][name] = state["tool_counts"].get(name, 0) + 1
                            state["last_tool"] = name
                            qprint(f"tool_use: {name}")
            if etype == "result":
                qprint(f"RESULT subtype={rec.get('subtype')} is_error={rec.get('is_error')} "
                       f"cost_usd={rec.get('total_cost_usd')} turns={rec.get('num_turns')}")
    except Exception as e:  # never let a read hiccup mask the run
        qprint(f"stream read error: {type(e).__name__}: {e}")
    finally:
        rc = proc.wait()
        stop_heartbeat.set()
        hb.join(timeout=2)

    duration = time.time() - start
    article = _read_report(run_dir)
    with lock:
        ptok, ctok = state["prompt_tokens"], state["completion_tokens"]

    if timed_out["flag"]:
        final_state = "timeout"
    elif rc is not None and rc < 0:
        final_state = f"killed_signal_{-rc}"
    elif rc not in (0, None):
        final_state = f"exit_{rc}"
    else:
        final_state = "completed" if article else "no_report"

    with lock:
        state["state"], state["returncode"] = final_state, rc
    final = snapshot()
    final["duration_s"] = round(duration, 1)
    _write_status(run_dir, final)
    qprint(f"END state={final_state} rc={rc} duration={duration:.0f}s "
           f"report_chars={final['report_chars']} tok={ptok}p+{ctok}c")
    qlog.close(); raw.close()

    return article, ptok, ctok, duration


def main() -> None:
    parser = argparse.ArgumentParser(
        description="DeepResearch-Bench harness for Claude Code agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--setup", action="store_true", help="Download benchmark queries and exit")
    parser.add_argument("--limit", type=int, help="Run only the first N queries")
    parser.add_argument("--query", type=int, dest="only_id", help="Run only the query with this ID")
    parser.add_argument("--lang", choices=["en", "zh"], help="Run only English or Chinese queries")
    parser.add_argument("--resume", action="store_true", help="Skip queries already present in the output JSONL")
    parser.add_argument("--output", default="claude-research", help="JSONL filename suffix (default: claude-research)")
    parser.add_argument("--model", default="opus", help="Claude model alias (default: opus)")
    parser.add_argument("--timeout", type=int, default=3600, help="Per-query timeout in seconds (default: 3600 = 1hr)")
    parser.add_argument("--runs-dir", default=str(DEFAULT_RUNS_DIR), help="Directory for per-query run subdirs (default: runs/)")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR), help="Directory for JSONL output (default: results/)")
    parser.add_argument("--project-root", default=str(HERE.parent.parent), help="Path to the Claude Code project whose .claude/ + CLAUDE.md the harness should mirror into each run subdir (default: parent of this repo)")

    args = parser.parse_args()

    if args.setup:
        download_queries()
        return

    if not QUERY_FILE.exists():
        print(f"[ERROR] {QUERY_FILE} not found. Run: python harness.py --setup")
        sys.exit(1)

    parent_project = Path(args.project_root).resolve()
    if not (parent_project / ".claude").exists() and not (parent_project / "CLAUDE.md").exists():
        print(
            f"[WARN] {parent_project} has neither .claude/ nor CLAUDE.md. "
            f"The harness will still run but each query will use vanilla Claude Code "
            f"(no project-specific skills/agents/hooks)."
        )

    queries = load_queries(limit=args.limit, lang_filter=args.lang, only_id=args.only_id)
    if not queries:
        print("[ERROR] No queries matched the filters")
        sys.exit(1)

    runs_dir = Path(args.runs_dir)
    results_dir = Path(args.results_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    out_path = results_dir / f"{args.output}.jsonl"

    # Resume support: skip queries whose ID is already in the output JSONL
    completed_ids: set[int] = set()
    if args.resume and out_path.exists():
        with out_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    completed_ids.add(int(json.loads(line)["id"]))
                except (json.JSONDecodeError, KeyError, ValueError):
                    pass
        print(f"[RESUME] {len(completed_ids)} queries already completed in {out_path.name}")

    print(f"[INFO] Project root: {parent_project}")
    print(f"[INFO] Runs dir:     {runs_dir}")
    print(f"[INFO] Results JSON: {out_path}")
    print(f"[INFO] Queries:      {len(queries)} total, {len([q for q in queries if int(q.get('id', 0)) not in completed_ids])} pending")
    print()

    pending_queries = [q for q in queries if int(q.get("id", 0)) not in completed_ids]
    for i, q in enumerate(pending_queries, 1):
        qid = int(q["id"])
        prompt_preview = q["prompt"][:100].replace("\n", " ")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        print(f"=== [{i}/{len(pending_queries)}] Query {qid} ({q.get('language')}) @ {ts} ===")
        print(f"    {prompt_preview}...")

        run_dir = runs_dir / f"query_{qid}"
        try:
            article, ptok, ctok, dur = run_query(
                q, run_dir, parent_project,
                timeout=args.timeout, model=args.model,
            )
        except KeyboardInterrupt:
            print("\n[ABORT] Interrupted by user. Resume later with --resume")
            sys.exit(1)
        except Exception as e:
            print(f"[ERROR] Query {qid} crashed: {type(e).__name__}: {e}")
            continue

        if not article:
            # Surface the distinct failure mode the streaming run recorded
            # (timeout / killed_signal_N / exit_N / no_report) rather than a
            # generic "no report", and point at the per-query diagnostics.
            final_state = "unknown"
            try:
                st = json.loads((run_dir / "harness-status.json").read_text(encoding="utf-8"))
                final_state = st.get("state", "unknown")
            except (OSError, json.JSONDecodeError):
                pass
            print(f"[FAIL] Query {qid}: no report (state={final_state}, duration={dur:.0f}s). "
                  f"See {run_dir}/harness-query.log and harness-status.json")
            continue

        result = {
            "id": qid,
            "language": q.get("language"),
            "prompt": q["prompt"],
            "article": article,
            "model": args.model,
            "prompt_tokens": ptok,
            "completion_tokens": ctok,
            "duration_seconds": dur,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        with out_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")

        print(f"[OK] Query {qid}: {len(article):,} chars, {dur/60:.1f}min, "
              f"{ptok:,}p+{ctok:,}c tok")
        print()

    print(f"[DONE] Results: {out_path}")
    if out_path.exists():
        print(f"       Total: {sum(1 for _ in out_path.open(encoding='utf-8'))} entries")
    else:
        # No query produced a report (e.g. all timed out) so the JSONL was never
        # created. Exit cleanly with a clear note instead of crashing on open() —
        # a crash here would abort the whole compare.sh run under `set -e`.
        print("       Total: 0 entries — no reports produced "
              "(check each runs/*/query_*/harness-status.json for the failure state)")


if __name__ == "__main__":
    main()

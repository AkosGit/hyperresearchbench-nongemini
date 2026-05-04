#!/usr/bin/env python3
"""DeepResearch-Bench harness for Claude Code agents.

Runs the 100 benchmark queries through `claude -p` (Claude Code CLI in
non-interactive mode), captures the resulting research reports, and writes
JSONL ready for RACE/FACT evaluation.

Agent-agnostic. Whatever your Claude Code project has installed (a custom
research skill, hyperresearch, vanilla Claude Code with WebSearch/WebFetch,
etc.), the harness just passes the query, waits for `research/notes/final_report*.md`
to land, and records what came back. The harness does NOT inject any
particular research workflow — that's controlled by your project's
`.claude/`, `CLAUDE.md`, and `.claude/skills/` setup.

Usage:
    python harness.py --setup                     # Download benchmark queries
    python harness.py --limit 1                   # Single-query smoke test
    python harness.py --limit 1 --query 67        # Run a specific query ID
    python harness.py                             # Full 100-query run
    python harness.py --resume                    # Resume from last checkpoint
    python harness.py --lang en                   # English queries only (50)
    python harness.py --model sonnet              # Use Sonnet instead of Opus
    python harness.py --timeout 1800              # 30-min per-query cap

Prerequisites:
    - Claude Code CLI installed and authenticated (`claude --version`)
    - Whatever research workflow you want to test, configured in your
      project (the harness runs `claude -p` in a fresh per-query subdir
      that inherits the parent project's .claude/ + CLAUDE.md by default)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Windows cp1252 can't encode Unicode characters that agents emit in progress
# messages. Reconfigure stdout/stderr to UTF-8 with replacement so the
# harness never crashes mid-run on a print statement.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
QUERY_FILE = DATA_DIR / "query.jsonl"
QUERY_URL = (
    "https://raw.githubusercontent.com/Ayanami0730/deep_research_bench/main/"
    "data/prompt_data/query.jsonl"
)

DEFAULT_RUNS_DIR = HERE / "runs"
DEFAULT_RESULTS_DIR = HERE / "results"

# Generic prompt: ask for a comprehensive report with inline URL citations
# and a stable output path. The agent's actual research methodology comes
# from whatever skill the user has installed — not from this prompt.
RESEARCH_PROMPT = """\
Research this topic and write a comprehensive report with inline URL citations: {prompt}

Save your final report to research/notes/final_report.md (or research/notes/final_report_<slug>.md, relative to the current working directory). The harness reads the most-recently-modified file matching that glob.
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


def run_query(
    query: dict,
    run_dir: Path,
    parent_project: Path,
    timeout: int = 3600,
    model: str = "opus",
) -> tuple[str | None, int, int, float]:
    """Run a single benchmark query through `claude -p`.

    Returns:
        (article, prompt_tokens, completion_tokens, duration_seconds)
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

    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True,
            timeout=timeout,
            cwd=str(run_dir),
            env=env,
        )
    except subprocess.TimeoutExpired:
        duration = time.time() - start
        # Even on timeout, the agent may have written the report before timing out
        article = _read_report(run_dir)
        return article, 0, 0, duration

    duration = time.time() - start
    article = _read_report(run_dir)

    # Best-effort token accounting from stream-json
    prompt_tokens = completion_tokens = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = rec.get("usage") or rec.get("message", {}).get("usage")
        if usage:
            prompt_tokens += usage.get("input_tokens", 0)
            completion_tokens += usage.get("output_tokens", 0)

    return article, prompt_tokens, completion_tokens, duration


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
        print(f"=== [{i}/{len(pending_queries)}] Query {qid} ({q.get('language')}) ===")
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
            print(f"[FAIL] No final_report*.md found for query {qid} (duration: {dur:.0f}s)")
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
    print(f"       Total: {sum(1 for _ in out_path.open(encoding='utf-8'))} entries")


if __name__ == "__main__":
    main()

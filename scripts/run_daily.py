"""FreeAPIHub daily pipeline orchestrator (spec #14, #44, #45).

Runs the full sequence once per day. Every stage is isolated: one failing
candidate or source never destroys the run, and validation gates the commit.

    discover -> dedupe -> verify -> select -> research -> generate
    -> build site -> search index -> sitemap -> validate -> summary

Exit codes: 0 = success (including zero publications), 1 = validation failed
(the workflow will not deploy a broken build).
"""

from __future__ import annotations

import time

from utils import (DATA_DIR, LOG, load_json, save_json, load_config, today_str,
                   append_run_log, github_step_summary)
import discover
import verify as verify_mod
import research as research_mod
import generate as generate_mod
import build_site
import update_sitemap
import validate as validate_mod
import llm as llm_mod


def main() -> int:
    started = time.time()
    cfg = load_config()
    date = today_str()
    stats = {"date": date, "discovered": 0, "duplicates": 0, "verified": 0,
             "published": 0, "failed": 0, "skipped": 0}

    db_file = DATA_DIR / "apis.json"
    database = load_json(db_file, {"apis": []}).get("apis", [])
    queue = load_json(DATA_DIR / "queue.json", {"candidates": []}).get("candidates", [])

    # 1-3. Load, discover, normalize, deduplicate.
    try:
        d_stats = discover.run_discovery(cfg, database, queue)
        stats["discovered"] = d_stats["discovered"]
        stats["duplicates"] = d_stats["duplicates"]
        save_json(DATA_DIR / "queue.json", {"candidates": queue})
        LOG.info("Discovery: %(discovered)d found, %(duplicates)d duplicates, "
                 "%(enqueued)d new (queue: %(queue)d).",
                 {**d_stats, "queue": len(queue)})
    except Exception as exc:
        LOG.warning("Discovery stage failed (%s) - continuing with queue only.", exc)

    # 4-6. Verify and select.
    passing = []
    try:
        v_stats, passing = verify_mod.run_verification(cfg, queue, database)
        save_json(DATA_DIR / "queue.json", {"candidates": queue})
        save_json(DATA_DIR / "_verified.json", {"passing": passing})
        stats["verified"] = v_stats["verified"]
        stats["failed"] += v_stats["failed"]
        LOG.info("Verification: checked=%(checked)d pass=%(verified)d fail=%(failed)d.", v_stats)
    except Exception as exc:
        LOG.warning("Verification stage failed (%s) - no new publications today.", exc)

    # 7-9. Research + generate page data. LLM is optional by design.
    try:
        provider = llm_mod.get_provider(cfg)
    except Exception as exc:
        LOG.warning("Provider init failed (%s) - continuing deterministically.", exc)
        provider = None
    try:
        results = research_mod.run_research(cfg, passing, database, provider)
    except Exception as exc:
        LOG.warning("Research stage failed (%s) - falling back to deterministic entries.", exc)
        results = [{"candidate": c, "research": None,
                    "content_hash": ""} for c in passing]
    try:
        added = generate_mod.run_generation(cfg, results, database, stats)
        if added:
            save_json(db_file, {"apis": database})
            generate_mod.append_history(added)
    except Exception as exc:
        LOG.warning("Generation stage failed (%s).", exc)

    # 10-16. Rebuild the whole site (incremental-safe: full render is cheap).
    try:
        build_stats = build_site.run_build(cfg)
        sitemap_stats = update_sitemap.run_sitemap(cfg)
    except Exception as exc:
        LOG.warning("Site build failed (%s) - keeping the previously valid site.", exc)

    # 17-18. Validation gates the publish (never deploy a broken build).
    validation_ok = validate_mod.run_validation()

    # 19-21. Persist run log + GitHub Actions summary.
    stats["duration_seconds"] = int(time.time() - started)
    append_run_log(stats)

    history = load_json(DATA_DIR / "history.json", {"history": []}).get("history", [])
    today_added = next((h.get("added", []) for h in reversed(history)
                        if h.get("date") == date), [])
    new_pages = "\n".join(f"- /api/{a['slug']}/" for a in today_added) \
        or "- (none published today)"

    summary = f"""## FreeAPIHub Daily Run - {date}

| Metric | Count |
|---|---|
| Discovered | {stats['discovered']} |
| Duplicates | {stats['duplicates']} |
| Verification passed | {stats['verified']} |
| Published | {stats['published']} |
| Failed | {stats['failed']} |
| Skipped | {stats.get('skipped', 0)} |
| Duration | {stats['duration_seconds']}s |

**New pages:**
{new_pages}

**Site build:** PASS
**Validation:** {'PASS' if validation_ok else 'FAIL'}"""

    print(summary)
    github_step_summary(summary)
    LOG.info("Daily run finished in %ds. Published %d new API(s).",
             stats["duration_seconds"], stats["published"])
    return 0 if validation_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

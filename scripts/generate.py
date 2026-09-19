"""FreeAPIHub page-data generation.

Merges researched, verified candidates into the persistent database
(data/apis.json). This is the publication gate:

  - thin pages are rejected (spec #55)
  - slugs are deterministic and unique
  - entries already in the database are never duplicated
  - if the LLM research failed, deterministic source metadata is used instead
    (strictly what the source provided - never invented)
"""

from __future__ import annotations

from utils import (DATA_DIR, LOG, load_json, save_json, today_str, now_iso,
                   unique_slug, find_duplicate, quality_score, truncate,
                   slugify, content_hash)

FEATURE_FALLBACKS = {}  # never invent features to pad thin pages


def thin_page_check(entry: dict) -> tuple[bool, str]:
    """Reject pages without enough meaningful information (spec #55)."""
    if not entry.get("description") or len(entry.get("description", "")) < 60:
        return False, "description missing or too short"
    if not entry.get("official_url"):
        return False, "official source missing"
    if not entry.get("category"):
        return False, "category missing"
    # A missing documentation URL is tolerated when the official site itself
    # carries the API signal (verify.py already scores it lower for that).
    # "Documentation missing when expected" = a listed docs URL failed.
    if entry.get("_docs_url_failed"):
        return False, "documentation missing when expected (listed URL failed)"
    unknown_count = sum(1 for field in ("authentication", "free_tier", "license", "cors")
                        if entry.get(field) in ("unknown", None, ""))
    if unknown_count >= 4:
        return False, "almost all fields unknown"
    return True, "ok"


def build_entry(result: dict, cfg: dict, database: list[dict]) -> dict | None:
    """Create one database entry from a research result. Returns None if rejected."""
    candidate = result.get("candidate", {})
    research = result.get("research") or {}

    name = (research.get("name") or candidate.get("name") or "").strip()
    if not name:
        return None

    description = (research.get("description") or candidate.get("description") or "").strip()
    category = research.get("category") or candidate.get("category_hint") or "other"
    documentation_url = research.get("documentation_url") or candidate.get("documentation_url") or ""

    entry = {
        "slug": unique_slug(research.get("slug") or slugify(name), database),
        "name": name,
        "description": truncate(description, 800),
        "category": category,
        "official_url": candidate.get("official_url", ""),
        "documentation_url": documentation_url,
        "repo_url": candidate.get("repo_url", ""),
        "authentication": research.get("authentication") or candidate.get("auth_hint") or "unknown",
        "https": bool(candidate.get("https", True)),
        "cors": research.get("cors") or candidate.get("cors_hint") or "unknown",
        "free_tier": research.get("free_tier") or "unknown",
        "pricing_url": research.get("pricing_url") or "",
        "license": research.get("license") or "unknown",
        "status": "verified",
        "source": candidate.get("source", ""),
        "source_urls": [u for u in (candidate.get("official_url"),
                                    candidate.get("documentation_url")) if u],
        "last_verified": today_str(),
        "last_researched": today_str() if research else "",
        "last_updated": today_str(),
        "date_added": today_str(),
        "tags": research.get("tags") or [],
        "features": research.get("features") or [],
        "use_cases": research.get("use_cases") or [],
        "example_request": research.get("example_request"),
        "example_response": research.get("example_response"),
        "content_hash": result.get("content_hash", ""),
        "consecutive_failures": 0,
        "_official_ok": True,      # verified in this run
        "_docs_ok": bool(documentation_url),
        "generated_at": now_iso(),
        "researched": bool(research),
    }

    ok, reason = thin_page_check(entry)
    if not ok:
        LOG.info("Thin page rejected: '%s' (%s)", name, reason)
        return None

    entry["quality_score"] = quality_score(entry, cfg)

    threshold = int(cfg.get("minimum_quality_score", 70))
    if entry["quality_score"] < threshold:
        LOG.info("Below quality threshold (%d < %d): '%s' - not published.",
                 entry["quality_score"], threshold, name)
        return None
    return entry


def run_generation(cfg: dict, results: list[dict], database: list[dict],
                   stats: dict) -> list[dict]:
    """Merge pass. Returns the newly added entries. Mutates database + stats."""
    added = []
    for result in results:
        candidate = result.get("candidate", {})
        try:
            duplicate = find_duplicate({"name": candidate.get("name"),
                                        "official_url": candidate.get("official_url"),
                                        "documentation_url": candidate.get("documentation_url"),
                                        "repo_url": candidate.get("repo_url")}, database)
            if duplicate:
                stats["duplicates"] = stats.get("duplicates", 0) + 1
                LOG.info("Duplicate blocked at merge: '%s' ~= '%s'",
                         candidate.get("name"), duplicate.get("name"))
                continue
            entry = build_entry(result, cfg, database)
            if entry is None:
                stats["skipped"] = stats.get("skipped", 0) + 1
                continue
            database.append(entry)
            added.append(entry)
            stats["published"] = stats.get("published", 0) + 1
            LOG.info("PUBLISH [%2d] %-32s /api/%s/",
                     entry["quality_score"], entry["name"][:32], entry["slug"])
        except Exception as exc:
            # One failing candidate must never abort the batch (spec #28).
            LOG.warning("Generation error for '%s': %s - candidate skipped.",
                        candidate.get("name"), exc)
            stats["failed"] = stats.get("failed", 0) + 1
    return added


def append_history(added: list[dict]) -> None:
    if not added:
        return
    path = DATA_DIR / "history.json"
    data = load_json(path, {"history": []})
    data.setdefault("history", []).append({
        "date": today_str(),
        "added": [{"slug": a["slug"], "name": a["name"], "category": a["category"]}
                  for a in added],
    })
    data["history"] = data["history"][-730:]
    save_json(path, data)


def main() -> int:
    from utils import load_config
    cfg = load_config()
    database = load_json(DATA_DIR / "apis.json", {"apis": []}).get("apis", [])
    results = load_json(DATA_DIR / "_researched.json", {"results": []}).get("results", [])
    stats = {}
    added = run_generation(cfg, results, database, stats)
    if added:
        save_json(DATA_DIR / "apis.json", {"apis": database})
        append_history(added)
    LOG.info("Generation: published=%(published)d skipped=%(skipped)d "
             "duplicates=%(duplicates)d failed=%(failed)d", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

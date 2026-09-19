"""FreeAPIHub one-time seeder.

Loads the curated starter APIs from data/seed.json (all real, documented,
free-access services), runs them through the SAME verification gate as the
daily pipeline, and - only for entries that pass live checks - writes them
into the database and builds the complete site.

Run once:  python scripts/seed.py
"""

from __future__ import annotations

from utils import (DATA_DIR, LOG, load_json, save_json, load_config, today_str)
import verify as verify_mod
import generate as generate_mod
import build_site
import update_sitemap
import validate as validate_mod


def main() -> int:
    cfg = load_config()
    seed = load_json(DATA_DIR / "seed.json", {"seed": []}).get("seed", [])
    db = load_json(DATA_DIR / "apis.json", {"apis": []})
    database = db.get("apis", [])

    LOG.info("Seeding %d curated starter APIs through live verification...", len(seed))
    passing, stats = [], {"verified": 0, "failed": 0, "checked": 0}
    for entry in seed:
        candidate = {
            "name": entry.get("name", ""),
            "official_url": entry.get("official_url", ""),
            "documentation_url": entry.get("documentation_url", ""),
            "description": entry.get("description", ""),
            "category_hint": entry.get("category", "other"),
            "auth_hint": entry.get("authentication", "unknown"),
            "https_hint": entry.get("https", True),
            "cors_hint": entry.get("cors", "unknown"),
            "source": "seed",
            "added_date": today_str(),
            "failed_attempts": 0,
        }
        stats["checked"] += 1
        try:
            verify_mod.verify_candidate(candidate, cfg, {})
        except Exception as exc:
            LOG.warning("Seed verification error for '%s': %s", candidate["name"], exc)
            stats["failed"] += 1
            continue
        ok, reason = verify_mod.candidate_passes(candidate, cfg)
        if ok:
            candidate["verified_date"] = today_str()
            candidate["status"] = "verified"
            passing.append(candidate)
            stats["verified"] += 1
            LOG.info("PASS %-32s %s", candidate["name"][:32], candidate["official_url"])
        else:
            stats["failed"] += 1
            LOG.info("FAIL %-32s %s", candidate["name"][:32], reason)

    # Build database entries from seed facts (no LLM needed for the seed:
    # facts were curated by hand from official sources, never invented).
    results = []
    for candidate in passing:
        entry_data = next((e for e in seed if e.get("name") == candidate.get("name")), {})
        research = {
            "name": entry_data.get("name", candidate["name"]),
            "slug": entry_data.get("slug"),
            "description": entry_data.get("description", ""),
            "category": entry_data.get("category", candidate.get("category_hint", "other")),
            "features": entry_data.get("features", []),
            "use_cases": entry_data.get("use_cases", []),
            "authentication": entry_data.get("authentication", "unknown"),
            "https": entry_data.get("https", True),
            "cors": entry_data.get("cors", "unknown"),
            "free_tier": entry_data.get("free_tier", "unknown"),
            "pricing_url": entry_data.get("pricing_url") or None,
            "license": entry_data.get("license", "unknown"),
            "tags": entry_data.get("tags", []),
            "seo_title": f"{entry_data.get('name', candidate['name'])} API - free tier, docs & features",
            "meta_description": (entry_data.get("description", "") or "")[:150],
            "example_request": entry_data.get("example_request"),
            "example_response": entry_data.get("example_response"),
        }
        from utils import content_hash
        results.append({"candidate": candidate, "research": research,
                        "content_hash": content_hash({"name": candidate.get("name"),
                                                      "url": candidate.get("official_url"),
                                                      "seed": True})})

    publish_stats = {}
    added = generate_mod.run_generation(cfg, results, database, publish_stats)
    if added:
        save_json(DATA_DIR / "apis.json", {"apis": database})
        generate_mod.append_history(added)

    build_site.run_build(cfg)
    update_sitemap.run_sitemap(cfg)
    ok = validate_mod.run_validation()

    LOG.info("Seed complete: pass=%(verified)d fail=%(failed)d published=%(published)d",
             {**stats, "published": publish_stats.get("published", 0)})
    if not ok:
        LOG.error("Validation FAILED after seeding.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

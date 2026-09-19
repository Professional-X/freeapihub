"""FreeAPIHub maintenance engine (spec #24, #25, #26, #43).

Periodic upkeep - conservative by design:

  - re-validates the oldest-checked listings in batches (never the whole DB)
  - status policy: verified -> warning (1 failure) -> temporarily_unavailable
    (3 failures) -> dead (6 failures). Nothing is deleted on one failure.
  - flags entries whose documentation content changed significantly for
    regeneration (the daily pipeline re-researches a small budget of them)
  - duplicate audit and thin-page audit (deterministic only)
  - rebuilds the site, search index and sitemap afterwards
"""

from __future__ import annotations

from utils import (DATA_DIR, LOG, save_json, today_str, days_between, load_json,
                   load_config, fetch_url, normalize_url, url_domain, similarity,
                   normalize_name, quality_score)
import build_site
import update_sitemap


def status_for_failures(failures: int, cfg: dict) -> str:
    if failures >= int(cfg.get("dead_after_failures", 6)):
        return "dead"
    if failures >= int(cfg.get("unavailable_after_failures", 3)):
        return "temporarily_unavailable"
    if failures >= int(cfg.get("warning_after_failures", 1)):
        return "warning"
    return "verified"


def revalidate_entry(api: dict, cfg: dict) -> dict:
    """Re-check one entry's official + documentation URLs. Returns result dict."""
    official = fetch_url(api.get("official_url", ""),
                         timeout=int(cfg.get("verification_timeout_seconds", 10)),
                         max_retries=1)
    official_ok = official.ok or official.status in (401, 403, 429)

    docs_ok = official_ok
    if api.get("documentation_url") and \
            normalize_url(api["documentation_url"]) != normalize_url(api.get("official_url", "")):
        docs = fetch_url(api["documentation_url"],
                         timeout=int(cfg.get("verification_timeout_seconds", 10)),
                         max_retries=1)
        docs_ok = docs.ok or docs.status in (401, 403, 429)
        api["_docs_sample_new"] = docs.text[:4000]

    if official_ok and docs_ok:
        api["consecutive_failures"] = 0
        api["status"] = "verified"
    else:
        api["consecutive_failures"] = api.get("consecutive_failures", 0) + 1
        api["status"] = status_for_failures(api["consecutive_failures"], cfg)
        api["_last_failure"] = f"official={official.status or official.error} docs_ok={docs_ok}"

    api["last_verified"] = today_str()
    return {"ok": official_ok and docs_ok, "status": api["status"]}


def detect_content_change(api: dict) -> bool:
    """True when documentation text differs a lot from the stored sample."""
    old = (api.get("_docs_sample") or "")[:2000]
    new = (api.get("_docs_sample_new") or "")[:2000]
    if not old or not new:
        return False
    return similarity(normalize_name(old)[:2000], normalize_name(new)[:2000]) < 0.75


def run_maintenance(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    batch = int(cfg.get("maintenance_batch_size", 30))
    regen_budget = int(cfg.get("maintenance_regenerate_per_run", 2))
    stale_days = int(cfg.get("maintenance_days", 14))

    db = load_json(DATA_DIR / "apis.json", {"apis": []})
    database = db.get("apis", [])
    if not database:
        LOG.info("Maintenance: database empty - nothing to do.")
        return {"checked": 0, "recovered": 0, "degraded": 0, "flagged_research": 0}

    # Oldest verification first, skipping entries verified very recently.
    candidates = [a for a in database
                  if days_between(a.get("last_verified") or "1970-01-01") >= max(1, stale_days // 2)]
    candidates.sort(key=lambda a: a.get("last_verified") or "0000-00-00")
    batch = candidates[:batch]

    stats = {"checked": len(batch), "recovered": 0, "degraded": 0,
             "flagged_research": 0, "duplicates": 0, "thin": 0}

    for api in batch:
        previous = api.get("status", "verified")
        try:
            revalidate_entry(api, cfg)
        except Exception as exc:
            LOG.warning("Maintenance revalidation error for '%s': %s", api.get("slug"), exc)
            api["consecutive_failures"] = api.get("consecutive_failures", 0) + 1
            api["status"] = status_for_failures(api["consecutive_failures"], cfg)
            continue
        api.pop("_docs_sample_new", None)
        if previous != "verified" and api["status"] == "verified":
            stats["recovered"] += 1
        elif previous == "verified" and api["status"] != "verified":
            stats["degraded"] += 1
            LOG.info("DEGRADED: %s -> %s (%s)", api.get("slug"), api["status"],
                     api.get("_last_failure", ""))

        # Recent-verification score component is now refreshed automatically.
        api["quality_score"] = quality_score(api, cfg)

    # Duplicate audit (deterministic).
    seen: dict[str, str] = {}
    for api in database:
        key = normalize_url(api.get("official_url", ""))
        if key and key in seen:
            stats["duplicates"] += 1
            LOG.info("Duplicate detected: %s ~= %s", api.get("slug"), seen[key])
        else:
            seen[key] = api.get("slug", "")

    # Thin page audit.
    for api in database:
        unknown = sum(1 for f in ("authentication", "free_tier", "license")
                      if api.get(f) in ("unknown", None, ""))
        if unknown >= 3 or len(api.get("description", "")) < 60:
            stats["thin"] += 1

    save_json(DATA_DIR / "apis.json", db)

    # Budgeted regeneration flags consumed by the next daily run.
    stale = sorted(
        (a for a in database
         if a.get("status") == "verified"
         and days_between(a.get("last_researched") or "1970-01-01") >= stale_days * 2),
        key=lambda a: a.get("last_researched") or "0000-00-00")
    flagged = [{"slug": a["slug"], "name": a.get("name", ""),
                "official_url": a.get("official_url"),
                "documentation_url": a.get("documentation_url"),
                "category": a.get("category")} for a in stale[:regen_budget]]
    save_json(DATA_DIR / "_needs_research.json", flagged)

    # Rebuild everything so the site reflects new statuses.
    build_result = build_site.run_build(cfg)
    sitemap_result = update_sitemap.run_sitemap(cfg)

    LOG.info("Maintenance: checked=%(checked)d recovered=%(recovered)d degraded=%(degraded)d "
             "duplicates=%(duplicates)d thin=%(thin)d flagged=%(flagged)d",
             {**stats, "flagged": len(flagged)})
    stats["search_entries"] = build_result.get("search_entries")
    stats["sitemap_urls"] = sitemap_result.get("sitemap_urls")
    return stats


def main() -> int:
    run_maintenance()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

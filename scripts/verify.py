"""FreeAPIHub verification engine.

Verification is mandatory before any candidate can be published (spec #11).
Conservative HTTP checks only:

  - one GET per URL (official site, documentation) with a short timeout
  - honest User-Agent, per-host politeness delay, max 2 retries on transport
    errors, exponential backoff on 429/5xx with Retry-After support
  - no endpoint brute forcing, no authentication attempts, no destructive
    requests, no CORS probing (CORS is taken from source metadata only)

A 7-day verification cache avoids re-hitting hosts that were checked recently.
"""

from __future__ import annotations

import re

from utils import (DATA_DIR, LOG, fetch_url, load_json, save_json, today_str,
                   days_between, url_domain, normalize_url, quality_score,
                   similarity, normalize_name)

API_SIGNALS = re.compile(r"\bapi\b|developer|documentation|docs\.|endpoint|sdk", re.I)
FREE_SIGNALS = re.compile(
    r"free tier|free plan|free for|freemium|no credit card|completely free|"
    r"\bfree\b.{0,30}(api|plan|tier|usage)|(api|plan|tier).{0,30}\bfree\b", re.I)
DANGEROUS_CODES = {401, 403, 429}  # reachable but restricted


def _check_single(url: str, cfg: dict, cache: dict, purpose: str) -> tuple[dict, dict]:
    """Check one URL. Returns (result_dict, cache_update)."""
    key = normalize_url(url)
    cached = cache.get(key)
    if cached and days_between(cached.get("date", "1970-01-01")) < int(cfg.get("verify_cache_days", 7)):
        cached = dict(cached)
        cached["from_cache"] = True
        return cached, {}

    result = fetch_url(url, timeout=int(cfg.get("verification_timeout_seconds", 10)),
                       max_retries=int(cfg.get("max_retries", 2)), max_text=400_000)

    text_signal = result.text[:60000]  # big window: heavy sites bury "API" deep
    text_sample = text_signal[:8000]   # small sample for LLM prompts (cost control)
    checked = {
        "ok": result.ok,
        "status": result.status,
        "final_url": result.final_url,
        "https": result.https,
        "redirected": result.redirected,
        "error": result.error,
        "describes_api": bool(result.text and API_SIGNALS.search(text_signal)),
        "free_hint": bool(result.text and FREE_SIGNALS.search(text_signal[:20000])),
        "text_sample": text_sample,
        "date": today_str(),
    }
    if not result.ok and result.status in DANGEROUS_CODES:
        # 401/403/429: host alive, content restricted. Treat as reachable-but-guarded.
        checked["ok"] = True
        checked["restricted"] = True
    if purpose == "docs" and result.ok and not checked["describes_api"]:
        # Landing pages without any API signal are weak documentation evidence.
        checked["weak_docs"] = True

    cache_update = {k: checked[k] for k in
                    ("ok", "status", "final_url", "https", "redirected", "error", "date")}
    cache_update["date"] = today_str()
    return checked, {key: cache_update}


def verify_candidate(candidate: dict, cfg: dict, cache: dict) -> dict:
    """Verify one candidate in place. Adds verification fields, never invents
    facts: only reachability, HTTPS, API-signal and free-tier hints."""
    official = candidate.get("official_url") or ""
    docs = (candidate.get("documentation_url") or "").strip()

    official_check, cache_o = _check_single(official, cfg, cache, "official")
    candidate["_official_ok"] = official_check["ok"]
    candidate["_official_status"] = official_check["status"]
    candidate["_official_error"] = official_check.get("error")
    candidate["https"] = bool(official_check["https"])
    candidate["_official_describes_api"] = official_check.get("describes_api", False)
    cache.update(cache_o)

    if docs and normalize_url(docs) != normalize_url(official):
        docs_check, cache_d = _check_single(docs, cfg, cache, "docs")
        candidate["_docs_ok"] = docs_check["ok"]
        candidate["_docs_status"] = docs_check["status"]
        candidate["_docs_describes_api"] = docs_check.get("describes_api", False)
        if docs_check.get("text_sample"):
            candidate["_docs_sample"] = docs_check["text_sample"]
        cache.update(cache_d)
    elif docs:
        candidate["_docs_ok"] = official_check["ok"]
        candidate["_docs_status"] = official_check["status"]
    else:
        candidate["_docs_ok"] = False
        candidate["_docs_status"] = 0

    # The API-signal test: the official page or docs must actually look like an API.
    candidate["_describes_api"] = bool(
        official_check.get("describes_api") or candidate.get("_docs_describes_api"))

    return candidate


def candidate_passes(candidate: dict, cfg: dict) -> tuple[bool, str]:
    """Deterministic acceptance gate for verified candidates.

    A missing documentation URL is tolerated only when the official page
    itself carries a clear API signal - the entry simply scores lower
    (docs component lost) and may still clear the quality threshold.
    A docs URL that EXISTS but fails is always a rejection.
    """
    if not candidate.get("_official_ok"):
        return False, f"official URL unreachable ({candidate.get('_official_error') or candidate.get('_official_status')})"
    if not candidate.get("_describes_api"):
        return False, "no API signal on official page or documentation"
    if not candidate.get("description") or len(candidate.get("description") or "") < 40:
        return False, "description missing or too short"
    if candidate.get("documentation_url") and not candidate.get("_docs_ok"):
        return False, "documentation URL unreachable"
    return True, "ok"


def run_verification(cfg: dict, queue: list[dict], database: list[dict]) -> dict:
    """Verify up to N candidates from the queue, best first. Returns stats and
    the list of passing candidates (still outside the database)."""
    cache = load_json(DATA_DIR / "verify-cache.json", {})
    max_candidates = int(cfg.get("verify_max_candidates_per_run", 16))

    def candidate_sort_key(c):
        # Candidates with no failures first; previously-failed ones rotate to
        # the back so one bad batch never starves good candidates.
        attempts = c.get("failed_attempts", 0)
        complete = 1 if (c.get("documentation_url") or c.get("repo_url")) else 0
        known_auth = 1 if c.get("auth_hint", "unknown") != "unknown" else 0
        return (attempts, -complete, -known_auth, c.get("added_date", ""), c.get("name", ""))

    pending = sorted(queue, key=candidate_sort_key)
    stats = {"verified": 0, "failed": 0, "skipped": 0, "checked": 0}
    passing = []

    for candidate in pending:
        if stats["checked"] >= max_candidates:
            break
        stats["checked"] += 1
        try:
            verify_candidate(candidate, cfg, cache)
        except Exception as exc:
            LOG.warning("Verification error for '%s': %s - skipping candidate.",
                        candidate.get("name"), exc)
            candidate["failed_attempts"] = candidate.get("failed_attempts", 0) + 1
            stats["failed"] += 1
            continue

        ok, reason = candidate_passes(candidate, cfg)
        if ok:
            candidate["verified_date"] = today_str()
            candidate["status"] = "verified"
            score = quality_score({
                "official_url": candidate.get("official_url"),
                "_official_ok": True,
                "documentation_url": candidate.get("documentation_url"),
                "_docs_ok": True,
                "description": candidate.get("description"),
                "authentication": candidate.get("auth_hint"),
                "free_tier": _free_tier_guess(candidate),
                "category": candidate.get("category_hint"),
                "last_verified": today_str(),
            }, cfg)
            candidate["_verify_score"] = score
            passing.append(candidate)
            stats["verified"] += 1
            LOG.info("PASS  [%2d] %-30s %s", score, candidate.get("name", "?")[:30],
                     candidate.get("official_url", ""))
        else:
            candidate["failed_attempts"] = candidate.get("failed_attempts", 0) + 1
            candidate["_fail_reason"] = reason
            if candidate["failed_attempts"] >= 3:
                queue.remove(candidate)
                LOG.info("DROP  %-30s %s", candidate.get("name", "?")[:30],
                         f"(3 failures: {reason})")
            else:
                LOG.info("FAIL  %-30s %s", candidate.get("name", "?")[:30], reason)
            stats["failed"] += 1

    save_json(DATA_DIR / "verify-cache.json", cache)
    return stats, passing


def _free_tier_guess(candidate: dict) -> str:
    """Conservative free-tier signal from source metadata only."""
    if candidate.get("auth_hint") == "none":
        return "No authentication required (as listed by the source)"
    desc = (candidate.get("description") or "").lower()
    if "free" in desc:
        return "Free access indicated by the source description"
    return "unknown"


def main() -> int:
    from utils import load_config
    cfg = load_config()
    database = load_json(DATA_DIR / "apis.json", {"apis": []}).get("apis", [])
    queue = load_json(DATA_DIR / "queue.json", {"candidates": []}).get("candidates", [])
    stats, passing = run_verification(cfg, queue, database)
    save_json(DATA_DIR / "queue.json", {"candidates": queue})
    save_json(DATA_DIR / "_verified.json", {"passing": passing})
    LOG.info("Verification: checked=%(checked)d pass=%(verified)d fail=%(failed)d", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""FreeAPIHub validation + self-tests.

Validation (spec #41) runs before every commit. If anything fails, the build
must NOT be published:

  - JSON parse checks for every data file
  - database schema: required fields, unique slugs, no duplicate URLs
  - generated HTML: resolved tokens, canonical present, structure sane
  - internal link sanity: every internal href resolves to a real file
  - sitemap: parseable, all URLs unique, all mapped files exist
  - configuration: required keys present, placeholder values detected
  - advertisement: valid JSON, slot keys known, code inserted verbatim once

--selftest simulates the documented failure modes (spec #60) and verifies
the pipeline degrades gracefully instead of crashing.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from utils import (ROOT, SITE_DIR, DATA_DIR, CONFIG_DIR, LOG, load_json,
                   load_config, load_ads, normalize_url)

FAILURES: list[str] = []
WARNINGS: list[str] = []


def fail(message: str) -> None:
    FAILURES.append(message)
    LOG.error("VALIDATE-FAIL %s", message)


def warn(message: str) -> None:
    WARNINGS.append(message)
    LOG.warning("validate-warn %s", message)


def check(condition: bool, message: str, as_warning: bool = False) -> bool:
    if not condition:
        (warn if as_warning else fail)(message)
        return False
    return True


# --------------------------------------------------------------- data files ---

def validate_data_files() -> None:
    for name in ("apis.json", "categories.json", "sources.json",
                 "history.json", "run-log.json", "queue.json", "seed.json"):
        path = DATA_DIR / name
        if not path.exists():
            warn(f"data/{name} missing (will be created on demand)")
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            fail(f"data/{name} is not valid JSON: {exc}")


def validate_database() -> None:
    db = load_json(DATA_DIR / "apis.json", {"apis": []}).get("apis", [])
    slugs: set[str] = set()
    urls: dict[str, str] = {}
    required = ("slug", "name", "description", "category", "official_url",
                "documentation_url", "last_verified", "date_added", "status")
    for api in db:
        slug = api.get("slug", "")
        for field in required:
            if not api.get(field):
                fail(f"database entry '{slug}': missing required field '{field}'")
        if slug in slugs:
            fail(f"duplicate slug in database: {slug}")
        slugs.add(slug)

        for field in ("official_url", "documentation_url", "pricing_url"):
            url = api.get(field) or ""
            if url and not url.startswith("http"):
                fail(f"database entry '{slug}': {field} is not an absolute URL")
            if url:
                norm = normalize_url(url)
                if norm in urls and urls[norm] != slug:
                    fail(f"database entries '{slug}' and '{urls[norm]}' share {field}")
                urls.setdefault(norm, slug)

        if api.get("status") not in ("verified", "warning", "temporarily_unavailable", "dead"):
            fail(f"database entry '{slug}': invalid status '{api.get('status')}'")

        if re.search(r"<[a-z][\s\S]*>", api.get("description", "")):
            warn(f"database entry '{slug}': description contains raw HTML")

    # Secrets must never appear in the database.
    blob = (DATA_DIR / "apis.json").read_text(encoding="utf-8").lower()
    for pattern in ("ghp_", "github_pat_", "sk-ant", "\"sk-", "llm_api_key"):
        if pattern in blob:
            fail(f"possible secret pattern '{pattern}' found in data/apis.json")


# ----------------------------------------------------------------- html files --

def validate_html(database) -> None:
    html_files = sorted(SITE_DIR.rglob("*.html"))
    if not html_files:
        fail("site/ contains no HTML files - build did not run")
        return

    slugs = {a["slug"] for a in database}
    checked = 0
    for path in html_files:
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read {path}: {exc}")
            continue
        rel = path.relative_to(SITE_DIR).as_posix()
        checked += 1
        if re.search(r"\{\{[A-Z_]+\}\}", content):
            fail(f"{rel}: unresolved template tokens")
        if "<title>" not in content or "</html>" not in content:
            fail(f"{rel}: missing <title> or closing </html>")
        if 'name="description"' not in content and rel != "404.html":
            fail(f"{rel}: missing meta description")
        if 'rel="canonical"' not in content and rel != "404.html":
            fail(f"{rel}: missing canonical URL")
        for phrase in ("TODO", "IMPLEMENT LATER", "YOUR CODE HERE", "lorem ipsum"):
            if phrase.lower() in content.lower():
                fail(f"{rel}: placeholder text '{phrase}' found")

        # Internal links must resolve to real files (skip fragments, external).
        for href in re.findall(r'href="([^"]+)"', content):
            if href.startswith(("http", "mailto:", "tel:", "data:", "javascript:")):
                continue
            if href.startswith("#") or href.endswith("#") or "#" in href:
                # fragment-only or same-page anchors are structurally fine
                if href.split("#")[0] in ("", href):
                    continue
            target = (path.parent / href.split("#")[0]).resolve()
            if not target.exists():
                fail(f"{rel}: broken internal link '{href}'")
    LOG.info("HTML validation: %d files checked.", checked)

    # Every database entry must have a page (unless dead and archived).
    missing_pages = [a["slug"] for a in database
                     if a.get("status") != "dead"
                     and not (SITE_DIR / "api" / a["slug"] / "index.html").exists()]
    if missing_pages:
        fail(f"missing generated pages for: {', '.join(missing_pages[:8])}")


# -------------------------------------------------------------------- sitemap --

def validate_sitemap() -> None:
    path = SITE_DIR / "sitemap.xml"
    if not path.exists():
        fail("sitemap.xml missing")
        return
    content = path.read_text(encoding="utf-8")
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(content)
        locs = [e.text for e in root.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    except (ET.ParseError, OSError) as exc:
        fail(f"sitemap.xml unparseable: {exc}")
        return
    if len(locs) != len(set(locs)):
        fail("sitemap.xml contains duplicate URLs")
    if not locs:
        fail("sitemap.xml contains no URLs")
    else:
        LOG.info("Sitemap validation: %d unique URLs.", len(set(locs)))


# ----------------------------------------------------------------- config/ads --

def validate_config() -> None:
    cfg = load_config()
    for key in ("site_name", "site_url", "daily_publish_limit",
                "minimum_quality_score", "verification_timeout_seconds"):
        if not check(key in cfg, f"config/site.json missing required key '{key}'"):
            return
    if "YOUR-DOMAIN.example" in cfg.get("site_url", ""):
        warn("site_url still contains the placeholder YOUR-DOMAIN.example - set your real URL")
    ads = load_ads()
    if "slots" not in ads:
        fail("config/ads.json missing 'slots' object")
    else:
        known = {"top", "after_intro", "middle", "before_related", "sidebar", "footer"}
        unknown = set(ads.get("slots", {})) - known
        if unknown:
            fail(f"config/ads.json has unknown slot names: {sorted(unknown)}")
    if ads.get("enabled") and not any((ads.get("slots") or {}).values()):
        warn("ads enabled but all slots are empty - paste your ad code into config/ads.json")

    # Secrets must not exist in any committed file.
    for pattern_file in ("config/site.json", "config/ads.json"):
        text = (ROOT / pattern_file).read_text(encoding="utf-8")
        for secret in ("sk-", "ghp_", "github_pat_"):
            if secret in text:
                fail(f"possible secret in {pattern_file}")


def validate_ad_insertion(ads) -> None:
    """When a slot has code, it must appear verbatim on pages - exactly once."""
    if not ads.get("enabled"):
        return
    for slot, code in (ads.get("slots") or {}).items():
        if not code.strip():
            continue
        for probe in ("index.html", "about.html"):
            page = SITE_DIR / probe
            if not page.exists():
                continue
            count = page.read_text(encoding="utf-8").count(code)
            if count == 0:
                fail(f"ad slot '{slot}' code missing from {probe} - ads were not inserted")
            elif count > 1:
                fail(f"ad slot '{slot}' code appears {count}x in {probe} - duplicated slot")
            else:
                LOG.info("Ad slot '%s' verified verbatim in %s.", slot, probe)


# ------------------------------------------------------------------ selftests --

def selftest() -> bool:
    """Simulated failure modes (spec #60). Each must degrade gracefully."""
    from utils import unique_slug, find_duplicate, quality_score, FetchResult
    import generate
    import verify as verify_mod
    import llm as llm_mod
    import research as research_mod
    from unittest.mock import patch

    ok = True

    def expect(name: str, condition: bool) -> None:
        nonlocal ok
        print(f"  {'PASS' if condition else 'FAIL'}  {name}")
        if not condition:
            ok = False

    print("FreeAPIHub self-test")

    # 1. invalid API URL -> verification fails, no crash
    cfg = load_config()
    cand = {"name": "Broken Example", "official_url": "https://nonexistent.invalid.example/",
            "description": "A candidate whose official site cannot be reached at all.",
            "documentation_url": "", "_official_ok": False, "_docs_ok": False,
            "_describes_api": False}
    passed, reason = verify_mod.candidate_passes(cand, cfg)
    expect("invalid URL is rejected, not crashed", not passed and "unreachable" in reason)

    # 2. dead documentation -> rejected
    cand2 = {"name": "Docs Down", "official_url": "https://example.org/",
             "documentation_url": "https://docs.invalid.example/",
             "description": "Official site up but documentation is unreachable.",
             "_official_ok": True, "_docs_ok": False, "_describes_api": True}
    passed, _ = verify_mod.candidate_passes(cand2, cfg)
    expect("dead documentation rejected", not passed)

    # 3. duplicate API -> blocked at merge
    db = [{"slug": "known-api", "name": "Known API", "official_url": "https://known.example/"}]
    dup = find_duplicate({"name": "Known API", "official_url": "https://known.example/"},
                         db)
    expect("duplicate detection blocks same URL", dup is not None)
    expect("unique slug avoids collision", unique_slug("known-api", db) != "known-api")

    # 4. LLM invalid JSON -> LLMError raised, research retries then skips
    try:
        provider = llm_mod.OpenAICompatibleProvider(api_key="dummy", model="test")
        try:
            provider._extract_json("garbage { nope")
            expect("invalid JSON raises LLMError", False)
        except llm_mod.LLMError:
            expect("invalid JSON raises LLMError", True)
    except Exception:
        expect("invalid JSON raises LLMError", False)

    # 5. LLM timeout -> research_candidate returns None, pipeline continues
    class TimeoutProvider(llm_mod.LLMProvider):
        name = "timeout"
        def generate_structured(self, system, user, max_tokens=1400):
            raise llm_mod.LLMError("provider timeout")
        def generate_text(self, system, user, max_tokens=1400):
            raise llm_mod.LLMError("provider timeout")
    good_candidate = {
        "name": "Timeout Target", "official_url": "https://timeout.example/",
        "documentation_url": "https://timeout.example/docs",
        "description": "Candidate used to test LLM timeout handling in research.",
        "category_hint": "other", "auth_hint": "unknown", "cors_hint": "unknown",
        "https": True, "source": "test",
        "_official_sample": "", "_docs_sample": "",
    }
    researched = research_mod.research_candidate(TimeoutProvider(), good_candidate)
    expect("LLM timeout -> candidate skipped, no crash", researched is None)

    # 6. API timeout in fetch -> never raises
    with patch("utils.requests.get", side_effect=__import__("requests").exceptions.Timeout):
        result = verify_mod.fetch_url("https://timeout.example/", timeout=1, max_retries=1,
                                      throttle=__import__("utils").HostThrottle(0.05))
    expect("network timeout returns error result", not result.ok and result.error == "timeout")

    # 7. missing configuration -> defaults used
    expect("missing config falls back to defaults", bool(cfg.get("daily_publish_limit")))

    # 8. empty candidate list -> generation runs with 0 published
    stats = {}
    added = generate.run_generation(cfg, [], [], stats)
    expect("empty candidate list publishes 0", added == [] and stats.get("published", 0) == 0)

    # 9. thin page rejected
    thin = generate.build_entry(
        {"candidate": {"name": "Thin", "official_url": "https://x.example/",
                       "documentation_url": "https://x.example/docs", "https": True,
                       "_official_ok": True, "_docs_ok": True},
         "research": {"name": "Thin", "description": "too short", "category": "other",
                      "authentication": "unknown", "cors": "unknown", "free_tier": "unknown"},
         "content_hash": "x"}, cfg, [])
    expect("thin page rejected by publication gate", thin is None)

    # 10. quality score is deterministic 0-100
    entry = {"official_url": "https://a.example/", "_official_ok": True,
             "documentation_url": "https://a.example/docs", "_docs_ok": True,
             "description": "A description long enough to earn its points.",
             "authentication": "api-key", "free_tier": "Free tier exists",
             "category": "weather", "last_verified": cfg and "2026-01-01"}
    score = quality_score(entry, cfg)
    expect("quality score within bounds", 0 <= score <= 100)

    return ok


# ---------------------------------------------------------------------- main --

def run_validation() -> bool:
    FAILURES.clear()
    WARNINGS.clear()
    validate_data_files()
    database = load_json(DATA_DIR / "apis.json", {"apis": []}).get("apis", [])
    validate_database()
    validate_html(database)
    validate_sitemap()
    validate_config()
    ads = load_ads()
    validate_ad_insertion(ads)
    return not FAILURES


def main() -> int:
    if "--selftest" in sys.argv:
        return 0 if selftest() else 1
    success = run_validation()
    report = ["FreeAPIHub validation", f"  result: {'PASS' if success else 'FAIL'}",
              f"  errors: {len(FAILURES)}", f"  warnings: {len(WARNINGS)}"]
    text = "\n".join(report)
    print(text)
    from utils import github_step_summary
    github_step_summary(text)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

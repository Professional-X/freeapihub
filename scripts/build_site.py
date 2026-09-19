"""FreeAPIHub static site builder.

Renders every HTML page from templates + the JSON database:
  - homepage (hero, dynamic statistics, categories, latest, featured)
  - categories index + one page per non-empty category (with pagination)
  - one page per API (full detail, related links, ad slots, JSON-LD, OG/Twitter)
  - static pages: about, submit, privacy, terms, search, 404
  - site/data/search.json client-side search index

Deterministic only - no LLM, no network. Ad code from config/ads.json is
inserted verbatim into configured slots (never modified, never duplicated).
"""

from __future__ import annotations

import html as html_mod
import json
import re

from utils import (SITE_DIR, TEMPLATES_DIR, DATA_DIR, LOG, load_json, save_json,
                   load_config, load_ads, today_str, truncate, days_between,
                   quality_score, url_domain)

STATUS_LABELS = {
    "verified": "Verified",
    "warning": "Re-check pending",
    "temporarily_unavailable": "Temporarily unavailable",
    "dead": "Unreachable",
}
STATUS_LONG = {
    "verified": "Verified and reachable at last automated check.",
    "warning": "The listing was not fully confirmed at the last automated check. It will be re-verified automatically.",
    "temporarily_unavailable": "The official source could not be reached at the last automated check. The listing is kept and will be retried.",
    "dead": "The official source has failed repeated automated checks. Treat all information on this page as outdated until confirmed.",
}
AUTH_LABELS = {
    "none": "No authentication",
    "api-key": "API key",
    "oauth": "OAuth",
    "user-password": "Username & password",
    "unknown": "Unknown",
}

_CATEGORIES = None


def esc(text) -> str:
    return html_mod.escape(str(text if text is not None else ""), quote=True)


# ------------------------------------------------------------- template io ----

def load_template(path: str) -> str:
    with open(TEMPLATES_DIR / path, encoding="utf-8") as fh:
        return fh.read()


def render(template: str, mapping: dict) -> str:
    out = template
    for key, value in mapping.items():
        out = out.replace("{{" + key + "}}", value)
    # Safety net: any unresolved token must not leak into production HTML.
    leftovers = re.findall(r"\{\{[A-Z_]+\}\}", out)
    if leftovers:
        LOG.warning("Unresolved template tokens: %s", sorted(set(leftovers)))
    return out


# ----------------------------------------------------------------- helpers ----

def categories() -> list[dict]:
    global _CATEGORIES
    if _CATEGORIES is None:
        data = load_json(DATA_DIR / "categories.json", {"categories": []})
        _CATEGORIES = {c["slug"]: c for c in data.get("categories", [])}
    return _CATEGORIES


def category_name(slug: str) -> str:
    cat = categories().get(slug)
    return cat["name"] if cat else slug.replace("-", " ").title()


def category_description(slug: str) -> str:
    cat = categories().get(slug)
    return cat["description"] if cat else "Useful APIs in this category."


def ad_slot(ads: dict, slot: str) -> str:
    if not ads.get("enabled"):
        return ""
    code = (ads.get("slots") or {}).get(slot) or ""
    if not code.strip():
        return ""
    return f'<div class="ad-slot" data-has-content="{slot}">{code}</div>'


def header_html(cfg, base: str) -> str:
    return render(load_template("components/header.html"),
                  {"BASE": base, "SITE_NAME": cfg.get("site_name", SITE_NAME)})


def footer_html(cfg, ads, base: str, page_script: str = "") -> str:
    """Footer component. Ad slots inside it are inserted verbatim or empty."""
    return render(load_template("components/footer.html"), {
        "BASE": base,
        "ASSET": base,
        "SITE_NAME": cfg.get("site_name", SITE_NAME),
        "SITE_TAGLINE": esc(cfg.get("site_tagline", "")),
        "YEAR": today_str()[:4],
        "PAGE_SCRIPT": page_script,
        "AD_FOOTER": ad_slot(ads, "footer"),
    })


def auth_label(value: str) -> str:
    return AUTH_LABELS.get(value, value or "Unknown")


def status_badge(api: dict) -> str:
    status = api.get("status", "verified")
    return (f'<span class="badge status-badge status-{esc(status)}">'
            f'{STATUS_LABELS.get(status, status)}</span>')


def og_tags(title: str, description: str, url: str, og_type: str = "website") -> str:
    t, d, u = esc(title), esc(truncate(description, 160)), esc(url)
    return (
        f'<meta property="og:site_name" content="{esc(SITE_NAME)}">\n'
        f'<meta property="og:type" content="{og_type}">\n'
        f'<meta property="og:title" content="{t}">\n'
        f'<meta property="og:description" content="{d}">\n'
        f'<meta property="og:url" content="{u}">\n'
        f'<meta name="twitter:card" content="summary">\n'
        f'<meta name="twitter:title" content="{t}">\n'
        f'<meta name="twitter:description" content="{d}">'
    )


SITE_NAME = "FreeAPIHub"
SITE_TAGLINE = ""
BASE = ""


def head_block(cfg, base: str, title: str, description: str, canonical_path: str,
               robots: str = "index, follow") -> str:
    base_url = (cfg.get("site_url", "").rstrip("/") + cfg.get("site_url_path", "")).rstrip("/")
    canonical = f"{base_url}{canonical_path}"
    head = render(load_template("components/head.html"), {
        "TITLE": esc(title),
        "META_DESCRIPTION": esc(truncate(description, 158)),
        "CANONICAL": esc(canonical),
        "ROBOTS_META": f'<meta name="robots" content="{robots}">' if robots != "index, follow" else "",
        "OG_TAGS": og_tags(title, description, canonical),
        "ASSET": base,
    })
    return head


def api_card(api: dict, base: str) -> str:
    https_badge = '<span class="badge">HTTPS</span>' if api.get("https") else ""
    cors = api.get("cors")
    cors_badge = f'<span class="badge">CORS: {esc(cors)}</span>' if cors in ("yes", "no") else ""
    auth = api.get("authentication", "unknown")
    auth_badge = "" if auth in ("unknown", "") else \
        f'<span class="badge badge-auth">{esc(auth_label(auth))}</span>'
    return render(load_template("components/api_card.html"), {
        "BASE": base,
        "SLUG": esc(api["slug"]),
        "NAME": esc(api["name"]),
        "DESCRIPTION": esc(truncate(api.get("description", ""), 220)),
        "CATEGORY_SLUG": esc(api.get("category", "other")),
        "CATEGORY_NAME": esc(category_name(api.get("category", "other"))),
        "AUTH_BADGE": auth_badge,
        "HTTPS_BADGE": https_badge,
        "CORS_BADGE": cors_badge,
        "STATUS_BADGE": status_badge(api) if api.get("status") != "verified" else "",
    })


def related_apis(api: dict, database: list[dict], limit: int = 6) -> list[dict]:
    """Deterministic related-API selection: category match, tag overlap,
    feature overlap. Never an LLM call (spec #18)."""
    my_tags = set(api.get("tags") or [])
    my_features = set()
    for f in api.get("features") or []:
        my_features |= set(re.findall(r"[a-z]{4,}", f.lower()))
    scored = []
    for other in database:
        if other.get("slug") == api.get("slug"):
            continue
        if other.get("status") == "dead":
            continue
        score = 0.0
        if other.get("category") == api.get("category"):
            score += 3
        score += 1.2 * len(my_tags & set(other.get("tags") or []))
        other_features = set()
        for f in other.get("features") or []:
            other_features |= set(re.findall(r"[a-z]{4,}", f.lower()))
        score += 0.6 * len(my_features & other_features)
        if score <= 0:
            continue
        scored.append((score, other["slug"], other))
    scored.sort(key=lambda t: (-t[0], t[1]))
    result = [s[2] for s in scored[:limit]]
    if len(result) < 3:  # fill deterministically for healthy internal linking
        filler = sorted(
            (o for o in database
             if o.get("slug") != api.get("slug")
             and o.get("status") != "dead"
             and o not in result),
            key=lambda o: (-(o.get("quality_score") or 0), o["slug"]))
        result += filler[: 3 - len(result)]
    return result


# ----------------------------------------------------------------- blocks -----

def features_section(api: dict) -> str:
    features = api.get("features") or []
    if not features:
        return ""
    items = "".join(f"<li>{esc(f)}</li>" for f in features[:8])
    return f'<section class="api-section" aria-label="Features"><h2>Key features</h2><ul class="feature-list">{items}</ul></section>'


def use_cases_section(api: dict) -> str:
    cases = api.get("use_cases") or []
    if not cases:
        return ""
    items = "".join(f"<li>{esc(c)}</li>" for c in cases[:6])
    return f'<section class="api-section" aria-label="What can you build"><h2>What can you build?</h2><ul class="use-case-list">{items}</ul></section>'


def code_block(label: str, code: str) -> str:
    return (f'<p class="code-label">{esc(label)}</p>'
            f'<pre class="code-block"><code>{esc(code)}</code></pre>')


def examples_section(api: dict) -> str:
    """Only verified, officially documented examples are shown (spec #7)."""
    parts = []
    if api.get("example_request"):
        parts.append(code_block("Example request", api["example_request"]))
    if api.get("example_response"):
        parts.append(code_block("Example response", api["example_response"]))
    if not parts:
        return ""
    body = "".join(parts)
    return (f'<section class="api-section" aria-label="Examples"><h2>Examples</h2>'
            f'<p class="section-note">From the official documentation. Confirm current formats there.</p>{body}</section>')


def source_links(api: dict) -> str:
    links, seen = [], set()
    for label, url in (("Official website", api.get("official_url")),
                       ("Documentation", api.get("documentation_url")),
                       ("Pricing", api.get("pricing_url"))):
        if url and url not in seen:
            seen.add(url)
            links.append(f'<li><a href="{esc(url)}" rel="noopener">{esc(label)}</a> '
                         f'<span class="text-faint">({esc(url_domain(url))})</span></li>')
    return "".join(links)


def external(url: str) -> str:
    return (f'<a href="{esc(url)}" rel="noopener nofollow">{esc(url)}</a>'
            if url else "Unknown")


# ------------------------------------------------------------------ pages -----

def build_home(cfg, ads, database):
    base = "."  # relative to site root - works on any static host
    non_dead = [a for a in database if a.get("status") != "dead"]
    cats_count = {}
    for a in non_dead:
        cats_count[a.get("category", "other")] = cats_count.get(a.get("category", "other"), 0) + 1

    free_count = sum(1 for a in non_dead
                     if (a.get("free_tier") not in (None, "", "unknown")) or a.get("authentication") == "none")
    recent = [a for a in non_dead if days_between(a.get("date_added", "1970-01-01")) <= 30]

    cat_cards = []
    for slug in sorted(cats_count, key=lambda s: (-cats_count[s], s))[:12]:
        cat_cards.append(
            f'<a class="category-card" href="{base}/category/{esc(slug)}/">'
            f'<span class="cc-name">{esc(category_name(slug))}</span>'
            f'<span class="cc-count">{cats_count[slug]} APIs</span></a>')

    latest = sorted(non_dead, key=lambda a: (a.get("date_added", ""), a["slug"]), reverse=True)[:int(cfg.get("home_latest_count", 12))]
    latest_cards = "".join(api_card(a, base) for a in latest)

    featured = sorted((a for a in non_dead if a.get("quality_score")),
                      key=lambda a: (-a.get("quality_score", 0), a["slug"]))[:int(cfg.get("home_featured_count", 8))]
    featured_cards = "".join(api_card(a, base) for a in featured)

    title = f"{cfg.get('site_name', SITE_NAME)} - {cfg.get('site_tagline', '')}"
    desc = cfg.get("site_description", "")
    page = render(load_template("index.html"), {
        "BASE": base,
        "ASSET": base,
        "HEAD": head_block(cfg, base, title, desc, "/"),
        "HEADER": header_html(cfg, base),
        "FOOTER": footer_html(cfg, ads, base),
        "AD_TOP": ad_slot(ads, "top"),
        "AD_AFTER_INTRO": ad_slot(ads, "after_intro"),
        "SITE_TAGLINE": esc(cfg.get("site_tagline", "")),
        "API_COUNT": str(len(non_dead)),
        "TOTAL_APIS": str(len(non_dead)),
        "TOTAL_CATEGORIES": str(len(cats_count)),
        "FREE_APIS": str(free_count),
        "RECENT_COUNT": str(len(recent)),
        "CATEGORY_CARDS": "".join(cat_cards),
        "LATEST_CARDS": latest_cards,
        "FEATURED_CARDS": featured_cards,
    })
    write_page(base, "index.html", page)


def build_categories_index(cfg, ads, database):
    base = "."
    counts = {}
    for a in database:
        if a.get("status") != "dead":
            counts[a.get("category", "other")] = counts.get(a.get("category", "other"), 0) + 1
    cards = []
    for slug in sorted(counts, key=lambda s: (-counts[s], s)):
        cards.append(
            f'<a class="category-card" href="{base}/category/{esc(slug)}/">'
            f'<span class="cc-name">{esc(category_name(slug))}</span>'
            f'<span class="cc-count">{counts[slug]} APIs</span></a>')
    if not cards:
        cards = ['<p class="prose">No categories published yet.</p>']
    title = f"All categories - {cfg.get('site_name', SITE_NAME)}"
    page = render(load_template("page.html"), {
        "BASE": base,
        "ASSET": base,
        "HEAD": head_block(cfg, base, title, "Browse every API category in the directory.", "/categories.html"),
        "HEADER": header_html(cfg, base),
        "FOOTER": footer_html(cfg, ads, base),
        "AD_TOP": ad_slot(ads, "top"),
        "PAGE_TITLE": "All categories",
        "PAGE_SUBTITLE": "Every category currently listed in the directory.",
        "PAGE_CONTENT": f'<div class="categories-index">{"".join(cards)}</div>',
    })
    write_page(base, "categories.html", page)


def build_category_pages(cfg, ads, database):
    page_size = int(cfg.get("category_page_size", 24))
    base = "../.."  # site/category/{slug}/ -> site root
    by_category = {}
    for a in sorted(database, key=lambda x: (-(x.get("quality_score") or 0), x["slug"])):
        if a.get("status") == "dead":
            continue
        by_category.setdefault(a.get("category", "other"), []).append(a)

    for slug, apis in sorted(by_category.items()):
        chunks = [apis[i:i + page_size] for i in range(0, len(apis), page_size)] or [[]]
        for page_num, chunk in enumerate(chunks, start=1):
            filename = "index.html" if page_num == 1 else f"page-{page_num}.html"
            canonical = f"/category/{slug}/" if page_num == 1 else f"/category/{slug}/page-{page_num}/"
            cards = "".join(api_card(a, base) for a in chunk)

            pagination = ""
            if len(chunks) > 1:
                parts = []
                for p in range(1, len(chunks) + 1):
                    if p == page_num:
                        parts.append(f'<span class="current">{p}</span>')
                    else:
                        href = "../" if p == 1 else f"page-{p}/"
                        parts.append(f'<a href="{href}">{p}</a>')
                pagination = f'<nav class="pagination" aria-label="Pagination">{"".join(parts)}</nav>'

            other_chips = "".join(
                f'<a class="chip" href="{base}/category/{esc(s)}/">{esc(category_name(s))}</a>'
                for s in sorted(by_category) if s != slug)[:4000]

            title = f"{category_name(slug)} APIs - {cfg.get('site_name', SITE_NAME)}"
            description = f"Browse {len(apis)} {category_name(slug).lower()} APIs. " + category_description(slug)
            page = render(load_template("category.html"), {
                "BASE": base,
                "ASSET": base,
                "HEAD": head_block(cfg, base, title, description, canonical),
                "HEADER": header_html(cfg, base),
                "FOOTER": footer_html(cfg, ads, base),
                "AD_TOP": ad_slot(ads, "top"),
                "AD_AFTER_INTRO": ad_slot(ads, "after_intro"),
                "CATEGORY_NAME": esc(category_name(slug)),
                "CATEGORY_DESCRIPTION": esc(category_description(slug)),
                "API_COUNT": str(len(apis)),
                "CARD_GRID": cards,
                "PAGINATION": pagination,
                "OTHER_CATEGORY_CHIPS": other_chips,
            })
            write_page(base, f"category/{slug}/{filename}", page)


def build_api_pages(cfg, ads, database):
    base = "../.."
    for api in database:
        try:
            build_api_page(cfg, ads, database, api, base)
        except Exception as exc:
            # One broken page must not block the rest of the build (spec #28).
            LOG.warning("API page build failed for '%s': %s - page skipped.", api.get("slug"), exc)


def build_api_page(cfg, ads, database, api, base):
    slug = api["slug"]
    name = api["name"]
    cat_slug = api.get("category", "other")
    related = related_apis(api, database)
    related_cards = "".join(api_card(a, base) for a in related) or \
        '<p class="section-note">Related listings will appear as the directory grows.</p>'

    status = api.get("status", "verified")
    notice = ""
    if status != "verified":
        css = "warning" if status in ("warning", "temporarily_unavailable") else "danger"
        notice = f'<p class="status-notice {css}">{esc(STATUS_LONG.get(status, status))}</p>'

    primary = f'<a class="button" href="{esc(api.get("official_url", ""))}" rel="noopener">Visit website</a>'
    docs_button = (f'<a class="button button-outline" href="{esc(api["documentation_url"])}" rel="noopener">Read documentation</a>'
                   if api.get("documentation_url") else "")

    pricing_row = ""
    if api.get("pricing_url"):
        pricing_row = f'<div class="info-row"><dt>Pricing</dt><dd>{external(api["pricing_url"])}</dd></div>'

    https_label = "Yes" if api.get("https") else "No / unknown"
    cors_label = {"yes": "Yes", "no": "No", "unknown": "Unknown"}.get(api.get("cors"), "Unknown")

    title = api.get("seo_title") or f"{name} API - free tier, docs & features"
    description = api.get("meta_description") or truncate(api.get("description", ""), 155)

    jsonld = build_jsonld(cfg, api, related)

    page = render(load_template("api.html"), {
        "BASE": base,
        "ASSET": base,
        "HEAD": head_block(cfg, base, title, description, f"/api/{slug}/"),
        "HEADER": header_html(cfg, base),
        "FOOTER": footer_html(cfg, ads, base),
        "AD_TOP": ad_slot(ads, "top"),
        "AD_AFTER_INTRO": ad_slot(ads, "after_intro"),
        "MIDDLE_AD_SLOT": ad_slot(ads, "middle"),
        "AD_BEFORE_RELATED": ad_slot(ads, "before_related"),
        "NAME": esc(name),
        "CATEGORY_SLUG": esc(cat_slug),
        "CATEGORY_NAME": esc(category_name(cat_slug)),
        "DESCRIPTION": esc(api.get("description", "")),
        "STATUS_BADGE": status_badge(api),
        "PRIMARY_BUTTON": primary,
        "DOCS_BUTTON": docs_button,
        "LAST_VERIFIED": esc(api.get("last_verified", "")),
        "OFFICIAL_LINK": external(api.get("official_url", "")),
        "DOCS_LINK": external(api.get("documentation_url", "")),
        "AUTH_LABEL": esc(auth_label(api.get("authentication"))),
        "HTTPS_LABEL": https_label,
        "CORS_LABEL": cors_label,
        "FREE_TIER": esc(api.get("free_tier") or "Unknown"),
        "PRICING_ROW": pricing_row,
        "LICENSE": esc(api.get("license") or "Unknown"),
        "STATUS_LONG": esc(STATUS_LONG.get(status, status)),
        "STATUS_NOTICE": notice,
        "FEATURES_SECTION": features_section(api),
        "EXAMPLES_SECTION": examples_section(api),
        "USE_CASES_SECTION": use_cases_section(api),
        "RELATED_CARDS": related_cards,
        "SOURCE_LINKS": source_links(api),
        "JSONLD": jsonld,
    })
    write_page(base, f"api/{slug}/index.html", page)


def build_jsonld(cfg, api, related) -> str:
    base_url = (cfg.get("site_url", "").rstrip("/") + cfg.get("site_url_path", "")).rstrip("/")
    page_url = f"{base_url}/api/{api['slug']}/"
    data = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebPage",
                "name": api.get("seo_title") or api["name"],
                "description": api.get("meta_description") or api.get("description", ""),
                "url": page_url,
                "about": {"@type": "SoftwareApplication",
                          "name": api["name"],
                          "applicationCategory": "WebAPI",
                          "operatingSystem": "Web",
                          "url": api.get("official_url", "")},
            },
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Home", "item": base_url + "/"},
                    {"@type": "ListItem", "position": 2, "name": category_name(api.get("category", "other")),
                     "item": f"{base_url}/category/{api.get('category', 'other')}/"},
                    {"@type": "ListItem", "position": 3, "name": api["name"], "item": page_url},
                ],
            },
        ],
    }
    return f'<script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}</script>'


# -------------------------------------------------------------- static pages --

def static_pages(cfg, ads) -> dict:
    base = ""
    return {
        "about.html": {
            "title": "About",
            "subtitle": "What this directory is and how it is maintained.",
            "robots": "index, follow",
            "content": """
<div class="prose">
  <h2>What is this site?</h2>
  <p>This site is an automatically maintained directory of public APIs. It helps developers find useful APIs quickly, understand what each one offers, and jump straight to official documentation. The catalog focuses on APIs that document free access or a free tier, because those are the ones most useful for side projects, prototypes and learning.</p>
  <h2>How listings are created</h2>
  <p>New candidates are discovered from legitimate public sources such as curated API lists and open API directories. Before anything is published, an automated verification step checks that the official website and documentation actually exist, use HTTPS, and clearly describe an API. Entries that fail verification are not published - quality matters more than volume, so some days the directory adds nothing at all.</p>
  <h2>How information is written</h2>
  <p>Summaries are generated from the official source material only: the site never invents facts, pricing, limits or examples. Where the source does not document something, the listing says so explicitly rather than guessing. Example requests and responses appear only when they come from official documentation.</p>
  <h2>How listings are maintained</h2>
  <p>A scheduled job re-checks existing listings against their official sources. Listings that stop responding are flagged, shown with a clear status notice, and eventually archived if they stay unreachable. Nothing is deleted on the first failure, because temporary outages are normal.</p>
  <h2>Important limitations</h2>
  <ul>
    <li>Information can change at any time. Third-party APIs change pricing, limits and features without notice.</li>
    <li>Always verify critical details - especially pricing and rate limits - with the official documentation before committing to an API.</li>
    <li>External links lead to third-party services that this site does not control.</li>
  </ul>
  <h2>Transparency</h2>
  <p>The site runs on scheduled automation and static hosting. Listing summaries are produced with the help of automated tooling under strict no-invention rules, and every page links back to its official sources so you can check the original information yourself.</p>
</div>
"""
        },
        "submit.html": {
            "title": "Submit an API",
            "subtitle": "Suggest an API for the directory.",
            "robots": "index, follow",
            "content": """
<div class="prose">
  <p>Know a great public API that developers should discover? Submit it and - if it passes automated verification against its official website and documentation - it will be listed. Submissions are never published automatically without validation.</p>
  <h2>How to submit</h2>
  <p>Open an issue in the project's GitHub repository using the <strong>API submission</strong> template, and include:</p>
  <ul>
    <li><strong>API name</strong> - the official name</li>
    <li><strong>Official website</strong> - the main URL</li>
    <li><strong>Documentation</strong> - link to the API docs</li>
    <li><strong>Category</strong> - the best fit (e.g. weather, finance, AI)</li>
    <li><strong>Description</strong> - one or two factual sentences</li>
    <li><strong>Free tier</strong> - what is available for free, if anything</li>
  </ul>
  <p>If you cannot use GitHub, email the same details to the site owner address listed in the repository.</p>
  <h2>What happens next</h2>
  <p>Every submission goes through the same pipeline as any other candidate: reachability checks, HTTPS validation, and confirmation that the documentation genuinely describes an API. If verification fails, the listing is not published. Duplicate submissions are merged with existing entries.</p>
  <h2>What is NOT accepted</h2>
  <ul>
    <li>APIs without official documentation</li>
    <li>APIs whose free tier cannot be confirmed from official sources</li>
    <li>Malicious, illegal or privacy-violating services</li>
    <li>Pure resellers of another API with no added documentation</li>
  </ul>
</div>
"""
        },
        "privacy.html": {
            "title": "Privacy",
            "subtitle": "The short version: this static site collects nothing.",
            "robots": "index, follow",
            "content": """
<div class="prose">
  <h2>Overview</h2>
  <p>This website is a static directory. It has no user accounts, no comment system and no server-side processing of personal data. This page describes the limited ways data may be involved when you visit.</p>
  <h2>Data collected by this site</h2>
  <p>The site itself does not collect, store or share personal information. There is no login, no newsletter and no contact form that stores submissions. Your theme preference (light or dark mode) is stored locally in your browser only.</p>
  <h2>Third-party advertising</h2>
  <p>Advertising slots on this site may be served by third-party networks such as Google AdSense. If ads are enabled, those networks may use cookies or similar technologies to measure and personalize ads, subject to their own privacy policies. The site owner does not control and does not have access to data collected by advertising providers. You can typically opt out of personalized advertising through your ad settings (for example, Google Ads Settings).</p>
  <h2>External links</h2>
  <p>API pages link to third-party websites and services. This site has no control over the content or privacy practices of those services and is not responsible for them. When you follow an external link, that provider's own privacy policy applies.</p>
  <h2>Hosting</h2>
  <p>The site is hosted by a static hosting provider (for example GitHub Pages). Standard hosting infrastructure may log requests (such as IP addresses) for security and operational purposes. See the hosting provider's own documentation for details.</p>
  <h2>Changes</h2>
  <p>If the site ever begins collecting data beyond what is described here, this page will be updated to say so plainly. No fabricated compliance claims are made; for questions, use the contact channel listed in the project repository.</p>
</div>
"""
        },
        "terms.html": {
            "title": "Terms of Use",
            "subtitle": "Simple, honest terms for using this directory.",
            "robots": "index, follow",
            "content": """
<div class="prose">
  <h2>Acceptance</h2>
  <p>By using this website you agree to these terms. If you do not agree, please do not use the site.</p>
  <h2>Nature of the content</h2>
  <p>This site aggregates and summarizes publicly available information about third-party APIs. That information can change at any time and may become outdated. The site does not guarantee the availability, pricing, limits, security or legality of any listed API. Always verify critical information - especially pricing and rate limits - with the API's official documentation before relying on it.</p>
  <h2>No endorsement</h2>
  <p>A listing in this directory is not an endorsement, partnership or certification. Status badges reflect automated reachability checks only and are provided as-is.</p>
  <h2>Use of APIs</h2>
  <p>When you use any API discovered through this site, you do so directly with that provider under their terms. You are responsible for complying with the provider's terms of service, rate limits and usage policies. This site never proxies API requests.</p>
  <h2>Intellectual property</h2>
  <p>API names and trademarks belong to their respective owners. Listings contain short, factual summaries and links to official sources; no third-party documentation is reproduced wholesale. Report concerns through the repository's issue tracker.</p>
  <h2>Advertising</h2>
  <p>The site may display advertising provided by the site owner's chosen networks. Ads are clearly separated from content and are never manipulated or auto-interacted with.</p>
  <h2>Limitation of liability</h2>
  <p>The site is provided "as is" without warranties of any kind. To the maximum extent permitted by law, the site owner is not liable for any damages arising from use of the site or from decisions made based on its listings.</p>
</div>
"""
        },
        "404.html": {
            "title": "Page not found",
            "subtitle": "The page you are looking for does not exist.",
            "robots": "noindex, follow",
            "content": """
<div class="prose">
  <p>The page may have been renamed, archived, or it may never have existed. Nothing was lost - try one of these instead:</p>
  <ul>
    <li><a href="./">Go to the homepage</a></li>
    <li><a href="search.html">Search the API directory</a></li>
    <li><a href="categories.html">Browse all categories</a></li>
  </ul>
</div>
"""
        },
    }


def build_static_pages(cfg, ads):
    base = "."
    for filename, spec in static_pages(cfg, ads).items():
        title = f"{spec['title']} - {cfg.get('site_name', SITE_NAME)}"
        page = render(load_template("page.html"), {
            "BASE": base,
            "ASSET": base,
            "HEAD": head_block(cfg, base, title, spec["subtitle"], f"/{filename}", spec["robots"]),
            "HEADER": header_html(cfg, base),
            "FOOTER": footer_html(cfg, ads, base),
            "AD_TOP": ad_slot(ads, "top"),
            "PAGE_TITLE": esc(spec["title"]),
            "PAGE_SUBTITLE": esc(spec["subtitle"]),
            "PAGE_CONTENT": spec["content"],
        })
        write_page(base, filename, page)


def build_search_page(cfg, ads, database):
    base = "."
    counts = {}
    for a in database:
        if a.get("status") != "dead":
            counts[a.get("category", "other")] = counts.get(a.get("category", "other"), 0) + 1
    top = sorted(counts, key=lambda s: (-counts[s], s))[:10]
    chips = '<button type="button" class="filter-chip" data-category="" aria-pressed="true">All</button>'
    chips += "".join(
        f'<button type="button" class="filter-chip" data-category="{esc(s)}" aria-pressed="false">'
        f'{esc(category_name(s))}</button>' for s in top)

    content = f"""
<div class="search-bar">
  <label class="visually-hidden" for="search-input">Search APIs</label>
  <input id="search-input" type="search" data-index="data/search.json"
         placeholder="Search by name, category, tag or feature&hellip;" autocomplete="off" autofocus>
</div>
<div id="filter-row" class="filter-row" role="group" aria-label="Filter by category">{chips}</div>
<p id="search-meta" class="search-meta" role="status" aria-live="polite">Loading search index&hellip;</p>
<div id="search-results" class="search-results"></div>
"""
    title = f"Search APIs - {cfg.get('site_name', SITE_NAME)}"
    page = render(load_template("page.html"), {
        "BASE": base,
        "ASSET": base,
        "HEAD": head_block(cfg, base, title, "Search every API in the directory by name, category, tag or feature.", "/search.html"),
        "HEADER": header_html(cfg, base),
        "FOOTER": footer_html(cfg, ads, base,
                              page_script='<script src="assets/js/search.js" defer></script>'),
        "AD_TOP": ad_slot(ads, "top"),
        "PAGE_TITLE": "Search APIs",
        "PAGE_SUBTITLE": "Fast client-side search across the entire directory - no tracking, no server.",
        "PAGE_CONTENT": content,
    })
    write_page(base, "search.html", page)


def build_search_index(database):
    """Lightweight index for client-side search (spec #16)."""
    entries = []
    for a in sorted(database, key=lambda x: x["slug"]):
        if a.get("status") == "dead":
            continue
        entries.append({
            "name": a.get("name", ""),
            "url": f"api/{a['slug']}/",
            "description": truncate(a.get("description", ""), 200),
            "category": a.get("category", "other"),
            "category_url": f"category/{a.get('category', 'other')}/",
            "tags": (a.get("tags") or [])[:6],
            "features": [truncate(f, 60) for f in (a.get("features") or [])[:5]],
            "auth": a.get("authentication", "unknown"),
            "https": bool(a.get("https")),
            "status": a.get("status", "verified"),
        })
    save_json(SITE_DIR / "data" / "search.json", {
        "generated": today_str(),
        "count": len(entries),
        "apis": entries,
    })
    return len(entries)


# ------------------------------------------------------------------ output ----

def write_page(base: str, relpath: str, content: str) -> None:
    path = SITE_DIR / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def run_build(cfg: dict | None = None) -> dict:
    global SITE_NAME, SITE_TAGLINE
    cfg = cfg or load_config()
    ads = load_ads()
    database = load_json(DATA_DIR / "apis.json", {"apis": []}).get("apis", [])

    SITE_NAME = cfg.get("site_name", "FreeAPIHub")
    SITE_TAGLINE = cfg.get("site_tagline", "")

    build_home(cfg, ads, database)
    build_categories_index(cfg, ads, database)
    build_category_pages(cfg, ads, database)
    build_api_pages(cfg, ads, database)
    build_static_pages(cfg, ads)
    build_search_page(cfg, ads, database)
    search_count = build_search_index(database)

    stats = {
        "apis": len(database),
        "search_entries": search_count,
        "pages_built": 1 + 1 + 1 + len(database) + 5,
    }
    LOG.info("Site build complete: %d API pages, %d search entries.", len(database), search_count)
    return stats


def main() -> int:
    run_build()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

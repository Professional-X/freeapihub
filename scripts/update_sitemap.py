"""FreeAPIHub sitemap.xml + robots.txt generator (spec #20, #21).

Regenerated automatically whenever pages change. Never includes duplicate
URLs. Dead/archived listings are dropped from the sitemap after the configured
grace period (their pages remain online with a status notice).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from utils import (SITE_DIR, DATA_DIR, LOG, load_json, load_config, today_str,
                   days_between, base_url)

STATIC_PATHS = [
    ("/", "daily", 1.0),
    ("/categories.html", "weekly", 0.8),
    ("/search.html", "weekly", 0.7),
    ("/about.html", "monthly", 0.5),
    ("/submit.html", "monthly", 0.5),
    ("/privacy.html", "yearly", 0.3),
    ("/terms.html", "yearly", 0.3),
]


def collect_urls(database: list[dict], cfg: dict) -> list[dict]:
    base = base_url(cfg)
    urls: list[dict] = []
    seen: set[str] = set()

    def add(path: str, changefreq: str, priority: float, lastmod: str) -> None:
        url = f"{base}{path}"
        if url in seen:
            return
        seen.add(url)
        urls.append({"loc": url, "lastmod": lastmod, "changefreq": changefreq,
                     "priority": priority})

    today = today_str()
    for path, freq, prio in STATIC_PATHS:
        add(path, freq, prio, today)

    for api in database:
        status = api.get("status", "verified")
        if status == "dead":
            dead_days = days_between(api.get("last_updated") or api.get("last_verified") or "1970-01-01")
            if dead_days < int(cfg.get("archive_from_sitemap_after_days", 30)):
                # Keep it discoverable for a grace period, then de-index the page.
                add(f"/api/{api['slug']}/", "monthly", 0.2,
                    api.get("last_updated") or api.get("last_verified") or today)
            continue
        add(f"/api/{api['slug']}/", "weekly", 0.7,
            api.get("last_updated") or api.get("last_verified") or today)

    counts: dict[str, int] = {}
    for api in database:
        if api.get("status") == "dead":
            continue
        counts[api.get("category", "other")] = counts.get(api.get("category", "other"), 0) + 1
    for slug in sorted(counts):
        add(f"/category/{slug}/", "weekly", 0.6, today)

    return urls


def write_sitemap(cfg: dict, database: list[dict]) -> int:
    urls = collect_urls(database, cfg)
    ET.register_namespace("", "http://www.sitemaps.org/schemas/sitemap/0.9")
    root = ET.Element("urlset", {"xmlns": "http://www.sitemaps.org/schemas/sitemap/0.9"})
    for entry in urls:
        node = ET.SubElement(root, "url")
        ET.SubElement(node, "loc").text = entry["loc"]
        ET.SubElement(node, "lastmod").text = entry["lastmod"]
        ET.SubElement(node, "changefreq").text = entry["changefreq"]
        ET.SubElement(node, "priority").text = f"{entry['priority']:.1f}"
    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    out = SITE_DIR / "sitemap.xml"
    out.parent.mkdir(parents=True, exist_ok=True)
    tree.write(out, encoding="utf-8", xml_declaration=True)
    with open(out, "a", encoding="utf-8") as fh:
        fh.write("\n")
    return len(urls)


def write_robots(cfg: dict) -> None:
    sitemap_url = f"{base_url(cfg)}/sitemap.xml"
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "\n"
        f"Sitemap: {sitemap_url}\n"
    )
    (SITE_DIR / "robots.txt").write_text(content, encoding="utf-8")


def run_sitemap(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    database = load_json(DATA_DIR / "apis.json", {"apis": []}).get("apis", [])
    count = write_sitemap(cfg, database)
    write_robots(cfg)
    LOG.info("Sitemap: %d URLs. robots.txt written.", count)
    return {"sitemap_urls": count}


def main() -> int:
    run_sitemap()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

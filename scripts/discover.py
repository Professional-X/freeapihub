"""FreeAPIHub discovery engine.

Finds potential APIs from legitimate, configurable public sources (spec #9)
and appends new candidates to the persistent queue after deterministic
duplicate detection (spec #10). Duplicate detection never uses the LLM.

Supported source types:
    github_readme  - markdown tables like public-apis/public-apis
    apis_guru      - https://api.apis.guru/v2/list.json JSON index
    github_search  - GitHub repository search API

If one source fails the run continues with the remaining sources (spec #28).
"""

from __future__ import annotations

import json
import re

from utils import (DATA_DIR, LOG, fetch_url, load_json, save_json, today_str,
                   normalize_url, url_domain, find_duplicate, similarity,
                   normalize_name, env)


# ------------------------------------------------------------------ parsers --

def _parse_github_readme(text: str, limit: int) -> list[dict]:
    """Parse the public-apis style tables:
    | [API](link) | Description | Auth | HTTPS | CORS |"""
    candidates, current_category = [], "other"
    seen_urls = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Category headings appear OUTSIDE the tables ("## Animals") - they
        # must be handled before the table-row filter below.
        if line.startswith("#"):
            title = re.sub(r"^#+\s*", "", line)
            current_category = _category_from_words(title)
            continue
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        link_match = re.match(r"^\[(.+?)\]\((https?://[^)\s]+)\)", cells[0])
        if not link_match:
            continue
        name, official_url = link_match.group(1), link_match.group(2)
        description, auth, https_flag, cors = cells[1], cells[2], cells[3], cells[4]
        if not name or not official_url:
            continue
        norm = normalize_url(official_url)
        if norm in seen_urls:
            continue
        seen_urls.add(norm)
        candidates.append({
            "name": name[:120],
            "official_url": official_url,
            "documentation_url": "",
            "repo_url": "",
            "description": _strip_markdown(description)[:400],
            "category_hint": current_category,
            "auth_hint": _normalize_auth(auth),
            "https_hint": https_flag.strip().lower() == "yes",
            "cors_hint": cors.strip().lower() if cors.strip().lower() in ("yes", "no") else "unknown",
            "source": "public-apis",
        })
        if len(candidates) >= limit:
            break
    return candidates


def _parse_apis_guru(data: dict, limit: int) -> list[dict]:
    """apis.guru list.json: {provider: {api_id: {info: {...}, ...}}}"""
    candidates = []
    for provider, apis in (data or {}).items():
        if not isinstance(apis, dict):
            continue
        for api_id, meta in apis.items():
            try:
                info = meta.get("info", {})
                preferred = meta.get("preferred", "")
                swagger_url = meta.get("versions", {}).get(preferred, {}).get("swaggerUrl", "")
                contact_url = ""
                contact = info.get("contact", {}) or {}
                if isinstance(contact.get("url"), str):
                    contact_url = contact["url"]
                official_url = contact_url or info.get("x-logo", {}).get("url", "") or swagger_url
                if not official_url or not official_url.startswith("http"):
                    continue
                title = info.get("title") or api_id
                description = info.get("description") or ""
                if not description:
                    continue
                candidates.append({
                    "name": title[:120],
                    "official_url": official_url,
                    "documentation_url": swagger_url if swagger_url.startswith("http") else "",
                    "repo_url": "",
                    "description": _strip_markdown(description)[:400],
                    "category_hint": "other",
                    "auth_hint": "unknown",
                    "https_hint": True,
                    "cors_hint": "unknown",
                    "source": "apis-guru",
                })
            except (AttributeError, TypeError):
                continue
            if len(candidates) >= limit:
                return candidates
    return candidates


def _parse_github_search(data: dict, limit: int) -> list[dict]:
    candidates = []
    for item in (data or {}).get("items", [])[:limit]:
        description = item.get("description") or ""
        if not description:
            continue
        homepage = (item.get("homepage") or "").strip()
        topics = item.get("topics") or []
        candidates.append({
            "name": (item.get("name") or "").replace("-", " ").title()[:120],
            "official_url": homepage if homepage.startswith("http") else f"https://github.com/{item.get('full_name', '')}",
            "documentation_url": f"https://github.com/{item.get('full_name', '')}#readme",
            "repo_url": f"https://github.com/{item.get('full_name', '')}",
            "description": description[:400],
            "category_hint": _category_from_words(" ".join(topics) + " " + description[:200]),
            "auth_hint": "unknown",
            "https_hint": True,
            "cors_hint": "unknown",
            "source": "github-search",
        })
    return candidates


# ----------------------------------------------------------------- helpers ----

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_BOLD = re.compile(r"\*\*|__")
_MD_FLAG = re.compile(r"[\u2705\u274c\u2757\u203c\ufe0f]|!\[\]\([^)]*\)")


def _strip_markdown(text: str) -> str:
    text = _MD_FLAG.sub("", text or "")
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_BOLD.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_auth(auth: str) -> str:
    auth = (auth or "").strip().lower()
    if not auth or auth in ("", "no"):
        return "none"
    for token, value in (("apikey", "api-key"), ("api key", "api-key"),
                         ("oauth", "oauth"), ("user", "user-password")):
        if token in auth:
            return value
    return "unknown"


CATEGORY_KEYWORDS = {
    "ai": ["ai", "artificial intelligence", "machine learning", "nlp", "language model", "chatbot", "gpt"],
    "finance": ["finance", "currency", "exchange rate", "stock", "crypto", "payment", "banking"],
    "weather": ["weather", "climate", "forecast", "meteorolog"],
    "maps": ["map", "geocod", "location", "places", "routing", "geograph"],
    "images": ["image", "photo", "picture", "screenshot"],
    "video": ["video", "movie", "stream", "youtube"],
    "audio": ["audio", "music", "song", "podcast", "sound"],
    "developer-tools": ["developer", "code", "programming", "sdk", "hosting", "testing", "git", "ip"],
    "education": ["education", "learning", "dictionary", "dictionary", "school", "quiz"],
    "sports": ["sport", "football", "soccer", "basketball", "cricket", "esports"],
    "news": ["news", "headline", "article", "press"],
    "entertainment": ["game", "anime", "meme", "comic", "joke", "pokemon", "movie db"],
    "productivity": ["todo", "task", "calendar", "notes", "email"],
    "science": ["space", "nasa", "astronomy", "science", "chemistry", "physics", "biology"],
    "security": ["security", "vulnerab", "breach", "malware", "cve"],
    "government": ["government", "holiday", "census", "public data", "open data"],
    "transportation": ["transport", "flight", "train", "transit", "fuel", "taxi"],
    "e-commerce": ["shop", "commerce", "product", "price", "store"],
    "books": ["book", "library", "reading", "literature"],
    "food": ["food", "recipe", "nutrition", "restaurant", "beer", "wine"],
    "health": ["health", "medical", "disease", "fitness", "nutrition"],
}


def _category_from_words(text: str) -> str:
    text = (text or "").lower()
    best_slug, best_hits = "other", 0
    for slug, keywords in CATEGORY_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits > best_hits:
            best_slug, best_hits = slug, hits
    return best_slug


# -------------------------------------------------------------------- main ----

def fetch_candidates_from_source(source: dict) -> list[dict]:
    stype = source.get("type")
    url = source.get("url", "")
    limit = int(source.get("max_items", 25))
    result = fetch_url(url, timeout=25, max_retries=2, max_text=3_000_000)
    if not result.ok:
        LOG.warning("Source '%s' unavailable (%s %s) - skipping.",
                    source.get("id"), result.status, result.error or "")
        return []
    try:
        if stype == "github_readme":
            return _parse_github_readme(result.text, limit)
        if stype == "apis_guru":
            return _parse_apis_guru(json.loads(result.text), limit)
        if stype == "github_search":
            from urllib.parse import urlencode
            params = urlencode({
                "q": source.get("query", "topic:api"),
                "sort": source.get("sort", "updated"),
                "per_page": limit,
                "order": "desc",
            })
            search_result = fetch_url(f"{url}?{params}", timeout=25, max_retries=1,
                                      max_text=2_000_000)
            if not search_result.ok:
                LOG.warning("GitHub search failed (%s %s) - skipping.",
                            search_result.status, search_result.error)
                return []
            return _parse_github_search(json.loads(search_result.text), limit)
    except (json.JSONDecodeError, ValueError) as exc:
        LOG.warning("Source '%s' returned unparseable data (%s) - skipping.", source.get("id"), exc)
    LOG.warning("Unknown source type '%s' for source '%s'.", stype, source.get("id"))
    return []


def run_discovery(cfg: dict, database: list[dict], queue: list[dict]) -> dict:
    """Discover, normalise, deduplicate and enqueue. Returns stats."""
    sources_cfg = load_json(DATA_DIR / "sources.json", {"sources": []})
    stats = {"discovered": 0, "duplicates": 0, "enqueued": 0, "sources_ok": 0, "sources_failed": 0}
    max_new = int(cfg.get("discovery_max_new_per_run", 40))
    seen_batch_keys = set()

    for source in sources_cfg.get("sources", []):
        if not source.get("enabled", True):
            continue
        try:
            candidates = fetch_candidates_from_source(source)
            stats["sources_ok"] += 1
        except Exception as exc:  # never let one source kill the run
            LOG.warning("Source '%s' raised %s - skipping.", source.get("id"), exc)
            stats["sources_failed"] += 1
            continue
        stats["discovered"] += len(candidates)
        for cand in candidates:
            if stats["enqueued"] >= max_new:
                break
            cand.setdefault("category_hint", "other")
            cand["official_url"] = normalize_url(cand.get("official_url") or "")
            if not cand["official_url"] or not cand.get("name"):
                continue
            keys = {
                "url:" + normalize_url(cand["official_url"]),
                "name:" + normalize_name(cand.get("name", "")),
            }
            if cand.get("documentation_url"):
                keys.add("url:" + normalize_url(cand["documentation_url"]))
            if keys & seen_batch_keys:
                stats["duplicates"] += 1
                continue
            if find_duplicate(cand, database) or _in_queue(cand, queue, database):
                stats["duplicates"] += 1
                seen_batch_keys |= keys
                continue
            seen_batch_keys |= keys
            cand["added_date"] = today_str()
            cand["failed_attempts"] = 0
            queue.append(cand)
            stats["enqueued"] += 1
        if stats["enqueued"] >= max_new:
            break

    # Cap the queue so it stays a meaningful, bounded work list.
    queue_max = int(cfg.get("queue_max_size", 600))
    if len(queue) > queue_max:
        queue[:] = sorted(queue, key=lambda c: c.get("added_date", ""), reverse=True)[:queue_max]

    return stats


def _in_queue(candidate: dict, queue: list[dict], database: list[dict]) -> bool:
    """Queue-level duplicate check including domain+name similarity."""
    cand_url = normalize_url(candidate.get("official_url") or "")
    cand_name = normalize_name(candidate.get("name") or "")
    cand_domain = url_domain(cand_url)
    for item in queue:
        if normalize_url(item.get("official_url") or "") == cand_url:
            return True
        if normalize_name(item.get("name", "")) == cand_name:
            return True
        if cand_domain and url_domain(item.get("official_url") or "") == cand_domain:
            if similarity(cand_name, normalize_name(item.get("name", ""))) >= 0.85:
                return True
    return False


def main() -> int:
    from utils import load_config
    cfg = load_config()
    db = load_json(DATA_DIR / "apis.json", {"apis": []}).get("apis", [])
    queue = load_json(DATA_DIR / "queue.json", {"candidates": []}).get("candidates", [])
    stats = run_discovery(cfg, db, queue)
    save_json(DATA_DIR / "queue.json", {"candidates": queue})
    LOG.info("Discovery: discovered=%(discovered)d duplicates=%(duplicates)d "
             "enqueued=%(enqueued)d queue_size=%(queue)d",
             {**stats, "queue": len(queue)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

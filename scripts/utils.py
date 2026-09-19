"""FreeAPIHub shared utilities.

Everything deterministic lives here: configuration, JSON persistence, URL
normalisation, polite HTTP fetching, duplicate detection, quality scoring.
No LLM calls are ever made from this module (see llm.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
SITE_DIR = ROOT / "site"
TEMPLATES_DIR = ROOT / "templates"

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "ref", "ref_src", "igshid", "mc_cid", "mc_eid",
}

UA = "FreeAPIHub-Bot/1.0 (+automated API directory verifier; contact via repository issues)"


# ---------------------------------------------------------------- logging ---

def get_logger(name: str = "freeapihub") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    # Never leak credentials through debug logging of request objects.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return logger


LOG = get_logger()


# ------------------------------------------------------------------- dates ---

def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_between(iso_date: str, ref: str | None = None) -> int:
    """Whole days between two YYYY-MM-DD strings."""
    try:
        d1 = datetime.strptime(iso_date, "%Y-%m-%d")
        d2 = datetime.strptime(ref or today_str(), "%Y-%m-%d")
        return (d2 - d1).days
    except (ValueError, TypeError):
        return 0


# ------------------------------------------------------------------- json ----

def load_json(path: Path, default=None):
    if default is None:
        default = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError) as exc:
        LOG.warning("Could not parse %s (%s); using default.", path, exc)
        return default


def save_json(path: Path, data) -> None:
    """Atomic JSON write (tmp file + replace) so a crash never corrupts data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


# ----------------------------------------------------------------- config ----

def load_config() -> dict:
    """Site configuration with environment-variable overrides."""
    cfg = load_json(CONFIG_DIR / "site.json", {})
    overrides = {
        "site_url": os.environ.get("SITE_URL"),
        "site_url_path": os.environ.get("SITE_URL_PATH"),
        "llm_model": os.environ.get("LLM_MODEL") or None,
    }
    for key, value in overrides.items():
        if value:
            cfg[key] = value
    return cfg


def base_url(cfg: dict) -> str:
    url = (cfg.get("site_url") or "").rstrip("/")
    path = (cfg.get("site_url_path") or "").rstrip("/")
    return url + path


def load_ads() -> dict:
    return load_json(CONFIG_DIR / "ads.json", {"enabled": False, "slots": {}})


# ------------------------------------------------------------------ slugs ----

def slugify(text: str) -> str:
    """Deterministic, URL-safe slug."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    text = re.sub(r"-{2,}", "-", text)
    return text[:60] or "api"


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# -------------------------------------------------------------------- urls ---

def normalize_url(url: str) -> str:
    """Canonical form used for duplicate detection."""
    if not url:
        return ""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def url_domain(url: str) -> str:
    try:
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        # Keep registrable domain + TLD only (crude but deterministic).
        pieces = host.split(".")
        return ".".join(pieces[-2:]) if len(pieces) >= 2 else host
    except ValueError:
        return ""


def content_hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ------------------------------------------------------------- rate limiting --

class HostThrottle:
    """Politeness: minimum delay between requests to the same host."""

    def __init__(self, delay_seconds: float = 1.2):
        self.delay = max(0.2, float(delay_seconds))
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = url_domain(url) or urlsplit(url).netloc
        now = time.monotonic()
        previous = self._last.get(host, 0.0)
        remaining = self.delay - (now - previous)
        if remaining > 0:
            time.sleep(min(remaining, 10.0))
        self._last[host] = time.monotonic()


THROTTLE = HostThrottle()


class FetchResult:
    __slots__ = ("ok", "status", "final_url", "https", "redirected", "text",
                 "elapsed_ms", "error", "retries")

    def __init__(self, **kw):
        self.ok = kw.get("ok", False)
        self.status = kw.get("status", 0)
        self.final_url = kw.get("final_url", "")
        self.https = kw.get("https", False)
        self.redirected = kw.get("redirected", False)
        self.text = kw.get("text", "")
        self.elapsed_ms = kw.get("elapsed_ms", 0)
        self.error = kw.get("error")
        self.retries = kw.get("retries", 0)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def fetch_url(url: str, timeout: int = 10, max_retries: int = 2,
              max_text: int = 200_000, throttle: HostThrottle | None = None) -> FetchResult:
    """Conservative GET. Never raises. Retries only on network errors / 429 / 5xx
    with exponential backoff. A single request per attempt, honest User-Agent."""
    if throttle is None:
        throttle = THROTTLE
    started = time.monotonic()
    attempt, last_error, status = 0, None, 0
    while attempt <= max_retries:
        throttle.wait(url)
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/json,application/xhtml+xml;q=0.9,*/*;q=0.5",
                    "Accept-Language": "en",
                },
            )
            status = resp.status_code
            if status == 429 or status >= 500:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after or "").isdigit() else min(2 ** attempt, 8)
                last_error = f"HTTP {status}"
                time.sleep(delay)
                attempt += 1
                continue
            text = resp.text[:max_text] if (resp.text or "") else ""
            final = str(resp.url)
            return FetchResult(
                ok=200 <= status < 400,
                status=status,
                final_url=final,
                https=final.startswith("https://"),
                redirected=final.rstrip("/") != url.rstrip("/"),
                text=text,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                retries=attempt,
            )
        except requests.exceptions.Timeout:
            last_error = "timeout"
            attempt += 1
            time.sleep(min(2 ** attempt, 8))
        except requests.exceptions.SSLError:
            last_error = "ssl-error"
            break  # do not fall back to http automatically
        except requests.exceptions.RequestException as exc:
            last_error = type(exc).__name__
            attempt += 1
            time.sleep(min(2 ** attempt, 8))
    return FetchResult(
        status=status,
        final_url=url,
        https=url.startswith("https://"),
        elapsed_ms=int((time.monotonic() - started) * 1000),
        error=last_error or "failed",
        retries=attempt,
    )


# -------------------------------------------------------- quality scoring ----

SCORE_COMPONENTS = {
    "official_verified": 20,
    "docs_verified": 20,
    "description": 10,
    "authentication": 10,
    "free_tier": 15,
    "category": 5,
    "example": 10,
    "recent_verification": 10,
}


def quality_score(api: dict, cfg: dict) -> int:
    """Deterministic publishing-quality score (0-100). Not a user rating."""
    score = 0
    if api.get("official_url") and api.get("_official_ok"):
        score += SCORE_COMPONENTS["official_verified"]
    if api.get("documentation_url") and api.get("_docs_ok"):
        score += SCORE_COMPONENTS["docs_verified"]
    if len(api.get("description") or "") >= 40:
        score += SCORE_COMPONENTS["description"]
    if api.get("authentication") not in (None, "", "unknown"):
        score += SCORE_COMPONENTS["authentication"]
    if api.get("free_tier") not in (None, "", "unknown"):
        score += SCORE_COMPONENTS["free_tier"]
    if api.get("category") and api.get("category") != "other":
        score += SCORE_COMPONENTS["category"]
    if api.get("example_request") or api.get("example_response"):
        score += SCORE_COMPONENTS["example"]
    if days_between(api.get("last_verified") or "1970-01-01") <= cfg.get("reverify_interval_days", 14):
        score += SCORE_COMPONENTS["recent_verification"]
    return min(score, 100)


# ------------------------------------------------------- duplicate detection --

def dedup_keyset(api: dict) -> set[str]:
    keys = set()
    for field in ("official_url", "documentation_url", "pricing_url"):
        if api.get(field):
            keys.add("url:" + normalize_url(api[field]))
    if api.get("repo_url"):
        keys.add("repo:" + normalize_url(api["repo_url"]))
    if api.get("name"):
        keys.add("name:" + normalize_name(api["name"]))
    if api.get("slug"):
        keys.add("slug:" + api["slug"])
    return keys


def find_duplicate(candidate: dict, database: list[dict]) -> dict | None:
    """Return the existing entry this candidate duplicates, else None.
    Deterministic checks only - never an LLM call."""
    cand_keys = dedup_keyset(candidate)
    cand_name = normalize_name(candidate.get("name") or "")
    cand_domain = url_domain(candidate.get("official_url") or "")
    for existing in database:
        if cand_keys & dedup_keyset(existing):
            return existing
        # Same registrable domain + very similar name -> same product.
        if cand_domain and url_domain(existing.get("official_url") or "") == cand_domain:
            if similarity(cand_name, normalize_name(existing.get("name") or "")) >= 0.8:
                return existing
    return None


def unique_slug(base: str, database: list[dict]) -> str:
    taken = {a.get("slug") for a in database}
    slug = slugify(base)
    if slug not in taken:
        return slug
    idx = 2
    while f"{slug}-{idx}" in taken:
        idx += 1
    return f"{slug}-{idx}"


# ------------------------------------------------------------------ helpers ---

def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def clean_text(text: str, limit: int = 4000) -> str:
    text = strip_html(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def append_run_log(entry: dict) -> None:
    log_path = DATA_DIR / "run-log.json"
    data = load_json(log_path, {"runs": []})
    data.setdefault("runs", []).append(entry)
    data["runs"] = data["runs"][-365:]
    save_json(log_path, data)


def github_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass

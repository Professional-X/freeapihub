# FreeAPIHub

**A self-maintaining directory of free and freemium APIs. Set it up once, let it run every day.**

FreeAPIHub discovers useful public APIs from legitimate sources, verifies them against their
official websites and documentation, researches them with an LLM (never inventing facts),
and publishes 0–4 quality pages per day to a fast static website - fully automatically,
on free infrastructure (GitHub Actions + GitHub Pages).

> Every listing is checked before publication. Days with no quality candidates publish
> zero pages. Quality over quota.

---

## What this project does

Once per day a GitHub Actions workflow runs this pipeline:

```
03:17 UTC
   ↓
Discover API candidates from public sources (curated lists, open directories, GitHub)
   ↓
Normalize + deduplicate (deterministic - never the LLM)
   ↓
Verify: is the official site alive? HTTPS? does it really describe an API?
   ↓
Select only candidates scoring above the quality threshold
   ↓
LLM research: summarize, categorize, extract only source-supported facts
   ↓
Generate API pages + update homepage, categories, search index, sitemap
   ↓
Validate everything (JSON, HTML, links, ads, config)
   ↓
Commit + deploy to GitHub Pages
```

A weekly **Maintenance** workflow re-checks existing listings, applies the status policy
(`verified → warning → temporarily_unavailable → dead`), and never deletes a listing on
a single failure. A manual **Deploy Site** workflow rebuilds after config changes.

The LLM is used *only* for summarizing verified candidates. Duplicate detection, URL
checks, scoring, sitemaps, statistics and page rendering are all deterministic code -
so running costs stay near zero (a handful of small LLM calls per day, at most).

---

## Requirements

- A GitHub account (free plan is enough)
- An LLM API key (OpenAI-compatible: OpenAI, Groq, OpenRouter, DeepSeek, etc.)
- Advertisement code from your ad network (optional - the site works without it)
- Optional: a custom domain

No VPS. No database server. No paid anything.

---

## One-time setup

### 1. Get the code

Fork or clone this repository to your own GitHub account, then clone locally if you
prefer. Everything also works by just pushing the repository as-is.

### 2. Configure the site

Edit **`config/site.json`** (the only file you normally need to touch):

| Field | What to put |
|---|---|
| `site_name` | Your site name |
| `site_url` | `https://YOUR-USERNAME.github.io` (or your custom domain) |
| `site_url_path` | `/REPO-NAME` for project pages, `""` for root/custom domains |
| `daily_publish_limit` | Max pages per day (default 4) |
| `minimum_quality_score` | Publishing threshold (default 70 of 100) |

### 3. Add the LLM API key (GitHub Secrets)

Go to **Settings → Secrets and variables → Actions → New repository secret**
(green button "New repository secret") and add:

| Secret | Required | Example |
|---|---|---|
| `LLM_API_KEY` | yes (for daily LLM research) | `sk-...` |
| `LLM_BASE_URL` | optional | `https://api.groq.com/openai/v1` |
| `LLM_MODEL` | optional | `llama-3.1-8b-instant` |

`LLM_API_KEY` is never exposed to the browser, never written to HTML/JSON, and never
logged. If you omit it the site still runs - new publications just use deterministic
metadata only (stricter, so fewer pages pass).

### 4. Add your advertisement code

Open **`config/ads.json`** and paste your ad network's code into the slots you want
to enable:

```json
{
  "enabled": true,
  "slots": {
    "top": "<script ...></script>",
    "after_intro": "",
    "middle": "",
    "before_related": "",
    "sidebar": "",
    "footer": ""
  }
}
```

- Available slots on API pages: `top`, `after_intro`, `middle`, `before_related`, `footer`
- The homepage and category pages use `top`, `after_intro` and `footer`
- Your code is inserted **verbatim** - never modified, escaped or duplicated
- Leave a slot `""` to disable it; delete nothing else

After editing, run **Actions → Deploy Site → Run workflow** to rebuild the pages.

### 5. Enable GitHub Pages

1. Repository **Settings → Pages**
2. Under "Build and deployment", set **Source: GitHub Actions**
3. Done - the workflows deploy automatically after every successful run

### 6. Enable GitHub Actions

If Actions are not already enabled: **Settings → Actions → General → Allow all
actions**. The Daily Publisher runs at 03:17 UTC; you can also trigger it manually
(see below).

### 7. (Optional) Custom domain

Follow the instructions inside **`CNAME.example`** in the repository root, then update
`site_url` / `site_url_path` in `config/site.json` and re-run **Deploy Site**.

---

## Running manually

You do not need to run anything by hand. But to test:

- **Daily pipeline:** Actions → *Daily Publisher* → **Run workflow**
- **Maintenance:** Actions → *Maintenance* → **Run workflow**
- **Rebuild/deploy only:** Actions → *Deploy Site* → **Run workflow**

Each run writes a human-readable summary (discovered / verified / published / failed)
at the bottom of the workflow run page.

Running locally instead:

```bash
pip install -r requirements.txt
python scripts/seed.py        # one-time: verify + publish the curated starter APIs
python scripts/run_daily.py   # full daily pipeline
python scripts/maintenance.py # weekly maintenance
python scripts/validate.py --selftest  # failure-mode tests
```

---

## Repository layout

```
config/       site.json (EDIT THESE VALUES) + ads.json (paste ad code)
data/         persistent JSON database (apis, queue, sources, history, run log)
scripts/      discovery, verification, LLM research, site build, sitemap,
              maintenance, validation - all plain Python + stdlib + requests
templates/    HTML templates and components
site/         the generated static website (committed, deploy-ready)
.github/      the three workflows + the API submission issue template
```

## Submitting an API

Use the **API submission** issue template (Issues → New issue). Every submission -
whether from the discovery pipeline or a human - goes through the same verification
before publication. Nothing is auto-published without validation.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Daily run shows `Published: 0` | No candidates cleared verification + quality threshold today. Normal and intentional - quality over quota. |
| Deploy job fails | GitHub Pages source is not set to **GitHub Actions** (Settings → Pages). Set it, then re-run **Deploy Site**. |
| `LLM_API_KEY not set` in logs | The secret is missing; the run continues deterministically. Add the secret to enable full research. |
| LLM errors / invalid JSON | Check `LLM_BASE_URL` and `LLM_MODEL` match your provider. One failed candidate never stops the run. |
| A listing shows a status notice | The automated re-check found the official source unreachable. It retries automatically; persistent failures are archived after the configured grace period. |
| Ads not visible | Slot empty in `config/ads.json`, or pages need a rebuild - run **Deploy Site** after editing. |
| Wrong canonical URLs | `site_url` / `site_url_path` in `config/site.json` do not match your Pages URL. Fix and re-run **Deploy Site**. |

## Honest expectations

This project optimizes for low cost, useful pages and long-term organic traffic.
It does **not** promise revenue, does not fabricate popularity, ratings or testimonials,
and does not manipulate ads. The site earns nothing automatically - it is the pages,
over time, that create the conditions for advertising income.

## License

MIT - see [LICENSE](LICENSE). Third-party API names referenced by the directory belong
to their respective owners.

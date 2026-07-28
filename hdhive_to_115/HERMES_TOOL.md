# Hermes tool: HDHive → 115

Agent-facing interface for:

> User: find a movie called **Fury**  
> Tool: search HDHive → pick a 115 resource (points ≤ limit) → unlock → save to 115

## Command

```bash
cd /path/to/TgtoDrive/hdhive_to_115
python3 cli.py want "Fury"
```

Stdout is **JSON only** (logs go to stderr).

### Flags

| Flag | Meaning |
| --- | --- |
| `--max-points N` | Skip paid unlocks above N (default `HDHIVE_MAX_POINTS` or 4) |
| `--pid CID` | 115 target folder (default `HDHIVE_115_PID` or 0) |
| `--index N` | Use N-th search hit if disambiguation needed (0 = best match) |
| `--dry-run` | Search + pick only (no unlock/save) |
| `--tmdb-id ID` | Skip title search; open `/tmdb/movie/ID` (or tv) |
| `--media-type movie\|tv` | Used with `--tmdb-id` (default `movie`) |
| `--no-save` | Unlock only, do not call 115 receive |
| `--headed` | Show browser (debug) |
| `-v` | Verbose logs on stderr |

### Env (`.env.local`)

```bash
HDHIVE_COOKIE=...
ENV_115_COOKIES=...
HDHIVE_115_PID=0
HDHIVE_MAX_POINTS=4
HDHIVE_HEADLESS=1
# Recommended for reliable title search (free from themoviedb.org):
TMDB_API_KEY=...
```

**Search note:** with `TMDB_API_KEY`, `want "Fury"` resolves the title via TMDB then opens HDHive resources. Without it, the tool scrapes HDHive’s on-site search (slower / flakier).

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success (saved, or dry-run pick ok) |
| 1 | Generic failure |
| 2 | Missing / bad auth (e.g. no 115 cookie after unlock) |
| 3 | Points / unlock policy skip |
| 4 | No search hits or no resources on title page |

## Success JSON (shape)

```json
{
  "ok": true,
  "query": "Fury",
  "movie": {
    "title": "Fury",
    "url": "https://hdhive.com/tmdb/movie/228150",
    "media_type": "movie",
    "year": 2014,
    "tmdb_id": 228150,
    "score": 105.0
  },
  "resource": {
    "resource_url": "https://hdhive.com/resource/115/...",
    "provider": "115",
    "points": 0,
    "is_free": true,
    "label": "..."
  },
  "unlock": {
    "share_urls": ["https://115cdn.com/s/xxx?password=yyyy"],
    "already_unlocked": true
  },
  "save": {
    "attempted": true,
    "saved": true,
    "share_url": "https://115cdn.com/s/xxx?password=yyyy",
    "pid": "0"
  },
  "error": "",
  "error_code": "",
  "candidates": [],
  "resources": []
}
```

## Suggested Hermes tool schema

```json
{
  "name": "hdhive_want_movie",
  "description": "Search HDHive for a movie/TV title, unlock a 115 pan resource within points budget, and save it to the user's 115 cloud drive.",
  "parameters": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "Movie or TV title to find, e.g. Fury, 狂怒, Inception"
      },
      "max_points": {
        "type": "integer",
        "description": "Max HDHive points to spend (default 4)",
        "default": 4
      },
      "pid": {
        "type": "string",
        "description": "115 folder CID to save into (default 0 = root)"
      },
      "index": {
        "type": "integer",
        "description": "Which search result to use if multiple matches (0 = best)",
        "default": 0
      },
      "dry_run": {
        "type": "boolean",
        "description": "If true, only search and select resource without unlock/save",
        "default": false
      }
    },
    "required": ["query"]
  }
}
```

### Example tool executor

```bash
python3 /path/to/hdhive_to_115/cli.py want "$query" \
  ${max_points:+--max-points $max_points} \
  ${pid:+--pid $pid} \
  ${index:+--index $index} \
  ${dry_run:+--dry-run}
```

## Agent behavior tips

1. Call `want "Fury"` first.
2. If `error_code=no_search_results`, try alternate titles / year (`Fury 2014`).
3. If `candidates` has several good matches, re-call with `--index 1` etc.
4. If `points_exceeded`, raise `max_points` or ask the user.
5. Do not print cookies from `.env.local` into the chat.

## Lower-level tools (if needed)

```bash
python3 cli.py check
python3 cli.py unlock 'https://hdhive.com/resource/115/<slug>'
python3 cli.py save115 'https://115cdn.com/s/xxx?password=yyyy' --pid 0
python3 cli.py run 'https://hdhive.com/resource/115/<slug>'
```

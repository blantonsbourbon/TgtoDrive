# hdhive_to_115

Standalone **HDHive session unlock → 115 pan save** without installing full TgtoDrive.

## How it works

1. Load your HDHive **browser cookies** into Playwright Chromium  
2. Open the resource page and click unlock (same as the web UI / userscripts)  
3. Parse the resulting **115 share link**  
4. Call 115 `share/snap` + `share/receive` with your 115 cookie  

This is **option A**: browser automation. It does not use TgtoDrive’s PyArmor client or HMAC proxy.

## Setup

```bash
cd hdhive_to_115
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env.local
# edit .env.local — never commit it
```

Required in `.env.local`:

| Variable | Description |
| --- | --- |
| `HDHIVE_COOKIE` | Cookie header from a logged-in hdhive.com tab |
| `ENV_115_COOKIES` | Cookie header for 115 (only for `save115` / `run`) |
| `HDHIVE_115_PID` | Target 115 folder CID (default `0`) |
| `HDHIVE_MAX_POINTS` | Skip unlock if cost &gt; N (default `4`) |

## Usage

```bash
# 0) Agent / “I want a movie” entrypoint (search → unlock → 115 save)
python3 cli.py want "Fury"
python3 cli.py want "Fury 2014" --max-points 4 --pid 0
python3 cli.py want "狂怒" --dry-run          # search+pick only
python3 cli.py want "Fury" --index 1         # pick 2nd search hit

# 1) Verify HDHive session
python3 cli.py check

# 2) Unlock only (prints 115 share URLs as JSON)
python3 cli.py unlock 'https://hdhive.com/resource/115/<slug>'
python3 cli.py unlock 'https://hdhive.com/resource/<slug>' --debug --headed

# 3) Save a known 115 share link
python3 cli.py save115 'https://115.com/s/xxxxx?password=abcd' --pid 0

# 4) Unlock + save in one step
python3 cli.py run 'https://hdhive.com/resource/115/<slug>' --pid 0
```

For Hermes / other agents, see **[HERMES_TOOL.md](./HERMES_TOOL.md)** (JSON contract, exit codes, tool schema).

## Security

- Put secrets only in `.env.local` (gitignored).  
- If cookies were pasted into chat/logs, **rotate them** (log out/in on HDHive and 115).  
- Cookies act as your account — treat them like passwords.

## Notes on homepage announcement

HDHive often shows a **site notice** on first load. The confirm button is usually
disabled for about **10 seconds** (e.g. `知道了 (10秒)`). The tool waits for that
countdown and clicks dismiss when enabled (`wait_for_site_ready` /
`dismiss_announcement`). Do **not** use `networkidle` waits — the marquee keeps
the network busy.

## Limits

- UI changes on HDHive can break button/link selectors. Use `--debug` to dump `debug/unlock_fail.png`.  
- Non-115 resources (123 / 天翼 / ed2k) are not saved by this tool yet.  
- Does not implement TgtoDrive channel monitor / OAuth proxy.
- If you see `未通过安全检测`, retry with `--headed`.

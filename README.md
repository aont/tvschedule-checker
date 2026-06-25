# Japanterebi Checker

Japanterebi Checker monitors an XMLTV guide for schedule changes that can affect recurring TV recordings. It compares the configured recording times in `recordings.json` with the current XMLTV programme start times and can notify a Slack channel when a programme moves, disappears, or recovers to its registered time.

The default guide source is the Japanterebi XMLTV feed:

```text
https://animenosekai.github.io/japanterebi-xmltv/guide.xml
```

## Features

- Checks daily, weekly, and one-time recording rules.
- Supports exact, substring, and regular-expression title matching.
- Matches one channel, multiple channels, or any channel.
- Detects shifted start times within a configurable matching window.
- Optionally reports missing programmes and recovered schedules.
- Stores notification state to avoid sending duplicate alerts.
- Caches `guide.xml` locally and can fall back to the cache if the guide fetch fails.
- Searches `guide.xml` by programme title to help build `recordings.json` entries.
- Supports dry runs for local testing without Slack.

## Requirements

- Python 3.11 or newer.
- Python packages listed in `requirements.txt`:
  - `aiohttp`
  - `lxml`

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy the example configuration and edit it for your recordings:

```bash
cp recordings.example.json recordings.json
```

A configuration file contains global settings and a `recordings` array:

```json
{
  "guide_url": "https://animenosekai.github.io/japanterebi-xmltv/guide.xml",
  "timezone": "Asia/Tokyo",
  "lookahead_days": 14,
  "matching_window_hours": 12,
  "shift_threshold_minutes": 0,
  "notify_missing": false,
  "notify_recovery": true,
  "recordings": [
    {
      "id": "weekly-friday-example",
      "title": "Example Weekly Anime",
      "title_match": "contains",
      "channel": "TokyoMX.jp",
      "recurrence": "weekly",
      "weekday": "fri",
      "time": "23:30"
    }
  ]
}
```

### Global options

| Option | Default | Description |
| --- | --- | --- |
| `guide_url` | Japanterebi XMLTV guide | XMLTV guide URL to fetch. |
| `timezone` | `Asia/Tokyo` | Time zone used for expected recording times and guide comparison. |
| `lookahead_days` | `14` | Number of days from today to check. |
| `matching_window_hours` | `12` | How far before or after the registered time to look for a matching programme. |
| `shift_threshold_minutes` | `0` | Minimum shift, in minutes, required before alerting. |
| `notify_missing` | `false` | Whether to alert when no matching programme is found. |
| `notify_recovery` | `false` | Whether to alert when a previously shifted programme returns to the registered time. |
| `http_timeout_seconds` | `60` | HTTP timeout for fetching the XMLTV guide. |
| `guide_cache` | `guide.xml` | Local path where the downloaded XMLTV guide is cached. Set to an empty string to disable caching. |
| `guide_cache_max_age_seconds` | `21600` | How long to reuse a cached guide before trying to download a fresh copy. If a fresh download fails, an existing cache is used as a fallback. |
| `state_retention_days` | `30` | Number of days to keep notification state entries. |

### Recording rule options

| Option | Required | Description |
| --- | --- | --- |
| `id` | Yes | Stable unique identifier for the recording rule. |
| `title` | Yes | Programme title or title pattern to match. |
| `time` | Yes | Registered start time in `HH:MM` or `HH:MM:SS` format. |
| `title_match` | No | `exact`, `contains`, or `regex`. Defaults to `exact`. |
| `channel` | No | Single XMLTV channel ID to match. Omit to match any channel. |
| `channels` | No | List of XMLTV channel IDs to match. |
| `recurrence` | No | `daily`, `weekly`, or `once`. Defaults to `once`. |
| `weekday` | Weekly rules | Weekday for a weekly rule, such as `mon`, `fri`, or `sun`. |
| `weekdays` | Weekly rules | List of weekdays for a weekly rule. |
| `date` | One-time rules | One-time recording date in `YYYY-MM-DD` format. |
| `start_date` | No | First date on which the rule is active. |
| `end_date` | No | Last date on which the rule is active. |

## Usage

Run a local dry run to print any Slack message that would be sent:

```bash
python xmltv_shift_monitor.py --config recordings.json --state state.json --dry-run
```

Run normally and send alerts to Slack:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
python xmltv_shift_monitor.py --config recordings.json --state state.json
```

When not using `--dry-run`, the script updates the state file after each run. Keep the state file between scheduled runs so duplicate alerts are suppressed.

### Local guide cache

By default, the checker stores the downloaded XMLTV document at `guide.xml` and reuses it for six hours. You can override those settings in `recordings.json` with `guide_cache` and `guide_cache_max_age_seconds`, or from the command line:

```bash
python xmltv_shift_monitor.py --config recordings.json --guide-cache cache/guide.xml --guide-cache-max-age-seconds 3600 --dry-run
```

Use `--refresh-guide` to force a download even when the cache is still fresh. If a download fails and a local cache exists, the checker uses the cached copy.

### Search the guide

Use `--search-title` to find programmes in `guide.xml` and print the channel, start time, stop time, categories, description, and a helper line with the fields commonly needed for a `recordings.json` rule:

```bash
python xmltv_shift_monitor.py --config recordings.json --search-title "Example Weekly Anime"
```

The search defaults to substring matching. Use `--search-title-match exact` or `--search-title-match regex` to change the matching mode, and `--search-limit` to control how many results are printed.

## Scheduling

You can run the checker from cron, systemd timers, GitHub Actions, or another scheduler. For example, a cron entry that checks every hour might look like this:

```cron
0 * * * * cd /path/to/japanterebi-checker && . .venv/bin/activate && python xmltv_shift_monitor.py --config recordings.json --state state.json
```

Make sure the scheduler provides `SLACK_WEBHOOK_URL` when Slack notifications are enabled.

## Alert behavior

For each configured occurrence in the lookahead window, the checker finds the nearest programme whose title and channel match within `matching_window_hours` of the registered start time.

- If the programme start differs by more than `shift_threshold_minutes`, a `shifted` alert is generated.
- If the same shifted start time was already reported, no duplicate alert is sent.
- If the programme later returns to its registered start time and `notify_recovery` is enabled, a recovery alert is generated.
- If no matching programme is found and `notify_missing` is enabled, a missing alert is generated.

## Repository files

- `xmltv_shift_monitor.py` — command-line checker implementation.
- `recordings.example.json` — example recording configuration.
- `requirements.txt` — Python runtime dependencies.

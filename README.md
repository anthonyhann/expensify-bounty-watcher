# Expensify Bounty Watcher

[![tests](https://github.com/anthonyhann/expensify-bounty-watcher/actions/workflows/tests.yml/badge.svg)](https://github.com/anthonyhann/expensify-bounty-watcher/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![dependencies](https://img.shields.io/badge/dependencies-zero-brightgreen)
![license](https://img.shields.io/badge/license-MIT-blue)

A zero-dependency Python watcher for paid issues in [`Expensify/App`](https://github.com/Expensify/App).

Expensify pays **$175–$250 per bug** through Upwork, but the window to claim one is short. Issues are opened continuously, and proposals are only accepted after the `Help Wanted` label is added. This tool watches both moments and tells you when a task becomes actionable.

## What it does

- Alerts on new `External` / `Help Wanted` issues.
- Alerts **again** when `Help Wanted` is added — that is the moment you can submit a proposal.
- Parses the reward amount from the title and the Upwork job URL from the issue body.
- Shows how many proposals already exist and whether MelvinBot posted one.
- Runs with zero third-party packages; Python 3.9+.
- Desktop notifications on macOS, `notify-send` on Linux, terminal bell everywhere.

## Quick start

```bash
python3 watch_expensify.py
```

Default poll interval is 60 seconds. Press `Ctrl+C` to stop.

```bash
python3 watch_expensify.py --interval 30      # faster polling
python3 watch_expensify.py --once             # one pass, for cron
python3 watch_expensify.py --no-notify        # terminal output only
python3 watch_expensify.py --min-amount 150   # ignore tasks below $150
```

The first run only seeds state: it will not dump every existing issue into your notifications. After that it reports only new issues and label changes.

## Example output

```text
[11-17 14:02:11] OPEN NOW $175 — Help Wanted added, proposal allowed
  Expensify/App#107099 · UNASSIGNED
  [$175] Receipt - Upload fails silently on retry
  https://github.com/Expensify/App/issues/107099
  Upwork: https://www.upwork.com/jobs/~022122567490983281705
  labels=['External', 'Help Wanted'] comments=4
  proposals already posted=1 · MelvinBot=yes
```

## How it works

- **ETag conditional requests.** When nothing changed, GitHub returns `304`, which does not count against the rate limit. Polling stays cheap.
- **State file.** Seen issues and their stage (`external` / `open`) live in `.state/expensify-watch.json`, so restarts do not re-alert.
- **Two-stage detection.** A new issue is announced once; a later `Help Wanted` label triggers a second, separate alert.
- **Retries and backoff.** DNS failures, truncated responses and HTTP errors do not kill the process; the loop backs off and retries.
- **Single instance.** A file lock prevents two watchers from fighting over the same state file.
- **gzip responses.** Smaller payloads, fewer partial reads.

## GitHub token

Unauthenticated requests are limited to 60 per hour. Conditional requests make that workable, but for long-running use set a token:

```bash
export GITHUB_TOKEN=ghp_xxx
python3 watch_expensify.py
```

## Run it in the background

macOS with `screen`:

```bash
screen -dmS expensify-watch /bin/sh -c \
  'exec /usr/bin/python3 -u "$PWD/watch_expensify.py" --interval 90 --verbose >> "$PWD/watch.log" 2>&1'
```

Cron, every two minutes:

```cron
*/2 * * * * cd /path/to/expensify-bounty-watcher && /usr/bin/python3 watch_expensify.py --once --no-notify >> watch.log 2>&1
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The test suite covers amount parsing, Upwork URL extraction, label filtering and the `external -> open` alert transition. No network access is required.

## Limitations

- Watches one repository (`Expensify/App`). The same pattern works for any repo with paid-issue labels.
- GitHub notifications are text-only; there is no click action.
- This project is not affiliated with Expensify.

## License

MIT

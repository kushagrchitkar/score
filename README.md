# score

Minimal live sports scores in your terminal title.

```text
ARS 0–2 MCI · 73′
```

`score` is designed around two concepts:

- **Follow** a team as a persistent preference.
- **Pin** one event to one terminal tab until it finishes or you unpin it.

The first release targets football and Ghostty. Its event model and provider boundary are sport-neutral so other sports and data sources can be added later.

## Commands

```bash
score                         # browse live/upcoming matches; select one to pin
score pin arsenal             # find and pin an Arsenal match
score pin "arsenal city"      # narrow by both participants
score pin arsenal --once      # print once; do not change the title
score unpin                   # stop this tab's watcher and restore its title
score follow arsenal          # save a team preference
score following               # list followed teams
```

One Ghostty tab has one independent pin. Different tabs can pin different events.

## Install from source

Requires Python 3.9 or later and no runtime dependencies.

```bash
git clone https://github.com/kushagrchitkar/score.git
cd score
python3 -m pip install --user .
```

Ensure the Python user scripts directory is on `PATH`. On macOS this is commonly `~/Library/Python/3.9/bin` for Apple's Python 3.9.

Ghostty must not have a permanently fixed `title = ...` configuration, because that tells Ghostty to ignore title escape sequences.

## How it works

1. The CLI discovers events using ESPN's public-facing scoreboard endpoints.
2. A selected match is stored by stable provider event ID.
3. A small watcher tied to the current TTY refreshes the exact event every 20 seconds.
4. The watcher writes a standard OSC title sequence supported by Ghostty.
5. Closing the terminal makes title writes fail and the watcher exits; `score unpin` stops it explicitly.

No score data passes through a `score` server.

## Data-source status

ESPN's public-facing endpoints are currently usable without an API key but are undocumented and not a contractual developer API. All ESPN-specific code lives in `score/espn.py`, allowing a licensed or alternative provider to replace it without changing title control, matching, storage, or rendering.

## Development

```bash
python3 -m unittest discover -s tests -v
```

## Current scope

- Football event discovery and fuzzy participant matching
- Live, upcoming, and full-time compact formatting
- Stable team identities for follows
- Per-terminal background pin watcher
- Ghostty-compatible title output

Following currently persists team identity and is the foundation for preferred ordering; prioritizing followed teams in the interactive event list is the next small product slice.

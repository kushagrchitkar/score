# score

Minimal live sports scores in your terminal title or beside an OpenPets companion.

```text
ARS 0–2 MCI · 73′
NYY 3–2 BOS · Top 7th
WI 194/3 (67 ov) · PAK · Stumps
```

`score` is designed around two concepts:

- **Follow** a team as a persistent preference.
- **Pin** one event to one terminal tab until it finishes or you unpin it.

The current release supports live football, MLB, and cricket events in Ghostty. Its event model and provider boundary remain sport-neutral so more sports and data sources can be added later.

## Commands

```bash
score                         # browse live matches; select one to pin
score pin arsenal             # find and pin a live Arsenal match
score pin yankees             # find and pin a live Yankees game
score pin india               # find and pin a live India cricket match
score pin "arsenal city"      # narrow by both participants
score pin arsenal --once      # print once; do not change the title
score unpin                   # stop this tab's watcher and restore its title
score demo                    # simulate a match in the title
score openpets arsenal        # pin a live score to one OpenPets bubble
score openpets                # browse all live events for OpenPets
score openpets unpin          # stop and clear the OpenPets score bubble
score follow arsenal          # save a team preference
score following               # list followed teams
```

One Ghostty tab has one independent pin. Different tabs can pin different events.

### Try it without a live match

```bash
score demo
```

The demo uses the same per-terminal background title mechanism as a real pin. It advances a simulated Arsenal–Manchester City match every two seconds, shows full time, and restores the previous title. Stop it early with `score unpin`.

## OpenPets

[`OpenPets`](https://github.com/alterhq/openpets/releases/latest) is an optional native macOS companion app (macOS 14+). Install and open the official app, wake a pet, then run:

```bash
score openpets arsenal
```

`score` connects directly to OpenPets' configured local Unix socket; its separate CLI is not required. The first update creates one bubble and every later 10-second refresh replaces that same bubble by thread ID. The watcher retains the last valid score through transient provider failures, shows a final result for ten minutes, then clears it. It continues after the invoking terminal closes, but does not resume automatically after a Mac restart.

Use `score openpets unpin` to stop the global watcher and clear only its score bubble. Ghostty title pins remain independent and per tab.

## Install

Requires Python 3.9 or later and no runtime dependencies. Install directly with `uv`:

```bash
uv tool install git+https://github.com/kushagrchitkar/score.git
```

Upgrade an existing installation with `uv tool upgrade ghostty-score`.

To install from a checkout instead:
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
3. A small watcher refreshes the exact event every 10 seconds.
4. Ghostty pins write standard OSC title sequences to one TTY; OpenPets pins update one local bubble by stable thread ID.
5. `score unpin` restores one terminal title, while `score openpets unpin` clears only the global pet bubble.

No score data passes through a `score` server.

## Data-source status

ESPN's public-facing endpoints are currently usable without an API key but are undocumented and not a contractual developer API. All ESPN-specific code lives in `score/espn.py`, allowing a licensed or alternative provider to replace it without changing title control, matching, storage, or rendering.

## Development

```bash
python3 -m unittest discover -s tests -v
```

## Current scope

- Football, MLB, and all-series cricket live-event discovery with fuzzy participant matching
- Football minute/full-time, baseball inning/final, and cricket runs/wickets/overs formatting
- Stable team identities for follows
- Per-terminal Ghostty title pins and one global OpenPets bubble pin
- Direct, local OpenPets socket integration with stable bubble thread identity
- Ghostty-compatible title output

Following currently persists team identity and is the foundation for preferred ordering; prioritizing followed teams in the interactive event list is the next small product slice.

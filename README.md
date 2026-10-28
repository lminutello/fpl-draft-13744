# fpl-draft-13744

Automated snapshots of FPL Draft league **13744** ("FPL 2026-27 Alhamdulillah").

This is a data store, not a website. A scheduled GitHub Action fetches the
public FPL Draft API every six hours and commits the results into `data/`.

## Why it exists

The FPL Draft API is public and needs no login, but it only ever reports
*current* totals — there is no endpoint that hands you a per-player,
per-gameweek time series. That series is what you actually need to judge
form, minutes security and rotation risk, so this rebuilds it from
`event/{gw}/live` and keeps it.

It also works around a narrower problem: the sandbox Claude runs in cannot
reach `draft.premierleague.com`, but it can reach GitHub. Parking the data
here makes it readable from anywhere, including unattended sessions.

## What lands in `data/`

| file | contents |
|---|---|
| `SUMMARY.md` | digest meant to be read directly — standings, squads, ranked free agents |
| `meta.json` | current/next gameweek, waiver and deadline timestamps |
| `standings.json` | league table |
| `rosters.json` | every manager's squad with derived metrics |
| `free_agents.json` | everyone unowned, ranked by points |
| `history.json` | per-player, per-gameweek stat lines |
| `players.json` | season totals, status and news for all ~669 players |
| `fixtures.json` | upcoming fixtures the API exposes |

## Derived metrics

Beyond raw totals, each player carries:

- **`defcon_rate`** — share of appearances in which the player actually
  cleared the defensive-contribution threshold (10 actions for defenders,
  12 for midfielders and forwards). This is the real hit rate, not an
  actions-per-90 estimate. The API's `defensive_contribution` field is
  already position-aware, so the threshold is the only thing applied here.
- **`starts` / `appearances`** — the rotation signal, week by week.
- **`last5`** — points by gameweek, most recent last.

## What it deliberately does not do

It does not recommend transfers or lineups. Nothing in this API knows that a
player was rested after international duty, or that a rival is doubtful with
concussion. Every bad call this season came from that layer, not from the
numbers. Treat this as the record of what happened, and check team news
separately before acting.

## Running it by hand

```bash
python scripts/fetch_fpl.py
```

Override the league or entry with `FPL_LEAGUE_ID` and `FPL_ENTRY_ID`.
Needs Python 3 and nothing else — standard library only.

# fpl-draft-13744

Automated snapshots of FPL Draft league **13744** ("FPL 2026-27 Alhamdulillah").

A scheduled GitHub Action fetches the public FPL Draft API and commits the
results into `data/`. `index.html` (served by GitHub Pages) is a live-score
app on top of it: the league table, each gameweek's head-to-head matchups
with projections, and a per-player view of every matchup.

GitHub only fires the schedule every few hours on a quiet repo, so when a run
lands within an hour of a kickoff (or while a match is on) it stays alive and
re-pulls every ~7 minutes, for up to about 5½ hours.

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
| `app.json` | app index: entries, standings, every H2H match, pull time |
| `gw/{n}.json` | per gameweek: fixtures, each XI + bench with auto-subs, earned and projected points |
| `picks.json`, `results.json` | caches of lineups and fixture results so past weeks aren't refetched |
| `transactions.json` | every waiver and free-agent move, accepted or denied |

## Projections

Season points per appearance, shrunk toward 3.0 as if we'd seen three extra
average appearances (so a few fluky games don't dominate), × chance of
playing (recent appearance rate × the injury flag), × number of fixtures.

Backtested on GW5–8 by projecting each week from the weeks before it.
Mean absolute error per player, for the 60 highest-projected each week:

| model | error |
|---|---|
| plain season average | 3.48 |
| 60% recent form + opponent goals for/against (previous version) | 3.72 |
| **current** | **3.11** |

Recent form and the opponent adjustment both made it worse over this many
games, so they're out. Re-run the backtest before adding either back.

Team totals include bench cover: a starter who might not play leaves part of
his slot to the first bench player, the way auto-subs pay out.

## Lineups before the deadline

Nobody's next lineup is public until the deadline. FPL Draft carries each
manager's last locked lineup forward, and a signing drops into the exact slot
of the player it replaced — so the app takes the last locked order and
replays that week's accepted transactions onto it. That's what each manager
has saved, minus any reorders made since, which only the deadline reveals.
If the replay can't produce a clean legal squad it falls back to the best
projected XI.

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

#!/usr/bin/env python3
"""
Snapshot the FPL Draft API for one league into data/.

Designed to run on a GitHub Actions runner. The runner has open outbound
internet and the FPL Draft API is fully public (verified: bootstrap-static,
league/{id}/details, league/{id}/element-status and event/{gw}/live all
return 200 with no session cookie), so no secrets are needed.

Writes:
    data/meta.json           current/next gameweek, deadlines, last updated
    data/standings.json      league table
    data/rosters.json        every manager's squad
    data/free_agents.json    everyone unowned, ranked
    data/history.json        per-player per-gameweek stat lines
    data/players.json        season totals + status/news per player
    data/fixtures.json       upcoming fixtures the API exposes
    data/SUMMARY.md          digest meant to be read directly

Idempotent. Finished gameweeks already stored are not refetched; the most
recent gameweek is always refetched in case it is still live.
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LEAGUE_ID = int(os.environ.get("FPL_LEAGUE_ID", "13744"))
MY_ENTRY = int(os.environ.get("FPL_ENTRY_ID", "174590"))

BASE = "https://draft.premierleague.com/api"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data")
UA = {"User-Agent": "Mozilla/5.0 (compatible; fpl-draft-snapshot/1.0)"}

POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# DEFCON: 2 pts for 10 defensive actions (DEF) or 12 (MID/FWD).
# The API's `defensive_contribution` field is already position-aware --
# for DEF it counts clearances/blocks/interceptions/tackles, for MID and FWD
# it also includes recoveries. So we only need the threshold here.
DEFCON_THRESHOLD = {"GKP": 10**9, "DEF": 10, "MID": 12, "FWD": 12}

# per-gameweek stats worth keeping (the raw payload is far larger)
KEEP = [
    "minutes", "total_points", "starts", "goals_scored", "assists",
    "clean_sheets", "goals_conceded", "saves", "bonus", "bps",
    "yellow_cards", "red_cards", "defensive_contribution",
    "clearances_blocks_interceptions", "recoveries", "tackles",
    "expected_goals", "expected_assists",
]


def get(path, tries=3):
    url = "{}/{}".format(BASE, path)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - surface and retry
            if attempt == tries - 1:
                print("  !! {} -> {}".format(url, exc), file=sys.stderr)
                return None
            time.sleep(3 * (attempt + 1))
    return None


def load(name, default):
    path = os.path.join(OUT, name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return default


def dump(name, obj):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def iso(ts):
    if not ts:
        return None
    return ts.replace("Z", "+00:00")


def main():
    print("bootstrap-static ...", file=sys.stderr)
    boot = get("bootstrap-static")
    if not boot:
        sys.exit("bootstrap-static failed; aborting without touching data/")

    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    elements = {e["id"]: e for e in boot["elements"]}

    events = boot.get("events") or {}
    ev_list = events.get("data", []) if isinstance(events, dict) else list(events)
    finished = sorted(e["id"] for e in ev_list if e.get("finished"))
    current = max(finished) if finished else 0
    nxt = next((e for e in ev_list if not e.get("finished")), None)

    print("league details + ownership ...", file=sys.stderr)
    details = get("league/{}/details".format(LEAGUE_ID))
    status = get("league/{}/element-status".format(LEAGUE_ID))
    if not details or not status:
        sys.exit("league endpoints failed; aborting without touching data/")

    managers = {e["entry_id"]: (e.get("entry_name") or "?")
                for e in details.get("league_entries", [])}
    lid_to_entry = {e.get("id"): e.get("entry_id")
                    for e in details.get("league_entries", [])}
    owner_of = {r["element"]: r.get("owner")
                for r in status.get("element_status", [])}

    # ---- per-gameweek history -------------------------------------------
    history = load("history.json", {})
    have = {int(gw) for gw in history.get("_gameweeks", [])}
    # always refetch the newest gameweek in case it is still being scored
    want = set(finished) | ({current} if current else set())
    todo = sorted(w for w in want if w not in have or w == current)

    for gw in todo:
        print("event/{}/live ...".format(gw), file=sys.stderr)
        live = get("event/{}/live".format(gw))
        if not live:
            continue
        els = live.get("elements", {})
        items = els.items() if isinstance(els, dict) else enumerate(els)
        for pid, row in items:
            try:
                pid = str(int(pid))
            except (TypeError, ValueError):
                continue
            stats = row.get("stats", row) if isinstance(row, dict) else {}
            if not isinstance(stats, dict):
                continue
            line = {k: stats.get(k) for k in KEEP if stats.get(k) not in (None, 0)}
            if not line:
                continue
            history.setdefault(pid, {})[str(gw)] = line
        have.add(gw)

    history["_gameweeks"] = sorted(have)
    dump("history.json", history)

    # ---- derived per-player metrics -------------------------------------
    def metrics(el):
        pid = str(el["id"])
        pos = POS.get(el.get("element_type"), "?")
        gws = {int(g): v for g, v in history.get(pid, {}).items()
               if not g.startswith("_")}
        played = {g: v for g, v in gws.items() if v.get("minutes", 0) > 0}
        starts = sum(1 for v in gws.values() if v.get("starts"))
        thresh = DEFCON_THRESHOLD.get(pos, 10**9)
        hits = sum(1 for v in played.values()
                   if (v.get("defensive_contribution") or 0) >= thresh)
        recent = [gws.get(g, {}).get("total_points", 0)
                  for g in sorted(gws)[-5:]]
        return {
            "id": el["id"],
            "name": el.get("web_name"),
            "team": teams.get(el.get("team"), "???"),
            "pos": pos,
            "points": el.get("total_points", 0),
            "minutes": el.get("minutes", 0),
            "starts": starts,
            "appearances": len(played),
            "defcon_hits": hits,
            "defcon_rate": round(hits / len(played), 2) if played else 0.0,
            "last5": recent,
            "status": el.get("status", "a"),
            "news": (el.get("news") or "").strip(),
        }

    rosters, free = {}, []
    for pid, owner in owner_of.items():
        el = elements.get(pid)
        if not el:
            continue
        m = metrics(el)
        if owner:
            rosters.setdefault(str(owner), []).append(m)
        else:
            free.append(m)

    order = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}
    for squad in rosters.values():
        squad.sort(key=lambda p: (order.get(p["pos"], 9), -p["points"]))
    free.sort(key=lambda p: -p["points"])

    standings = []
    for row in details.get("standings", []):
        eid = lid_to_entry.get(row.get("league_entry"))
        standings.append({
            "rank": row.get("rank"),
            "entry_id": eid,
            "name": managers.get(eid, "?"),
            "w": row.get("matches_won", 0),
            "d": row.get("matches_drawn", 0),
            "l": row.get("matches_lost", 0),
            "league_points": row.get("total", 0),
            "points_for": row.get("points_for", 0),
            "points_against": row.get("points_against", 0),
        })

    fixtures = boot.get("fixtures") or {}
    upcoming = {}
    if isinstance(fixtures, dict):
        for gw, rows in fixtures.items():
            upcoming[gw] = [{
                "kickoff": f.get("kickoff_time"),
                "home": teams.get(f.get("team_h")),
                "away": teams.get(f.get("team_a")),
            } for f in rows]

    meta = {
        "league_id": LEAGUE_ID,
        "my_entry_id": MY_ENTRY,
        "my_team": managers.get(MY_ENTRY, "?"),
        "last_finished_gw": current,
        "next_gw": (nxt or {}).get("id"),
        "next_deadline_utc": iso((nxt or {}).get("deadline_time")),
        "next_waivers_utc": iso((nxt or {}).get("waivers_time")),
        "updated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gameweeks_stored": sorted(have),
        "n_free_agents": len(free),
    }

    dump("meta.json", meta)
    dump("standings.json", standings)
    dump("rosters.json", {managers.get(int(k), k): v for k, v in rosters.items()})
    dump("free_agents.json", free)
    dump("fixtures.json", upcoming)
    dump("players.json", {str(e["id"]): metrics(e) for e in elements.values()})

    write_summary(meta, standings, rosters, free, managers, upcoming)

    # ---- live app data ----------------------------------------------------
    print("app data ...", file=sys.stderr)
    import app_data  # noqa: E402 - sibling module
    app = app_data.build(get, boot, details, status, history, OUT)
    app["updated_utc"] = meta["updated_utc"]
    app["matchday"] = matchday(boot)
    dump("app.json", app)
    meta["matchday"] = app["matchday"]
    dump("meta.json", meta)
    print("done. gw {} stored, {} free agents".format(current, len(free)),
          file=sys.stderr)


def matchday(boot, before_h=1.0, after_h=3.0):
    """True from an hour before any kickoff until three hours after it,
    or while a started fixture still has unconfirmed bonus. The workflow
    uses this to poll every few minutes instead of once a run."""
    now = dt.datetime.now(dt.timezone.utc)
    fixtures = boot.get("fixtures") or {}
    rows = []
    if isinstance(fixtures, dict):
        for v in fixtures.values():
            rows.extend(v)
    # bootstrap may drop a gameweek's fixtures once it is under way, so also
    # look at the fixture results the app builder just cached
    for v in load("results.json", {}).values():
        rows.extend({"kickoff_time": f.get("kickoff"), "started": f.get("started"),
                     "finished": f.get("confirmed")} for f in v)
    for f in rows:
        k = f.get("kickoff_time")
        if not k:
            continue
        t = dt.datetime.fromisoformat(k.replace("Z", "+00:00"))
        if t - dt.timedelta(hours=before_h) <= now <= t + dt.timedelta(hours=after_h):
            return True
        if f.get("started") and not f.get("finished"):
            return True
    return False


def write_summary(meta, standings, rosters, free, managers, upcoming):
    """A digest meant to be read directly rather than parsed."""
    L = []
    A = L.append
    A("# FPL Draft {} - snapshot".format(meta["league_id"]))
    A("")
    A("Updated {} | last finished GW{} | next GW{}".format(
        meta["updated_utc"], meta["last_finished_gw"], meta["next_gw"]))
    A("")
    A("- Waivers process: `{}`".format(meta["next_waivers_utc"]))
    A("- Gameweek deadline: `{}`".format(meta["next_deadline_utc"]))
    A("")
    A("Columns: `pts` season points, `st` starts, `app` appearances, "
      "`dc` DEFCON hit rate (share of appearances clearing the "
      "10/12-action threshold), `last5` points by gameweek.")
    A("")

    A("## Standings")
    A("")
    for r in standings:
        A("{}. {:<24} {}W-{}D-{}L  pts {}  for {}  against {}".format(
            r["rank"], r["name"], r["w"], r["d"], r["l"],
            r["league_points"], r["points_for"], r["points_against"]))
    A("")

    def line(p):
        flag = ""
        if p["status"] != "a":
            flag = "  [{}] {}".format(p["status"].upper(), p["news"])
        return ("- {:<4}{:<18}{:<5}{:>4}p {:>4}m st{:<2} app{:<2} "
                "dc {:<5} last5 {}{}").format(
            p["pos"], p["name"], p["team"], p["points"], p["minutes"],
            p["starts"], p["appearances"], p["defcon_rate"], p["last5"], flag)

    mine = rosters.get(str(meta["my_entry_id"]), [])
    if mine:
        A("## My squad - {}".format(meta["my_team"]))
        A("")
        for p in mine:
            A(line(p))
        A("")

    A("## Free agents - top 30 by points")
    A("")
    for p in free[:30]:
        A(line(p))
    A("")

    A("## Free agents by position - top 12 each")
    A("")
    for pos in ("GKP", "DEF", "MID", "FWD"):
        A("### {}".format(pos))
        A("")
        for p in [x for x in free if x["pos"] == pos][:12]:
            A(line(p))
        A("")

    A("## Other squads")
    A("")
    for eid, squad in rosters.items():
        name = managers.get(int(eid), eid)
        if int(eid) == meta["my_entry_id"]:
            continue
        A("### {}".format(name))
        A("")
        for p in squad:
            A(line(p))
        A("")

    nxt = str(meta.get("next_gw") or "")
    if nxt and nxt in upcoming:
        A("## GW{} fixtures".format(nxt))
        A("")
        for f in sorted(upcoming[nxt], key=lambda x: x["kickoff"] or ""):
            A("- {}  {} (H) v {} (A)".format(
                (f["kickoff"] or "")[:16], f["home"], f["away"]))
        A("")

    A("---")
    A("")
    A("Generated by `scripts/fetch_fpl.py`. Season totals come from "
      "`bootstrap-static`; per-gameweek lines are rebuilt from "
      "`event/{gw}/live`. No team news here - lineups and injuries still "
      "need checking against actual reporting before any transfer.")

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "SUMMARY.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()

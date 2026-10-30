"""
Build the data behind the live app (index.html).

Writes:
    data/app.json        league index: entries, standings, every H2H match,
                         which gameweek to open on, when this was pulled
    data/gw/{n}.json     one file per gameweek: fixtures with scores and
                         status, each manager's XI + bench (auto-subs
                         applied), and per-player earned / projected points

Earned points are whatever the API reported at the pull, plus provisional
bonus (top-3 BPS in a fixture) until the API confirms the real bonus.
Projections are a plain form model (see `project`) - no team news.
"""

import json
import math
import os

POS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
MIN_OUTFIELD = {"DEF": 3, "MID": 2, "FWD": 1}
# bump when the per-gameweek file layout changes, so cached finished
# gameweeks get rebuilt once instead of keeping the old shape forever
SCHEMA = 2


# ---------------------------------------------------------------- helpers

def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, separators=(",", ":"), sort_keys=True,
                  ensure_ascii=False)
        fh.write("\n")


def _fx(f, teams):
    """Normalise a fixture row from either bootstrap or event/live."""
    return {
        "id": f.get("id"),
        "home": teams.get(f.get("team_h"), "?"),
        "away": teams.get(f.get("team_a"), "?"),
        "th": f.get("team_h"),
        "ta": f.get("team_a"),
        "kickoff": (f.get("kickoff_time") or "").replace("Z", "+00:00") or None,
        "started": bool(f.get("started")),
        # finished_provisional = final whistle; finished = bonus confirmed
        "done": bool(f.get("finished") or f.get("finished_provisional")),
        "confirmed": bool(f.get("finished")),
        "minutes": f.get("minutes") or 0,
        "hs": f.get("team_h_score"),
        "as": f.get("team_a_score"),
    }


def _prov_bonus(live_elements, fixtures, el_team):
    """Top-3 BPS per fixture -> 3/2/1, ties share. Only for fixtures that
    have started but whose bonus the API has not confirmed yet."""
    out = {}
    for f in fixtures:
        if not f["started"] or f["confirmed"]:
            continue
        rows = []
        for pid, s in live_elements.items():
            if s.get("minutes", 0) <= 0:
                continue
            if el_team.get(pid) not in (f["th"], f["ta"]):
                continue
            rows.append((s.get("bps", 0), pid))
        rows.sort(reverse=True)
        awarded, i = 0, 0
        while i < len(rows) and awarded < 3:
            bps = rows[i][0]
            tied = [p for b, p in rows if b == bps]
            pts = 3 - awarded
            for p in tied:
                out[p] = out.get(p, 0) + pts
            awarded += len(tied)
            i += len(tied)
    return out


# ---------------------------------------------------------------- projection

def team_form(results):
    """Goals for/against per game per team id, from finished fixtures."""
    gf, ga, n = {}, {}, {}
    for rows in results.values():
        for f in rows:
            if not f["done"] or f["hs"] is None:
                continue
            for t, sc, con in ((f["th"], f["hs"], f["as"]), (f["ta"], f["as"], f["hs"])):
                gf[t] = gf.get(t, 0) + sc
                ga[t] = ga.get(t, 0) + con
                n[t] = n.get(t, 0) + 1
    rate = {t: (gf[t] / n[t], ga[t] / n[t]) for t in n}
    if rate:
        avg = sum(v[0] for v in rate.values()) / len(rate)
    else:
        avg = 1.4
    return rate, avg or 1.4


# Shrinkage: points-per-appearance from a handful of games is mostly noise,
# so blend it with a league-typical value, weighted as if we'd seen this
# many extra appearances. Picked by backtest, see `project`.
SHRINK_APPS = 3
SHRINK_TO = 3.0


def project(el, hist, gw_fixtures, form, avg_goals, upto):
    """
    Expected points for one gameweek, using only gameweeks <= `upto`.

    season points-per-appearance, shrunk toward SHRINK_TO
      x chance he plays (recent appearance rate x injury/availability flag)
      x number of fixtures (blank = 0, double = 2).

    Backtested on GW5-8 (project each week from prior weeks only, compare
    with what happened). Mean absolute error per player, startable players:
        plain season average           3.48
        60% recent form + fixture adj  3.72   <- the previous version
        this                           3.11
    Recent form and an opponent goals for/against adjustment were both
    tested and made it worse - over 4-8 games they're noise - so they're out.
    `form` / `avg_goals` are kept in the signature for when there's enough
    season to estimate opponent strength properly.

    The appearance window is anchored to `upto` (the last finished gameweek),
    not to the player's own last appearance: history only stores weeks a
    player registered stats, so anchoring on his last entry would make a
    run of recent absences invisible.
    """
    pid = str(el["id"])
    status = el.get("status", "a")
    if status in ("i", "s", "u", "n"):
        avail = 0.0
    else:
        ch = el.get("chance_of_playing_next_round")
        avail = 1.0 if ch is None else ch / 100.0
    if avail == 0:
        return 0.0, 0.0

    gws = {int(g): v for g, v in hist.get(pid, {}).items()
           if not g.startswith("_") and int(g) <= upto}
    last = list(range(max(1, upto - 4), upto + 1)) if upto >= 1 else []
    played_all = [v for v in gws.values() if (v.get("minutes") or 0) > 0]
    if not played_all:
        return 0.0, 0.0
    n = len(played_all)
    season = sum(r.get("total_points", 0) for r in played_all) / n
    base = (season * n + SHRINK_TO * SHRINK_APPS) / (n + SHRINK_APPS)

    # appearance rate, most recent gameweeks weighted heavier
    if last:
        w = [1, 1, 1.5, 2, 2.5][-len(last):]
        hit = [1 if (gws.get(g, {}).get("minutes") or 0) > 0 else 0 for g in last]
        app_rate = sum(a * b for a, b in zip(w, hit)) / sum(w)
    else:
        app_rate = 0.0
    p_play = max(0.0, min(1.0, app_rate * avail))

    tid = el.get("team")
    n_fx = sum(1 for f in gw_fixtures if tid in (f["th"], f["ta"]))
    return round(base * n_fx * p_play, 2), round(p_play, 2)


# ---------------------------------------------------------------- lineups

def best_xi(squad, proj):
    """Pre-deadline guess at each manager's XI: highest projections in a
    legal shape. Returns 15 element ids in pick order (12 = reserve GK)."""
    by = {p: sorted([e for e in squad if e["pos"] == p],
                    key=lambda e: -proj.get(e["id"], 0)) for p in ("GKP", "DEF", "MID", "FWD")}
    xi = by["GKP"][:1]
    for pos, n in MIN_OUTFIELD.items():
        xi += by[pos][:n]
    rest = sorted([e for pos in MIN_OUTFIELD for e in by[pos][MIN_OUTFIELD[pos]:]],
                  key=lambda e: -proj.get(e["id"], 0))
    xi += rest[:10 - (len(xi) - 1)]
    picked = {e["id"] for e in xi}
    bench = by["GKP"][1:2] + [e for e in rest if e["id"] not in picked]
    order = {"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}
    xi.sort(key=lambda e: order[e["pos"]])
    return [e["id"] for e in xi] + [e["id"] for e in bench]


def _legal(xi_pos):
    if xi_pos.count("GKP") != 1:
        return False
    return all(xi_pos.count(p) >= n for p, n in MIN_OUTFIELD.items())


def apply_subs(order, pos_of, players, team_done):
    """
    Auto-subs as the game applies them: a starter with zero minutes whose
    matches are all over is replaced by the first bench player (in order)
    who has played, as long as the shape stays legal. If the next eligible
    bench player's match is still to come, the sub is left pending rather
    than skipping ahead to someone further down the bench.
    Returns (xi ids, bench ids, list of (out, in)).
    """
    xi, bench = list(order[:11]), list(order[11:])
    subs = []
    for out in list(xi):
        p = players.get(out, {})
        if p.get("minutes", 0) > 0 or not team_done(out):
            continue
        for cand in bench:
            c = players.get(cand, {})
            if (pos_of[out] == "GKP") != (pos_of[cand] == "GKP"):
                continue
            trial = [cand if x == out else x for x in xi]
            if not _legal([pos_of[x] for x in trial]):
                continue  # couldn't come on for this player whatever happens
            if c.get("minutes", 0) <= 0:
                if not team_done(cand):
                    break  # he may still come on: the real sub waits for him
                continue
            xi = trial
            bench.remove(cand)
            bench.append(out)
            subs.append([out, cand])
            break
    return xi, bench, subs


def carry_forward(base_order, txs, owned_ids, pos_of):
    """
    What a manager has actually saved for the coming gameweek, as far as
    public data shows: FPL Draft carries the last locked lineup forward, and
    a signing drops into the exact slot of the player it replaced. So take
    the last locked order and replay accepted transactions onto it.
    Returns 15 ids in pick order, or None if the result isn't a clean,
    legal squad (then the caller falls back to a projected XI).
    Can't see reorders the manager made since - only the deadline reveals those.
    """
    order = list(base_order)
    for t in txs:
        o, i = t.get("element_out"), t.get("element_in")
        if o in order:
            order[order.index(o)] = i
    own = list(owned_ids)
    order = [p for p in order if p in own]        # traded away / dropped
    order += [p for p in own if p not in order]   # arrived some other way
    if len(order) != 15 or not _legal([pos_of.get(p, "?") for p in order[:11]]):
        return None
    return order


# ---------------------------------------------------------------- main build

def build(get, boot, details, status, history, out_dir):
    teams = {t["id"]: t["short_name"] for t in boot["teams"]}
    team_code = {t["id"]: t.get("code") for t in boot["teams"]}
    elements = {e["id"]: e for e in boot["elements"]}
    el_team = {pid: e.get("team") for pid, e in elements.items()}
    pos_of = {pid: POS.get(e.get("element_type"), "?") for pid, e in elements.items()}

    events = boot.get("events") or {}
    ev_list = events.get("data", []) if isinstance(events, dict) else list(events)
    ev_by = {e["id"]: e for e in ev_list}
    finished = sorted(e["id"] for e in ev_list if e.get("finished"))
    cur = events.get("current") if isinstance(events, dict) else None
    if not cur:
        cur = max(finished) if finished else 1
    cur_ev = ev_by.get(cur, {})
    nxt = events.get("next") if isinstance(events, dict) else None
    # open on the live gameweek; once it's finished, move to the next one
    app_gw = nxt if (cur_ev.get("finished") and nxt) else cur

    entries = {}
    for e in details.get("league_entries", []):
        if not e.get("entry_id"):
            continue
        entries[str(e["id"])] = {
            "entry_id": e["entry_id"],
            "name": e.get("entry_name") or "?",
            "manager": " ".join(x for x in (e.get("player_first_name"), e.get("player_last_name")) if x),
            "short": e.get("short_name") or "",
        }
    entry_to_lid = {v["entry_id"]: k for k, v in entries.items()}

    matches = [{
        "gw": m.get("event"),
        "a": str(m.get("league_entry_1")),
        "b": str(m.get("league_entry_2")),
        "ap": m.get("league_entry_1_points", 0),
        "bp": m.get("league_entry_2_points", 0),
        "started": bool(m.get("started")),
        "finished": bool(m.get("finished")),
    } for m in details.get("matches", [])]

    standings = [{
        "lid": str(r.get("league_entry")),
        "rank": r.get("rank"),
        "w": r.get("matches_won", 0), "d": r.get("matches_drawn", 0),
        "l": r.get("matches_lost", 0), "pts": r.get("total", 0),
        "pf": r.get("points_for", 0), "pa": r.get("points_against", 0),
    } for r in details.get("standings", [])]

    # --- caches: fixture results per gw, picks per gw per entry ----------
    results_path = os.path.join(out_dir, "results.json")
    picks_path = os.path.join(out_dir, "picks.json")
    results = _load(results_path, {})
    picks = _load(picks_path, {})

    live_cache = {}

    def live(gw):
        if gw not in live_cache:
            live_cache[gw] = get("event/{}/live".format(gw)) or {}
        return live_cache[gw]

    upcoming = boot.get("fixtures") or {}
    for gw in range(1, (app_gw or cur) + 1):
        key = str(gw)
        need = key not in results or gw >= cur
        if not need:
            continue
        if gw <= cur:
            rows = live(gw).get("fixtures") or []
        else:
            rows = upcoming.get(key, []) if isinstance(upcoming, dict) else []
        if rows:
            results[key] = [_fx(f, teams) for f in rows]
    if app_gw and str(app_gw) not in results and isinstance(upcoming, dict):
        results[str(app_gw)] = [_fx(f, teams) for f in upcoming.get(str(app_gw), [])]

    deadline_passed = lambda gw: gw <= cur  # noqa: E731
    for gw in range(1, (app_gw or cur) + 1):
        if not deadline_passed(gw):
            continue
        key = str(gw)
        have = picks.get(key, {})
        refresh = gw == cur and not cur_ev.get("finished")
        for lid, ent in entries.items():
            if lid in have and not refresh:
                continue
            r = get("entry/{}/event/{}".format(ent["entry_id"], gw))
            if not r or not r.get("picks"):
                continue
            have[lid] = {
                "order": [p["element"] for p in sorted(r["picks"], key=lambda p: p.get("position", 99))],
                "subs": [[s.get("element_out"), s.get("element_in")] for s in (r.get("subs") or [])],
            }
        picks[key] = have

    _dump(results_path, results)
    _dump(picks_path, picks)

    # --- transactions: waivers + free agency, kept for lineup carry-forward --
    league_id = (details.get("league") or {}).get("id")
    tx_raw = get("draft/league/{}/transactions".format(league_id)) if league_id else None
    tx = (tx_raw or {}).get("transactions", []) if isinstance(tx_raw, dict) else []
    by_eid = {v["entry_id"]: v["name"] for v in entries.values()}
    _dump(os.path.join(out_dir, "transactions.json"), [{
        "id": t.get("id"), "gw": t.get("event"),
        "team": by_eid.get(t.get("entry"), t.get("entry")),
        "in": (elements.get(t.get("element_in")) or {}).get("web_name"),
        "out": (elements.get(t.get("element_out")) or {}).get("web_name"),
        "kind": {"w": "waiver", "f": "free agent"}.get(t.get("kind"), t.get("kind")),
        "result": {"a": "accepted"}.get(t.get("result"), "denied"),
        "added": t.get("added"),
    } for t in sorted(tx, key=lambda t: t.get("id") or 0)])
    last_finished = max(finished) if finished else 0

    # --- current squads (for the pre-deadline view) -------------------------
    owned = {}
    for r in status.get("element_status", []):
        if r.get("owner"):
            owned.setdefault(r["owner"], []).append(r["element"])

    form = team_form({k: v for k, v in results.items() if int(k) < (app_gw or cur)})

    # --- per-gameweek files -------------------------------------------------
    gw_dir = os.path.join(out_dir, "gw")
    built = []
    for gw in range(1, (app_gw or cur) + 1):
        key = str(gw)
        fixtures = results.get(key, [])
        is_live_gw = gw == cur and not cur_ev.get("finished")
        is_future = gw > cur
        cached = os.path.join(gw_dir, key + ".json")
        if gw < cur - 1 and _load(cached, {}).get("v") == SCHEMA:
            built.append(gw)
            continue

        # earned this gw
        earned = {}
        if is_live_gw or gw == cur:
            els = live(gw).get("elements") or {}
            items = els.items() if isinstance(els, dict) else enumerate(els)
            for pid, row in items:
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    continue
                s = row.get("stats", row) if isinstance(row, dict) else {}
                earned[pid] = s
        else:
            for pid, rows in history.items():
                if pid.startswith("_"):
                    continue
                s = rows.get(key)
                if s:
                    earned[int(pid)] = s
        prov = _prov_bonus(earned, fixtures, el_team) if fixtures else {}

        team_fx = {}
        for f in fixtures:
            team_fx.setdefault(f["th"], []).append(f)
            team_fx.setdefault(f["ta"], []).append(f)

        def team_done(pid):
            fx = team_fx.get(el_team.get(pid), [])
            return all(f["done"] for f in fx)  # blank gw counts as done

        # who needs a line in this file
        lineups = {}
        proj = {}
        if is_future or is_live_gw:
            upto = min(gw - 1, last_finished)
            for pid, el in elements.items():
                pv, pp = project(el, history, fixtures, form[0], form[1], upto)
                proj[pid] = (pv, pp)
        if is_future:
            for lid, ent in entries.items():
                own = owned.get(ent["entry_id"], [])
                base = None
                for g in range(cur, 0, -1):
                    b = picks.get(str(g), {}).get(lid)
                    if b and b.get("order"):
                        base = (g, b["order"])
                        break
                order = None
                if base:
                    txs = sorted((t for t in tx if t.get("result") == "a"
                                  and t.get("entry") == ent["entry_id"]
                                  and (t.get("event") or 0) > base[0]),
                                 key=lambda t: t.get("id") or 0)
                    order = carry_forward(base[1], txs, own, pos_of)
                how = "carried"
                if not order:
                    squad = [{"id": pid, "pos": pos_of.get(pid, "?")} for pid in own]
                    order = best_xi(squad, {k: v[0] for k, v in proj.items()})
                    how = "projected"
                lineups[lid] = {"xi": order[:11], "bench": order[11:], "subs": [], "guess": how}
        else:
            for lid, pk in picks.get(key, {}).items():
                order = pk["order"]
                if pk.get("subs") and not is_live_gw:
                    xi, bench = list(order[:11]), list(order[11:])
                    for o, i in pk["subs"]:
                        if o in xi and i in bench:
                            xi[xi.index(o)] = i
                            bench[bench.index(i)] = o
                    subs = pk["subs"]
                else:
                    xi, bench, subs = apply_subs(
                        order, pos_of,
                        {p: {"minutes": (earned.get(p) or {}).get("minutes", 0)} for p in order},
                        team_done)
                lineups[lid] = {"xi": xi, "bench": bench, "subs": subs, "guess": False}

        need = {p for L in lineups.values() for p in L["xi"] + L["bench"]}
        players = {}
        for pid in need:
            el = elements.get(pid, {})
            s = earned.get(pid) or {}
            pb = prov.get(pid, 0) if not s.get("bonus") else 0
            pts = (s.get("total_points") or 0) + pb
            fx = team_fx.get(el.get("team"), [])
            if not fx:
                state = "blank"
            elif all(f["done"] for f in fx):
                state = "done"
            elif any(f["started"] for f in fx):
                state = "live"
            else:
                state = "pre"
            pv, pp = proj.get(pid, (0.0, 0.0))
            if state == "live" and (s.get("minutes") or 0) > 0:
                f0 = next((f for f in fx if f["started"] and not f["done"]), None)
                left = max(0, 90 - (f0["minutes"] if f0 else 90)) / 90.0
                pv = pts + pv * left
            elif state in ("done", "blank"):
                pv = pts
            elif state == "live":
                pv = pts + pv * 0.3  # on the bench in a live match
            players[str(pid)] = {
                "n": el.get("web_name", "?"),
                "t": teams.get(el.get("team"), "?"),
                "c": el.get("code"),                    # photo id
                "tc": team_code.get(el.get("team")),    # shirt id
                "p": pos_of.get(pid, "?"),
                # today's flags only mean something for a week still to play
                "st": el.get("status", "a") if (is_future or is_live_gw) else "a",
                "news": (el.get("news") or "").strip() if (is_future or is_live_gw) else "",
                "m": s.get("minutes") or 0,
                "pts": pts,
                "pb": pb,
                "g": s.get("goals_scored") or 0,
                "a": s.get("assists") or 0,
                "cs": s.get("clean_sheets") or 0,
                "b": s.get("bonus") or 0,
                "bps": s.get("bps") or 0,
                "yc": s.get("yellow_cards") or 0,
                "rc": s.get("red_cards") or 0,
                "sv": s.get("saves") or 0,
                "dc": s.get("defensive_contribution") or 0,
                "state": state,
                "proj": round(pv, 1),
                "pp": pp,
                "fx": [f["id"] for f in fx],
            }

        doc = {
            "v": SCHEMA,
            "gw": gw,
            "state": "upcoming" if is_future else ("live" if is_live_gw else "finished"),
            "deadline": (ev_by.get(gw, {}).get("deadline_time") or "").replace("Z", "+00:00") or None,
            "fixtures": [{k: v for k, v in f.items() if k not in ("th", "ta")} for f in fixtures],
            "lineups": lineups,
            "players": players,
        }
        _dump(os.path.join(gw_dir, key + ".json"), doc)
        built.append(gw)

    app = {
        "league": (details.get("league") or {}).get("name") or "FPL Draft",
        "league_id": (details.get("league") or {}).get("id"),
        "me": entry_to_lid.get(int(os.environ.get("FPL_ENTRY_ID", "174590"))),
        "entries": entries,
        "standings": standings,
        "matches": matches,
        "gws": sorted(built),
        "open_gw": app_gw,
        "current_gw": cur,
        "next_deadline": (ev_by.get(nxt, {}).get("deadline_time") or "").replace("Z", "+00:00") or None,
        "next_waivers": (ev_by.get(nxt, {}).get("waivers_time") or "").replace("Z", "+00:00") or None,
    }
    return app

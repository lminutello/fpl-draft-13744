"""
Bookmaker-odds projection model.

Team level
    Every match with odds (football-data.co.uk: 1X2 and over/under 2.5) is
    turned into expected goals for both sides - the Poisson pair (lh, la) whose
    home/draw/away and over-2.5 probabilities best match the de-vigged market.
    Those market-implied numbers are then used two ways:
      * a fixture whose own odds are posted uses them directly;
      * otherwise each team gets an attack and a defence rating fitted to the
        market-implied goals of its past matches (recent games weighted more),
        so the ratings are the market's view, not raw results.

Player level (per fixture, given he plays)
    appearance   2 if 60+ minutes else 1           (his 60+ rate, shrunk)
    goals        team goals x his share of team xG x points per goal
    assists      team goals x his xA / team xG    x 3
    clean sheet  P(opponent scores 0) x his 60+ rate x 4 (GK/DEF) or 1 (MID)
    conceded     -1 per 2 goals conceded (GK/DEF), scaled by minutes
    saves        +1 per 3 saves (GK), his saves rate scaled to the opponent
    DEFCON       his hit rate (shrunk to the position average) x 2
    bonus, cards his per-appearance averages (shrunk)
    ... then x chance he plays, summed over the gameweek's fixtures.

Pure standard library: it runs inside the GitHub Action with no installs.
"""

import csv
import math
import os

FD_TO_FPL = {
    "Arsenal": "ARS", "Aston Villa": "AVL", "Bournemouth": "BOU",
    "Brentford": "BRE", "Brighton": "BHA", "Burnley": "BUR", "Chelsea": "CHE",
    "Coventry": "COV", "Crystal Palace": "CRY", "Everton": "EVE",
    "Fulham": "FUL", "Hull": "HUL", "Ipswich": "IPS", "Leeds": "LEE",
    "Leicester": "LEI", "Liverpool": "LIV", "Man City": "MCI",
    "Man United": "MUN", "Newcastle": "NEW", "Nott'm Forest": "NFO",
    "Southampton": "SOU", "Sunderland": "SUN", "Tottenham": "TOT",
    "West Ham": "WHU", "Wolves": "WOL",
}

GOAL_PTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
CS_PTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
DEFCON_PTS = 2
MAX_G = 10
HALF_LIFE = 5.0      # gameweeks, for the team ratings
RIDGE = 0.25        # light pull toward average; tuned so ratings match next week's odds


# ------------------------------------------------------------ odds -> goals

def _pois(lam):
    out, p = [], math.exp(-lam)
    for k in range(MAX_G + 1):
        out.append(p)
        p *= lam / (k + 1)
    return out


def _probs(lh, la):
    ph, pa = _pois(lh), _pois(la)
    h = d = a = over = 0.0
    for i, x in enumerate(ph):
        for j, y in enumerate(pa):
            q = x * y
            if i > j:
                h += q
            elif i == j:
                d += q
            else:
                a += q
            if i + j >= 3:
                over += q
    return h, d, a, over


def _f(row, *keys):
    for k in keys:
        try:
            v = float(row.get(k) or "")
            if v > 1.0:
                return v
        except ValueError:
            pass
    return None


def implied_goals(row, closing=False):
    """(lh, la) from one football-data row, or None if the odds are missing.
    Pre-match = market average a couple of days out; closing = at kickoff."""
    c = "C" if closing else ""
    oh = _f(row, f"Avg{c}H", f"B365{c}H", f"Max{c}H")
    od = _f(row, f"Avg{c}D", f"B365{c}D", f"Max{c}D")
    oa = _f(row, f"Avg{c}A", f"B365{c}A", f"Max{c}A")
    if not (oh and od and oa):
        return None
    s = 1 / oh + 1 / od + 1 / oa
    th, td, ta = (1 / oh) / s, (1 / od) / s, (1 / oa) / s
    oo = _f(row, f"Avg{c}>2.5", f"B365{c}>2.5", f"Max{c}>2.5")
    ou = _f(row, f"Avg{c}<2.5", f"B365{c}<2.5", f"Max{c}<2.5")
    to = (1 / oo) / (1 / oo + 1 / ou) if (oo and ou) else None

    def err(lh, la):
        h, d, a, over = _probs(lh, la)
        e = (h - th) ** 2 + (d - td) ** 2 + (a - ta) ** 2
        if to is not None:
            e += (over - to) ** 2
        return e

    best = (9e9, 1.4, 1.1)
    for i in range(1, 23):
        for j in range(1, 23):
            lh, la = 0.2 * i, 0.2 * j
            e = err(lh, la)
            if e < best[0]:
                best = (e, lh, la)
    _, lh, la = best
    step = 0.1
    while step > 0.004:
        improved = True
        while improved:
            improved = False
            for dh, da in ((step, 0), (-step, 0), (0, step), (0, -step)):
                nh, na = lh + dh, la + da
                if nh <= 0.05 or na <= 0.05:
                    continue
                e = err(nh, na)
                if e < best[0]:
                    best, lh, la, improved = (e, nh, na), nh, na, True
        step /= 2
    return round(lh, 3), round(la, 3)


def load_odds(data_dir):
    """{(home, away): row} for this season's played matches and the upcoming
    sheet (FPL short names)."""
    out = {"season": {}, "upcoming": {}}
    for kind, name in (("season", "season.csv"), ("upcoming", "upcoming.csv")):
        path = os.path.join(data_dir, "odds", name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row.get("Div") not in (None, "", "E0"):
                    continue
                h, a = FD_TO_FPL.get(row.get("HomeTeam")), FD_TO_FPL.get(row.get("AwayTeam"))
                if h and a:
                    out[kind][(h, a)] = row
    return out


# ------------------------------------------------------------ team ratings

def fit_ratings(matches):
    """matches: [(gw, home, away, lh, la)] -> (mu, home_adv, att, dfn)
    log lh = mu + home + att[h] - dfn[a];  log la = mu + att[a] - dfn[h]
    Weighted least squares on the market-implied goals, solved by
    coordinate descent with a small ridge."""
    if not matches:
        return None
    last = max(m[0] for m in matches)
    obs = []
    for gw, h, a, lh, la in matches:
        w = 0.5 ** ((last - gw) / HALF_LIFE)
        obs.append((w, h, a, math.log(lh), True))
        obs.append((w, a, h, math.log(la), False))
    teams = sorted({o[1] for o in obs} | {o[2] for o in obs})
    att = {t: 0.0 for t in teams}
    dfn = {t: 0.0 for t in teams}
    mu, home = 0.25, 0.1
    for _ in range(200):
        r = [(w, y - (mu + (home if hm else 0) + att[s] - dfn[c])) for w, s, c, y, hm in obs]
        mu += sum(w * e for w, e in r) / sum(w for w, _ in r)
        num = sum(w * (y - (mu + home + att[s] - dfn[c])) for w, s, c, y, hm in obs if hm)
        den = sum(w for w, s, c, y, hm in obs if hm)
        home += num / den / 2
        for t in teams:
            num = sum(w * (y - (mu + (home if hm else 0) - dfn[c])) for w, s, c, y, hm in obs if s == t)
            den = sum(w for w, s, c, y, hm in obs if s == t) + RIDGE
            att[t] = num / den
            num = sum(w * ((mu + (home if hm else 0) + att[s]) - y) for w, s, c, y, hm in obs if c == t)
            den = sum(w for w, s, c, y, hm in obs if c == t) + RIDGE
            dfn[t] = num / den
        ma = sum(att.values()) / len(att)
        md = sum(dfn.values()) / len(dfn)
        for t in teams:
            att[t] -= ma
            dfn[t] -= md
        mu += ma - md
    return mu, home, att, dfn


def rated_goals(ratings, h, a):
    mu, home, att, dfn = ratings
    return (math.exp(mu + home + att.get(h, 0) - dfn.get(a, 0)),
            math.exp(mu + att.get(a, 0) - dfn.get(h, 0)))


class Market:
    """Expected goals per fixture as of a gameweek (only information that
    existed before it)."""

    def __init__(self, odds, gw_of_pair, gw):
        self.direct = {}
        past = []
        for (h, a), row in odds["season"].items():
            g = gw_of_pair.get((h, a))
            if g is None:
                continue
            if g < gw:
                lam = implied_goals(row, closing=True) or implied_goals(row)
                if lam:
                    past.append((g, h, a) + lam)
            elif g == gw:
                lam = implied_goals(row)  # pre-match only, never closing
                if lam:
                    self.direct[(h, a)] = lam
        for (h, a), row in odds["upcoming"].items():
            if gw_of_pair.get((h, a)) == gw and (h, a) not in self.direct:
                lam = implied_goals(row)
                if lam:
                    self.direct[(h, a)] = lam
        self.ratings = fit_ratings(past)
        self.team_xg_hist = {}

    def goals(self, h, a):
        if (h, a) in self.direct:
            return self.direct[(h, a)] + ("odds",)
        if self.ratings:
            return rated_goals(self.ratings, h, a) + ("ratings",)
        return 1.45, 1.15, "flat"


# ------------------------------------------------------------ players

def _floor_div_exp(lam, k):
    """E[floor(X / k)] for X ~ Poisson(lam)."""
    return sum(p * (i // k) for i, p in enumerate(_pois(lam)))


def player_rates(hist, team_of, team_xg, upto):
    """Per-player per-appearance rates from gameweeks <= upto, shrunk toward
    the position average. team_xg: {(team, gw): xg} from the odds sheet,
    used as the denominator for goal/assist shares."""
    raw = {}
    for pid, gws in hist.items():
        if pid.startswith("_") or pid not in team_of:
            continue
        team, pos = team_of[pid]
        rows = [(int(g), v) for g, v in gws.items()
                if not g.startswith("_") and int(g) <= upto and (v.get("minutes") or 0) > 0]
        if not rows:
            continue
        n = len(rows)
        tx = sum(team_xg.get((team, g), 1.3) for g, _ in rows)
        xg = sum((v.get("expected_goals") or 0) * 0.7 + (v.get("goals_scored") or 0) * 0.3 for _, v in rows)
        xa = sum((v.get("expected_assists") or 0) * 0.7 + (v.get("assists") or 0) * 0.3 for _, v in rows)
        raw[pid] = {
            "pos": pos, "n": n,
            "g_share": xg / tx if tx else 0,
            "a_share": xa / tx if tx else 0,
            "p60": sum(1 for _, v in rows if (v.get("minutes") or 0) >= 60) / n,
            "mins": sum(min(v.get("minutes") or 0, 90) for _, v in rows) / n / 90,
            "saves": sum(v.get("saves") or 0 for _, v in rows) / n,
            "defcon": sum(1 for _, v in rows if (v.get("defensive_contribution") or 0)
                          >= {"DEF": 10, "MID": 12, "FWD": 12}.get(pos, 10 ** 9)) / n,
            "bonus": sum(v.get("bonus") or 0 for _, v in rows) / n,
            "cards": sum((v.get("yellow_cards") or 0) + 3 * (v.get("red_cards") or 0) for _, v in rows) / n,
        }
    # position averages among regulars, then shrink each player toward them
    prior = {}
    for pos in ("GKP", "DEF", "MID", "FWD"):
        regs = [r for r in raw.values() if r["pos"] == pos and r["n"] >= 3]
        prior[pos] = {k: (sum(r[k] for r in regs) / len(regs) if regs else 0)
                      for k in ("g_share", "a_share", "p60", "mins", "saves", "defcon", "bonus", "cards")}
    shrink = {"g_share": 2, "a_share": 2, "p60": 2, "mins": 2, "saves": 2,
              "defcon": 3, "bonus": 4, "cards": 4}
    out = {}
    for pid, r in raw.items():
        p, n = prior[r["pos"]], r["n"]
        out[pid] = {k: (r[k] * n + p[k] * w) / (n + w) for k, w in shrink.items()}
        out[pid]["pos"] = r["pos"]
    return out


def expected_points(rate, lam_for, lam_against, league_against=1.35):
    """Points given he plays, for one fixture."""
    pos = rate["pos"]
    p60, mins = rate["p60"], rate["mins"]
    pts = 2 * p60 + 1 * (1 - p60)
    pts += lam_for * rate["g_share"] * GOAL_PTS[pos]
    pts += lam_for * rate["a_share"] * 3
    pts += math.exp(-lam_against) * p60 * CS_PTS[pos]
    if pos in ("GKP", "DEF"):
        pts -= _floor_div_exp(lam_against * mins, 2)
    if pos == "GKP":
        pts += _floor_div_exp(rate["saves"] * lam_against / league_against, 3)
    if pos != "GKP":
        pts += rate["defcon"] * DEFCON_PTS
    pts += rate["bonus"] - rate["cards"]
    return pts

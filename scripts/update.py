#!/usr/bin/env python3
"""Hourly data refresh for Game Finder v2. Pulls scores, records, AP ranks and spreads
from ESPN's public scoreboard feed and writes data.json. Uses only the standard library.
Safe by design: anything it cannot match to the app's own games is skipped."""
import json, re, os, sys, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://site.api.espn.com/apis/site/v2/sports/football/"
ALIAS = {"Massachusetts": "UMass", "Connecticut": "UConn", "Hawai'i": "Hawaii", "San José State": "San Jose State",
         "Louisiana": "Louisiana", "Miami": "Miami", "App State": "Appalachian State", "Appalachian St": "Appalachian State"}

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 game-finder"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:
            err = e
    print("fetch failed:", url, err, file=sys.stderr)
    return None

def load_app():
    html = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
    G = json.loads(re.search(r"^const G=(.*);$", html, re.M).group(1))
    return G

def split_teams(ev):
    return [t.strip() for t in re.split(r"\s+(?:at|vs\.)\s+", re.sub(r"\(.*?\)", "", ev))]

def et_date(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")

def name_of(team, lg):
    if lg == "nfl":
        return team.get("name") or team.get("shortDisplayName")
    n = team.get("location") or team.get("shortDisplayName") or ""
    return ALIAS.get(n, n)

def parse_events(data, lg):
    out = []
    for ev in (data or {}).get("events", []):
        try:
            c = ev["competitions"][0]
            comp = {x["homeAway"]: x for x in c["competitors"]}
            a, h = comp["away"], comp["home"]
            st = ev["status"]["type"]
            rec = lambda x: next((r.get("summary") for r in x.get("records", []) if r.get("type") in ("total", None) or r.get("name") == "overall"), "") or ""
            rank = lambda x: (x.get("curatedRank") or {}).get("current")
            spread = None
            if c.get("odds"):
                d = (c["odds"][0].get("details") or "").strip()
                if d.upper() in ("EVEN", "PK", "PICK"):
                    spread = "PK"
                else:
                    m = re.match(r"^([A-Za-z&.\-]+)\s+([+-]?[\d.]+)$", d)
                    if m:
                        ab, line = m.group(1), m.group(2)
                        for side in (a, h):
                            if side["team"].get("abbreviation") == ab:
                                spread = name_of(side["team"], lg) + " " + re.sub(r"\.0+$", "", (line if line.startswith("-") else "-" + line.lstrip("+")))
            venue = c.get("venue") or {}
            addr = venue.get("address") or {}
            note = ""
            if c.get("neutralSite"):
                note = "Neutral site: " + ", ".join(x for x in (addr.get("city"), addr.get("state") or addr.get("country")) if x)
            out.append(dict(
                date=et_date(ev["date"]), lg=lg,
                away=name_of(a["team"], lg), home=name_of(h["team"], lg),
                ascore=int(a.get("score") or 0), hscore=int(h.get("score") or 0),
                completed=bool(st.get("completed")), ot=int(ev["status"].get("period", 4) > 4),
                arec=rec(a), hrec=rec(h), arank=rank(a), hrank=rank(h), spread=spread, note=note))
        except Exception as e:
            print("skip event:", e, file=sys.stderr)
    return out

def main():
    G = load_app()
    games = {}
    for g in G:
        if g[2] not in ("nfl", "ncaa"):
            continue
        t = split_teams(g[3])
        if len(t) == 2:
            games[(et_date(g[0]), frozenset(t))] = (g, t)
    today = datetime.now(ET).date()
    start, end = today - timedelta(days=2), today + timedelta(days=14)
    rng = f"{start:%Y%m%d}-{end:%Y%m%d}"
    events = []
    events += parse_events(get(BASE + f"nfl/scoreboard?dates={rng}&limit=100"), "nfl")
    events += parse_events(get(BASE + f"college-football/scoreboard?dates={rng}&groups=80&limit=400"), "ncaa")
    if not events:
        print("no events fetched; leaving data.json unchanged"); return

    path = os.path.join(ROOT, "data.json")
    try:
        old = json.load(open(path))
    except Exception:
        old = {}
    finals = old.get("finals", [])
    seen = {(f[0], frozenset((f[2], f[4]))) for f in finals}
    spreads, R = {}, {"nfl": {}, "ncaa": {}}
    matched = unmatched = 0
    for e in events:
        key = (e["date"], frozenset((e["away"], e["home"])))
        # records / ranks for any team in the window
        for side, team in (("a", e["away"]), ("h", e["home"])):
            rec = e["arec" if side == "a" else "hrec"]
            rk = e["arank" if side == "a" else "hrank"]
            if rec:
                rk = rk if isinstance(rk, int) and 1 <= rk <= 25 else None
                R[e["lg"]][team] = [rec, rk]
        if key not in games:
            unmatched += 1
            continue
        matched += 1
        g, _ = games[key]
        if e["completed"]:
            if key not in seen:
                finals.append([e["date"], e["lg"], e["away"], e["ascore"], e["home"], e["hscore"], e["ot"], e["note"], e["arec"], e["hrec"]])
                seen.add(key)
        elif e["spread"]:
            spreads[g[0] + "|" + g[3]] = e["spread"]
    # keep only recent finals (keeps file small)
    cutoff = (today - timedelta(days=45)).strftime("%Y-%m-%d")
    finals = [f for f in finals if f[0] >= cutoff]
    data = dict(updated=datetime.now(ET).strftime("%b %-d, %Y %-I:%M %p ET"), finals=finals, spreads=spreads, R=R,
                stats=dict(events=len(events), matched=matched, unmatched=unmatched))
    # only rewrite if content (ignoring the timestamp) changed, so we do not spam commits
    cmp = lambda d: json.dumps({k: v for k, v in d.items() if k not in ("updated", "stats")}, sort_keys=True)
    if old and cmp(old) == cmp(data):
        print("no changes", data["stats"]); return
    json.dump(data, open(path, "w"), ensure_ascii=False, separators=(",", ":"))
    print("wrote data.json", data["stats"], len(finals), "finals", len(spreads), "spreads")

if __name__ == "__main__":
    main()

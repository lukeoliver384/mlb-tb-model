"""
Multi-book Total Bases odds scraper.

PRIMARY SOURCE: Bovada (your book — where you find the most value).
ADAPTERS: DraftKings, FanDuel, BetMGM/Caesars, Pinnacle (pluggable; fill in as
you confirm each payload). Each source implements one method, so adding/removing
a book is a few lines.

WHAT IT PRODUCES
----------------
match_to_slate() returns, per (game, batter):
    { "bovada": {"line": 1.5, "over": "+115", "under": "-145"},
      "draftkings": {...}, ... }
so the board can show every book side by side, pick the best price, and de-vig a
consensus fair line (Pinnacle-anchored when present). collapse_to_store() flattens
that to the app's existing odds-store shape — key
(date, "TB", "{away} @ {home}", batter) → {"line","over","under"} — using the
BEST available Over price, so it drops into the current app with no schema change.

IMPORTANT — I could not reach Bovada from the build sandbox (network allowlisted).
The Bovada parser below walks the JSON generically for "total bases" markets, which
is robust, but run `--dump` once on your machine and share the file so I can pin the
exact field names if anything is off.

USAGE
-----
    python odds_scrape.py --date 2026-07-03 --books bovada,draftkings
    python odds_scrape.py --date 2026-07-03 --dump bovada_raw.json   # save raw for tuning
    python odds_scrape.py --date 2026-07-03 --dry-run                # print matches, don't write
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sys
import unicodedata

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # accented names print correctly on Windows

import data as D

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Accept": "application/json", "Referer": "https://www.bovada.lv/"}
TIMEOUT = 15


# --------------------------------------------------------------------------- #
# Name matching                                                               #
# --------------------------------------------------------------------------- #
def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv)\b\.?", "", s)
    s = re.sub(r"[^a-z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def best_name_match(target: str, candidates: dict, cutoff: float = 0.86):
    """candidates: {norm_name: original}. Returns matched original or None."""
    t = norm_name(target)
    if t in candidates:
        return candidates[t]
    # last-name + first-initial fallback, then fuzzy
    keys = list(candidates)
    hit = difflib.get_close_matches(t, keys, n=1, cutoff=cutoff)
    if hit:
        return candidates[hit[0]]
    tp = t.split()
    if len(tp) >= 2:
        for k in keys:
            kp = k.split()
            if kp and tp and kp[-1] == tp[-1] and kp[0][:1] == tp[0][:1]:
                return candidates[k]
    return None


# --------------------------------------------------------------------------- #
# Odds sources                                                                #
# --------------------------------------------------------------------------- #
class OddsSource:
    name = "base"

    def fetch_tb_props(self, date_str: str) -> list[dict]:
        """Return [{'player': str, 'line': float, 'over': str, 'under': str}]."""
        raise NotImplementedError


class BovadaSource(OddsSource):
    name = "bovada"
    COUPON = ("https://www.bovada.lv/services/sports/event/coupon/events/A/description"
              "/baseball/mlb?marketFilterId=def&preMatchOnly=true&lang=en")
    EVENT = "https://www.bovada.lv/services/sports/event/coupon/events/A/description{link}?lang=en"

    def _get(self, url):
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _event_links(self, date_str: str) -> list[str]:
        data = self._get(self.COUPON)
        links = []
        for grp in data if isinstance(data, list) else [data]:
            for ev in grp.get("events", []):
                link = ev.get("link")
                if link:
                    links.append(link)
        return links

    # Bovada labels player-prop markets exactly "Total Bases - <Player> (TEAM)".
    MARKET_RE = re.compile(r"(?i)^\s*total bases\s*-\s*(.+?)\s*(?:\(([A-Za-z]{2,4})\))?\s*$")

    @staticmethod
    def _american(price: dict) -> str:
        if not isinstance(price, dict):
            return ""
        v = str(price.get("american") or price.get("americanOdds") or "").strip()
        return "+100" if v.upper() == "EVEN" else v          # Bovada uses EVEN for +100

    def _walk_total_bases(self, node, out: list[dict]):
        """Recursively find 'Total Bases - <Player> (TEAM)' markets and extract
        {player, team, line, over, under}. The team tag is captured SEPARATELY so
        it never contaminates the player name (that was breaking name-matching)."""
        if isinstance(node, dict):
            desc = str(node.get("description", ""))
            outcomes = node.get("outcomes")
            m = self.MARKET_RE.match(desc) if outcomes else None
            if m:
                player = m.group(1).strip()
                team = (m.group(2) or "").upper()
                rec = {"player": player, "team": team, "line": None, "over": "", "under": ""}
                for oc in outcomes:
                    od = str(oc.get("description", "")).lower()
                    price = oc.get("price", {})
                    hcap = price.get("handicap") or oc.get("handicap")
                    if hcap:
                        try:
                            rec["line"] = float(hcap)
                        except (TypeError, ValueError):
                            pass
                    if od.startswith("over") or od == "o":
                        rec["over"] = self._american(price)
                    elif od.startswith("under") or od == "u":
                        rec["under"] = self._american(price)
                if player and (rec["over"] or rec["under"]):
                    out.append(rec)
            for v in node.values():
                self._walk_total_bases(v, out)
        elif isinstance(node, list):
            for v in node:
                self._walk_total_bases(v, out)

    def fetch_tb_props(self, date_str: str, _dump=None) -> list[dict]:
        out: list[dict] = []
        for link in self._event_links(date_str):
            try:
                data = self._get(self.EVENT.format(link=link))
            except Exception:
                continue
            if _dump is not None:
                _dump.append(data)
            self._walk_total_bases(data, out)
        # dedup by player, keep first
        seen, dedup = set(), []
        for r in out:
            k = norm_name(r["player"])
            if k and k not in seen:
                seen.add(k)
                dedup.append(r)
        return dedup


class _StubSource(OddsSource):
    """Adapter skeleton — confirm the book's public endpoint, then implement
    fetch_tb_props to return the same {player,line,over,under} shape."""
    def fetch_tb_props(self, date_str: str) -> list[dict]:
        return []


class DraftKingsSource(_StubSource):
    name = "draftkings"   # public: sportsbook.draftkings.com eventgroup 84240, batter-props/total-bases


class FanDuelSource(_StubSource):
    name = "fanduel"


class BetMGMSource(_StubSource):
    name = "betmgm"


class PinnacleSource(_StubSource):
    name = "pinnacle"     # sharp anchor for the no-vig consensus


SOURCES = {s.name: s for s in [BovadaSource(), DraftKingsSource(), FanDuelSource(),
                               BetMGMSource(), PinnacleSource()]}


# --------------------------------------------------------------------------- #
# Slate matching + consensus                                                  #
# --------------------------------------------------------------------------- #
def _slate_batters(date_str: str) -> list[tuple[str, str]]:
    """(game, batter_name) for the date, matching the app's keys exactly."""
    games = D.get_schedule(date_str)
    pairs = []
    for mu in games:
        try:
            mu.home_lineup = D.get_lineup(mu.game_pk, "home")
            mu.away_lineup = D.get_lineup(mu.game_pk, "away")
        except Exception:
            continue
        g = f"{mu.away} @ {mu.home}"
        for b in (mu.home_lineup + mu.away_lineup):
            pairs.append((g, b.name))
    return pairs


def match_to_slate(props_by_book: dict, slate_pairs: list[tuple[str, str]]) -> dict:
    """{(game, batter): {book: {line,over,under}}}."""
    result: dict = {}
    # candidate map per book: {norm_player: record}
    book_cand = {}
    for book, props in props_by_book.items():
        book_cand[book] = ({norm_name(p["player"]): p for p in props})
    for game, batter in slate_pairs:
        row = {}
        for book, cand in book_cand.items():
            m = best_name_match(batter, {k: k for k in cand})
            if m:
                p = cand[m]
                row[book] = {"line": p.get("line"), "over": p.get("over"), "under": p.get("under")}
        if row:
            result[(game, batter)] = row
    return result


def _american_to_prob(a: str):
    if str(a).strip().upper() == "EVEN":
        a = 100
    try:
        a = float(str(a).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return 100 / (a + 100) if a > 0 else -a / (-a + 100)


def consensus_fair(row: dict, anchor: str = "pinnacle"):
    """De-vigged fair P(Over) from the books. Pinnacle-anchored if available,
    else average of each book's two-sided no-vig."""
    def novig(rec):
        po, pu = _american_to_prob(rec.get("over")), _american_to_prob(rec.get("under"))
        if po and pu and (po + pu) > 0:
            return po / (po + pu)
        return None
    if anchor in row:
        v = novig(row[anchor])
        if v is not None:
            return v
    vals = [novig(r) for r in row.values()]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def best_over(row: dict):
    """(book, american) with the best (highest payout) Over price."""
    best = None
    for book, rec in row.items():
        p = _american_to_prob(rec.get("over"))
        if p is not None and (best is None or p < best[2]):
            best = (book, rec.get("over"), p)
    return (best[0], best[1]) if best else (None, None)


def collapse_to_store(matched: dict, date_str: str, prop: str = "TB") -> dict:
    """Flatten to the app's odds store: (date, prop, game, batter) -> {line,over,under}
    using the best Over price and its book's line/under."""
    store = {}
    for (game, batter), row in matched.items():
        book, over = best_over(row)
        if not book:
            continue
        rec = row[book]
        entry = {}
        if rec.get("line") is not None:
            entry["line"] = str(rec["line"])
        if over:
            entry["over"] = str(over)
        if rec.get("under"):
            entry["under"] = str(rec["under"])
        if entry:
            store[(date_str, prop, game, batter)] = entry
    return store


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="Multi-book Total Bases odds scraper.")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    ap.add_argument("--books", default="bovada",
                    help="comma list: bovada,draftkings,fanduel,betmgm,pinnacle")
    ap.add_argument("--dump", default=None, help="save raw payloads to this file (Bovada only)")
    ap.add_argument("--dry-run", action="store_true", help="print matches, don't write to store")
    args = ap.parse_args()

    books = [b.strip() for b in args.books.split(",") if b.strip() in SOURCES]
    props_by_book = {}
    for b in books:
        src = SOURCES[b]
        try:
            if b == "bovada" and args.dump is not None:
                dump = []
                props = src.fetch_tb_props(args.date, _dump=dump)
                with open(args.dump, "w", encoding="utf-8") as f:
                    json.dump(dump, f)
                print(f"[bovada] dumped {len(dump)} event payloads -> {args.dump}")
            else:
                props = src.fetch_tb_props(args.date)
            props_by_book[b] = props
            print(f"[{b}] {len(props)} TB props")
        except Exception as e:
            print(f"[{b}] ERROR {e}")
            props_by_book[b] = []

    slate = _slate_batters(args.date)
    matched = match_to_slate(props_by_book, slate)
    print(f"\nMatched {len(matched)} batters to the slate ({len(slate)} lineup spots).")

    for (game, batter), row in list(matched.items())[:15]:
        fair = consensus_fair(row)
        book, over = best_over(row)
        books_str = "  ".join(f"{k}:{v.get('over','?')}/{v.get('under','?')}@{v.get('line','?')}"
                              for k, v in row.items())
        fair_s = f"{fair*100:.1f}%" if fair else "n/a"
        print(f"  {batter:22s} | best Over {over} ({book}) | fair {fair_s} | {books_str}")

    if args.dry_run:
        print("\n--dry-run: not writing to store.")
        return

    store = collapse_to_store(matched, args.date)
    try:
        import tracker as T
        existing = T.read_odds()
        existing.update(store)          # scraped odds fill/refresh; manual entries preserved otherwise
        where = T.write_odds(existing)
        print(f"\nWrote {len(store)} odds rows to store ({where}).")
    except Exception as e:
        print(f"\n(store write skipped: {e}) — {len(store)} rows ready.")


if __name__ == "__main__":
    main()

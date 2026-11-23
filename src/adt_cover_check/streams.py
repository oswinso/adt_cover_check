"""Fetch Spotify stream counts (total and daily) for every covered song.

Spotify's own API doesn't publish stream counts, so the numbers come from kworb.net, which lists
them per artist. The artist's Spotify id is found once through MusicBrainz and cached.

Usage:
    uv run adt-streams                  # refresh the counts (also runs at the end of adt-sync)
    uv run adt-streams --retry-missing  # also retry artists whose Spotify id wasn't found
"""
import argparse
import collections
import html
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse

from .paths import DB_PATH, OVERRIDES_PATH
from .sync import NO_ARTIST, artist_matches, build_records, http_json, http_text, key_of, strip_parens, title_keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS spotify_artists (
    lookup_key TEXT PRIMARY KEY, spotify_id TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS song_streams (
    artist_key TEXT, song_key TEXT, track_id TEXT, track_name TEXT, total INTEGER, daily INTEGER,
    PRIMARY KEY (artist_key, song_key)
);
"""
MB = "https://musicbrainz.org/ws/2/"
ROW_RE = re.compile(
    r'<a href="https://open\.spotify\.com/track/(\w+)"[^>]*>(.*?)</a>.*?<td>([\d,]+)</td>\s*<td>([\d,]*)</td>', re.S)


def wanted_keys(rec):
    return {key_of(rec["lookup_song"]), key_of(rec["song"]), key_of(strip_parens(rec["song"]))} - {""}


def track_keys(name):
    """Keys for a kworb track name; "Uh-Oh - Japanese ver." also matches on the part before " - "."""
    return title_keys(name) | title_keys(re.split(r"\s+-\s+", name)[0])


def find_spotify_artist(artist, recs):
    """MusicBrainz: who is credited on this artist's songs -> their Spotify artist link."""
    mbids = collections.Counter()
    for rec in recs[:3]:
        q = f'recording:"{rec["lookup_song"]}" AND artist:"{artist}"'
        data = http_json(MB + "recording?fmt=json&limit=50&query=" + urllib.parse.quote(q))
        time.sleep(1.1)
        for r in data.get("recordings", []):
            if not title_keys(r.get("title", "")) & wanted_keys(rec):
                continue
            for c in r.get("artist-credit", []):
                a = c.get("artist", {})
                if a.get("id") and artist_matches([c.get("name", ""), a.get("name", "")], artist):
                    mbids[a["id"]] += 1
        if mbids:
            break
    if not mbids:  # no recording matched: fall back to an exact-name artist search
        data = http_json(MB + "artist?fmt=json&limit=10&query=" + urllib.parse.quote(f'artist:"{artist}"'))
        time.sleep(1.1)
        for a in data.get("artists", []):
            if artist_matches([a.get("name", "")] + [al.get("name", "") for al in a.get("aliases", [])], artist):
                mbids[a["id"]] += 1
                break
    for mbid, _ in mbids.most_common(2):
        data = http_json(MB + f"artist/{mbid}?fmt=json&inc=url-rels")
        time.sleep(1.1)
        for rel in data.get("relations", []):
            m = re.match(r"https://open\.spotify\.com/artist/(\w+)", rel.get("url", {}).get("resource", ""))
            if m:
                return m.group(1)
    return None


def kworb_tracks(spotify_id):
    """-> [(track_id, name, total, daily)] for one artist, or None if kworb has no page for them."""
    try:
        page = http_text(f"https://kworb.net/spotify/artist/{spotify_id}_songs.html")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    num = lambda s: int(s.replace(",", "")) if s else None
    return [(tid, html.unescape(re.sub(r"<[^>]+>", "", name)), num(total), num(daily))
            for tid, name, total, daily in ROW_RE.findall(page)]


def refresh(db, records, retry_missing=False):
    db.executescript(SCHEMA)
    by_artist = collections.defaultdict(list)       # lookup artist name -> its distinct songs
    seen = set()
    for r in records:
        k = (r["artist_key"], r["song_key"])
        if r["artist_key"] != key_of(NO_ARTIST) and k not in seen:
            seen.add(k)
            by_artist[r["lookup_artist"]].append(r)

    # 1. Spotify artist ids (cached; manual "spotify_artist_id" in overrides.json wins).
    cached = {k: (sid, status) for k, sid, status in db.execute("SELECT * FROM spotify_artists")}
    for artist, recs in by_artist.items():
        lk = key_of(artist)
        manual = next((r["spotify_artist_id"] for r in recs if r.get("spotify_artist_id")), None)
        if manual:
            cached[lk] = (manual, "override")
        elif lk not in cached or cached[lk][1] == "error" or (retry_missing and cached[lk][1] == "none"):
            try:
                sid = find_spotify_artist(artist, recs)
                cached[lk] = (sid, "ok" if sid else "none")
            except Exception as e:
                print(f"    musicbrainz error: {e}", file=sys.stderr)
                cached[lk] = (None, "error")
            print(f"  Spotify artist for {artist}: {cached[lk][0] or 'not found'}")
        else:
            continue
        db.execute("INSERT OR REPLACE INTO spotify_artists VALUES (?,?,?)", (lk, *cached[lk]))
        db.commit()

    # 2. Stream counts from kworb, one page per artist.
    found = total_songs = 0
    for artist, recs in by_artist.items():
        total_songs += len(recs)
        sid = cached.get(key_of(artist), (None,))[0]
        if not sid:
            continue
        try:
            tracks = kworb_tracks(sid)
        except Exception as e:  # keep whatever counts we already have
            print(f"    kworb error for {artist}: {e}", file=sys.stderr)
            continue
        finally:
            time.sleep(1.0)
        for rec in recs:
            k = (rec["artist_key"], rec["song_key"])
            wanted = wanted_keys(rec)
            hits = [t for t in tracks or [] if track_keys(t[1]) & wanted]
            if hits:  # prefer an exact title; among versions (remixes, Japanese ver.) the most-streamed
                exact = [t for t in hits if key_of(t[1]) in wanted]
                tid, name, total, daily = max(exact or hits, key=lambda t: t[2] or 0)
                db.execute("INSERT OR REPLACE INTO song_streams VALUES (?,?,?,?,?,?)", (*k, tid, name, total, daily))
                found += 1
            else:
                db.execute("DELETE FROM song_streams WHERE artist_key = ? AND song_key = ?", k)
        db.commit()
    db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('streams_updated', ?)", (time.strftime("%Y-%m-%d %H:%M"),))
    db.commit()
    print(f"Spotify streams found for {found} of {total_songs} songs.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--retry-missing", action="store_true", help="retry artists whose Spotify id wasn't found before")
    args = ap.parse_args()
    if not DB_PATH.exists():
        sys.exit("No covers.db yet - run `uv run adt-sync` first.")
    db = sqlite3.connect(DB_PATH)
    overrides = json.loads(OVERRIDES_PATH.read_text()) if OVERRIDES_PATH.exists() else {}
    records = build_records(db.execute("SELECT video_id, title FROM covers").fetchall(), overrides)
    print("Refreshing Spotify stream counts...")
    refresh(db, records, args.retry_missing)


if __name__ == "__main__":
    main()

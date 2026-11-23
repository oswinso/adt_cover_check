"""Serve the ADT cover browser.  Usage: uv run adt-serve [port]  (default 8000)"""
import collections
import datetime
import json
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .paths import ARTIST_TYPES_PATH, DB_PATH, INDEX_HTML
from .credits import load_people_config
from .sync import key_of

QUERY = """
SELECT c.video_id, c.title, c.upload_date, c.artist, c.artist_key, c.song, c.song_key, c.tags, d.release_date,
       s.track_id, s.total, s.daily
FROM covers c
LEFT JOIN song_dates d ON d.artist_key = c.artist_key AND d.song_key = c.song_key
LEFT JOIN song_streams s ON s.artist_key = c.artist_key AND s.song_key = c.song_key
ORDER BY c.upload_date DESC
"""


def load_artist_types():
    """artist_types.json maps a type ("girl group", ...) to artist names -> {normalised name: type}."""
    if not ARTIST_TYPES_PATH.exists():
        return {}
    data = json.loads(ARTIST_TYPES_PATH.read_text())
    return {key_of(name): kind for kind, names in data.items() for name in names}


def load_people(db, today=None):
    """Aggregate the credits table -> (people, {video_id: {"dancers": [...], "directors": [...]}}).

    Class rules: anyone ever credited with a class year ('24) is an undergrad of that year, even if
    later credited as 'G (e.g. a one-year MEng). Everyone else is treated as a grad student, and
    counts as current only if they appeared in a cover within the last two years.
    """
    today = today or datetime.date.today()
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'credits'").fetchone():
        return [], {}
    cfg = load_people_config()
    display = {key: name for key, name in cfg["canonical"].values()}
    next_class = today.year + (1 if today.month >= 7 else 0)   # first class that hasn't graduated yet
    two_years_ago = today.replace(year=today.year - 2, day=min(today.day, 28)).isoformat()

    people = collections.defaultdict(lambda: {"names": collections.Counter(), "years": [], "ig": None, "last": ""})
    by_video = collections.defaultdict(lambda: {"dancers": [], "directors": []})
    rows = db.execute(
        "SELECT c.video_id, c.kind, c.name, c.person_key, c.class_tag, c.ig, c.role, v.upload_date "
        "FROM credits c JOIN covers v USING (video_id) ORDER BY v.upload_date, c.kind, c.position")
    for vid, kind, name, key, tag, ig, role, date in rows:
        p = people[key]
        p["names"][name] += 1
        if tag and tag.isdigit():
            p["years"].append(tag)
        p["ig"] = ig or p["ig"]                      # rows are oldest first, so the newest handle wins
        p["last"] = max(p["last"], date or "")
        if kind == "director":
            by_video[vid]["directors"].append(key)
        else:
            by_video[vid]["dancers"].append({"key": key, "role": role})

    out = []
    for key, p in people.items():
        forced = cfg["class"].get(key) or cfg["class"].get(key_of(display.get(key, "")))
        years = [forced] if forced and forced.isdigit() else ([] if forced else p["years"])
        if years:   # most common year; on a tie, the one seen first
            yy = max(dict.fromkeys(years), key=years.count)
            year = 2000 + int(yy)
            category, current, label = "undergrad", year >= next_class, f"'{yy}"
        else:
            year, category, current, label = None, "grad", p["last"] >= two_years_ago, "G"
        out.append({"key": key, "name": display.get(key) or p["names"].most_common(1)[0][0], "ig": p["ig"],
                    "category": category, "year": year, "current": current, "label": label})
    return out, by_video


def load_covers():
    if not DB_PATH.exists():
        return {"last_sync": None, "covers": [], "people": []}
    db = sqlite3.connect(DB_PATH)
    try:
        types = load_artist_types()
        # song_streams only exists once `adt-streams` has run; an empty stand-in keeps the query simple.
        db.execute("CREATE TEMP TABLE IF NOT EXISTS song_streams_missing (artist_key, song_key, track_id, total, daily)")
        has_streams = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'song_streams'").fetchone()
        query = QUERY if has_streams else QUERY.replace("song_streams s", "song_streams_missing s")
        meta = dict(db.execute("SELECT key, value FROM meta"))
        covers = [
            {"id": vid, "title": title, "cover_date": up, "artist": artist, "artist_key": akey,
             "song": song, "song_key": skey, "tags": json.loads(tags or "[]"), "song_date": rel,
             "artist_type": types.get(akey) or types.get(key_of(artist)) or "other",
             "streams_total": total, "streams_daily": daily,
             "spotify_url": f"https://open.spotify.com/track/{track}" if track else None,
             "url": f"https://www.youtube.com/watch?v={vid}"}
            for vid, title, up, artist, akey, song, skey, tags, rel, track, total, daily in db.execute(query)
        ]
        people, credits = load_people(db)
        for c in covers:
            c.update(credits.get(c["id"], {"dancers": [], "directors": []}))
        return {"last_sync": meta.get("last_sync"), "streams_updated": meta.get("streams_updated"),
                "covers": covers, "people": people}
    finally:
        db.close()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/covers.json", "/api/covers"):   # covers.json is what the static export serves too
            self.reply(200, "application/json", json.dumps(load_covers(), ensure_ascii=False).encode())
        elif path in ("/", "/index.html"):
            self.reply(200, "text/html; charset=utf-8", INDEX_HTML.read_bytes())
        else:
            self.reply(404, "text/plain", b"not found")

    def reply(self, status, ctype, body):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"ADT Cover Check running at http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

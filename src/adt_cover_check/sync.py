"""Scrape the MIT Asian Dance Team YouTube channel into a local SQLite database.

Usage:
    uv run adt-sync                  # incremental: only new videos / new songs hit the network
    uv run adt-sync --retry-missing  # also retry songs whose release date wasn't found
    uv run adt-sync --dry-run        # just print how the current titles parse

Hand corrections live in overrides.json (keyed by YouTube video id).
"""
import argparse
import collections
import json
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .paths import DB_PATH, OVERRIDES_PATH

YT_DLP = [sys.executable, "-m", "yt_dlp"]  # the yt-dlp installed in this project's environment
CHANNEL_URL = "https://www.youtube.com/@MITAsianDanceTeam/videos"
USER_AGENT = "adt-cover-check/1.0 (local hobby script)"
NO_ARTIST = "Traditional / Other"

# Different spellings of the same artist -> one key.
ARTIST_ALIASES = {
    "tomorrowxtogether": "txt",
    "seventeensvtperformanceteam": "seventeen",
}
# Preferred display name per artist key (otherwise the most common spelling wins).
ARTIST_DISPLAY = {
    "txt": "TXT (Tomorrow X Together)",
    "seventeen": "SEVENTEEN",
    "straykids": "Stray Kids",
    "gidle": "(G)I-DLE",
    "newjeans": "NewJeans",
    "monstax": "MONSTA X",
    "riize": "RIIZE",
    "kissoflife": "KISS OF LIFE",
    "周深": "Zhou Shen (周深)",
    "毛不易": "Mao Buyi (毛不易)",
}
# Name to use when searching music databases, per artist key.
ARTIST_LOOKUP = {
    "txt": "TOMORROW X TOGETHER",
    "gidle": "(G)I-DLE",
}

TAG_RE = re.compile(r"\[([^\]]*)\]")
MARKER_RE = re.compile(
    r"\s*((?:\b(?:Full|Virtual|ONE TAKE)\s+)?)"
    r"\b(Traditional Chinese (?:Dance|Technique) Cover|Traditional Chinese Dance"
    r"|Dance & Acapella Cover|Dance Cover|Behind the Scenes)\b(.*)$",
    re.I,
)
SEP_RE = re.compile(r"\s+[-–—]\s*|\s*[-–—]\s+|\s*[–—]\s*")
INNER_PAREN_RE = re.compile(r"\s*\([^()]*\)")
ORIGINAL_BY_RE = re.compile(r"\s*\(Original by ([^()]+)\)", re.I)
VERSION_PAREN_RE = re.compile(r"\s*\(([^()]*\bVer(?:sion|\.)?[^()]*)\)", re.I)
QUOTES = "'\"‘’“” "


def key_of(s):
    """Normalised comparison key: lowercase letters/digits only (any script)."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    return "".join(ch for ch in s if ch.isalnum())


def has_ascii_alnum(s):
    return any(ch.isascii() and ch.isalnum() for ch in s)


def strip_parens(s, only_non_ascii=False):
    """Remove (...) groups, innermost first. Optionally only those with non-ASCII text."""
    while True:
        def repl(m):
            if only_non_ascii and m.group(0).isascii():
                return m.group(0).replace("(", "\x00").replace(")", "\x01")
            return ""
        new = INNER_PAREN_RE.sub(repl, s)
        if new == s:
            break
        s = new
    return s.replace("\x00", "(").replace("\x01", ")").strip()


def ascii_part(s):
    """Drop non-ASCII tokens if some ASCII text remains (e.g. 'NCT DREAM 엔시티 드림')."""
    if not has_ascii_alnum(s):
        return s.strip()
    return " ".join(t for t in s.split() if t.isascii()).strip()


def clean_tag(tag):
    tag = tag.strip()
    up = tag.upper()
    if up in ("MIT ADT", "ADT", ""):
        return None
    if up in ("MIT ADT VARIETY", "ADT VARIETY"):
        return "Variety"
    tag = re.sub(r"^MIT ADT\s+", "", tag, flags=re.I)
    if re.match(r"^(KPOP|JPOP|ONE TAKE|SPEED COVER|2X SPEED)", up):
        return tag.upper()
    return tag


def add_tag(tags, tag):
    if tag and key_of(tag) not in {key_of(t) for t in tags}:
        tags.append(tag)


def parse_title(title):
    """Best-effort split of a video title into artist / song / version tags."""
    tags = []
    for raw in TAG_RE.findall(title):
        for part in re.split(r"\s+[-/]\s+", raw):
            add_tag(tags, clean_tag(part))
    head = TAG_RE.sub(" ", title)
    head = re.sub(r"\s+by MIT ADT\b", "", head, flags=re.I)
    head = re.sub(r"\s+", " ", head).strip()

    m = MARKER_RE.search(head)
    if m:
        head, prefix, marker, tail = head[: m.start()], m.group(1), m.group(2), m.group(3)
        add_tag(tags, prefix.strip().upper() if prefix.strip().upper() == "ONE TAKE" else prefix.strip().title())
        if marker.lower() != "dance cover":
            add_tag(tags, marker.title().replace(" Cover", ""))
        tail = tail.strip(" -|")
        if tail.startswith("(") and tail.endswith(")") and tail.count("(") == 1:
            tail = tail[1:-1]
        add_tag(tags, tail.strip())

    lookup_artist = None
    m = ORIGINAL_BY_RE.search(head)
    if m:
        lookup_artist = m.group(1).strip()
        head = ORIGINAL_BY_RE.sub("", head)
    for v in VERSION_PAREN_RE.findall(head):
        add_tag(tags, v.strip())
    head = VERSION_PAREN_RE.sub("", head).strip()

    parts = SEP_RE.split(head, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        artist = ascii_part(strip_parens(parts[0], only_non_ascii=True)) or parts[0].strip()
        song = parts[1]
    else:
        artist, song = None, head
    song = re.sub(r"\s+", " ", song).strip(QUOTES)
    return {"artist": artist, "song": song, "tags": tags, "lookup_artist": lookup_artist}


def lookup_title(song):
    """Simplified song title for searching music databases."""
    base = ascii_part(strip_parens(song)).strip(QUOTES)
    return base if key_of(base) else song


def build_records(videos, overrides):
    """videos: [(id, title)] -> list of cover dicts with overrides + artist grouping applied."""
    records = []
    for vid, title in videos:
        ov = overrides.get(vid, {})
        if ov.get("skip"):
            continue
        rec = parse_title(title)
        rec.update({k: v for k, v in ov.items() if k in ("artist", "song", "tags", "lookup_artist", "lookup_song", "release_date", "spotify_artist_id")})
        rec["video_id"], rec["title"] = vid, title
        artist = rec["artist"] or NO_ARTIST
        akey = key_of(artist)
        rec["artist_key"] = ARTIST_ALIASES.get(akey, akey)
        rec["artist"] = artist
        rec["song_key"] = key_of(strip_parens(rec["song"])) or key_of(rec["song"])
        rec.setdefault("lookup_song", lookup_title(rec["song"]))
        records.append(rec)

    spellings = collections.defaultdict(collections.Counter)
    for r in records:
        spellings[r["artist_key"]][r["artist"]] += 1
    for r in records:
        r["artist"] = ARTIST_DISPLAY.get(r["artist_key"]) or spellings[r["artist_key"]].most_common(1)[0][0]
        if not r.get("lookup_artist"):
            r["lookup_artist"] = ARTIST_LOOKUP.get(r["artist_key"], r["artist"])
    return records


# ---------------------------------------------------------------- network

def list_channel_videos():
    out = subprocess.run(
        [*YT_DLP, "--flat-playlist", "--print", "%(id)s\t%(title)s", CHANNEL_URL],
        capture_output=True, text=True, check=True,
    ).stdout
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]


def fetch_video_info(video_id):
    """-> (upload date as YYYY-MM-DD, description), or (None, None) if YouTube won't answer."""
    for attempt in range(3):
        res = subprocess.run(
            [*YT_DLP, "--skip-download", "--print", "%(.{upload_date,description})j",
             f"https://www.youtube.com/watch?v={video_id}"],
            capture_output=True, text=True,
        )
        try:
            info = json.loads(res.stdout)
            d = info["upload_date"]
            return f"{d[:4]}-{d[4:6]}-{d[6:]}", info.get("description") or ""
        except (ValueError, KeyError, TypeError):
            time.sleep(2 * (attempt + 1))
    return None, None


def http_text(url, tries=6):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/html"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:  # both APIs answer 503/429/403 when rate limited
            if e.code not in (403, 429, 503) or attempt == tries - 1:
                raise
            time.sleep(3 * 2 ** attempt)


def http_json(url):
    return json.loads(http_text(url))


def artist_matches(candidates, wanted):
    """True if any credited artist name is exactly the wanted artist (after normalising)."""
    want = {key_of(wanted), key_of(strip_parens(wanted))} - {""}
    names = set()
    for c in candidates:
        names.add(key_of(c))
        names.update(key_of(part) for part in re.split(r",|&| x | feat\.? | ft\.? | with ", c, flags=re.I))
    return bool(want & names)


def title_keys(title):
    """Keys a title can match on: whole thing, without (...) parts, and each (...) part on its own."""
    keys = {key_of(title), key_of(strip_parens(title))}
    keys.update(key_of(p) for p in re.findall(r"\(([^()]*)\)", title))
    return keys - {""}


def musicbrainz_dates(artist, song, wanted):
    q = f'recording:"{song}" AND artist:"{artist}"'
    url = "https://musicbrainz.org/ws/2/recording?fmt=json&limit=100&query=" + urllib.parse.quote(q)
    dates = []
    for rec in http_json(url).get("recordings", []):
        names = [n for c in rec.get("artist-credit", []) for n in (c.get("name", ""), c.get("artist", {}).get("name", ""))]
        d = rec.get("first-release-date")
        if d and len(d) == 10 and title_keys(rec.get("title", "")) & wanted and artist_matches(names, artist):
            dates.append(d)
    return dates


def itunes_dates(artist, song, wanted):
    url = "https://itunes.apple.com/search?entity=song&limit=50&term=" + urllib.parse.quote(f"{artist} {song}")
    return [
        r["releaseDate"][:10]
        for r in http_json(url).get("results", [])
        if r.get("releaseDate") and title_keys(r.get("trackName", "")) & wanted and artist_matches([r.get("artistName", "")], artist)
    ]


def find_release_date(artist, song, full_title):
    """MusicBrainz's earliest matching release, else iTunes' most common date -> (date, source).

    iTunes sometimes reports compilation or pre-order dates, so it is only a fallback. If it was
    used because MusicBrainz was unreachable, the source is "itunes-unverified" and the next sync
    asks MusicBrainz again.
    """
    wanted = {key_of(song), key_of(full_title), key_of(strip_parens(full_title))} - {""}
    mb_failed = it_failed = False
    try:
        dates = musicbrainz_dates(artist, song, wanted)
    except Exception as e:
        print(f"    musicbrainz error: {e}", file=sys.stderr)
        dates, mb_failed = [], True
    time.sleep(1.1)
    if dates:
        return min(dates), "musicbrainz"
    try:
        dates = itunes_dates(artist, song, wanted)
    except Exception as e:
        print(f"    itunes error: {e}", file=sys.stderr)
        dates, it_failed = [], True
    time.sleep(3.1)
    if dates:
        counts = collections.Counter(dates)
        top = max(counts.values())
        return min(d for d, n in counts.items() if n == top), ("itunes-unverified" if mb_failed else "itunes")
    return None, ("error" if mb_failed or it_failed else "none")


RETRY_SOURCES = ("error", "itunes-unverified")

# ---------------------------------------------------------------- database

SCHEMA = """
CREATE TABLE IF NOT EXISTS covers (
    video_id TEXT PRIMARY KEY, title TEXT, upload_date TEXT,
    artist TEXT, artist_key TEXT, song TEXT, song_key TEXT, tags TEXT, description TEXT
);
CREATE TABLE IF NOT EXISTS song_dates (
    artist_key TEXT, song_key TEXT, release_date TEXT, source TEXT,
    PRIMARY KEY (artist_key, song_key)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--retry-missing", action="store_true", help="retry release-date lookups that previously found nothing")
    ap.add_argument("--skip-streams", action="store_true", help="don't refresh Spotify stream counts")
    ap.add_argument("--dry-run", action="store_true", help="print parsed titles and exit without touching the database")
    args = ap.parse_args()

    overrides = json.loads(OVERRIDES_PATH.read_text()) if OVERRIDES_PATH.exists() else {}
    print("Listing channel videos...")
    videos = list_channel_videos()
    records = build_records(videos, overrides)
    print(f"  {len(videos)} videos, {len(records)} covers after overrides")

    if args.dry_run:
        for r in records:
            print(f"{r['video_id']}  {r['artist']!r:32} {r['song']!r:45} {r['tags']}")
        return

    db = sqlite3.connect(DB_PATH, timeout=60)
    db.executescript(SCHEMA)

    if "description" not in [row[1] for row in db.execute("PRAGMA table_info(covers)")]:
        db.execute("ALTER TABLE covers ADD COLUMN description TEXT")  # database from an older version
    known = {vid: (d, desc) for vid, d, desc in db.execute("SELECT video_id, upload_date, description FROM covers")}
    todo = [r["video_id"] for r in records if None in known.get(r["video_id"], (None, None))]
    if todo:
        print(f"Fetching upload date and description for {len(todo)} videos...")
        with ThreadPoolExecutor(6) as pool:
            for vid, info in zip(todo, pool.map(fetch_video_info, todo)):
                if info[0] is None:
                    print(f"  could not get details for {vid}", file=sys.stderr)
                else:
                    known[vid] = info

    db.execute("DELETE FROM covers")
    db.executemany(
        "INSERT INTO covers VALUES (?,?,?,?,?,?,?,?,?)",
        [(r["video_id"], r["title"], known.get(r["video_id"], (None, None))[0], r["artist"], r["artist_key"],
          r["song"], r["song_key"], json.dumps(r["tags"], ensure_ascii=False), known.get(r["video_id"], (None, None))[1])
         for r in records],
    )
    db.commit()

    from .credits import rebuild
    stats = rebuild(db, overrides)
    print(f"  credits parsed; {len(stats['no_dancers'])} videos without dancers, {len(stats['no_directors'])} without a director")
    for names in stats["maybe_same"]:
        print(f"  same Instagram handle, different names - same person? {names}  (see people.json)")

    # Song release dates: manual overrides first, then look up whatever isn't cached.
    cached = {(a, s): src for a, s, src in db.execute("SELECT artist_key, song_key, source FROM song_dates")}
    songs = {}
    for r in records:
        k = (r["artist_key"], r["song_key"])
        if r.get("release_date"):
            db.execute("INSERT OR REPLACE INTO song_dates VALUES (?,?,?,?)", (*k, r["release_date"], "override"))
            cached[k] = "override"
        elif r["artist_key"] != key_of(NO_ARTIST):
            songs.setdefault(k, r)
    todo = [(k, r) for k, r in songs.items()
            if k not in cached or cached[k] in RETRY_SOURCES or (args.retry_missing and cached[k] == "none")]
    if todo:
        print(f"Looking up release dates for {len(todo)} songs...")
    for i, (k, r) in enumerate(todo, 1):
        d, source = find_release_date(r["lookup_artist"], r["lookup_song"], r["song"])
        print(f"  [{i}/{len(todo)}] {r['lookup_artist']} - {r['lookup_song']}: {d or 'not found'} ({source})")
        if d or k not in cached:  # a failed retry must not wipe a date we already have
            db.execute("INSERT OR REPLACE INTO song_dates VALUES (?,?,?,?)", (*k, d, source))
            db.commit()

    db.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync', ?)", (time.strftime("%Y-%m-%d %H:%M"),))
    db.commit()
    missing = db.execute(
        "SELECT COUNT(DISTINCT c.artist_key || '|' || c.song_key) FROM covers c LEFT JOIN song_dates d "
        "ON d.artist_key = c.artist_key AND d.song_key = c.song_key WHERE d.release_date IS NULL").fetchone()[0]
    if not args.skip_streams:
        from .streams import refresh
        print("Refreshing Spotify stream counts...")
        refresh(db, records, args.retry_missing)
    print(f"Done. {len(records)} covers in {DB_PATH.name}; {missing} songs without a release date.")


if __name__ == "__main__":
    main()

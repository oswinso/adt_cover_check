# ADT Cover Check

Browse every song the MIT Asian Dance Team has covered on
[their YouTube channel](https://www.youtube.com/@MITAsianDanceTeam/videos), by artist or by search.

The project is managed with [uv](https://docs.astral.sh/uv/); run these from the project root:

```
uv run adt-serve                  # open http://127.0.0.1:8000  (optional port argument)
uv run adt-sync                   # pull new videos into covers.db (incremental)
uv run adt-sync --retry-missing   # also retry songs with no release date yet
uv run adt-streams                # refresh only the Spotify stream counts (adt-sync does this too)
uv run adt-sync --dry-run         # show how titles parse, without writing anything
uv run adt-export                 # write the static site (index.html + covers.json) to ./site
```

The site is hosted on GitHub Pages at <https://oswinso.xyz/adt_cover_check/>.
`.github/workflows/pages.yml` runs `adt-export` on every push to `main`, so to update the live
site: `uv run adt-sync`, commit `covers.db` (and any edited json files), and push.

`uv run` creates the virtualenv and installs dependencies (just `yt-dlp`) on first use.
Update yt-dlp with `uv lock --upgrade-package yt-dlp` if YouTube scraping starts failing.

- `src/adt_cover_check/sync.py` lists the channel with yt-dlp, parses each title into
  artist / song / version tags, and looks up each song's release date (MusicBrainz, with iTunes
  as a fallback). Results are cached in `covers.db`, so later syncs only hit the network for new
  videos and songs.
- `src/adt_cover_check/streams.py` adds each song's total and daily Spotify streams. Spotify's API
  doesn't publish these, so they are read from kworb.net's per-artist pages; the artist's Spotify
  id is found via MusicBrainz (or set `spotify_artist_id` on any of the artist's videos in
  `overrides.json`). Counts are a snapshot from the last `adt-sync` / `adt-streams` run.
- `overrides.json` holds hand corrections keyed by YouTube video id. Supported fields:
  `skip`, `artist`, `song`, `tags`, `release_date` (YYYY-MM-DD), `lookup_artist`, `lookup_song`.
  Edit it and re-run the sync to fix a mis-parsed title or a wrong/missing release date.
- `src/adt_cover_check/server.py` + `index.html` are the web app (one JSON endpoint, `/api/covers`).
- `src/adt_cover_check/credits.py` reads each video's description for its "Directed by" line and
  "Dancers" list, which feeds the People page. `people.json` holds the manual fixes: other
  spellings of the same person, same-named different people, and forced class years. A video's
  whole list can be replaced with `"dancers": ["Name '27 (@ig)", ...]` / `"directors": [...]` in
  `overrides.json`. Re-run `uv run adt-sync` after editing either file.
- `artist_types.json` says which artists are girl groups, boy groups, soloists or co-ed, for the
  filter buttons. Artists not listed there show as "other"; edit the file and refresh the page.
- `covers.db`, `overrides.json` and `artist_types.json` are read from the directory you run from; set `ADT_DATA_DIR`
  to keep them elsewhere.

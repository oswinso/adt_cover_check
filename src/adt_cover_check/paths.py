"""Where the data files live.

covers.db, overrides.json and artist_types.json are kept in the directory you run from (the project root),
or in $ADT_DATA_DIR if that is set.
"""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("ADT_DATA_DIR") or Path.cwd())
DB_PATH = DATA_DIR / "covers.db"
OVERRIDES_PATH = DATA_DIR / "overrides.json"
ARTIST_TYPES_PATH = DATA_DIR / "artist_types.json"
PEOPLE_PATH = DATA_DIR / "people.json"
INDEX_HTML = Path(__file__).parent / "index.html"

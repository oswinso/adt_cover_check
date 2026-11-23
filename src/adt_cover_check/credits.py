"""Parse who danced in / directed each cover out of the YouTube video descriptions.

Typical description:

    Directed by: Karen Guo '26 (@bsl_cheesecake), Amber Wang '26 (@a.w.0303)
    Shot & Edited by: ...

    Dancers (ig):
    🐱 Jun - Karen Guo '26 (@bsl_cheesecake)
    Seungkwan - Matt Feng 'G

The tag after a name is the MIT class year ('26), 'G for grad students, or absent.
"""
import json
import re

from .paths import PEOPLE_PATH
from .sync import key_of

QUOTE = "'’‘`´′"
CLASS_RE = re.compile(rf"[{QUOTE}]\s*(\d{{2}}|G|M)\b(?!\w)", re.I)  # 'M (MEng) is treated like 'G
IG_RE = re.compile(r"@\s*([A-Za-z0-9_.]+)")
DIRECTOR_RE = re.compile(r"^[^:]{0,30}\bdirect[^:]{0,30}:\s*(.+)$", re.I)  # "Directed by:", "Directed & Edited by:"
DANCERS_RE = re.compile(r"^\W*(?:dancers?|members?|cast)\b[^:\n]*:?\s*$", re.I)
SEP_RE = re.compile(r"\s+[-–—:]+\s*|\s*[-–—:]+\s+|\s*[–—❗]+\s*")
NOT_A_DANCER_RE = re.compile(r"^\W*(original|choreograph|adapted|music|arrang|audio|mixing|special|thanks|shot|film|edit|video|sing|solo)", re.I)
SPLIT_PEOPLE_RE = re.compile(r"\s*(?:,|&|/|\band\b|\+)\s*", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS credits (
    video_id TEXT, kind TEXT, position INTEGER, name TEXT, person_key TEXT, class_tag TEXT, ig TEXT, role TEXT
);
"""


def parse_person(text):
    """'Karen Guo '26 (@bsl_cheesecake)' -> {name, class_tag ('26' | 'G' | None), ig}; None if no name."""
    ig = IG_RE.search(text)
    tag = CLASS_RE.search(text)
    name = text
    for m in (ig, tag):  # the name is whatever comes before the first tag / handle
        if m:
            name = name[: min(len(name), m.start())]
    name = re.sub(r"\([^)]*\)?|\bMIT\b", " ", name)
    name = re.sub(r"^[^\w]+", "", re.sub(r"\s+", " ", name)).strip(" -–—:;,.(@|" + QUOTE)
    if not re.search(r"[^\W\d_]", name) or len(name) > 40 or "http" in name.lower():
        return None
    return {"name": name, "class_tag": tag.group(1).upper().replace("M", "G") if tag else None, "ig": ig.group(1).strip(".").lower() if ig else None}


def split_people(text):
    # Split on , & / and - but never inside "(...)", e.g. "(@a.upa | https://youtube.com/...)".
    groups = []
    flat = re.sub(r"\([^)]*\)?", lambda m: groups.append(m.group(0)) or f"\x00{len(groups) - 1}\x00", text)
    parts = [re.sub(r"\x00(\d+)\x00", lambda m: groups[int(m.group(1))], part) for part in SPLIT_PEOPLE_RE.split(flat)]
    return [p for p in (parse_person(part) for part in parts) if p]


def parse_description(description):
    """-> {"directors": [person], "dancers": [person with role]}"""
    directors, dancers = [], []
    in_dancers = False
    for raw in (description or "").splitlines():
        line = raw.strip()
        if line.startswith("--") or line.startswith("The MIT Asian Dance Team"):
            break
        m = DIRECTOR_RE.match(line)
        if m:
            directors += split_people(m.group(1))
            in_dancers = False
        elif DANCERS_RE.match(line):
            in_dancers = True
        elif in_dancers and line:
            parts = SEP_RE.split(line)
            has_marker = len(parts) > 1 or CLASS_RE.search(line) or IG_RE.search(line)
            if not has_marker or line.endswith(":") or NOT_A_DANCER_RE.match(line):  # next section ("Singers:", "Soloists")
                in_dancers = False
                continue
            role = re.sub(r"^[^\w(]+", "", " - ".join(parts[:-1])).strip() or None
            for person in split_people(parts[-1]):
                dancers.append({**person, "role": role})
    return {"directors": directors, "dancers": dancers}


def load_people_config():
    cfg = json.loads(PEOPLE_PATH.read_text()) if PEOPLE_PATH.exists() else {}
    canonical = {}   # any spelling's key -> (canonical key, canonical display name)
    for name, variants in cfg.get("aliases", {}).items():
        for v in [name, *variants]:
            canonical[key_of(v)] = (key_of(name), name)
    return {
        "canonical": canonical,
        "split": {key_of(n) for n in cfg.get("split_by_class", [])},
        "different": {frozenset(key_of(n) for n in pair) for pair in cfg.get("different_people", [])},
        "class": {key_of(n): str(v).strip("'").upper() for n, v in cfg.get("class", {}).items()},
    }


def person_key(person, cfg):
    key = cfg["canonical"].get(key_of(person["name"]), (key_of(person["name"]),))[0]
    if key in cfg["split"] and person["class_tag"]:
        key += person["class_tag"].lower()
    return key


def _pairs(items):
    return [(a, b) for i, a in enumerate(items) for b in items[i + 1:]]


def rebuild(db, overrides=None):
    """Re-parse every stored description into the credits table (cheap, so parser fixes apply)."""
    overrides = overrides or {}
    cfg = load_people_config()
    ig_owners = {}
    db.executescript(SCHEMA)
    db.execute("DELETE FROM credits")
    stats = {"videos": 0, "no_dancers": [], "no_directors": []}
    for vid, description in db.execute("SELECT video_id, description FROM covers").fetchall():
        parsed = parse_description(description)
        ov = overrides.get(vid, {})
        for kind in ("directors", "dancers"):  # manual lists in overrides.json replace the parsed ones
            if kind in ov:
                parsed[kind] = [{**p, "role": None} for line in ov[kind] for p in split_people(line)]
        stats["videos"] += 1
        if not parsed["dancers"]:
            stats["no_dancers"].append(vid)
        if not parsed["directors"]:
            stats["no_directors"].append(vid)
        for kind, people in (("director", parsed["directors"]), ("dancer", parsed["dancers"])):
            for i, p in enumerate(people):
                pk = person_key(p, cfg)
                db.execute("INSERT INTO credits VALUES (?,?,?,?,?,?,?,?)",
                           (vid, kind, i, p["name"], pk, p["class_tag"], p["ig"], p.get("role")))
                if p["ig"] and pk[:-2] not in cfg["split"]:
                    ig_owners.setdefault(p["ig"], {})[pk] = p["name"]
    db.commit()
    # Same Instagram handle under two names usually means one person spelled two ways.
    stats["maybe_same"] = sorted(
        sorted(owners.values()) for owners in ig_owners.values()
        if len(owners) > 1 and not any(frozenset(pair) in cfg["different"] for pair in _pairs(list(owners))))
    return stats

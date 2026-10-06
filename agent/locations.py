"""Find the place a buyer means, in Zameen's own location tree. Read-only.

The tree is exactly Zameen's hierarchy (Pakistan > Sindh > Karachi > Cantt >
Malir Cantonment > Askari 5 > Askari 5 - Sector J), loaded from the listings.

No hand-written alias table. Two layers:
  1. The extractor model is given the real place names (place_choices) and
     picks one by id; it understands "Askari V", "ask 6", "malir cantt".
     The code only accepts an id that exists in the tree.
  2. find_location is a conservative backup for plain typos: whole-name
     similarity, so a shared generic word ("town") cannot make a match.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg import AsyncConnection

# A name match this good, and clearly ahead of the next one, is accepted;
# below ASK_SCORE nothing is close enough to suggest.
ACCEPT_SCORE = 0.6
ACCEPT_LEAD = 0.15
ASK_SCORE = 0.35


@dataclass(frozen=True)
class Place:
    id: int
    name: str
    parent_id: int | None
    path: tuple[int, ...]          # Zameen ids, root first
    lat: float | None              # centre of every listing ever under this place
    lng: float | None

    @property
    def depth(self) -> int:
        return len(self.path) - 1


@dataclass(frozen=True)
class Tree:
    places: dict[int, Place]

    def label(self, place_id: int) -> str:
        """'Askari 5 - Sector J, Askari 5, Malir Cantonment, Cantt, Karachi':
        the place and its ancestors, nearest first, without province/country."""
        place = self.places[place_id]
        names = [self.places[i].name for i in reversed(place.path) if i in self.places]
        return ", ".join(names[: max(1, len(names) - 2)])


async def load_tree(conn: AsyncConnection) -> Tree:
    rows = await (await conn.execute(
        """SELECT l.id, l.name, l.parent_id, l.path,
                  avg(x.lat), avg(x.lng)
           FROM locations l
           LEFT JOIN locations d ON d.path @> ARRAY[l.id]
           LEFT JOIN listings x ON x.location_id = d.id AND x.lat IS NOT NULL
           GROUP BY l.id, l.name, l.parent_id, l.path"""
    )).fetchall()
    return Tree({
        r[0]: Place(r[0], r[1], r[2], tuple(r[3]), r[4], r[5]) for r in rows
    })


@dataclass(frozen=True)
class PlaceMatch:
    place_id: int
    name: str
    label: str
    score: float
    available: int                 # available listings under it


async def find_location(conn: AsyncConnection, text: str, limit: int = 3) -> list[PlaceMatch]:
    """Best matches for what the buyer typed, best first."""
    text = (text or "").strip()
    if not text:
        return []
    tree = await load_tree(conn)
    rows = await (await conn.execute(
        """SELECT l.id,
                  extensions.similarity(lower(l.name), lower(%(q)s)) AS score,
                  (SELECT count(*) FROM listings x
                    WHERE x.status = 'available'
                      AND x.location_id IN (SELECT d.id FROM locations d WHERE d.path @> ARRAY[l.id])
                  ) AS available
           FROM locations l
           ORDER BY score DESC, l.depth DESC
           LIMIT %(n)s""",
        {"q": text, "n": limit * 3},
    )).fetchall()
    matches = [
        PlaceMatch(r[0], tree.places[r[0]].name, tree.label(r[0]), float(r[1]), r[2])
        for r in rows if float(r[1]) >= ASK_SCORE
    ]
    return matches[:limit]


async def place_choices(conn: AsyncConnection) -> list[dict]:
    """Every place we have listings under, for the extractor to choose from:
    [{"id": 17289, "name": "Askari 5 - Sector J", "in": "Askari 5, Malir
    Cantonment, Cantt, Karachi"}]. Small (a few dozen places).

    Ancestors of the deepest place that holds ALL the stock (here Pakistan and
    Sindh above Karachi) are left out: choosing one says nothing, and a model
    that cannot find "Bahria Town" must not fall back to "Pakistan"."""
    tree = await load_tree(conn)
    stock = dict(await (await conn.execute(
        """SELECT l.id, count(x.id) FROM locations l
           LEFT JOIN listings x ON x.location_id IN
                (SELECT d.id FROM locations d WHERE d.path @> ARRAY[l.id])
           GROUP BY l.id""")).fetchall())
    total = max(stock.values(), default=0)
    covering = [p for p in tree.places.values() if stock.get(p.id) == total]
    deepest = max(covering, key=lambda p: p.depth, default=None)
    too_broad = set(deepest.path[:-1]) if deepest else set()
    out = []
    for place in sorted(tree.places.values(), key=lambda p: p.path):
        if place.id in too_broad:
            continue
        ancestors = tree.label(place.id).split(", ")[1:]
        out.append({"id": place.id, "name": place.name, "in": ", ".join(ancestors)})
    return out


def decide(matches: list[PlaceMatch]) -> str:
    """'match' (use it), 'ask' (offer the options), or 'none' (we have nothing there)."""
    if not matches:
        return "none"
    top = matches[0]
    if top.score >= ACCEPT_SCORE and (len(matches) == 1 or top.score - matches[1].score >= ACCEPT_LEAD):
        return "match"
    return "ask"

"""Lists: shopping, to-do and any named list ("add milk and eggs", "put sunscreen on the packing list").

Items live in SQLite on the robot (``skill_list_items``), in the order they were added. Names are tidied ("my
grocery list" is the shopping list, "todo" the to-do list) and an item already on a list isn't added twice
("eggs" and "Eggs"). Removing is forgiving: "milk" takes "2% milk" off, "bananna" takes "bananas" off; when the
words fit several items the result asks which. With no list named, groceries go on the shopping list and the
rest on the to-do list.

After a change the lists card (littlebot/screens/cards/lists.py) shows the list for a short look; ``read_list``
shows it for longer. A tap on an item there ticks it off (:meth:`Lists.tick_off`).
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
from typing import Any, Callable

from littlebot.skills.base import STR, Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.lists")

TABLE = "skill_list_items"
SHOPPING, TODO = "shopping", "to-do"
MAX_ITEMS = 200  # per list
MAX_PER_CALL = 30
MAX_ITEM = 80
MAX_NAME = 30
MAX_LISTS = 30
SAY_ITEMS = 25  # read_list returns at most this many (the card shows them all, paged)
CHANGED_LINGER_S = 12.0  # the card after adding / removing: a short look

_SHOPPING_NAMES = {"shopping", "grocery", "groceries", "grocery shopping", "food", "food shopping", "supermarket",
                   "store", "the store", "shop", "shops", "things to buy", "to buy", "buy", "market", "errands shopping"}
_TODO_NAMES = {"todo", "to do", "to-do", "todos", "to dos", "to-dos", "task", "tasks", "things to do", "jobs", "do",
               "action items", "reminders list"}
_ARTICLE = re.compile(r"^(?:(?:a|an|the|some|more|any|a few|a couple of|another)\s+)+", re.I)
_SHOP_VERB = re.compile(r"^(?:buy|get|pick up|grab|purchase)\s+", re.I)
_TASK_VERBS = {"call", "phone", "ring", "email", "text", "message", "book", "schedule", "fix", "repair", "pay", "clean",
               "wash", "tidy", "finish", "send", "return", "renew", "cancel", "mow", "file", "write", "read", "check",
               "take", "make", "plan", "organise", "organize", "sort", "visit", "see", "meet", "drop", "change",
               "replace", "install", "update", "submit", "sign", "post", "mail", "order", "pack", "wrap", "prepare",
               "cook", "bake", "vacuum", "hoover", "iron", "fold", "water", "feed", "walk", "charge", "learn",
               "practice", "practise", "ask", "tell", "remind", "look", "find", "research", "print", "register",
               "apply", "cut", "paint", "move", "bring", "collect", "do", "go", "put", "set", "start", "help"}
_GROCERIES = set("""
milk egg bread butter cheese yogurt yoghurt cream sour juice coffee tea sugar flour salt pepper oil vinegar rice pasta
noodle cereal oat oatmeal granola honey jam peanut nut almond cashew walnut apple banana orange lemon lime grape berry
strawberry blueberry raspberry melon watermelon pineapple mango peach pear plum cherry avocado tomato potato onion
garlic ginger carrot celery lettuce spinach kale cabbage broccoli cauliflower cucumber pepper zucchini squash pumpkin
mushroom corn pea bean lentil chickpea tofu chicken beef pork lamb turkey bacon ham sausage salami fish salmon tuna
shrimp prawn steak mince meat egg bagel muffin croissant tortilla wrap pita bun roll cracker chip crisp cookie biscuit
chocolate candy snack popcorn pretzel cake pie ice soda pop water sparkling beer wine ketchup mustard mayo mayonnaise
sauce salsa soup stock broth spice herb basil parsley cilantro coriander oregano cinnamon vanilla yeast baking
diaper nappy wipe formula puree pouch toilet paper towel tissue napkin detergent soap shampoo conditioner toothpaste
toothbrush deodorant razor lotion sunscreen dish dishwasher bleach sponge foil wrap bag trash garbage bin battery
lightbulb bulb hummus pesto olive pickle margarine ghee paneer naan chapati atta dal basmati spaghetti macaroni
lasagna pizza frozen fries waffle pancake syrup maple creamer kombucha lemonade smoothie protein bar
""".split())
_QUANTITY = re.compile(r"^\d+(?:\.\d+)?\s*(?:%|x\b)?|\b(?:kg|g|lb|lbs|pounds?|litres?|liters?|l|ml|oz|ounces?|packs?|"
                       r"packets?|bags?|box(?:es)?|bottles?|cans?|tins?|dozen|cartons?|jars?|loa(?:f|ves)|bunch(?:es)?|"
                       r"tubs?|rolls?)\b", re.I)


# ---------------------------------------------------------------- names and items
def list_key(name: Any) -> str:
    """ "My grocery list" -> "shopping", "To Do" -> "to-do", "Packing List" -> "packing"; "" for none."""
    if not isinstance(name, str):
        return ""
    s = re.sub(r"[^\w\s&'-]", " ", name.lower().replace("_", " "))
    s = " ".join(s.split())
    s = re.sub(r"^(?:(?:my|our|the|a|an|family|family's|shared)\s+)+", "", s)
    s = re.sub(r"\s+lists?$|^lists?$", "", s).strip(" -'")
    if s in _SHOPPING_NAMES or s.endswith(("grocery", "groceries")):
        return SHOPPING
    if s in _TODO_NAMES:
        return TODO
    return s[:MAX_NAME].strip()


def title(key: str) -> str:
    """ "shopping" -> "Shopping list", "to-do" -> "To-do list", "costco" -> "Costco list"."""
    if not key:
        return "List"
    return key[:1].upper() + key[1:] + " list"


def clean_item(value: Any, *, shopping: bool = False) -> str:
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return ""
    s = " ".join(str(value).split()).strip(" \"'“”‘’.,;:!?-*•")
    if shopping:
        s = _SHOP_VERB.sub("", s)
    s = _ARTICLE.sub("", s).strip()
    if len(s) > MAX_ITEM:
        s = s[:MAX_ITEM].rsplit(" ", 1)[0].rstrip(",;:-")
    return s


def _singular(w: str) -> str:
    if len(w) <= 3 or w.endswith(("ss", "us", "is")):
        return w
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("oes"):
        return w[:-2]
    if w.endswith(("ches", "shes", "xes", "sses")):
        return w[:-2]
    if w.endswith("ves") and w not in ("olives", "chives"):
        return w[:-3] + "f"
    return w[:-1] if w.endswith("s") else w


def item_norm(text: str) -> str:
    """For telling items apart: lower case, no articles or punctuation, singular ("Eggs" == "egg")."""
    s = _ARTICLE.sub("", " ".join(str(text).lower().split()))
    words = re.findall(r"[a-z0-9%]+", s)
    return " ".join(_singular(w) for w in words)


def looks_task(item: str) -> bool:
    """ "call the plumber", "water the plants" (a verb, then what to do it to)."""
    words = item_norm(item).split()
    return len(words) >= 2 and words[0] in _TASK_VERBS


def looks_grocery(item: str) -> bool:
    """ "milk", "2 litres of milk", "buy diapers", "a bag of rice"."""
    item = _SHOP_VERB.sub("", str(item).strip())
    words = item_norm(item).split()
    if not words or looks_task(item):
        return False
    return bool(_QUANTITY.search(item)) or any(w in _GROCERIES for w in words)


def guess_list(items: list[str]) -> str:
    """The shopping list for groceries, else the to-do list."""
    g = sum(1 for i in items if looks_grocery(i))
    t = sum(1 for i in items if looks_task(i))
    return SHOPPING if g and g >= t else TODO


def _as_items(value: Any) -> list[str]:
    """The model's ``items``: a list of strings, or one string ("milk, eggs and bread")."""
    if isinstance(value, str):  # "milk, eggs and bread" (but "mac and cheese" alone is one item)
        parts = re.split(r"\s*[,;\n]\s*", value)
        if len(parts) > 1:
            parts = parts[:-1] + re.split(r"\s+(?:and|&)\s+", parts[-1])
    elif isinstance(value, (list, tuple)):
        parts = []
        for v in value:
            if isinstance(v, str):
                parts.append(v)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                parts.append(str(v))
            elif isinstance(v, dict) and isinstance(v.get("name") or v.get("item"), str):
                parts.append(str(v.get("name") or v.get("item")))
    else:
        return []
    return [p for p in (" ".join(str(x).split()) for x in parts) if p]


class Lists(Skill):
    name = "lists"

    def __init__(self, ctx: Any, *, now: Callable[[], float] | None = None) -> None:
        super().__init__(ctx, **({"now": now} if now is not None else {}))
        self._ready = False
        self._lock = threading.RLock()
        self.version = 0  # bumped on every change (the card redraws)

    def available(self) -> bool:
        return bool(getattr(getattr(self.ctx.config, "skills", None), "lists", True))

    # ------------------------------------------------------------ storage (also used by the card)
    def _db(self) -> Any:
        db = self.ctx.db
        if not self._ready:
            db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT, list TEXT NOT NULL, "
                       "item TEXT NOT NULL, norm TEXT NOT NULL, added REAL NOT NULL, UNIQUE(list, norm))")
            self._ready = True
        return db

    def items(self, name: str) -> list[dict[str, Any]]:
        """[{id, item}] on list ``name`` (a key or a name said), in the order they were added."""
        key = list_key(name)
        with self._lock:
            return self._db().query(f"SELECT id, item, norm FROM {TABLE} WHERE list=? ORDER BY added, id", (key,))

    def names(self) -> list[tuple[str, int]]:
        """(list, how many items) for every list with something on it, the shopping and to-do lists first."""
        with self._lock:
            rows = self._db().query(f"SELECT list, COUNT(*) AS n, MIN(added) AS first FROM {TABLE} GROUP BY list")
        order = {SHOPPING: 0, TODO: 1}
        rows.sort(key=lambda r: (order.get(r["list"], 2), float(r["first"])))
        return [(str(r["list"]), int(r["n"])) for r in rows]

    def tick_off(self, name: str, item_id: int) -> str | None:
        """Take one item off (a tap on the card). The item's words, or None if it was already gone."""
        key = list_key(name)
        with self._lock:
            row = self._db().query_one(f"SELECT item FROM {TABLE} WHERE id=? AND list=?", (int(item_id), key))
            if row is None:
                return None
            self._db().execute(f"DELETE FROM {TABLE} WHERE id=?", (int(item_id),))
            self.version += 1
        log.info("lists: an item ticked off the %s list on the screen", key)
        return str(row["item"])

    def add(self, key: str, items: list[str]) -> tuple[list[str], list[str]]:
        """(added, already there)."""
        added, already = [], []
        with self._lock:
            have = {r["norm"]: r["item"] for r in self.items(key)}
            for raw in items:
                item = clean_item(raw, shopping=key == SHOPPING)
                n = item_norm(item)
                if not item or not n:
                    continue
                if n in have:
                    already.append(have[n])
                    continue
                if len(have) >= MAX_ITEMS:
                    break
                self._db().execute(f"INSERT OR IGNORE INTO {TABLE}(list, item, norm, added) VALUES(?, ?, ?, ?)",
                                   (key, item, n, self.now() + len(added) * 1e-3))
                have[n] = item
                added.append(item)
            if added:
                self.version += 1
        return added, already

    def find(self, key: str, query: str) -> list[dict[str, Any]]:
        """The items on ``key`` that ``query`` means ("milk" -> "2% milk"; several when it fits several)."""
        rows = self.items(key)
        q = item_norm(clean_item(query))
        if not q:
            return []
        exact = [r for r in rows if r["norm"] == q]
        if exact:
            return exact
        qw = set(q.split())
        within = [r for r in rows if qw <= set(str(r["norm"]).split())]  # "milk" -> "2% milk"
        if within:
            return within
        around = [r for r in rows if set(str(r["norm"]).split()) and set(str(r["norm"]).split()) <= qw]  # "the 2% milk"
        if around:
            return around
        said = {str(r["norm"]): r for r in rows} | {str(r["item"]).lower(): r for r in rows}  # "egs": "eggs"
        close = difflib.get_close_matches(q, list(said), n=1, cutoff=0.8)
        if close:
            return [said[close[0]]]
        words = [w for w in qw if len(w) >= 4]
        fuzzy = [r for r in rows if words and all(
            difflib.get_close_matches(w, str(r["norm"]).split(), n=1, cutoff=0.8) for w in words)]
        return fuzzy

    def _remove_ids(self, ids: list[int]) -> None:
        with self._lock:
            for i in ids:
                self._db().execute(f"DELETE FROM {TABLE} WHERE id=?", (int(i),))
            if ids:
                self.version += 1

    # ------------------------------------------------------------ for the model
    def tools(self) -> list[ToolSpec]:
        items = {"type": "array", "items": {"type": "string"},
                 "description": "Short item names, one per entry: ['milk', 'eggs'] (not a sentence)."}
        name = {"type": "string", "description": "The list: 'shopping', 'to-do', or any name ('packing', 'Costco')."}
        return [
            tool("add_to_list", "Add items to a list. No list named: groceries go on the shopping list, other things "
                 "on the to-do list. Items already there aren't added twice.", {"items": items, "list": name},
                 ("items",)),
            tool("remove_from_list", "Take items off a list (bought, done, not needed). Forgiving: 'milk' takes '2% "
                 "milk' off. No list named: whichever list has it.", {"items": items, "list": name}, ("items",)),
            tool("read_list", "What is on a list (shown on the screen too). No list named: the shopping list.",
                 {"list": name}),
            tool("clear_list", "Empty a whole list, only when the user clearly asks to clear or empty it.",
                 {"list": name}, ("list",)),
            tool("list_lists", "Which lists there are and how many items each has."),
        ]

    def prompt_note(self) -> str:
        return ("Lists: keep the family's lists (shopping, to-do, or any name like packing) with add_to_list / "
                "remove_from_list / read_list / clear_list / list_lists; items are short names ('milk', 'eggs'). When "
                "they say they bought or did something on a list, take it off. Reading a list out: the count and a "
                "few items, not a long recital (the screen shows it all).")

    def context_note(self) -> str:
        if not self.available():
            return ""
        try:
            lists = self.names()
        except Exception:  # noqa: BLE001
            return ""
        if not lists:
            return ""
        return "Lists: " + ", ".join(f"{k} ({n} item{'s' if n != 1 else ''})" for k, n in lists[:12]) + "."

    def status(self) -> dict[str, Any]:
        return {**super().status(), "lists": dict(self.names())}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        args = args if isinstance(args, dict) else {}
        if not self.available():
            return err("lists are switched off")
        raw_list = args.get("list")
        if raw_list is not None and not isinstance(raw_list, str):
            return err("list must be a list's name")
        if name == "add_to_list":
            return await self._add(args.get("items"), raw_list)
        if name == "remove_from_list":
            return await self._remove(args.get("items"), raw_list)
        if name == "read_list":
            return await self._read(raw_list)
        if name == "clear_list":
            return await self._clear(raw_list)
        if name == "list_lists":
            lists = self.names()
            if not lists:
                return ok("no lists yet (the shopping and to-do lists are empty)")
            return ok("; ".join(f"{k} ({n})" for k, n in lists))
        return err(f"lists has no tool called {str(name)[:40]}")

    async def _show(self, key: str, linger: float | None = None) -> None:
        spec: dict[str, Any] = {"card": "lists", "list": key}
        if linger is not None:
            spec["linger"] = linger
        await self.show(spec)

    async def _add(self, raw_items: Any, raw_list: str | None) -> str:
        items = _as_items(raw_items)[:MAX_PER_CALL]
        if not items:
            return err("give the items to add, as short names")
        key = list_key(raw_list) if raw_list else guess_list(items)
        if not key:
            return err("that list name is empty")
        if key not in dict(self.names()) and len(self.names()) >= MAX_LISTS:
            return err(f"there are already {MAX_LISTS} lists; clear one first")
        added, already = self.add(key, items)
        count = len(self.items(key))
        if not added and not already:
            return err("those items were empty")
        await self._show(key, CHANGED_LINGER_S)
        parts = []
        if added:
            parts.append(f"added {_join(added)} to the {title(key)} (now {count} item{'s' if count != 1 else ''})")
        if already:
            parts.append(f"already on the {title(key)}: {_join(already)}")
        if added and len(added) < len(items) - len(already):
            parts.append(f"the list is full ({MAX_ITEMS} items)")
        log.info("lists: %d added to the %s list", len(added), key)
        return ok("; ".join(parts))

    async def _remove(self, raw_items: Any, raw_list: str | None) -> str:
        items = _as_items(raw_items)[:MAX_PER_CALL]
        if not items:
            return err("give the items to take off")
        keys = [list_key(raw_list)] if raw_list else [k for k, _ in self.names()]
        if raw_list and not keys[0]:
            return err("that list name is empty")
        removed: list[tuple[str, str]] = []
        missing, unsure = [], []
        for q in items:
            found_in = None
            for key in keys:
                hits = self.find(key, q)
                if len(hits) == 1:
                    self._remove_ids([int(hits[0]["id"])])
                    removed.append((key, str(hits[0]["item"])))
                    found_in = key
                    break
                if len(hits) > 1:
                    unsure.append(f"'{q}' could be {_join([str(h['item']) for h in hits[:4]], 'or')}")
                    found_in = key
                    break
            if found_in is None:
                missing.append(q)
        parts = []
        touched = list(dict.fromkeys(k for k, _ in removed))
        for key in touched:
            gone = [i for k, i in removed if k == key]
            left = len(self.items(key))
            parts.append(f"took {_join(gone)} off the {title(key)} ({left} left)")
        if missing:
            where = f"the {title(keys[0])}" if raw_list else "any list"
            parts.append(f"not on {where}: {_join(missing)}")
        if unsure:
            parts.append("; ".join(unsure) + ": ask which")
        if touched:
            await self._show(touched[0], CHANGED_LINGER_S)
            log.info("lists: %d taken off", len(removed))
        return ok("; ".join(parts)) if removed or unsure else ok(parts[0] if parts else "nothing taken off")

    async def _read(self, raw_list: str | None) -> str:
        if raw_list:
            key = list_key(raw_list)
        else:
            lists = [k for k, n in self.names()]
            key = SHOPPING if SHOPPING in lists or not lists else lists[0]
        if not key:
            return err("that list name is empty")
        rows = self.items(key)
        await self._show(key)
        if not rows:
            others = [f"{k} ({n})" for k, n in self.names() if k != key]
            extra = f"; lists with items: {', '.join(others)}" if others else ""
            return ok(f"the {title(key)} is empty{extra}")
        names = [str(r["item"]) for r in rows]
        more = f", and {len(names) - SAY_ITEMS} more" if len(names) > SAY_ITEMS else ""
        return ok(f"{title(key)}, {len(names)} item{'s' if len(names) != 1 else ''}: " + ", ".join(names[:SAY_ITEMS])
                  + more)

    async def _clear(self, raw_list: str | None) -> str:
        key = list_key(raw_list)
        if not key:
            return err("say which list to clear")
        rows = self.items(key)
        if not rows:
            return ok(f"the {title(key)} was already empty")
        self._remove_ids([int(r["id"]) for r in rows])
        log.info("lists: the %s list cleared (%d items)", key, len(rows))
        await self._show(key, CHANGED_LINGER_S)
        return ok(f"cleared the {title(key)} ({len(rows)} item{'s' if len(rows) != 1 else ''})")


def _join(words: list[str], last: str = "and") -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + f" {last} " + words[-1]


__all__ = ["SHOPPING", "TODO", "Lists", "clean_item", "guess_list", "item_norm", "list_key", "looks_grocery", "title"]

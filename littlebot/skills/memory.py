"""Memory: lasting facts about the family and the user's preferences ("Ali takes his coffee black").

Facts are short lines of text in SQLite on the robot (``skill_memories``), never pictures, never anything from the
camera, never passwords or card numbers. The most recent ``skills.memory_prompt_facts`` go with every question
(:meth:`Memory.context_note`), so the brain uses them without asking; ``recall`` searches the rest by keyword.

Tools: ``remember(fact, about?, replaces?)``, ``recall(query?)``, ``forget(query?, id?, all?)``, ``list_memories``.
Saying the same thing again refreshes it instead of keeping a copy; a rephrasing replaces the older wording.
The notes the live voice kept before (``profile.remembered``, littlebot/brain/remembered.py) are copied in once.
"""

from __future__ import annotations

import difflib
import logging
import re
from typing import Any, Callable

from littlebot.skills.base import BOOL, INT, STR, Skill, ToolSpec, err, ok, tool

log = logging.getLogger("littlebot.skills.memory")

TABLE = "skill_memories"
IMPORTED_KEY = "skills.memory.imported_profile_notes"
MAX_FACTS = 500  # the oldest (least recently said) go first
MAX_FACT = 300
MAX_ABOUT = 40
RECALL_MAX = 8
LIST_MAX = 30
NEAR_SAME = 0.8  # word overlap at which a new fact is a rewording of an old one (it replaces it)
MATCH = 0.6  # how much of a query a fact must cover to count as a match

_STOP = {"a", "an", "the", "is", "are", "was", "were", "be", "been", "and", "or", "of", "to", "that", "this", "it",
         "in", "on", "at", "for", "with", "my", "our", "your", "his", "her", "their", "me", "i", "we", "you", "he",
         "she", "they", "them", "us", "s", "do", "does", "did", "has", "have", "had", "about", "what", "who", "which",
         "know", "remember", "forget", "fact", "facts", "thing", "things", "anything", "something", "any", "some",
         "please", "really", "very", "also", "just", "like"}
_SYNONYMS = {"mum": "mom", "mother": "mom", "mommy": "mom", "mummy": "mom", "mama": "mom", "father": "dad",
             "daddy": "dad", "papa": "dad", "bday": "birthday", "born": "birthday", "favourite": "favorite",
             "fave": "favorite", "fav": "favorite", "colour": "color", "loves": "love", "likes": "love",
             "enjoys": "love", "enjoy": "love", "adores": "love", "hates": "hate", "dislikes": "hate",
             "dislike": "hate", "allergic": "allergy", "allergies": "allergy", "intolerant": "allergy",
             "wifey": "wife", "hubby": "husband", "kids": "child", "kid": "child", "children": "child"}
_CAMERA = re.compile(r"\b(?:camera|webcam|cctv|footage|photos?|photographs?|pictures? of|selfies?|snapshots?|"
                     r"images? of|videos? of|what (?:i|you|he|she|they|we) (?:look|looks|looked) like|"
                     r"face ?prints?|(?:saw|seen|see) (?:on|through|with) (?:the |your )?(?:camera|eyes))\b", re.I)
_SECRET = re.compile(r"\b(?:passwords?|passcodes?|pass ?phrases?|pin(?: number| code)?s?|credit ?cards?|debit ?cards?|"
                     r"card numbers?|cvv|cvc|security codes?|bank accounts?|account numbers?|routing numbers?|"
                     r"social insurance|social security|sin number|ssn|api keys?|secret keys?|2fa|otp)\b", re.I)
_LONG_NUMBER = re.compile(r"(?:\d[ -]?){12,}")
_LEAD = re.compile(r"^(?:(?:please\s+)?remember\s+(?:that\s+)?|(?:note|fyi|for the record)\s*(?:that|:)?\s+|"
                   r"that\s+)", re.I)
_EVERYTHING = {"everything", "all", "all of it", "all memories", "all facts", "everything you know",
               "all of them", "every memory", "your memory", "memory", "memories"}


def clean_fact(value: Any) -> str:
    """One tidy line ("Ali takes his coffee black"), "" when there's nothing to keep."""
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return ""
    s = " ".join(str(value).split()).strip(" \"'“”‘’")
    for _ in range(2):
        s = _LEAD.sub("", s).strip()
    s = s.rstrip(" .!,;:")
    if len(s) > MAX_FACT:
        s = s[:MAX_FACT].rsplit(" ", 1)[0].rstrip(",;:-")
    return s[:1].upper() + s[1:] if s else ""


def clean_about(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return ""
    s = re.sub(r"[^\w' &.-]", " ", str(value))
    s = " ".join(s.split()).strip(" .'-")[:MAX_ABOUT]
    if s.lower() in ("", "none", "user", "the user", "me", "unknown", "general", "n/a", "null"):
        return ""
    return s[:1].upper() + s[1:]


def norm(text: str) -> str:
    """For telling two wordings of the same fact apart: lower case, no punctuation, one spelling."""
    words = re.findall(r"[a-z0-9]+", text.lower().replace("’", "'").replace("'s", ""))
    return " ".join(_SYNONYMS.get(w, w) for w in words)


def tokens(text: str) -> set[str]:
    """The words that matter, lightly stemmed ("Amara's birthdays" -> {"amara", "birthday"})."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower().replace("’", "'").replace("'s", "")):
        w = _SYNONYMS.get(w, w)
        if w in _STOP:
            continue
        if len(w) > 3 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(_SYNONYMS.get(w, w))
    return out


def _score(query: set[str], have: set[str]) -> float:
    """How much of ``query`` the fact's words cover (0..1): exact words, near spellings, word starts."""
    if not query:
        return 0.0
    words = list(have)
    total = 0.0
    for q in query:
        if q in have:
            total += 1.0
        elif difflib.get_close_matches(q, words, n=1, cutoff=0.85):
            total += 0.8
        elif len(q) >= 4 and any(w.startswith(q) or (len(w) >= 4 and q.startswith(w)) for w in words):
            total += 0.6
    return total / len(query)


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _first_word(text: str) -> str:
    return next((w for w in norm(text).split() if w not in _STOP), "")


def _one_value_changed(new: str, old: str) -> bool:
    """ "Ali's favourite food is pizza" after "Ali's favourite food is sushi": the same fact with a new value (the
    same subject and at least three words alike, one word different on each side)."""
    a, b = tokens(new), tokens(old)
    subject = tokens(_first_word(new))
    return (len(a & b) >= 3 and len(a - b) == 1 and len(b - a) == 1 and _first_word(new) == _first_word(old)
            and bool(subject) and subject <= a & b)


class Memory(Skill):
    name = "memory"

    def __init__(self, ctx: Any, *, now: Callable[[], float] | None = None) -> None:
        super().__init__(ctx, **({"now": now} if now is not None else {}))
        self._ready = False
        self.version = 0

    def _cfg(self) -> Any:
        return getattr(self.ctx.config, "skills", None)

    def available(self) -> bool:
        return bool(getattr(self._cfg(), "memory", True))

    # ------------------------------------------------------------ storage
    def _db(self) -> Any:
        db = self.ctx.db
        if not self._ready:
            db.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (id INTEGER PRIMARY KEY AUTOINCREMENT, fact TEXT NOT NULL, "
                       "about TEXT NOT NULL DEFAULT '', norm TEXT NOT NULL UNIQUE, created REAL NOT NULL, "
                       "updated REAL NOT NULL)")
            self._ready = True
            self._import_profile_notes(db)
        return db

    def _import_profile_notes(self, db: Any) -> None:
        """Copy the live voice's old notes (``profile.remembered``) in, once."""
        try:
            if db.kv_get(IMPORTED_KEY, False):
                return
            db.kv_set(IMPORTED_KEY, True)
            prof = self.ctx.settings.get("profile") or {}
            notes = prof.get("remembered") if isinstance(prof, dict) else None
            n = 0
            for raw in notes or []:
                fact = clean_fact(raw)
                if not fact or _CAMERA.search(fact) or _SECRET.search(fact):
                    continue
                if self._insert(fact, "") is not None:
                    n += 1
            if n:
                log.info("memory: copied %d notes from the profile", n)
        except Exception:  # noqa: BLE001 - old notes are a nice-to-have
            log.warning("memory: couldn't copy the profile's notes", exc_info=True)

    def _rows(self, limit: int | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT id, fact, about, norm, created, updated FROM {TABLE} ORDER BY updated DESC, id DESC"
        return self._db().query(sql + (f" LIMIT {int(limit)}" if limit else ""))

    def _insert(self, fact: str, about: str) -> int | None:
        now = self.now()
        try:
            cur = self._db().execute(f"INSERT INTO {TABLE}(fact, about, norm, created, updated) VALUES(?, ?, ?, ?, ?)",
                                     (fact, about, norm(fact), now, now))
        except Exception:  # noqa: BLE001 - the same fact (a race): keep the one there
            return None
        self._trim()
        self.version += 1
        return int(cur.lastrowid or 0)

    def _trim(self) -> None:
        row = self._db().query_one(f"SELECT COUNT(*) AS n FROM {TABLE}")
        extra = int(row["n"]) - MAX_FACTS if row else 0
        if extra > 0:
            self._db().execute(f"DELETE FROM {TABLE} WHERE id IN (SELECT id FROM {TABLE} ORDER BY updated, id LIMIT ?)",
                               (extra,))

    def _delete(self, ids: list[int]) -> None:
        for i in ids:
            self._db().execute(f"DELETE FROM {TABLE} WHERE id=?", (int(i),))
        if ids:
            self.version += 1

    @staticmethod
    def render(row: dict[str, Any]) -> str:
        """The fact as the brain sees it ("Ali: takes his coffee black" when the fact doesn't name who)."""
        fact, about = str(row["fact"]), str(row.get("about") or "")
        if about and about.lower() not in fact.lower():
            return f"{about}: {fact[:1].lower() + fact[1:] if fact[1:2].islower() else fact}"
        return fact

    def facts(self, limit: int | None = None) -> list[str]:
        """The facts, most recently said first (for the brain, a briefing, the dashboard)."""
        out, seen = [], set()
        for r in self._rows(limit):
            line = self.render(r)
            if line.lower() not in seen:
                seen.add(line.lower())
                out.append(line)
        return out

    def entries(self) -> list[dict[str, Any]]:
        """[{id, text}] of every fact, most recent first (the dashboard's Assistant page)."""
        return [{"id": int(r["id"]), "text": self.render(r)} for r in self._rows()]

    def delete(self, fact_id: int) -> None:
        """Forget one fact by its id (the dashboard)."""
        self._delete([int(fact_id)])

    def search(self, query: str, limit: int = RECALL_MAX) -> list[tuple[float, dict[str, Any]]]:
        """(score, row) of the facts that match ``query``, best (then most recent) first."""
        want = tokens(query)
        if not want:
            return []
        scored = []
        for r in self._rows():
            s = _score(want, tokens(f"{r['fact']} {r['about']}"))
            if s >= MATCH:
                scored.append((s, r))
        scored.sort(key=lambda sr: (-sr[0], -float(sr[1]["updated"]), -int(sr[1]["id"])))
        return scored[:limit]

    # ------------------------------------------------------------ for the model
    def tools(self) -> list[ToolSpec]:
        return [
            tool("remember", "Keep a lasting fact about the family or the user's preferences, on the robot, for "
                 "every later chat. Only when they share something lasting or say 'remember ...'.",
                 {"fact": {"type": "string", "description": "One short self-contained sentence in the third person: "
                                                            "'Ali takes his coffee black', 'Amara's birthday is "
                                                            "March 3'."},
                  "about": {"type": "string", "description": "Optional: who it is about ('Ali', 'Amara', 'Amara's "
                                                             "mom', 'family')."},
                  "replaces": {"type": "string", "description": "Optional: words of an older fact this one replaces "
                                                                "(a changed preference); that one is deleted."}},
                 ("fact",)),
            tool("recall", "Search what LittleBot remembers (keywords: 'birthday', 'Ali coffee'). No query: the most "
                 "recent facts.", {"query": STR}),
            tool("forget", "Forget remembered facts that match the words (or an id from recall / list_memories). If "
                 "several match, the result lists them: ask which.",
                 {"query": STR, "id": INT, "all": {**BOOL, "description": "true: forget every fact that matches "
                                                                         "(only when they clearly asked for all)."}}),
            tool("list_memories", "Everything LittleBot remembers (most recent first)."),
        ]

    def prompt_note(self) -> str:
        return ("Memory: when the user shares a lasting personal fact or preference (names, birthdays, likes and "
                "dislikes, allergies, routines, where things are) or says 'remember ...', call remember with one short "
                "third-person sentence. Never for passing chit-chat, a mood of the moment, or what you can see; never "
                "passwords or card numbers. Use the facts naturally, without announcing that you remember them; "
                "recall when asked what you know or the facts below don't cover it; forget when asked to.")

    def context_note(self) -> str:
        if not self.available():
            return ""
        n = int(getattr(self._cfg(), "memory_prompt_facts", 40) or 0)
        if n <= 0:
            return ""
        try:
            facts = self.facts(n)
        except Exception:  # noqa: BLE001
            log.debug("memory: no facts", exc_info=True)
            return ""
        if not facts:
            return ""
        return "Things you know about the family:\n" + "\n".join(f"- {f}" for f in facts)

    def status(self) -> dict[str, Any]:
        row = self._db().query_one(f"SELECT COUNT(*) AS n FROM {TABLE}")
        return {**super().status(), "facts": int(row["n"]) if row else 0}

    async def run(self, name: str, args: dict[str, Any]) -> str:
        args = args if isinstance(args, dict) else {}
        if not self.available():
            return err("memory is switched off")
        if name == "remember":
            return self._remember(args.get("fact"), args.get("about"), args.get("replaces"))
        if name == "recall":
            return self._recall(args.get("query"))
        if name == "forget":
            return self._forget(args.get("query"), args.get("id"), args.get("all"))
        if name == "list_memories":
            return self._list()
        return err(f"memory has no tool called {str(name)[:40]}")

    def _remember(self, fact_arg: Any, about_arg: Any, replaces_arg: Any) -> str:
        fact = clean_fact(fact_arg)
        if len(fact) < 3:
            return err("give the fact to remember as a short sentence")
        if _CAMERA.search(fact):
            return err("LittleBot only remembers facts in words, never anything about the camera or what it saw")
        if _SECRET.search(fact) or _LONG_NUMBER.search(fact):
            return err("LittleBot doesn't keep passwords, PINs, card or account numbers: say it's safer in a "
                       "password manager")
        about = clean_about(about_arg)
        replaced = ""
        if isinstance(replaces_arg, str) and replaces_arg.strip():
            hits = self.search(replaces_arg, limit=1)
            if hits and norm(str(hits[0][1]["fact"])) != norm(fact):
                replaced = str(hits[0][1]["fact"])
                self._delete([int(hits[0][1]["id"])])
        key = norm(fact)
        new_words = tokens(fact)
        for r in self._rows():
            if r["norm"] == key or _jaccard(new_words, tokens(str(r["fact"]))) >= NEAR_SAME or \
                    _one_value_changed(fact, str(r["fact"])):
                same = r["norm"] == key
                self._db().execute(f"UPDATE {TABLE} SET fact=?, about=?, norm=?, updated=? WHERE id=?",
                                   (fact, about or r["about"], key, self.now(), r["id"]))
                self.version += 1
                log.info("memory: a fact said again (%s)", "same" if same else "reworded")
                return ok(f"already knew that; kept: {fact}" if same else f"updated: {fact} (was: {r['fact']})")
        if self._insert(fact, about) is None:
            return err("couldn't keep that")
        log.info("memory: remembered a fact%s", " (replacing one)" if replaced else "")
        return ok(f"remembered: {fact}" + (f" (forgot the old one: {replaced})" if replaced else ""))

    def _recall(self, query: Any) -> str:
        q = str(query or "").strip()[:120] if isinstance(query, (str, int, float)) else ""
        if not q or q.lower() in _EVERYTHING:
            rows = self._rows(RECALL_MAX)
            if not rows:
                return ok("nothing remembered yet")
            return ok("most recent: " + "; ".join(f"#{r['id']} {self.render(r)}" for r in rows))
        hits = self.search(q)
        if not hits:
            return ok(f"nothing remembered about '{q[:40]}'")
        return ok(f"{len(hits)} found: " + "; ".join(f"#{r['id']} {self.render(r)}" for _, r in hits))

    def _forget(self, query: Any, rid: Any, all_arg: Any) -> str:
        if rid not in (None, ""):
            try:
                i = int(str(rid).lstrip("#"))
            except (TypeError, ValueError):
                return err("id must be a number")
            row = self._db().query_one(f"SELECT * FROM {TABLE} WHERE id=?", (i,))
            if row is None:
                return err(f"there is no fact #{i}")
            self._delete([i])
            return ok(f"forgot: {row['fact']}")
        q = " ".join(str(query or "").split())[:200] if isinstance(query, (str, int, float)) else ""
        m = re.fullmatch(r"#?(\d+)", q)
        if m:
            return self._forget(None, m.group(1), all_arg)
        if not q:
            return err("say what to forget (words of the fact, or its id)")
        everything = all_arg is True or str(all_arg).lower() == "true"
        if q.lower().strip(" .!") in _EVERYTHING:
            rows = self._rows()
            if not rows:
                return ok("nothing remembered to forget")
            if not everything:
                return ok(f"that would forget all {len(rows)} facts. Check they mean everything; if so, call forget "
                          "with all=true.")
            self._delete([int(r["id"]) for r in rows])
            log.info("memory: forgot everything (%d facts)", len(rows))
            return ok(f"forgot all {len(rows)} facts")
        key = norm(clean_fact(q))
        exact = [r for r in self._rows() if r["norm"] == key]
        if exact:
            self._delete([int(r["id"]) for r in exact])
            return ok(f"forgot: {exact[0]['fact']}")
        hits = self.search(q, limit=50)
        if not hits:
            return ok(f"nothing remembered matches '{q[:40]}'")
        if len(hits) == 1 or everything:
            self._delete([int(r["id"]) for _, r in hits])
            return ok("forgot: " + "; ".join(str(r["fact"]) for _, r in hits[:10]) +
                      (f" (and {len(hits) - 10} more)" if len(hits) > 10 else ""))
        top, second = hits[0][0], hits[1][0]
        if top >= 0.99 and second < 0.75 and len(tokens(q)) >= 3:  # specific words, one clear match
            self._delete([int(hits[0][1]["id"])])
            return ok(f"forgot: {hits[0][1]['fact']}")
        return ok(f"{len(hits)} facts match: " + "; ".join(f"#{r['id']} {self.render(r)}" for _, r in hits[:6]) +
                  ". Ask which one to forget (then forget it by id), or all=true for all of them.")

    def _list(self) -> str:
        rows = self._rows()
        if not rows:
            return ok("nothing remembered yet")
        shown = "; ".join(f"#{r['id']} {self.render(r)}" for r in rows[:LIST_MAX])
        more = f" (and {len(rows) - LIST_MAX} older)" if len(rows) > LIST_MAX else ""
        return ok(f"{len(rows)} facts: {shown}{more}")


__all__ = ["Memory", "clean_about", "clean_fact", "norm", "tokens"]

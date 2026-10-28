"""Who is in front of the camera: the family's faces, recognised on the robot (opt-in).

A parent adds the people LittleBot may know on the dashboard's Profile page ("Ali", "dad";
"Amara", "the baby") and takes a few samples of each with the robot's camera. Nothing is
recognised until the parent switches on "Recognise family members"
(``privacy.face_recognition_enabled``, off by default) on the Privacy page, and the camera switch
must be on too. Live voice then knows who is in front of the camera (littlebot/chatgpt/live.py): a
line in the call's instructions, a quiet note when that changes, and the names with a camera look.

How (on the robot, OpenCV only): YuNet finds each face with its eyes, nose and mouth
(``cv2.FaceDetectorYN``, the model the face detector already uses), and SFace
(``cv2.FaceRecognizerSF``, OpenCV Zoo's ``face_recognition_sface_2021dec.onnx``,
``scripts/download_models.py faces``) turns the lined-up face into 128 numbers. A face is someone's
only when it is close to the average of their samples (cosine similarity of at least
``vision.face_match_threshold``, 0.45: stricter than SFace's own 0.363) AND clearly closer to them
than to anyone else (``MATCH_MARGIN``). Anything else is "someone LittleBot doesn't know", never a
guess. Small, unclear or turned-away faces aren't compared at all (``MIN_FACE_PX``, ``DETECT_SCORE``).

Privacy:

* kept: each person's name and role and, per sample, the 128 numbers (512 bytes) and when it was
  taken, in LittleBot's database on the robot (tables ``face_people`` and ``face_samples``). Never a
  photo: a sample's photo is looked at in memory and dropped (the dashboard gets a small preview to
  show the parent, not kept). Delete a person, or "Forget all faces", on the Profile page; deleted
  rows are overwritten on disk (``secure_delete``);
* photos come from the camera like a look's (the one a live call keeps warm, else one new still),
  only while the switch and the camera allow it, and are dropped straight after;
* the numbers never leave the robot and are never logged, shown or sent. Only the names (with their
  roles) of the people in view go to the live voice (OpenAI) during a call; with
  ``ai.share_child_name`` off, the child's nickname in them becomes "the little one";
* switched off: nothing is recognised and the models are unloaded; the samples stay until deleted.

Cost on a Pi 5: SFace needs ~110 MB of RAM once loaded. A 4 GB board keeps it while recognition is on; a
2 GB one only while it is used (dropped, and given back to the system (littlebot/core/memory.py), after
``UNLOAD_IDLE_S`` without use). Switched off, it is always dropped. One check (YuNet on a 640x480 photo, then SFace
for each face) took ~0.1 s per face on an x86 VM, so expect ~0.2-0.3 s on the Pi; it runs in a worker
thread every ``CHECK_EVERY_S`` (3 s) during a live call, and never otherwise.

Babies' faces change quickly: take new samples every month or so (``RETAKE_DAYS``). Each sample is
kept on its own (at most ``MAX_SAMPLES`` per person, the oldest goes first), so new ones are simply
added. Tested here with fake numbers and fake models only, not with real faces.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import dataclasses
import datetime as _dt
import logging
import re
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from littlebot.core import memory
from littlebot.core.events import Event, Topics
from littlebot.vision.imaging import get_cv2, quiet_opencv

log = logging.getLogger("littlebot.vision.faces")

_KEY = "faces"
SFACE_MODEL = "models/face_recognition_sface_2021dec.onnx"
FEATURES = 128  # SFace's numbers per face
MATCH_THRESHOLD = 0.45  # default for vision.face_match_threshold (cosine similarity to a person's average)
MATCH_MARGIN = 0.08  # ...and at least this much closer to them than to the next person
DETECT_SCORE = 0.8  # YuNet's confidence before a face is compared at all
SAMPLE_SCORE = 0.9  # a sample needs a clear, frontal face
FLOOR_SCORE = 0.6  # weaker detections are ignored (they only help say "come closer")
MIN_FACE_PX = 48  # narrower faces (in a 640 px photo) are too small to tell apart
SAMPLE_MIN_FACE_PX = 72  # about an arm's length from the camera
PHOTO_WIDTH = 640
PHOTO_QUALITY = 90
PHOTO_TIMEOUT_S = 3.0
WARM_MAX_AGE_S = 1.5  # a live call's warm photo is used when at most this old
PREVIEW_WIDTH = 320
MAX_PEOPLE = 12
MAX_SAMPLES = 10  # per person; the oldest goes when a new one would make more
SUGGESTED_SAMPLES = 5
RETAKE_DAYS = 30  # babies' faces change fast: new samples every month or so
LABEL_MAX = 30
# The models (~110 MB) are dropped after this long without use on a 2 GB board (a 4 GB one keeps them): a chat
# started again soon after keeps them; later, the next chat loads them again (~0.4-1 s on a Pi 5; the call
# connects without waiting for them).
UNLOAD_IDLE_S = 120.0
# During a live call: a check every CHECK_EVERY_S. Someone counts as arrived after ARRIVE_SIGHTINGS
# sightings within ARRIVE_WINDOW_S (the very first check of a call counts on its own) and as gone
# LEAVE_AFTER_S after they were last seen (a baby turning away for a moment hasn't left). The voice is
# told when someone arrives it wasn't told about yet in this call (never when they leave: every note makes
# it talk, and recognition that comes and goes would keep it chattering), at most once every NOTE_MIN_GAP_S
# and only at a quiet moment.
CHECK_EVERY_S = 3.0
TICK_S = 0.5
ARRIVE_SIGHTINGS = 2
ARRIVE_WINDOW_S = 10.0
LEAVE_AFTER_S = 12.0
NOTE_MIN_GAP_S = 15.0
LOOK_FRESH_S = 8.0  # a camera look is told who was seen this recently

ERRORS = {
    "off": "Recognising family members is switched off. Turn it on in Privacy.",
    "camera_off": "The camera is turned off in Privacy.",
    "no_camera": "No camera is working right now.",
    "no_opencv": "OpenCV isn't installed, so LittleBot can't recognise faces.",
    "old_opencv": "This OpenCV is too old to recognise faces (it needs 4.8 or newer).",
    "no_models": "The face models aren't downloaded yet. On the robot, run: "
                 ".venv/bin/python scripts/download_models.py faces",
    "load_failed": "The face models couldn't be loaded. Download them again: "
                   ".venv/bin/python scripts/download_models.py faces --force, then switch it off and on.",
    "photo_failed": "LittleBot couldn't take a photo. Try again.",
}
# Why a sample wasn't kept, for the parent ({name}: the person; {other}: who it looks like instead).
SAMPLE_ERRORS = {
    "no_face": "LittleBot couldn't see a face clearly. Face the camera from about an arm's length away, in good light.",
    "too_small": "The face is too far away or turned aside. Come a little closer and face the camera.",
    "many_faces": "LittleBot can see more than one face. Make sure only {name} is in front of the camera.",
    "looks_like_other": "That looks more like {other} than {name}, so it wasn't kept. Make sure only {name} is in "
                        "front of the camera, then try again.",
}
FACES_INTRO = ("LittleBot recognises the family members a grown-up showed it, on the robot, and tells you who is "
               "in front of your camera. The person talking may be someone else nearby. Use only the names "
               "LittleBot gives you; never guess who someone is. When someone asks who they are or who you can "
               "see, delegate it to the client as \"who is here\" and wait.")
QUIET = "Don't announce this; just know it."

_SCHEMA = """
CREATE TABLE IF NOT EXISTS face_people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL
);
-- 128 float32 numbers per sample (512 bytes), never a photo
CREATE TABLE IF NOT EXISTS face_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id INTEGER NOT NULL REFERENCES face_people(id) ON DELETE CASCADE,
    features BLOB NOT NULL CHECK (length(features) = 512),
    taken_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS face_samples_person ON face_samples (person_id, id);
"""
_LABEL_RE = re.compile(r"^[^\W\d_][\w .'\-]*$", re.UNICODE)


class FacesUnavailable(Exception):
    """Recognition can't run now. ``reason``: a key of ``ERRORS``."""

    def __init__(self, reason: str) -> None:
        super().__init__(ERRORS.get(reason, reason))
        self.reason = reason


# ------------------------------------------------------------------ people and matching
@dataclasses.dataclass(frozen=True, eq=False)
class Known:
    """Someone LittleBot may recognise: the average of their samples (unit length, never shown or sent)."""

    id: int
    name: str
    role: str
    centre: np.ndarray = dataclasses.field(repr=False)
    samples: int = 1

    @property
    def label(self) -> str:
        return f"{self.name} ({self.role})" if self.role else self.name


@dataclasses.dataclass(frozen=True)
class SeenFace:
    """One face in a photo: where (fractions of the photo), how clear, and its numbers (None: not compared)."""

    box: tuple[float, float, float, float]  # x, y, w, h
    score: float
    width_px: int
    features: np.ndarray | None = dataclasses.field(default=None, repr=False, compare=False)


@dataclasses.dataclass(frozen=True)
class Seen:
    """A compared face: who it is (None: someone LittleBot doesn't know) and its best similarity."""

    person: Known | None
    score: float
    box: tuple[float, float, float, float]


@dataclasses.dataclass(frozen=True)
class Sighting:
    """One check: the compared faces in one photo, and when."""

    at: float
    faces: tuple[Seen, ...] = ()

    def people(self) -> list[Known]:
        return [f.person for f in self.faces if f.person is not None]

    @property
    def unknown(self) -> int:
        return sum(1 for f in self.faces if f.person is None)


def as_features(values: Any) -> np.ndarray:
    """128 finite numbers as a unit-length float32 vector (ValueError otherwise)."""
    v = np.asarray(values, dtype=np.float32).reshape(-1)
    if v.size != FEATURES or not np.all(np.isfinite(v)):
        raise ValueError(f"a face sample is {FEATURES} numbers")
    norm = float(np.linalg.norm(v))
    if norm < 1e-6:
        raise ValueError("a face sample can't be all zeros")
    return v / norm


def centre_of(samples: Iterable[np.ndarray]) -> np.ndarray:
    """The average of unit-length samples, made unit length again (it sits between them)."""
    mean = np.mean([as_features(s) for s in samples], axis=0)
    return as_features(mean)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(as_features(a), as_features(b)))


def best_match(features: np.ndarray, known: Sequence[Known], *, threshold: float = MATCH_THRESHOLD,
               margin: float = MATCH_MARGIN) -> tuple[Known | None, float]:
    """Who this face is, or None (someone LittleBot doesn't know), and its best similarity.

    Conservative: the face must be close to that person's average AND clearly closer to them than to
    anyone else, so it never picks between two people it can't tell apart."""
    scores = sorted(((similarity(features, k.centre), n) for n, k in enumerate(known)), reverse=True)
    if not scores:
        return None, 0.0
    best, idx = scores[0]
    second = scores[1][0] if len(scores) > 1 else -1.0
    if best >= threshold and best - second >= margin:
        return known[idx], best
    return None, best


def assign(faces: Sequence[SeenFace], known: Sequence[Known], *, threshold: float = MATCH_THRESHOLD,
           margin: float = MATCH_MARGIN) -> list[Seen]:
    """Each compared face's person; one person is at most one face in a photo (the closest one)."""
    compared = [f for f in faces if f.features is not None]
    matches = [best_match(f.features, known, threshold=threshold, margin=margin) for f in compared]
    order = sorted(range(len(compared)), key=lambda i: matches[i][1], reverse=True)
    taken: set[int] = set()
    out: dict[int, Seen] = {}
    for i in order:
        person, score = matches[i]
        if person is not None and person.id in taken:
            person = None  # a second face "is" the same person (a photo on the wall?): unknown
        if person is not None:
            taken.add(person.id)
        out[i] = Seen(person, score, compared[i].box)
    return [out[i] for i in range(len(compared))]


def clean_label(value: object, *, field: str = "name", required: bool = True) -> str:
    """A name or role as LittleBot may keep and say it ("Ali", "Amara's dad"); ValueError otherwise."""
    from littlebot.safety.content import find_blocked  # noqa: PLC0415

    text = unicodedata.normalize("NFC", str(value or "")).replace("’", "'")
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch == " ")
    text = " ".join(text.split())
    if not text:
        if required:
            raise ValueError(f"{field} is required")
        return ""
    if len(text) > LABEL_MAX:
        raise ValueError(f"{field} must be at most {LABEL_MAX} characters")
    if not _LABEL_RE.match(text):
        raise ValueError(f"{field} may only contain letters, spaces, apostrophes and hyphens")
    if find_blocked(text):
        raise ValueError(f"that {field} can't be used")
    return text


def join_names(parts: Sequence[str]) -> str:
    """ "A", "A and B", "A, B and C"."""
    parts = list(parts)
    if len(parts) < 2:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def describe(people: Sequence[Known], hide: str | None = None) -> str:
    """ "Ali (dad) and Amara (the baby)"; ``hide``: a name to say as "the little one" instead."""
    text = join_names([p.label for p in people])
    if hide:
        text = re.sub(rf"\b{re.escape(hide)}\b", "the little one", text, flags=re.I)
    return text


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class PeopleStore:
    """The people LittleBot may recognise and their samples, in ``ctx.db`` (thread-safe: the DB locks)."""

    def __init__(self, db: Any) -> None:
        self.db = db
        db.executescript(_SCHEMA)
        db.execute("PRAGMA secure_delete = ON")  # deleted samples are overwritten, not just unlinked

    # -- reads
    def people(self) -> list[dict[str, Any]]:
        """Names, roles and sample counts (never the numbers), oldest first."""
        rows = self.db.query(
            "SELECT p.id, p.name, p.role, p.added_at, COUNT(s.id) AS samples, MAX(s.taken_at) AS last_sample_at "
            "FROM face_people p LEFT JOIN face_samples s ON s.person_id = p.id GROUP BY p.id ORDER BY p.id")
        return [self._row(r) for r in rows]

    def person(self, person_id: int) -> dict[str, Any] | None:
        return next((p for p in self.people() if p["id"] == int(person_id)), None)

    def known(self) -> list[Known]:
        """Everyone with at least one sample, with the average of their samples."""
        names = {r["id"]: r for r in self.db.query("SELECT id, name, role FROM face_people")}
        grouped: dict[int, list[np.ndarray]] = collections.defaultdict(list)
        for r in self.db.query("SELECT person_id, features FROM face_samples ORDER BY id"):
            try:
                grouped[int(r["person_id"])].append(as_features(np.frombuffer(r["features"], dtype="<f4")))
            except ValueError:
                continue
        return [Known(pid, names[pid]["name"], names[pid]["role"], centre_of(v), len(v))
                for pid, v in sorted(grouped.items()) if pid in names]

    def has_samples(self) -> bool:
        return self.db.query_one("SELECT 1 AS x FROM face_samples LIMIT 1") is not None

    # -- writes
    def add_person(self, name: object, role: object = "") -> dict[str, Any]:
        name = clean_label(name, field="name")
        role = clean_label(role, field="role", required=False)
        people = self.people()
        if len(people) >= MAX_PEOPLE:
            raise ValueError(f"LittleBot can know at most {MAX_PEOPLE} people. Remove someone first.")
        if any(p["name"].lower() == name.lower() and p["role"].lower() == role.lower() for p in people):
            raise ValueError(f"{name} is already on the list.")
        cur = self.db.execute("INSERT INTO face_people (name, role, added_at) VALUES (?, ?, ?)",
                              (name, role, _utc_now()))
        return self.person(int(cur.lastrowid)) or {}

    def update_person(self, person_id: int, *, name: object = None, role: object = None) -> dict[str, Any]:
        if self.person(person_id) is None:
            raise KeyError("That person isn't on the list.")
        if name is not None:
            self.db.execute("UPDATE face_people SET name = ? WHERE id = ?", (clean_label(name), int(person_id)))
        if role is not None:
            self.db.execute("UPDATE face_people SET role = ? WHERE id = ?",
                            (clean_label(role, field="role", required=False), int(person_id)))
        return self.person(person_id) or {}

    def delete_person(self, person_id: int) -> bool:
        cur = self.db.execute("DELETE FROM face_people WHERE id = ?", (int(person_id),))  # samples cascade
        self._flush()
        return cur.rowcount > 0

    def add_sample(self, person_id: int, features: Any) -> int:
        """Keep one sample (128 numbers) for ``person_id``; how many they have now (oldest dropped past the limit)."""
        if self.person(person_id) is None:
            raise KeyError("That person isn't on the list.")
        blob = as_features(features).astype("<f4").tobytes()
        with self.db.transaction() as db:
            db.execute("INSERT INTO face_samples (person_id, features, taken_at) VALUES (?, ?, ?)",
                       (int(person_id), blob, _utc_now()))
            db.execute("DELETE FROM face_samples WHERE person_id = ? AND id NOT IN (SELECT id FROM face_samples "
                       "WHERE person_id = ? ORDER BY id DESC LIMIT ?)", (int(person_id), int(person_id), MAX_SAMPLES))
        row = self.db.query_one("SELECT COUNT(*) AS n FROM face_samples WHERE person_id = ?", (int(person_id),))
        return int(row["n"]) if row else 0

    def clear_samples(self, person_id: int) -> int:
        cur = self.db.execute("DELETE FROM face_samples WHERE person_id = ?", (int(person_id),))
        self._flush()
        return cur.rowcount

    def forget_all(self) -> int:
        """Every person and every sample. How many people were forgotten."""
        with self.db.transaction() as db:
            db.execute("DELETE FROM face_samples")
            cur = db.execute("DELETE FROM face_people")
        self._flush()
        return cur.rowcount

    def _flush(self) -> None:
        """Push deleted (overwritten) pages out of the write-ahead log too."""
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 - an in-memory or busy database: nothing more to do
            pass

    @staticmethod
    def _row(r: dict[str, Any]) -> dict[str, Any]:
        last = r.get("last_sample_at")
        days = None
        if last:
            try:
                days = (_dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromisoformat(last)).days
            except ValueError:
                days = None
        return {"id": int(r["id"]), "name": r["name"], "role": r["role"] or "", "samples": int(r["samples"] or 0),
                "added_at": r["added_at"], "last_sample_at": last, "last_sample_days": days,
                "needs_new_samples": days is not None and days >= RETAKE_DAYS}


# ------------------------------------------------------------------ the models
class FaceEngine:
    """YuNet (where the faces are, with their eyes, nose and mouth) + SFace (128 numbers per face)."""

    def __init__(self, detector_model: str | Path, recogniser_model: str | Path) -> None:
        cv2 = get_cv2()
        if cv2 is None:
            raise FacesUnavailable("no_opencv")
        if not (hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")):
            raise FacesUnavailable("old_opencv")
        with quiet_opencv():
            self._det = cv2.FaceDetectorYN.create(str(detector_model), "", (PHOTO_WIDTH, 480), FLOOR_SCORE, 0.3, 10)
            self._rec = cv2.FaceRecognizerSF.create(str(recogniser_model), "")
        self._size = (PHOTO_WIDTH, 480)
        self._lock = threading.Lock()
        # A broken or wrong model fails here (FaceRecognition then says so), not in the middle of a call.
        self.look(np.zeros((480, PHOTO_WIDTH, 3), np.uint8))
        as_features(self._rec.feature(np.full((112, 112, 3), 128, np.uint8)))

    def look(self, image: np.ndarray, *, min_score: float = DETECT_SCORE, min_px: int = MIN_FACE_PX) -> list[SeenFace]:
        """Every face YuNet finds (largest first); the clear, big-enough ones with their numbers."""
        h, w = image.shape[:2]
        with self._lock:
            if (w, h) != self._size:
                self._det.setInputSize((w, h))
                self._size = (w, h)
            _, rows = self._det.detect(image)
            out: list[SeenFace] = []
            for row in (np.asarray(rows) if rows is not None else ()):
                score, width = float(row[-1]), int(round(float(row[2])))
                features = None
                if score >= min_score and width >= min_px:
                    crop = self._rec.alignCrop(image, row)
                    features = as_features(self._rec.feature(crop))
                    del crop
                box = (float(row[0]) / w, float(row[1]) / h, float(row[2]) / w, float(row[3]) / h)
                out.append(SeenFace(box, score, width, features))
        out.sort(key=lambda f: f.width_px, reverse=True)
        return out


def decode_jpeg(jpeg: bytes) -> np.ndarray:
    cv2 = get_cv2()
    if cv2 is None:
        raise FacesUnavailable("no_opencv")
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise FacesUnavailable("photo_failed")
    return image


def preview_url(image: np.ndarray, marks: Sequence[tuple[tuple[float, float, float, float], bool]]) -> str | None:
    """A small JPEG for the parent's dashboard (a data: URL), each face boxed and numbered (green: known
    or kept). Made only when the parent asks; never kept on the robot."""
    cv2 = get_cv2()
    if cv2 is None:
        return None
    h, w = image.shape[:2]
    scale = min(1.0, PREVIEW_WIDTH / max(1, w))
    img = cv2.resize(image, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    ih, iw = img.shape[:2]
    for n, ((x, y, bw, bh), good) in enumerate(marks, 1):
        p0, p1 = (int(x * iw), int(y * ih)), (int((x + bw) * iw), int((y + bh) * ih))
        colour = (80, 200, 80) if good else (0, 170, 255)
        cv2.rectangle(img, p0, p1, colour, 2)
        cv2.putText(img, str(n), (p0[0] + 3, max(12, p0[1] - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 2)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    del img
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode() if ok else None


# ------------------------------------------------------------------ the shared recogniser
class FaceRecognition:
    """``get_faces(ctx)``: the family's faces (people, samples, models), shared by live voice and the dashboard."""

    def __init__(self, ctx: Any, *, engine: Any = None, root: Path | None = None) -> None:
        self.ctx = ctx
        self.store = PeopleStore(ctx.db)
        self._engine = engine  # injected (tests) or loaded on first use
        self._injected = engine is not None
        self._root = root
        self._lock = threading.Lock()
        self._load_error: str | None = None
        self._last_used = 0.0
        self._unload_at: asyncio.TimerHandle | None = None
        self._sub = ctx.bus.subscribe(Topics.PRIVACY_CHANGED, self._on_privacy)

    # -- gates
    def enabled(self) -> bool:
        return bool(getattr(self.ctx.privacy, "face_recognition_enabled", False))

    @property
    def threshold(self) -> float:
        value = getattr(getattr(self.ctx.config, "vision", None), "face_match_threshold", MATCH_THRESHOLD)
        try:
            return min(0.95, max(0.3, float(value)))
        except (TypeError, ValueError):
            return MATCH_THRESHOLD

    def blocked_reason(self) -> str | None:
        """Why recognising isn't possible now (a key of ``ERRORS``), or None. Checked before every photo."""
        if not self.enabled():
            return "off"
        if not self.ctx.privacy.camera_enabled:
            return "camera_off"
        vision = self.ctx.vision
        if vision is None or not hasattr(vision, "capture_photo"):
            return "no_camera"
        try:
            camera = (vision.status() or {}).get("camera")
        except Exception:  # noqa: BLE001
            camera = None
        if camera not in ("ok", "starting"):
            return "no_camera"
        return self.models_problem()

    def model_paths(self) -> tuple[Path, Path]:
        from littlebot.vision.detector import resolve_model_path  # noqa: PLC0415

        v = getattr(self.ctx.config, "vision", None)
        det = str(getattr(v, "yunet_model", "") or "models/face_detection_yunet_2023mar.onnx")
        rec = str(getattr(v, "sface_model", "") or SFACE_MODEL)
        if self._root is not None:
            return self._root / Path(det).name, self._root / Path(rec).name
        return resolve_model_path(det), resolve_model_path(rec)

    def models_problem(self) -> str | None:
        """no_opencv | old_opencv | no_models | load_failed, or None (cheap: no loading)."""
        if self._injected:
            return None
        if self._load_error:
            return self._load_error
        cv2 = get_cv2()
        if cv2 is None:
            return "no_opencv"
        if not (hasattr(cv2, "FaceDetectorYN") and hasattr(cv2, "FaceRecognizerSF")):
            return "old_opencv"
        if not all(p.is_file() for p in self.model_paths()):
            return "no_models"
        return None

    def status(self) -> dict[str, Any]:
        reason = self.blocked_reason()
        problem = self.models_problem()
        return {"enabled": self.enabled(), "blocked": reason, "blocked_detail": ERRORS.get(reason) if reason else None,
                "models_ready": problem is None, "models_detail": ERRORS.get(problem) if problem else None,
                "loaded": self._engine is not None, "threshold": round(self.threshold, 3)}

    # -- models
    def engine(self) -> Any:
        """The models, loaded on first use (call from a worker thread: ~0.2-1 s and ~110 MB of RAM)."""
        with self._lock:  # one load at a time
            engine = self._engine
            if engine is not None:
                return engine
            problem = self.models_problem()
            if problem:
                raise FacesUnavailable(problem)
            det, rec = self.model_paths()
            t0 = time.perf_counter()
            try:
                engine = FaceEngine(det, rec)
            except Exception as e:  # noqa: BLE001 - cv2.error: a broken or wrong model file
                self._load_error = "load_failed"
                log.warning("face recognition: the face models couldn't be loaded (%s)", type(e).__name__)
                raise FacesUnavailable("load_failed") from None
            log.info("face recognition: models loaded in %d ms", int((time.perf_counter() - t0) * 1000))
            if self.enabled():  # not when it was switched off meanwhile: this one use only
                self._engine = engine
            return engine

    def unload(self, why: str) -> None:
        """Drop the models (never waits for a load in progress; that one isn't kept once switched off)."""
        self._load_error = None
        if self._injected or self._engine is None:
            return
        self._engine = None
        memory.give_back_soon()
        log.info("face recognition: models unloaded (%s)", why)

    def _used(self) -> None:
        """Keep the models a while after each use; drop them after UNLOAD_IDLE_S without one. With room to spare
        (a 4 GB Pi: littlebot/core/memory.py roomy) they stay while switched on: every chat knows faces at once."""
        self._last_used = time.monotonic()
        if memory.roomy_for(self.ctx):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._unload_at is not None:
            self._unload_at.cancel()
        self._unload_at = loop.call_later(UNLOAD_IDLE_S, self._unload_if_idle)

    def _unload_if_idle(self) -> None:
        self._unload_at = None
        if time.monotonic() - self._last_used >= UNLOAD_IDLE_S - 1:
            self.unload(f"not used for {UNLOAD_IDLE_S / 60:g} minutes")

    def _on_privacy(self, event: Event) -> None:
        if not self.enabled() or not self.ctx.privacy.camera_enabled:
            self.unload("switched off")

    # -- photos
    async def _photo(self, *, fresh: bool) -> bytes:
        """A live call's warm photo when recent (no extra work for the camera), else one new still."""
        vision = self.ctx.vision
        latest = None if fresh else getattr(vision, "latest_photo", None)
        if latest is not None:
            try:
                got = latest(max_age=WARM_MAX_AGE_S)
            except Exception:  # noqa: BLE001
                got = None
            if got and got[0]:
                return got[0]
        try:
            return await vision.capture_photo(max_width=PHOTO_WIDTH, quality=PHOTO_QUALITY, timeout=PHOTO_TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - PhotoUnavailable(reason) or a broken camera
            code = getattr(e, "reason", None)
            raise FacesUnavailable(code if code in ("camera_off", "no_camera") else "photo_failed") from None

    async def _faces(self, *, fresh: bool, min_score: float = DETECT_SCORE,
                     min_px: int = MIN_FACE_PX) -> tuple[np.ndarray, list[SeenFace]]:
        reason = self.blocked_reason()
        if reason:
            raise FacesUnavailable(reason)
        jpeg = await self._photo(fresh=fresh)
        engine = await asyncio.to_thread(self.engine)
        self._used()

        def run() -> tuple[np.ndarray, list[SeenFace]]:
            image = decode_jpeg(jpeg)
            return image, engine.look(image, min_score=min_score, min_px=min_px)

        try:
            return await asyncio.to_thread(run)  # the photo goes with this call: nothing keeps it
        finally:
            self._used()  # the idle time counts from the end of a check, not its start

    async def who_is_there(self) -> Sighting | None:
        """Who is in front of the camera now (one photo, then dropped), or None when nobody is enrolled.
        Raises ``FacesUnavailable`` when recognising isn't possible."""
        known = self.store.known()
        if not known:
            return None
        t0 = time.perf_counter()
        image, faces = await self._faces(fresh=False)
        del image
        seen = assign(faces, known, threshold=self.threshold)
        sighting = Sighting(time.monotonic(), tuple(seen))
        log.debug("face recognition: %d face(s), %d known, in %d ms", len(seen), len(sighting.people()),
                  int((time.perf_counter() - t0) * 1000))
        return sighting

    async def check(self) -> dict[str, Any]:
        """The dashboard's "Who's in view?": a new photo, who LittleBot recognises and a preview."""
        known = self.store.known()
        image, faces = await self._faces(fresh=True)
        seen = assign(faces, known, threshold=self.threshold)
        preview = await asyncio.to_thread(preview_url, image, [(f.box, f.person is not None) for f in seen])
        del image
        found = [{"n": n, "name": f.person.name if f.person else None, "role": f.person.role if f.person else None,
                  "score": round(f.score, 2)} for n, f in enumerate(seen, 1)]
        log.info("face recognition: checked from the dashboard (%d face(s), %d known)", len(seen),
                 sum(1 for f in seen if f.person is not None))
        return {"ok": True, "faces": found, "threshold": round(self.threshold, 2), "preview": preview,
                "nobody_enrolled": not known}

    async def take_sample(self, person_id: int) -> dict[str, Any]:
        """One new photo of ``person_id`` alone: keep its 128 numbers (never the photo) when it is clear."""
        person = self.store.person(person_id)
        if person is None:
            raise KeyError("That person isn't on the list.")
        image, faces = await self._faces(fresh=True, min_score=SAMPLE_SCORE, min_px=SAMPLE_MIN_FACE_PX)
        good = [f for f in faces if f.features is not None]
        others = [f for f in faces if f.features is None and f.score >= DETECT_SCORE and f.width_px >= MIN_FACE_PX]
        error, other = None, None
        if len(good) + len(others) > 1:
            error = "many_faces"
        elif not good:
            error = "too_small" if faces else "no_face"
        else:
            known = self.store.known()
            own = next((k for k in known if k.id == person["id"]), None)
            mine = similarity(good[0].features, own.centre) if own is not None else -1.0
            rivals = [(similarity(good[0].features, k.centre), k) for k in known if k.id != person["id"]]
            close = max(rivals, key=lambda r: r[0], default=None)
            if close is not None and close[0] >= self.threshold and close[0] > mine:
                error, other = "looks_like_other", close[1].name
        marks = [(f.box, error is None and f is good[0]) for f in faces]
        preview = await asyncio.to_thread(preview_url, image, marks)
        del image
        if error is not None:
            log.info("face recognition: sample not kept (%s)", error)
            return {"ok": False, "error": error, "preview": preview, "person": person,
                    "detail": SAMPLE_ERRORS[error].format(name=person["name"], other=other)}
        count = self.store.add_sample(person["id"], good[0].features)
        log.info("face recognition: sample kept (%d for that person)", count)
        return {"ok": True, "samples": count, "preview": preview, "person": self.store.person(person["id"])}


def get_faces(ctx: Any) -> FaceRecognition:
    """The one FaceRecognition shared by live voice and the dashboard (``ctx.extras["faces"]``)."""
    faces = ctx.extras.get(_KEY)
    if faces is None:
        faces = ctx.extras[_KEY] = FaceRecognition(ctx)
    return faces


def hidden_child_name(ctx: Any) -> str | None:
    """The child's nickname when the parent doesn't share it with the cloud (``ai.share_child_name`` off)."""
    from littlebot.brain.prompts import clean_child_name  # noqa: PLC0415

    try:
        settings = ctx.settings
        if bool(settings.get("ai.share_child_name", ctx.config.ai.share_child_name)):
            return None
        profile = settings.get("profile") or {}
        return clean_child_name(profile.get("nickname")) if isinstance(profile, dict) else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ during a live call
class Presence:
    """Who is in view, steadied over a few seconds (see ARRIVE_SIGHTINGS / LEAVE_AFTER_S)."""

    def __init__(self) -> None:
        self.here: dict[int, Known] = {}  # in the order they arrived
        self._sightings: dict[int, collections.deque[float]] = {}
        self._last_seen: dict[int, float] = {}

    def update(self, people: Iterable[Known], now: float, *, at_once: bool = False) -> bool:
        """One check's people (``at_once``: they count as arrived straight away). True if ``here`` changed."""
        before = list(self.here)
        for p in people:
            seen = self._sightings.setdefault(p.id, collections.deque(maxlen=ARRIVE_SIGHTINGS))
            seen.append(now)
            self._last_seen[p.id] = now
            if p.id in self.here:
                self.here[p.id] = p  # a new name or role from the dashboard
            elif at_once or (len(seen) >= ARRIVE_SIGHTINGS and now - seen[0] <= ARRIVE_WINDOW_S):
                self.here[p.id] = p
        for pid in [pid for pid in self.here if now - self._last_seen.get(pid, now) >= LEAVE_AFTER_S]:
            del self.here[pid]
        return list(self.here) != before

    def reset(self) -> None:
        self.here.clear()
        self._sightings.clear()
        self._last_seen.clear()


def _where(box: tuple[float, float, float, float]) -> str:
    cx = box[0] + box[2] / 2
    return "on the left of the photo" if cx < 0.38 else "on the right of the photo" if cx > 0.62 else \
        "in the middle of the photo"


def look_context(sighting: Sighting, hide: str | None = None) -> str | None:
    """What a camera look is told about who LittleBot recognised (names only), or None."""
    if not sighting.faces:
        return None
    many = len(sighting.faces) > 1
    known = [f for f in sighting.faces if f.person is not None]
    if not known:
        who = "the people" if many else "the person"
        return f"LittleBot's face recognition doesn't know {who} in front of the camera: never guess who they are."
    parts = [describe([f.person], hide) + (f" {_where(f.box)}" if many else "") for f in known]  # type: ignore[list-item]
    if sighting.unknown:
        parts.append("someone LittleBot doesn't know" if sighting.unknown == 1 else "people LittleBot doesn't know")
    return (f"LittleBot's face recognition, a moment ago: {join_names(parts)}. When the question is about these "
            "people you may call them by these names; never guess a name for anyone else, and use no names if "
            "you can't tell who is who.")


_CHILD_ROLE = re.compile(r"\b(?:baby|child|kid|toddler|daughter|son|little one)\b", re.I)


def is_child(name: str, role: str, child: str | None = None) -> bool:
    """Whether someone on the list is the little one: a child's role ("the baby"), or the child's nickname
    (``child``). Also used for voices (littlebot/voices/)."""
    return bool(_CHILD_ROLE.search(role or "") or (child and (name or "").lower() == child.lower()))


def who_answer(sighting: Sighting | None, hide: str | None = None, child: str | None = None) -> str:
    """For "do you know who I am?" / "who is this?": what a fresh check saw, for the voice to say now.
    ``child``: the child's nickname. Whoever asks is talking, so the child in view (too young to ask)
    is never taken for the person asking."""
    if sighting is None:
        return ("Nobody has been added to LittleBot's face recognition yet, so you can't tell who anyone is. Say so; "
                "a grown-up can add the family under Who's who on the Profile page of the dashboard.")
    if not sighting.faces:
        return ("Nobody is in front of your camera right now, so you can't tell who is talking. Say so, and ask "
                "them to come in front of your camera.")
    people = sighting.people()
    if not people:
        return ("LittleBot's face recognition doesn't know the person in front of the camera. Say you don't "
                "recognise them yet, never guess a name; a grown-up can add them under Who's who on the dashboard.")
    kids = [p for p in people if _CHILD_ROLE.search(p.role) or (child and p.name.lower() == child.lower())]
    grown = [p for p in people if p not in kids]
    others = f", and {sighting.unknown} {'person' if sighting.unknown == 1 else 'people'} it doesn't know" \
        if sighting.unknown else ""
    seen = f"LittleBot's face recognition sees {describe(people, hide)} in front of the camera right now{others}."
    if len(grown) == 1 and not sighting.unknown:
        return f"{seen} So the grown-up talking to you is {describe(grown, hide)}: answer warmly, by name."
    if not grown:
        return (f"{seen} The person asking is a grown-up who isn't in view, so you can't tell which grown-up it "
                "is: say who you can see, and ask who they are. Never call them by the little one's name.")
    return f"{seen} Say who you can see; if you can't tell which of them is talking, say so rather than guess."


class CallFaces:
    """Who is in front of the camera during one live call (littlebot/chatgpt/live.py).

    ``intro()``: a line for the call's instructions. ``run()``: a check every CHECK_EVERY_S while
    recognising is allowed; when someone arrives (steadied by ``Presence``) that the voice wasn't told
    about yet in this call, one quiet note goes through ``tell(text)``, which returns False when it can't
    be sent right now (it is tried again). Nobody leaving is ever a note. ``for_look()``: their names for
    a camera look. Only names and roles are ever passed on."""

    def __init__(self, faces: FaceRecognition, *, tell: Callable[[str], bool], hide: str | None = None,
                 child: str | None = None, now: Callable[[], float] = time.monotonic) -> None:
        self.faces = faces
        self.tell = tell
        self.hide = hide
        self.child = child  # the child's nickname: never taken for the person asking "who am I?"
        self._now = now
        self.presence = Presence()
        self.latest: Sighting | None = None
        self.latest_at: float | None = None
        self.told: frozenset[int] = frozenset()  # who the voice was told about in this call
        self._told_at: float | None = None
        self._checks = 0
        self._said_why: str | None = None
        self.closed = False
        self.checked = asyncio.Event()  # the first check is done (or recognising can't run): the intro can say who

    def intro(self) -> str:
        """For the call's instructions (built just before connecting): the latest check, if there is one."""
        if self.faces.blocked_reason() is not None or not self.faces.store.has_samples():
            return ""
        text = FACES_INTRO
        if self.latest is not None:
            people = self.latest.people()
            self.told |= frozenset(p.id for p in people)
            text += (f" In front of your camera now: {describe(people, self.hide)}." if people else
                     " Nobody LittleBot knows is in front of the camera right now.")
            log.info("live voice: %d known face(s) in view at the start", len(people))
        return text

    def for_look(self) -> str | None:
        s = self.latest
        if s is None or self.latest_at is None or self._now() - self.latest_at > LOOK_FRESH_S \
                or self.faces.blocked_reason() is not None:
            return None
        return look_context(s, self.hide)

    def summary(self) -> str:
        """For the decision log (littlebot/decide): who is in view now, by face ("Ali (dad)", "nobody it knows")."""
        if self.closed or self.faces.blocked_reason() is not None:
            return "off"
        people = list(self.presence.here.values())
        unknown = self.latest.unknown if self.latest is not None else 0
        text = describe(people, self.hide) if people else "nobody it knows"
        return text + (f", and {unknown} face(s) it doesn't know" if unknown else "")

    def grown_up(self) -> str | None:
        """The one grown-up in view now ("Ali (dad)"), when there is exactly one; else None."""
        child = (self.child or "").lower()
        grown = [p for p in self.presence.here.values()
                 if not (_CHILD_ROLE.search(p.role) or (child and p.name.lower() == child))]
        return describe(grown, self.hide) if len(grown) == 1 else None

    async def answer_now(self) -> str:
        """A fresh check for "who am I?", also kept as the latest (the voice then knows who is here)."""
        try:
            sighting = await self.faces.who_is_there()
        except FacesUnavailable as e:
            self._say_why(e.reason)
            return ("LittleBot's face recognition can't look right now, so you can't tell who is there. Say so "
                    "briefly; never guess.")
        now = self._now()
        if sighting is not None:
            self.latest, self.latest_at = sighting, now
            self.presence.update(sighting.people(), now, at_once=True)
            self.told, self._told_at = self.told | frozenset(p.id for p in sighting.people()), now
        log.info("live voice: asked who is there (%d face(s), %d known)",
                 len(sighting.faces) if sighting else 0, len(sighting.people()) if sighting else 0)
        return who_answer(sighting, self.hide, self.child)

    def close(self) -> None:
        self.closed = True
        self.latest = None
        self.presence.reset()

    async def run(self) -> None:
        last_check: float | None = None
        while not self.closed:
            now = self._now()
            reason = self.faces.blocked_reason()
            if reason is not None:
                self._blocked(reason, now)
            elif last_check is None or now - last_check >= CHECK_EVERY_S:
                last_check = now
                try:
                    await self._check()
                finally:
                    self.checked.set()
            self._maybe_tell(self._now())
            await asyncio.sleep(TICK_S)

    async def _check(self) -> None:
        try:
            sighting = await self.faces.who_is_there()
        except FacesUnavailable as e:
            self._say_why(e.reason)
            return
        except Exception:  # noqa: BLE001 - knowing who is there must never break the call
            log.warning("live voice: face recognition failed", exc_info=True)
            return
        now = self._now()
        if sighting is None:  # nobody is enrolled
            self.latest = None
            self.presence.update([], now)
            return
        self._checks += 1
        if self._checks == 1:
            log.info("live voice: face recognition is on (%d face(s) in view, %d known)", len(sighting.faces),
                     len(sighting.people()))
        self.latest, self.latest_at = sighting, now
        self.presence.update(sighting.people(), now, at_once=self._checks == 1)

    def _blocked(self, reason: str, now: float) -> None:
        self.checked.set()
        self._say_why(reason)
        if reason in ("off", "camera_off"):  # the parent withdrew it: forget at once, and tell the voice
            self.latest = None
            self.presence.reset()
            if self.told and self.tell("LittleBot has stopped recognising faces, so you don't know who is in "
                                       f"front of the camera any more. {QUIET}"):
                self.told, self._told_at = frozenset(), now
        else:  # no camera for a moment: people leave in their own time
            self.presence.update([], now)

    def _say_why(self, reason: str) -> None:
        if reason != self._said_why:
            self._said_why = reason
            if reason != "off":
                log.info("live voice: face recognition can't run (%s)", reason)

    def _maybe_tell(self, now: float) -> None:
        here = list(self.presence.here.values())
        if not any(p.id not in self.told for p in here):
            return
        if self._told_at is not None and now - self._told_at < NOTE_MIN_GAP_S:
            return
        if self.tell(f"Now in front of your camera: {describe(here, self.hide)}. {QUIET}"):
            self.told, self._told_at = self.told | frozenset(p.id for p in here), now
            log.info("live voice: told who is in view (%d known)", len(here))


__all__ = ["CallFaces", "FaceEngine", "FaceRecognition", "FacesUnavailable", "Known", "PeopleStore", "Presence",
           "Seen", "SeenFace", "Sighting", "assign", "best_match", "centre_of", "clean_label", "describe",
           "get_faces", "hidden_child_name", "is_child", "look_context", "who_answer"]

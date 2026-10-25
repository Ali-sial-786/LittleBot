"""Recognising the family's faces (littlebot/vision/faces.py), with made-up numbers and fake models.

Nothing here looks at a real face: the models are stood in for by ``FakeEngine`` (each "face" is a
hand-picked set of 128 numbers), so this tests the matching, the storage, the privacy gates and what
the live voice is told, not how well SFace tells real people apart.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from littlebot.core.settings import build_defaults
from littlebot.core.storage import Database
from littlebot.vision import faces as F
from littlebot.vision.imaging import get_cv2
from littlebot.vision.service import PhotoUnavailable

needs_cv2 = pytest.mark.skipif(get_cv2() is None, reason="OpenCV needed to decode the photo")


# ------------------------------------------------------------------ helpers
def face(*weights: tuple[int, float]) -> np.ndarray:
    """A made-up face: ``(axis, weight)`` pairs on orthogonal axes, so similarities are exact."""
    v = np.zeros(F.FEATURES, np.float32)
    for axis, w in weights:
        v[axis] += w
    return F.as_features(v)


def known(pid: int, name: str, role: str, *weights: tuple[int, float]) -> F.Known:
    return F.Known(pid, name, role, face(*weights))


def seen_face(features: np.ndarray | None, *, x: float = 0.4, score: float = 0.95, width: int = 120) -> dict:
    return {"box": (x, 0.3, 0.2, 0.3), "score": score, "width": width, "features": features}


class FakeEngine:
    """Stands in for YuNet + SFace: returns the scripted faces, with numbers only when clear and big enough."""

    def __init__(self, scripted: list[dict] | None = None) -> None:
        self.scripted = scripted or []
        self.calls: list[tuple[tuple[int, ...], float, int]] = []

    def look(self, image: np.ndarray, *, min_score: float = F.DETECT_SCORE,
             min_px: int = F.MIN_FACE_PX) -> list[F.SeenFace]:
        self.calls.append((image.shape, min_score, min_px))
        out = []
        for s in self.scripted:
            ok = s["features"] is not None and s["score"] >= min_score and s["width"] >= min_px
            out.append(F.SeenFace(s["box"], s["score"], s["width"], s["features"] if ok else None))
        return out


def jpeg(width: int = 640, height: int = 480) -> bytes:
    cv2 = get_cv2()
    ok, buf = cv2.imencode(".jpg", np.full((height, width, 3), 120, np.uint8))
    assert ok
    return buf.tobytes()


class FakeVision:
    """The camera: ``photos`` counts new stills; ``warm`` is a live call's warm photo (or None)."""

    def __init__(self, camera: str = "ok") -> None:
        self.camera = camera
        self.photos = 0
        self.warm: tuple[bytes, float] | None = None
        self.warm_asked = 0
        self.error: Exception | None = None

    def status(self) -> dict[str, Any]:
        return {"camera": self.camera}

    def latest_photo(self, max_age: float = 1.0) -> tuple[bytes, float] | None:
        self.warm_asked += 1
        return self.warm

    async def capture_photo(self, *, max_width: int = 640, quality: int = 85, timeout: float = 3.0) -> bytes:
        if self.error is not None:
            raise self.error
        self.photos += 1
        return jpeg()


@pytest.fixture
def setup(ctx):
    """A FaceRecognition with fake models and a fake camera, switched on."""
    ctx.vision = FakeVision()
    engine = FakeEngine()
    rec = F.FaceRecognition(ctx, engine=engine)
    ctx.extras["faces"] = rec
    ctx.privacy.set(face_recognition_enabled=True)
    return SimpleNamespace(ctx=ctx, rec=rec, engine=engine, vision=ctx.vision, store=rec.store)


def no_numbers_logged(caplog: Any) -> None:
    for r in caplog.records:
        msg = r.getMessage()
        assert not re.search(r"-?\d\.\d{3,}", msg), msg  # never a face's numbers (or anything like them)
        assert "features" not in msg and "array" not in msg


# ------------------------------------------------------------------ matching
def test_a_sample_is_128_finite_numbers_made_unit_length():
    v = F.as_features(np.arange(1, 129))
    assert v.dtype == np.float32 and v.shape == (128,) and abs(float(np.linalg.norm(v)) - 1) < 1e-5
    for bad in (np.ones(127), np.zeros(128), [float("nan")] * 128, np.full(128, np.inf)):
        with pytest.raises(ValueError):
            F.as_features(bad)


def test_a_persons_samples_are_averaged():
    a, b = face((0, 1.0), (3, 0.5)), face((0, 1.0), (3, -0.5))
    centre = F.centre_of([a, b])
    assert F.similarity(centre, face((0, 1.0))) == pytest.approx(1.0, abs=1e-5)  # the wobble cancels out
    assert F.similarity(centre, a) > 0.85


def test_a_face_is_someone_only_when_clearly_close_to_them():
    ali, mum = known(1, "Ali", "dad", (0, 1.0)), known(2, "Mum", "", (1, 1.0))
    who, score = F.best_match(face((0, 0.6), (2, 0.8)), [ali, mum])  # 0.6 to Ali, 0 to Mum
    assert who is ali and score == pytest.approx(0.6, abs=1e-4)
    who, score = F.best_match(face((0, 0.4), (2, 0.92)), [ali, mum])  # 0.4: below 0.45
    assert who is None and score == pytest.approx(0.4, abs=0.01)
    assert F.best_match(face((0, 0.45), (2, 0.893)), [ali, mum], threshold=0.45)[0] is ali  # just at it


def test_a_face_between_two_people_is_nobody_never_a_guess():
    ali, mum = known(1, "Ali", "dad", (0, 1.0)), known(2, "Mum", "", (1, 1.0))
    who, score = F.best_match(face((0, 0.75), (1, 0.7)), [ali, mum])  # close to both: can't tell
    assert who is None and score > 0.45
    assert F.best_match(face((0, 0.8), (1, 0.6)), [ali, mum])[0] is ali  # clearly closer to Ali
    assert F.best_match(face((0, 1.0)), []) == (None, 0.0)  # nobody enrolled


def test_someone_new_is_unknown():
    ali, mum = known(1, "Ali", "dad", (0, 1.0)), known(2, "Mum", "", (1, 1.0))
    who, score = F.best_match(face((5, 1.0)), [ali, mum])
    assert who is None and score == pytest.approx(0.0, abs=1e-5)


def test_one_person_is_at_most_one_face_in_a_photo():
    ali = known(1, "Ali", "dad", (0, 1.0))
    faces = [F.SeenFace((0.1, 0.1, 0.2, 0.2), 0.9, 100, face((0, 0.6), (2, 0.8))),  # Ali (0.6)
             F.SeenFace((0.6, 0.1, 0.2, 0.2), 0.9, 100, face((0, 0.9), (2, 0.436))),  # more like Ali (0.9)
             F.SeenFace((0.4, 0.5, 0.1, 0.1), 0.7, 40, None)]  # too small to compare: left out
    seen = F.assign(faces, [ali])
    assert len(seen) == 2
    assert seen[0].person is None and seen[1].person is ali  # the closer one is Ali, the other is unknown
    assert seen[0].box == faces[0].box


def test_labels_and_names():
    assert F.Known(1, "Ali", "dad", face((0, 1))).label == "Ali (dad)"
    assert F.Known(2, "Mum", "", face((1, 1))).label == "Mum"
    assert F.join_names([]) == "" and F.join_names(["A"]) == "A" and F.join_names(["A", "B"]) == "A and B"
    assert F.join_names(["A", "B", "C"]) == "A, B and C"
    people = [known(1, "Ali", "dad", (0, 1)), known(2, "Amara", "the baby", (1, 1))]
    assert F.describe(people) == "Ali (dad) and Amara (the baby)"
    assert F.describe(people, hide="amara") == "Ali (dad) and the little one (the baby)"


@pytest.mark.parametrize("value,expected", [
    ("Ali", "Ali"), ("  Grandma   Rose ", "Grandma Rose"), ("Amara’s dad", "Amara's dad"), ("Zoë", "Zoë"),
    ("Mary-Jane", "Mary-Jane"),
])
def test_names_are_tidied(value, expected):
    assert F.clean_label(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "42", "<b>Ali</b>", "Ali!", "x" * 31, "Sexy Sam", "a‮b!"])
def test_odd_names_are_refused(value):
    with pytest.raises(ValueError):
        F.clean_label(value)


def test_a_role_may_be_left_empty():
    assert F.clean_label("", field="role", required=False) == ""
    assert F.clean_label(None, field="role", required=False) == ""


# ------------------------------------------------------------------ storage
def test_people_and_samples_are_kept_without_any_photo(ctx):
    store = F.PeopleStore(ctx.db)
    ali = store.add_person("Ali", "dad")
    assert ali["name"] == "Ali" and ali["role"] == "dad" and ali["samples"] == 0
    assert store.add_sample(ali["id"], face((0, 1.0))) == 1
    assert store.add_sample(ali["id"], face((0, 1.0), (4, 0.2))) == 2
    cols = {t: {r["name"] for r in ctx.db.query(f"PRAGMA table_info({t})")} for t in ("face_people", "face_samples")}
    assert cols == {"face_people": {"id", "name", "role", "added_at"},
                    "face_samples": {"id", "person_id", "features", "taken_at"}}
    blobs = [r["features"] for r in ctx.db.query("SELECT features FROM face_samples")]
    assert [len(b) for b in blobs] == [512, 512]  # 128 numbers, nothing else
    row = store.people()[0]
    assert row["samples"] == 2 and not row["needs_new_samples"] and row["last_sample_days"] == 0
    assert "features" not in row and "centre" not in row
    (k,) = store.known()
    assert k.name == "Ali" and k.samples == 2 and F.similarity(k.centre, face((0, 1.0))) > 0.99
    assert store.has_samples()


def test_the_database_refuses_anything_but_128_numbers(ctx):
    store = F.PeopleStore(ctx.db)
    pid = store.add_person("Ali")["id"]
    for blob in (b"\xff\xd8 a jpeg" * 100, b"x" * 511, b""):
        with pytest.raises(sqlite3.IntegrityError):
            ctx.db.execute("INSERT INTO face_samples (person_id, features, taken_at) VALUES (?, ?, 'now')",
                           (pid, blob))
    with pytest.raises(ValueError):
        store.add_sample(pid, np.ones(64))
    with pytest.raises(KeyError):
        store.add_sample(999, face((0, 1)))


def test_adding_people_checks_names_duplicates_and_the_limit(ctx, monkeypatch):
    store = F.PeopleStore(ctx.db)
    store.add_person("Ali", "dad")
    with pytest.raises(ValueError, match="already"):
        store.add_person("ali", "Dad")
    store.add_person("Ali", "uncle")  # another Ali is fine
    with pytest.raises(ValueError):
        store.add_person("<script>")
    monkeypatch.setattr(F, "MAX_PEOPLE", 2)
    with pytest.raises(ValueError, match="at most 2"):
        store.add_person("Mum")
    assert [p["name"] for p in store.people()] == ["Ali", "Ali"]


def test_renaming_someone(ctx):
    store = F.PeopleStore(ctx.db)
    pid = store.add_person("Ali", "dad")["id"]
    assert store.update_person(pid, name="Alistair")["name"] == "Alistair"
    assert store.update_person(pid, role="")["role"] == ""
    with pytest.raises(ValueError):
        store.update_person(pid, name="")
    with pytest.raises(KeyError):
        store.update_person(999, name="Bob")


def test_only_the_newest_samples_are_kept(ctx, monkeypatch):
    monkeypatch.setattr(F, "MAX_SAMPLES", 3)
    store = F.PeopleStore(ctx.db)
    pid = store.add_person("Amara", "the baby")["id"]
    for axis in range(5):
        n = store.add_sample(pid, face((axis, 1.0)))
    assert n == 3 and store.person(pid)["samples"] == 3
    (k,) = store.known()
    assert F.similarity(k.centre, face((2, 1.0), (3, 1.0), (4, 1.0))) == pytest.approx(1.0, abs=1e-5)


def test_old_samples_are_flagged_for_retaking(ctx):
    store = F.PeopleStore(ctx.db)
    pid = store.add_person("Amara")["id"]
    store.add_sample(pid, face((0, 1.0)))
    ctx.db.execute("UPDATE face_samples SET taken_at = '2020-01-01T00:00:00+00:00'")
    row = store.person(pid)
    assert row["needs_new_samples"] and row["last_sample_days"] > F.RETAKE_DAYS


def test_deleting_someone_deletes_their_samples(ctx):
    store = F.PeopleStore(ctx.db)
    ali, mum = store.add_person("Ali")["id"], store.add_person("Mum")["id"]
    store.add_sample(ali, face((0, 1.0)))
    store.add_sample(mum, face((1, 1.0)))
    assert store.delete_person(ali) and not store.delete_person(ali)
    assert [k.name for k in store.known()] == ["Mum"]
    assert ctx.db.query_one("SELECT COUNT(*) AS n FROM face_samples")["n"] == 1
    assert store.clear_samples(mum) == 1 and not store.has_samples() and store.person(mum)["samples"] == 0
    store.add_sample(mum, face((1, 1.0)))
    assert store.forget_all() == 1
    assert store.people() == [] and not store.has_samples()


def test_deleted_samples_are_overwritten_on_disk(tmp_path):
    db = Database(tmp_path / "littlebot.db")
    try:
        store = F.PeopleStore(db)
        assert db.query_one("PRAGMA secure_delete")["secure_delete"] == 1
        pid = store.add_person("Ali")["id"]
        sample = face((0, 0.3), (7, 0.5), (90, 0.81))
        store.add_sample(pid, sample)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        blob = sample.astype("<f4").tobytes()

        def on_disk() -> bytes:
            return b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("littlebot.db"))

        assert blob in on_disk()
        store.forget_all()
        assert blob not in on_disk()  # not just unlinked: gone from the file and its write-ahead log
    finally:
        db.close()


# ------------------------------------------------------------------ the privacy switch
def test_recognising_is_off_by_default(ctx):
    assert build_defaults(ctx.config)["privacy.face_recognition_enabled"] is False
    assert ctx.privacy.face_recognition_enabled is False
    assert ctx.privacy.snapshot()["face_recognition_enabled"] is False


@needs_cv2
async def test_nothing_is_looked_at_while_it_is_switched_off(setup):
    s = setup
    pid = s.store.add_person("Ali", "dad")["id"]
    s.store.add_sample(pid, face((0, 1.0)))
    s.ctx.privacy.set(face_recognition_enabled=False)
    assert s.rec.blocked_reason() == "off" and s.rec.status()["blocked"] == "off"
    for call in (s.rec.who_is_there(), s.rec.check(), s.rec.take_sample(pid)):
        with pytest.raises(F.FacesUnavailable) as e:
            await call
        assert e.value.reason == "off" and "Turn it on in Privacy" in str(e.value)
    assert s.vision.photos == 0 and s.vision.warm_asked == 0 and s.engine.calls == []


@needs_cv2
async def test_the_camera_switch_wins(setup):
    s = setup
    s.ctx.privacy.set(camera_enabled=False)
    assert s.rec.blocked_reason() == "camera_off"
    with pytest.raises(F.FacesUnavailable):
        await s.rec.check()
    assert s.vision.photos == 0


def test_no_camera(setup):
    s = setup
    s.vision.camera = "error"
    assert s.rec.blocked_reason() == "no_camera"
    s.ctx.vision = None
    assert s.rec.blocked_reason() == "no_camera"
    s.ctx.vision = SimpleNamespace(status=lambda: {"camera": "ok"})  # can't take photos
    assert s.rec.blocked_reason() == "no_camera"


# ------------------------------------------------------------------ the models
@pytest.fixture
def real(ctx, tmp_path):
    """A FaceRecognition that loads its models from ``tmp_path`` (none there yet)."""
    ctx.vision = FakeVision()
    ctx.privacy.set(face_recognition_enabled=True)
    rec = F.FaceRecognition(ctx, root=tmp_path)
    return SimpleNamespace(ctx=ctx, rec=rec, root=tmp_path)


def model_files(root) -> None:
    for name in ("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx"):
        (root / name).write_bytes(b"not really a model")


@needs_cv2
async def test_missing_models_say_how_to_get_them(real):
    rec = real.rec
    if not hasattr(get_cv2(), "FaceRecognizerSF"):
        pytest.skip("this OpenCV can't recognise faces")
    assert rec.blocked_reason() == "no_models"
    st = rec.status()
    assert not st["models_ready"] and "download_models.py faces" in st["models_detail"]
    with pytest.raises(F.FacesUnavailable) as e:
        await rec.check()
    assert e.value.reason == "no_models" and "scripts/download_models.py faces" in str(e.value)
    assert real.ctx.vision.photos == 0  # no photo for nothing


def test_without_opencv_it_says_so(real, monkeypatch):
    monkeypatch.setattr(F, "get_cv2", lambda: None)
    assert real.rec.blocked_reason() == "no_opencv"
    monkeypatch.setattr(F, "get_cv2", lambda: SimpleNamespace())  # an OpenCV without the face models
    assert real.rec.blocked_reason() == "old_opencv"
    with pytest.raises(F.FacesUnavailable):
        F.FaceEngine("a", "b")


@needs_cv2
async def test_broken_model_files_are_reported_not_crashed_on(real, caplog):
    if not hasattr(get_cv2(), "FaceRecognizerSF"):
        pytest.skip("this OpenCV can't recognise faces")
    model_files(real.root)
    rec = real.rec
    assert rec.blocked_reason() is None  # present: only loading tells
    with caplog.at_level(logging.WARNING, logger="littlebot.vision.faces"):
        with pytest.raises(F.FacesUnavailable) as e:
            await rec.check()
    assert e.value.reason == "load_failed" and "--force" in str(e.value)
    assert "couldn't be loaded" in caplog.text
    assert rec.blocked_reason() == "load_failed" and rec.status()["blocked"] == "load_failed"
    rec.unload("switched off")  # switching it off and on tries again
    assert rec.blocked_reason() is None


def test_the_models_load_once_and_go_when_switched_off(real, monkeypatch, caplog):
    model_files(real.root)
    made = []
    monkeypatch.setattr(F, "FaceEngine", lambda det, rec: made.append((det.name, rec.name)) or FakeEngine())
    monkeypatch.setattr(F, "get_cv2", lambda: SimpleNamespace(FaceDetectorYN=1, FaceRecognizerSF=1))
    rec = real.rec
    with caplog.at_level(logging.INFO, logger="littlebot.vision.faces"):
        first = rec.engine()
        assert rec.engine() is first and len(made) == 1 and rec.status()["loaded"]
        assert made == [("face_detection_yunet_2023mar.onnx", "face_recognition_sface_2021dec.onnx")]
        real.ctx.privacy.set(face_recognition_enabled=False)
    assert not rec.status()["loaded"]
    assert "models loaded in" in caplog.text and "models unloaded (switched off)" in caplog.text
    rec.engine()  # loaded while switched off (a check already under way): used once, not kept
    assert len(made) == 2 and not rec.status()["loaded"]
    real.ctx.privacy.set(face_recognition_enabled=True)
    rec.engine()
    real.ctx.privacy.set(camera_enabled=False)  # the camera switch drops them too
    assert not rec.status()["loaded"]


@needs_cv2
async def test_the_models_go_after_a_while_without_use(real, monkeypatch):
    model_files(real.root)
    monkeypatch.setattr(F, "FaceEngine", lambda det, rec: FakeEngine())
    monkeypatch.setattr(F, "UNLOAD_IDLE_S", 0.5)
    rec = real.rec
    pid = rec.store.add_person("Ali")["id"]
    rec.store.add_sample(pid, face((0, 1.0)))
    await rec.who_is_there()
    assert rec.status()["loaded"]
    for _ in range(100):
        if not rec.status()["loaded"]:
            break
        await asyncio.sleep(0.05)
    assert not rec.status()["loaded"]


def test_the_threshold_comes_from_the_config_within_sensible_limits(setup):
    s = setup
    assert s.rec.threshold == F.MATCH_THRESHOLD == s.ctx.config.vision.face_match_threshold == 0.45
    s.ctx.config.vision.face_match_threshold = 0.05  # far too loose: clamped
    assert s.rec.threshold == 0.3
    s.ctx.config.vision.face_match_threshold = 0.99  # nobody would ever match: clamped
    assert s.rec.threshold == 0.95


# ------------------------------------------------------------------ samples from the dashboard
@needs_cv2
async def test_taking_a_sample_keeps_only_its_numbers(setup, caplog):
    s = setup
    pid = s.store.add_person("Ali", "dad")["id"]
    s.engine.scripted = [seen_face(face((0, 1.0)))]
    s.vision.warm = (jpeg(), 0.1)  # a sample always takes a new photo
    with caplog.at_level(logging.DEBUG, logger="littlebot.vision.faces"):
        out = await s.rec.take_sample(pid)
    assert out["ok"] and out["samples"] == 1 and out["person"]["samples"] == 1
    assert out["preview"].startswith("data:image/jpeg;base64,")
    assert s.vision.photos == 1 and s.vision.warm_asked == 0
    assert s.engine.calls == [((480, 640, 3), F.SAMPLE_SCORE, F.SAMPLE_MIN_FACE_PX)]  # stricter for samples
    assert set(out) == {"ok", "samples", "preview", "person"}
    assert "sample kept (1 for that person)" in caplog.text
    no_numbers_logged(caplog)


@needs_cv2
@pytest.mark.parametrize("scripted,error", [
    ([], "no_face"),
    ([seen_face(face((0, 1.0)), width=60)], "too_small"),  # too far away
    ([seen_face(face((0, 1.0)), score=0.85)], "too_small"),  # not clear enough
    ([seen_face(face((0, 1.0))), seen_face(face((1, 1.0)), x=0.7)], "many_faces"),
    ([seen_face(face((0, 1.0))), seen_face(face((1, 1.0)), x=0.7, score=0.85, width=50)], "many_faces"),
])
async def test_unclear_samples_are_not_kept(setup, scripted, error):
    s = setup
    pid = s.store.add_person("Ali", "dad")["id"]
    s.engine.scripted = scripted
    out = await s.rec.take_sample(pid)
    assert not out["ok"] and out["error"] == error and out["detail"] == F.SAMPLE_ERRORS[error].format(
        name="Ali", other=None)
    assert out["preview"].startswith("data:image/jpeg;base64,")
    assert not s.store.has_samples()


@needs_cv2
async def test_a_sample_that_looks_like_someone_else_is_not_kept(setup):
    s = setup
    ali = s.store.add_person("Ali", "dad")["id"]
    mum = s.store.add_person("Mum")["id"]
    s.store.add_sample(ali, face((0, 1.0)))
    s.engine.scripted = [seen_face(face((0, 0.9), (3, 0.436)))]  # Ali stood in front of the camera
    out = await s.rec.take_sample(mum)
    assert not out["ok"] and out["error"] == "looks_like_other" and "more like Ali than Mum" in out["detail"]
    assert s.store.person(mum)["samples"] == 0
    assert (await s.rec.take_sample(ali))["ok"]  # but it's a fine sample of Ali
    s.engine.scripted = [seen_face(face((1, 1.0)))]
    assert (await s.rec.take_sample(mum))["ok"]
    with pytest.raises(KeyError):
        await s.rec.take_sample(999)


# ------------------------------------------------------------------ who is there
@needs_cv2
async def test_who_is_there_uses_the_warm_photo_and_never_guesses(setup):
    s = setup
    assert await s.rec.who_is_there() is None  # nobody enrolled: no photo at all
    assert s.vision.photos == 0
    pid = s.store.add_person("Ali", "dad")["id"]
    s.store.add_sample(pid, face((0, 1.0)))
    s.store.add_sample(pid, face((0, 1.0), (2, 0.3)))
    s.engine.scripted = [seen_face(face((0, 0.8), (6, 0.6)), x=0.1), seen_face(face((9, 1.0)), x=0.7),
                         seen_face(face((0, 1.0)), score=0.65, width=30)]
    s.vision.warm = (jpeg(), 0.2)
    sighting = await s.rec.who_is_there()
    assert s.vision.photos == 0 and s.vision.warm_asked == 1  # the call's warm photo: no extra photo
    assert [p.name for p in sighting.people()] == ["Ali"] and sighting.unknown == 1  # the tiny face isn't compared
    s.vision.warm = None
    await s.rec.who_is_there()
    assert s.vision.photos == 1


@needs_cv2
@pytest.mark.parametrize("error,reason", [
    (PhotoUnavailable("camera_off"), "camera_off"), (PhotoUnavailable("timeout"), "photo_failed"),
    (RuntimeError("broken"), "photo_failed"),
])
async def test_a_failed_photo_is_reported(setup, error, reason):
    s = setup
    s.vision.error = error
    with pytest.raises(F.FacesUnavailable) as e:
        await s.rec.check()
    assert e.value.reason == reason


@needs_cv2
async def test_the_dashboard_check_shows_names_scores_and_a_preview(setup, caplog):
    s = setup
    out = await s.rec.check()
    assert out["ok"] and out["nobody_enrolled"] and out["faces"] == []
    pid = s.store.add_person("Ali", "dad")["id"]
    s.store.add_sample(pid, face((0, 1.0)))
    s.engine.scripted = [seen_face(face((0, 0.8), (6, 0.6))), seen_face(face((9, 1.0)), x=0.7)]
    with caplog.at_level(logging.INFO, logger="littlebot.vision.faces"):
        out = await s.rec.check()
    assert out["faces"] == [{"n": 1, "name": "Ali", "role": "dad", "score": 0.8},
                            {"n": 2, "name": None, "role": None, "score": 0.0}]
    assert out["threshold"] == 0.45 and not out["nobody_enrolled"] and out["preview"].startswith("data:image/jpeg")
    assert s.vision.photos == 2  # always a new photo
    assert "checked from the dashboard (2 face(s), 1 known)" in caplog.text
    no_numbers_logged(caplog)


def test_the_childs_name_is_hidden_when_it_isnt_shared(ctx):
    ctx.settings.set("profile", {"nickname": "Amara"})
    assert F.hidden_child_name(ctx) is None
    ctx.settings.set("ai.share_child_name", False)
    assert F.hidden_child_name(ctx) == "Amara"


def test_get_faces_is_shared(ctx):
    assert F.get_faces(ctx) is F.get_faces(ctx)


# ------------------------------------------------------------------ steadying who is in view
def test_someone_arrives_after_two_sightings_close_together():
    ali = known(1, "Ali", "dad", (0, 1.0))
    p = F.Presence()
    assert not p.update([ali], 0.0) and p.here == {}
    assert p.update([ali], 3.0) and list(p.here) == [1]
    p2 = F.Presence()
    p2.update([ali], 0.0)
    assert not p2.update([ali], 11.0)  # too far apart
    assert p2.update([ali], 14.0)
    p3 = F.Presence()
    assert p3.update([ali], 0.0, at_once=True) and list(p3.here) == [1]  # the first check of a call


def test_someone_leaves_a_while_after_they_were_last_seen():
    ali, mum = known(1, "Ali", "dad", (0, 1.0)), known(2, "Mum", "", (1, 1.0))
    p = F.Presence()
    p.update([ali, mum], 0.0, at_once=True)
    assert not p.update([mum], 3.0) and set(p.here) == {1, 2}  # turned away for a moment
    assert not p.update([mum], 11.0)
    assert p.update([mum], 12.0) and list(p.here) == [2]
    renamed = F.Known(2, "Mummy", "", mum.centre)
    assert not p.update([renamed], 13.0) and p.here[2].name == "Mummy"
    p.reset()
    assert p.here == {}


# ------------------------------------------------------------------ during a live call
class StubFaces:
    """Stands in for FaceRecognition in a call: ``next`` is what the next check sees."""

    def __init__(self) -> None:
        self.reason: str | None = None
        self.samples = True
        self.next: Any = F.Sighting(0.0)
        self.checks = 0
        self.store = SimpleNamespace(has_samples=lambda: self.samples)

    def blocked_reason(self) -> str | None:
        return self.reason

    async def who_is_there(self) -> F.Sighting | None:
        self.checks += 1
        if isinstance(self.next, Exception):
            raise self.next
        return self.next


ALI = known(1, "Ali", "dad", (0, 1.0))
MUM = known(2, "Mum", "", (1, 1.0))
AMARA = known(3, "Amara", "the baby", (2, 1.0))


def sighting(*people: F.Known | None) -> F.Sighting:
    boxes = [(0.05, 0.3, 0.2, 0.3), (0.7, 0.3, 0.2, 0.3), (0.4, 0.3, 0.2, 0.3)]
    return F.Sighting(0.0, tuple(F.Seen(p, 0.8 if p else 0.1, boxes[i]) for i, p in enumerate(people)))


@pytest.fixture
def call():
    clock = [100.0]
    told: list[str] = []
    accept = [True]
    stub = StubFaces()

    def tell(text: str) -> bool:
        if accept[0]:
            told.append(text)
        return accept[0]

    cf = F.CallFaces(stub, tell=tell, now=lambda: clock[0])

    async def check_at(t: float, seen: Any) -> None:
        clock[0] = t
        stub.next = seen
        await cf._check()
        cf._maybe_tell(t)

    return SimpleNamespace(cf=cf, stub=stub, told=told, accept=accept, clock=clock, check_at=check_at)


async def test_the_call_starts_knowing_who_is_there(call):
    cf = call.cf
    assert cf.intro() == F.FACES_INTRO  # no check yet: the names follow in a note
    await cf._check()
    call.stub.next = sighting(ALI, None)
    cf2 = F.CallFaces(call.stub, tell=lambda t: True)
    await cf2._check()
    text = cf2.intro()
    assert text.startswith(F.FACES_INTRO) and text.endswith(" In front of your camera now: Ali (dad).")
    assert "never guess" in text
    cf2._maybe_tell(0.0)
    assert cf2.told == frozenset({1})  # already in the instructions: no note about it
    call.stub.next = sighting(None)
    cf3 = F.CallFaces(call.stub, tell=lambda t: True)
    await cf3._check()
    assert cf3.intro().endswith("Nobody LittleBot knows is in front of the camera right now.")


def test_no_intro_when_switched_off_or_nobody_enrolled(call):
    call.stub.reason = "off"
    assert call.cf.intro() == ""
    call.stub.reason, call.stub.samples = None, False
    assert call.cf.intro() == ""
    call.stub.reason = "no_models"
    call.stub.samples = True
    assert call.cf.intro() == ""


async def test_the_voice_is_told_when_someone_arrives_once_per_call(call, caplog):
    c = call
    with caplog.at_level(logging.INFO, logger="littlebot.vision.faces"):
        await c.check_at(100.0, sighting(ALI))
    assert c.told == [f"Now in front of your camera: Ali (dad). {F.QUIET}"]
    assert "face recognition is on (1 face(s) in view, 1 known)" in caplog.text
    assert "told who is in view (1 known)" in caplog.text
    await c.check_at(103.0, sighting(ALI))
    await c.check_at(106.0, sighting(ALI, MUM))  # Mum's first sighting: not yet
    assert len(c.told) == 1
    await c.check_at(109.0, sighting(ALI, MUM))  # arrived, but the last note was only 9 s ago
    assert len(c.told) == 1
    await c.check_at(115.0, sighting(ALI, MUM))
    assert c.told[-1] == f"Now in front of your camera: Ali (dad) and Mum. {F.QUIET}"
    for t in (118.0, 121.0, 124.0, 140.0):
        await c.check_at(t, sighting(ALI, MUM))
    assert len(c.told) == 2  # nothing new: nothing said
    for t in range(143, 200, 3):  # recognition comes and goes (a face turned aside, poor light)
        await c.check_at(float(t), sighting(ALI) if t % 2 else sighting())
    assert len(c.told) == 2  # they were told about already in this call: never again


async def test_leaving_is_never_a_note(call):
    c = call
    await c.check_at(100.0, sighting(ALI))
    for t in (103.0, 106.0, 109.0):
        await c.check_at(t, sighting(None))  # someone LittleBot doesn't know
    for t in (112.0, 115.0, 130.0, 160.0):
        await c.check_at(t, sighting())
    assert c.cf.presence.here == {}  # gone
    assert c.told == [f"Now in front of your camera: Ali (dad). {F.QUIET}"]  # and the voice wasn't made to say so


async def test_unknown_faces_are_never_named(call):
    c = call
    await c.check_at(100.0, sighting(None, None))
    assert c.told == []  # nobody known, and the voice wasn't told anybody was
    assert "doesn't know the people in front of the camera: never guess" in c.cf.for_look()


async def test_a_note_that_cant_go_now_goes_at_the_next_quiet_moment(call):
    c = call
    c.accept[0] = False  # someone is talking
    await c.check_at(100.0, sighting(ALI))
    assert c.cf.told == frozenset()
    c.accept[0] = True
    c.cf._maybe_tell(100.5)
    assert c.told == [f"Now in front of your camera: Ali (dad). {F.QUIET}"]


async def test_switching_it_off_mid_call_tells_the_voice_to_forget(call, caplog):
    c = call
    await c.check_at(100.0, sighting(ALI))
    c.stub.reason = "off"
    with caplog.at_level(logging.INFO, logger="littlebot.vision.faces"):
        c.cf._blocked("off", 101.0)
    assert c.told[-1].startswith("LittleBot has stopped recognising faces") and c.told[-1].endswith(F.QUIET)
    assert c.cf.latest is None and c.cf.presence.here == {} and c.cf.for_look() is None
    c.cf._maybe_tell(130.0)
    c.cf._blocked("off", 131.0)
    assert len(c.told) == 2  # said once
    assert "can't run" not in caplog.text  # switched off isn't a problem to log


async def test_no_camera_for_a_moment_lets_people_leave_in_their_own_time(call, caplog):
    c = call
    await c.check_at(100.0, sighting(ALI))
    with caplog.at_level(logging.INFO, logger="littlebot.vision.faces"):
        c.cf._blocked("no_camera", 101.0)
        c.cf._blocked("no_camera", 104.0)
    assert list(c.cf.presence.here) == [1] and len(c.told) == 1
    assert caplog.text.count("face recognition can't run (no_camera)") == 1
    c.cf._blocked("no_camera", 113.0)
    c.cf._maybe_tell(116.0)
    assert c.cf.presence.here == {} and len(c.told) == 1  # gone after 12 s; leaving is never a note


async def test_a_failing_check_never_breaks_the_call(call, caplog):
    c = call
    with caplog.at_level(logging.INFO, logger="littlebot.vision.faces"):
        await c.check_at(100.0, F.FacesUnavailable("no_models"))
        await c.check_at(103.0, F.FacesUnavailable("no_models"))
        await c.check_at(106.0, RuntimeError("boom"))
    assert c.told == [] and caplog.text.count("can't run (no_models)") == 1
    await c.check_at(109.0, sighting(ALI))
    assert c.told == [f"Now in front of your camera: Ali (dad). {F.QUIET}"]  # the first good check counts at once


async def test_the_childs_name_stays_on_the_robot_when_it_isnt_shared(call):
    cf = F.CallFaces(call.stub, tell=lambda t: call.told.append(t) or True, hide="Amara", now=lambda: 0.0)
    call.stub.next = sighting(AMARA, ALI)
    await cf._check()
    cf._maybe_tell(0.0)
    assert call.told == [f"Now in front of your camera: the little one (the baby) and Ali (dad). {F.QUIET}"]
    assert "Amara" not in cf.intro() and "Amara" not in (cf.for_look() or "")


async def test_a_look_is_told_who_was_just_seen(call):
    c = call
    assert c.cf.for_look() is None  # nothing seen yet
    await c.check_at(100.0, sighting(ALI, None))
    text = c.cf.for_look()
    assert "Ali (dad) on the left of the photo and someone LittleBot doesn't know" in text
    assert "never guess a name for anyone else" in text
    await c.check_at(103.0, sighting(ALI))
    assert "a moment ago: Ali (dad)." in c.cf.for_look()  # one face: no "where"
    c.clock[0] = 103.0 + F.LOOK_FRESH_S + 1
    assert c.cf.for_look() is None  # too old to be sure
    assert F.look_context(F.Sighting(0.0)) is None


async def test_the_call_loop_checks_tells_and_stops(monkeypatch):
    monkeypatch.setattr(F, "CHECK_EVERY_S", 0.02)
    monkeypatch.setattr(F, "TICK_S", 0.01)
    stub = StubFaces()
    stub.next = sighting(ALI)
    told: list[str] = []
    cf = F.CallFaces(stub, tell=lambda t: told.append(t) or True)
    task = asyncio.create_task(cf.run())
    for _ in range(200):
        if told and stub.checks >= 3:
            break
        await asyncio.sleep(0.01)
    assert told == [f"Now in front of your camera: Ali (dad). {F.QUIET}"] and stub.checks >= 3
    stub.reason = "off"
    await asyncio.sleep(0.05)
    checks = stub.checks
    assert told[-1].startswith("LittleBot has stopped recognising faces")
    await asyncio.sleep(0.05)
    assert stub.checks == checks  # no checks while switched off
    cf.close()
    await asyncio.wait_for(task, 1.0)
    assert cf.latest is None


# ------------------------------------------------------------------ the real models, when downloaded
def test_the_real_models_load_when_they_are_downloaded(ctx):
    cv2 = get_cv2()
    if cv2 is None or not hasattr(cv2, "FaceRecognizerSF"):
        pytest.skip("this OpenCV can't recognise faces")
    rec = F.FaceRecognition(ctx)
    det, sface = rec.model_paths()
    if not (det.is_file() and sface.is_file()):
        pytest.skip("face models not downloaded (scripts/download_models.py faces)")
    engine = F.FaceEngine(det, sface)
    assert engine.look(np.full((480, 640, 3), 90, np.uint8)) == []  # a blank photo has nobody in it


def _who(*people: Any) -> F.Sighting:
    def known(pid: int, name: str, role: str) -> F.Known:
        centre = np.zeros(F.FEATURES, np.float32)
        centre[pid] = 1.0
        return F.Known(pid, name, role, centre)

    seen = [F.Seen(known(*p) if p else None, 0.8 if p else 0.1, (0.1, 0.1, 0.2, 0.2)) for p in people]
    return F.Sighting(0.0, tuple(seen))


def test_who_answer_names_the_grown_up_and_never_takes_the_baby_for_the_one_asking():
    assert "the grown-up talking to you is Ali (dad)" in F.who_answer(_who((1, "Ali", "dad")))
    baby = F.who_answer(_who((2, "Amara", "the baby")), child="Amara")
    assert "a grown-up who isn't in view" in baby and "Never call them by the little one's name" in baby
    assert "isn't in view" in F.who_answer(_who((2, "Amara", "")), child="amara")  # by name, whatever the role
    both = F.who_answer(_who((1, "Ali", "dad"), (2, "Amara", "the baby")), child="Amara")
    assert "Ali (dad) and Amara (the baby)" in both and "the grown-up talking to you is Ali (dad)" in both
    two = F.who_answer(_who((1, "Ali", "dad"), (3, "Sara", "mum")))
    assert "if you can't tell which of them is talking, say so" in two
    assert "1 person it doesn't know" in F.who_answer(_who((1, "Ali", "dad"), None))
    assert "doesn't know the person" in F.who_answer(_who(None))
    assert "Nobody is in front of your camera" in F.who_answer(_who())
    assert "Nobody has been added" in F.who_answer(None)
    hidden = F.who_answer(_who((2, "Amara", "the baby")), hide="Amara", child="Amara")
    assert "Amara" not in hidden and "the little one" in hidden


async def test_the_one_grown_up_in_view(call):
    c = call
    c.cf.child = "Amara"
    await c.check_at(100.0, sighting(AMARA, ALI))
    assert c.cf.grown_up() == "Ali (dad)"  # the baby in view isn't the one talking
    await c.check_at(103.0, sighting(AMARA, ALI, MUM))
    await c.check_at(106.0, sighting(AMARA, ALI, MUM))
    assert c.cf.grown_up() is None  # two grown-ups: can't tell which one is talking


async def test_the_decision_log_summary_says_who_is_in_view(call):
    cf = F.CallFaces(call.stub, tell=lambda t: True, hide="Amara", now=lambda: 0.0)
    assert cf.summary() == "nobody it knows"
    call.stub.next = sighting(AMARA, ALI, None)
    await cf._check()
    assert cf.summary() == "the little one (the baby) and Ali (dad), and 1 face(s) it doesn't know"
    call.stub.reason = "off"
    assert cf.summary() == "off"

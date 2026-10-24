"""Dashboard: Who's who (/api/people), with fake face models. Only names and counts reach the browser."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from dashboard_fakes import FakeVision, make_client

from littlebot.vision import faces as F
from littlebot.vision.imaging import get_cv2

needs_cv2 = pytest.mark.skipif(get_cv2() is None, reason="OpenCV needed to decode the photo")


def face(axis: int) -> np.ndarray:
    v = np.zeros(F.FEATURES, np.float32)
    v[axis] = 1.0
    return v


class FakeEngine:
    """Stands in for the face models: ``faces`` is what the next photo shows (128 made-up numbers each)."""

    def __init__(self) -> None:
        self.faces: list[np.ndarray] = []

    def look(self, image: np.ndarray, *, min_score: float = 0.8, min_px: int = 48) -> list[F.SeenFace]:
        return [F.SeenFace((0.1 + 0.4 * i, 0.2, 0.25, 0.35), 0.95, 150, f) for i, f in enumerate(self.faces)]


class PhotoVision(FakeVision):
    def __init__(self) -> None:
        super().__init__()
        self.photos = 0

    async def capture_photo(self, *, max_width: int = 640, quality: int = 85, timeout: float = 3.0) -> bytes:
        self.photos += 1
        ok, buf = get_cv2().imencode(".jpg", np.full((480, 640, 3), 100, np.uint8))
        return buf.tobytes()


@pytest.fixture
def engine(dctx):
    dctx.vision = PhotoVision()
    eng = FakeEngine()
    dctx.extras["faces"] = F.FaceRecognition(dctx, engine=eng)
    return eng


def no_numbers(data: Any) -> None:
    """No face's numbers anywhere in a response."""
    if isinstance(data, dict):
        assert not {"features", "centre", "embedding", "samples_data"} & set(data), data.keys()
        for v in data.values():
            no_numbers(v)
    elif isinstance(data, list):
        assert len(data) < 100
        for v in data:
            no_numbers(v)


async def test_who_is_who_needs_a_login(client, engine):
    assert (await client.get("/api/people")).status_code == 401
    r = await client.post("/api/people", json={"name": "Ali"})
    assert r.status_code in (401, 403)
    assert (await client.post("/api/people/forget-all", json={})).status_code in (401, 403)


async def test_the_list_starts_empty_and_switched_off(authed, engine):
    d = (await authed.get("/api/people")).json()
    assert d["people"] == [] and d["enabled"] is False and d["blocked"] == "off"
    assert "Turn it on in Privacy" in d["blocked_detail"]
    assert d["max_people"] == F.MAX_PEOPLE and d["suggested_samples"] == 5 and d["retake_days"] == 30
    assert d["threshold"] == 0.45
    assert (await authed.get("/api/privacy")).json()["face_recognition_enabled"] is False


async def test_adding_renaming_and_deleting_people(authed, engine, dctx):
    r = await authed.post("/api/people", json={"name": " Ali ", "role": "dad"})
    assert r.status_code == 200, r.text
    ali = r.json()["person"]
    assert ali["name"] == "Ali" and ali["role"] == "dad" and ali["samples"] == 0
    assert [p["name"] for p in r.json()["people"]] == ["Ali"]
    r = await authed.put(f"/api/people/{ali['id']}", json={"name": "Alistair"})
    assert r.status_code == 200 and r.json()["person"]["name"] == "Alistair" and r.json()["person"]["role"] == "dad"
    assert (await authed.put("/api/people/999", json={"name": "Bob"})).status_code == 404
    assert (await authed.delete(f"/api/people/{ali['id']}")).json()["people"] == []
    assert (await authed.delete(f"/api/people/{ali['id']}")).status_code == 404
    assert (await authed.delete("/api/people/0")).status_code == 422


@pytest.mark.parametrize("body", [
    {"name": ""}, {"name": "<b>Ali</b>"}, {"name": "x" * 31}, {"name": "Ali", "role": "dad!"}, {"role": "dad"},
    {"name": "Ali", "photo": "data:image/jpeg;base64,AAAA"}, {"name": "Ali", "features": [0.1] * 128},
])
async def test_odd_people_are_refused(authed, engine, body):
    r = await authed.post("/api/people", json=body)
    assert r.status_code == 422, r.text
    assert (await authed.get("/api/people")).json()["people"] == []


async def test_the_same_person_twice_is_refused(authed, engine):
    await authed.post("/api/people", json={"name": "Ali", "role": "dad"})
    r = await authed.post("/api/people", json={"name": "ali", "role": "Dad"})
    assert r.status_code == 422 and "already on the list" in r.json()["detail"]


@needs_cv2
async def test_samples_need_the_privacy_switch(authed, engine, dctx):
    pid = (await authed.post("/api/people", json={"name": "Ali", "role": "dad"})).json()["person"]["id"]
    engine.faces = [face(0)]
    r = await authed.post(f"/api/people/{pid}/samples", json={})
    assert r.status_code == 409 and "switched off" in r.json()["detail"]
    assert dctx.vision.photos == 0
    assert (await authed.put("/api/privacy", json={"face_recognition_enabled": True})).status_code == 200
    assert dctx.privacy.face_recognition_enabled is True
    r = await authed.post(f"/api/people/{pid}/samples", json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True and d["samples"] == 1 and d["person"]["samples"] == 1
    assert d["preview"].startswith("data:image/jpeg;base64,") and d["people"][0]["samples"] == 1
    assert d["blocked"] is None and d["enabled"] is True
    no_numbers(d)
    assert dctx.vision.photos == 1
    assert (await authed.post("/api/people/999/samples", json={})).status_code == 404
    await authed.put("/api/privacy", json={"camera_enabled": False})
    r = await authed.post(f"/api/people/{pid}/samples", json={})
    assert r.status_code == 409 and "camera is turned off" in r.json()["detail"]


@needs_cv2
async def test_an_unclear_sample_says_why(authed, engine, dctx):
    dctx.privacy.set(face_recognition_enabled=True)
    pid = (await authed.post("/api/people", json={"name": "Ali"})).json()["person"]["id"]
    engine.faces = [face(0), face(1)]
    d = (await authed.post(f"/api/people/{pid}/samples", json={})).json()
    assert d["ok"] is False and d["error"] == "many_faces" and "only Ali" in d["detail"]
    assert d["people"][0]["samples"] == 0 and d["preview"].startswith("data:image/jpeg")


@needs_cv2
async def test_checking_who_is_in_view_and_forgetting(authed, engine, dctx):
    dctx.privacy.set(face_recognition_enabled=True)
    ali = (await authed.post("/api/people", json={"name": "Ali", "role": "dad"})).json()["person"]["id"]
    mum = (await authed.post("/api/people", json={"name": "Mum"})).json()["person"]["id"]
    engine.faces = [face(0)]
    await authed.post(f"/api/people/{ali}/samples", json={})
    engine.faces = [face(1)]
    await authed.post(f"/api/people/{mum}/samples", json={})
    engine.faces = [face(0), face(7)]
    r = await authed.post("/api/people/check", json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert [(f["name"], f["role"]) for f in d["faces"]] == [("Ali", "dad"), (None, None)]
    assert d["preview"].startswith("data:image/jpeg") and d["threshold"] == 0.45
    no_numbers(d)
    d = (await authed.delete(f"/api/people/{mum}/samples")).json()
    assert [p["samples"] for p in d["people"]] == [1, 0]
    assert (await authed.delete("/api/people/999/samples")).status_code == 404
    d = (await authed.post("/api/people/forget-all", json={})).json()
    assert d["forgotten"] == 2 and d["people"] == []
    assert not dctx.extras["faces"].store.has_samples()


async def test_missing_models_say_how_to_get_them(authed, dctx, tmp_path):
    if not hasattr(get_cv2(), "FaceRecognizerSF"):
        pytest.skip("this OpenCV can't recognise faces")
    dctx.vision = PhotoVision()
    dctx.extras["faces"] = F.FaceRecognition(dctx, root=tmp_path)  # no models in there
    dctx.privacy.set(face_recognition_enabled=True)
    d = (await authed.get("/api/people")).json()
    assert d["models_ready"] is False and "download_models.py faces" in d["models_detail"]
    assert d["blocked"] == "no_models"
    pid = (await authed.post("/api/people", json={"name": "Ali"})).json()["person"]["id"]  # people can be added
    r = await authed.post(f"/api/people/{pid}/samples", json={})
    assert r.status_code == 503 and "aren't downloaded" in r.json()["detail"]
    assert (await authed.post("/api/people/check", json={})).status_code == 503
    assert dctx.vision.photos == 0


async def test_the_default_recogniser_is_used_without_any_setup(app, dctx):
    async with make_client(app) as c:
        from dashboard_fakes import do_setup  # noqa: PLC0415

        await do_setup(c, app)
        d = (await c.get("/api/people")).json()
    assert d["blocked"] == "off" and d["people"] == [] and "faces" in dctx.extras

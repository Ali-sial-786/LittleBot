"""Who's who: the family members LittleBot may recognise on its camera (opt-in; littlebot/vision/faces.py)
and by their voice (opt-in; littlebot/voices/).

Only names, roles and sample counts ever reach the browser, never a sample's numbers. Taking a sample
or checking who is in view sends back one small preview photo for the parent to see; it is not kept.
"Record voice" records about 20 s with LittleBot's microphone, in memory: only voiceprints are kept.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import Field

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body, PersonBody, PersonUpdateBody
from littlebot.vision.faces import (
    ERRORS, MAX_PEOPLE, MAX_SAMPLES, RETAKE_DAYS, SUGGESTED_SAMPLES, FaceRecognition, FacesUnavailable, get_faces,
)

router = APIRouter(prefix="/api/people", tags=["people"])

# Switched off (the parent decides): 409; nothing to recognise with (camera, models): 503.
_OFF = {"off", "camera_off", "microphone_off"}


class VoiceRecordBody(Body):
    seconds: float = Field(default=20.0, ge=5.0, le=30.0)


def _faces(dash: DashState) -> FaceRecognition:
    return get_faces(dash.ctx)


def _voices(dash: DashState) -> Any:
    from littlebot.voices import get_voices  # noqa: PLC0415

    return get_voices(dash.ctx)


def _voice_view(dash: DashState) -> dict[str, Any] | None:
    try:
        return _voices(dash).view()
    except Exception:  # noqa: BLE001 - Who's who still works without voices
        log.warning("who's who: voice recognition status unavailable", exc_info=True)
        return None


def _view(dash: DashState) -> dict[str, Any]:
    faces = _faces(dash)
    people = faces.store.people()
    voices = _voice_view(dash)
    counts = (voices or {}).get("counts") or {}
    for p in people:  # how many voiceprints each has (never the numbers)
        c = counts.get(str(p["id"])) or {}
        p["voice_samples"], p["voice_learned"] = int(c.get("samples", 0)), int(c.get("learned", 0))
    return {**faces.status(), "people": people, "max_people": MAX_PEOPLE, "max_samples": MAX_SAMPLES,
            "suggested_samples": SUGGESTED_SAMPLES, "retake_days": RETAKE_DAYS, "voices": voices}


def _refuse(reason: str) -> HTTPException:
    return HTTPException(status_code=409 if reason in _OFF else 503, detail=ERRORS.get(reason, ERRORS["photo_failed"]))


@router.get("")
async def people(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    return _view(dash)


@router.post("")
async def add_person(body: PersonBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    person = _faces(dash).store.add_person(body.name, body.role)  # ValueError (full, duplicate) -> 422
    log.info("who's who: a person was added from the dashboard")
    return {"ok": True, "person": person, **_view(dash)}


@router.put("/{person_id}")
async def update_person(body: PersonUpdateBody, person_id: int = Path(ge=1),
                        dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    changes = body.model_dump(exclude_unset=True)
    person = _faces(dash).store.update_person(person_id, name=changes.get("name"), role=changes.get("role"))
    return {"ok": True, "person": person, **_view(dash)}


@router.delete("/{person_id}")
async def delete_person(person_id: int = Path(ge=1), dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    if not _faces(dash).store.delete_person(person_id):
        raise HTTPException(status_code=404, detail="That person isn't on the list.")
    log.info("who's who: a person and their face samples and voiceprints were deleted from the dashboard")
    return {"ok": True, **_view(dash)}


@router.post("/{person_id}/samples")
async def take_sample(person_id: int = Path(ge=1), dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """One new photo now: keep its face numbers for this person (never the photo) if it is clear."""
    try:
        result = await _faces(dash).take_sample(person_id)  # KeyError -> 404
    except FacesUnavailable as e:
        raise _refuse(e.reason) from None
    return {**result, **_view(dash), "ok": result["ok"]}


@router.delete("/{person_id}/samples")
async def clear_samples(person_id: int = Path(ge=1), dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    faces = _faces(dash)
    if faces.store.person(person_id) is None:
        raise HTTPException(status_code=404, detail="That person isn't on the list.")
    faces.store.clear_samples(person_id)
    log.info("who's who: a person's face samples were deleted from the dashboard")
    return {"ok": True, **_view(dash)}


@router.post("/check")
async def check(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Who LittleBot recognises in front of its camera right now (one photo, shown, not kept)."""
    try:
        result = await _faces(dash).check()
    except FacesUnavailable as e:
        raise _refuse(e.reason) from None
    return {**result, **_view(dash)}


@router.post("/forget-all")
async def forget_all(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    gone = _faces(dash).store.forget_all()
    log.info("who's who: every face and voice was forgotten from the dashboard (%d people)", gone)
    return {"ok": True, "forgotten": gone, **_view(dash)}


# ------------------------------------------------------------------ voices (littlebot/voices/)
def _voice_refuse(reason: str) -> HTTPException:
    from littlebot.voices.service import ERRORS as VOICE_ERRORS  # noqa: PLC0415

    return HTTPException(status_code=409 if reason in _OFF else 503,
                         detail=VOICE_ERRORS.get(reason, "Voice recognition isn't available right now."))


@router.post("/{person_id}/voice")
async def record_voice(body: VoiceRecordBody | None = None, person_id: int = Path(ge=1),
                       dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """Record the person reading the three sentences (LittleBot's microphone, in memory): keep their voiceprints."""
    from littlebot.voices.engine import EngineUnavailable  # noqa: PLC0415

    voices = _voices(dash)
    if voices.store.people.person(person_id) is None:
        raise HTTPException(status_code=404, detail="That person isn't on the list.")
    reason = voices.blocked_reason()
    if reason is not None:
        raise _voice_refuse(reason)
    record = getattr(dash.need("audio"), "record_clip", None)
    if record is None:
        raise HTTPException(status_code=503, detail="This robot's audio can't record a voice.")
    got = await record(body.seconds if body is not None else 20.0)
    if not got.get("ok"):
        return {"ok": False, "error": "recording", "detail": got.get("detail") or "The recording didn't work.",
                **_view(dash)}
    samples, rate = got.pop("samples"), int(got.get("rate") or 0)
    try:
        result = await voices.enrol(person_id, samples, rate)
    except KeyError:  # deleted while recording
        raise HTTPException(status_code=404, detail="That person isn't on the list.") from None
    except EngineUnavailable as e:
        raise _voice_refuse(e.reason) from None
    finally:
        del samples  # the recording is dropped here: only the voiceprints are kept
    log.info("who's who: a voice was recorded from the dashboard (%s)", "kept" if result.get("ok") else "not kept")
    return {**result, **_view(dash), "ok": result["ok"]}


@router.delete("/{person_id}/voice")
async def forget_voice(person_id: int = Path(ge=1), dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    voices = _voices(dash)
    if voices.store.people.person(person_id) is None:
        raise HTTPException(status_code=404, detail="That person isn't on the list.")
    voices.forget(person_id)
    log.info("who's who: a person's voiceprints were deleted from the dashboard")
    return {"ok": True, **_view(dash)}

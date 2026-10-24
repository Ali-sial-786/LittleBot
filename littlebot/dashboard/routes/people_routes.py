"""Who's who: the family members LittleBot may recognise on its camera (opt-in; littlebot/vision/faces.py).

Only names, roles and sample counts ever reach the browser, never a sample's numbers. Taking a sample
or checking who is in view sends back one small preview photo for the parent to see; it is not kept.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import PersonBody, PersonUpdateBody
from littlebot.vision.faces import (
    ERRORS, MAX_PEOPLE, MAX_SAMPLES, RETAKE_DAYS, SUGGESTED_SAMPLES, FaceRecognition, FacesUnavailable, get_faces,
)

router = APIRouter(prefix="/api/people", tags=["people"])

# Switched off (the parent decides): 409; nothing to recognise with (camera, models): 503.
_OFF = {"off", "camera_off"}


def _faces(dash: DashState) -> FaceRecognition:
    return get_faces(dash.ctx)


def _view(dash: DashState) -> dict[str, Any]:
    faces = _faces(dash)
    return {**faces.status(), "people": faces.store.people(), "max_people": MAX_PEOPLE, "max_samples": MAX_SAMPLES,
            "suggested_samples": SUGGESTED_SAMPLES, "retake_days": RETAKE_DAYS}


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
    log.info("who's who: a person and their face samples were deleted from the dashboard")
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
    log.info("who's who: every face was forgotten from the dashboard (%d people)", gone)
    return {"ok": True, "forgotten": gone, **_view(dash)}

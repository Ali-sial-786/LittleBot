"""Weather & time (Settings page): where home is for the weather, the units, and the weather and time now.

Home is the parent's choice here (a place name, found with Open-Meteo's place search, or "lat, lon"),
else ``info.location`` in the config, else approximately where the robot's internet connection is
(littlebot/info/weather.py). The page always says which of these it is.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, field_validator

from littlebot.dashboard.deps import DashState, get_dash, log
from littlebot.dashboard.models import Body, clean_text
from littlebot.dashboard.scrub import scrub
from littlebot.info.clock import now_info
from littlebot.info.weather import WeatherError, get_weather, summary_text

router = APIRouter(prefix="/api/info", tags=["info"])

_STATUS = {"not_found": 404, "bad_place": 422, "offline": 403, "cloud_off": 403, "no_location": 409,
           "timeout": 504, "network": 502, "bad_response": 502, "limited": 503}
_DETAIL = {
    "offline": "LittleBot is in offline mode, so it can't look up places or the weather.",
    "cloud_off": "Turn on cloud communication in Privacy first, so LittleBot can find the place (or type its "
                 "latitude and longitude, for example 43.65, -79.38).",
    "not_found": "Couldn't find “{place}”. Try a nearby city, or the city and its country (for example "
                 "“Springfield, Illinois”).",
    "no_location": "LittleBot doesn't know where home is yet. Type a place above.",
    "timeout": "The weather service didn't answer in time. Check the robot's internet, then try again.",
    "network": "The weather service couldn't be reached. Check the robot's internet, then try again.",
    "bad_response": "The weather service gave an answer LittleBot couldn't read. Try again later.",
    "limited": "The weather service is busy (too many requests). Try again later.",
    "bad_place": "Type a place, or leave it empty to use your internet connection.",
}


class InfoBody(Body):
    location: str | None = Field(default=None, max_length=120)  # "" = automatic (config, else internet)
    units: Literal["auto", "celsius", "fahrenheit"] | None = None

    @field_validator("location")
    @classmethod
    def _clean(cls, v: str | None) -> str | None:
        return None if v is None else clean_text(v, max_len=120, field="location")


def _fail(e: WeatherError) -> HTTPException:
    detail = _DETAIL.get(e.kind, str(e)).replace("{place}", (e.place or "that place")[:60])
    return HTTPException(status_code=_STATUS.get(e.kind, 502), detail=detail)


def _time(dash: DashState) -> dict[str, Any]:
    tz = dash.ctx.config.info.timezone or None
    try:
        return {**now_info(tz), "configured_timezone": tz}
    except ValueError:
        return {**now_info(None), "configured_timezone": tz, "timezone_problem": f"“{tz}” isn't a known time zone"}


def _view(dash: DashState, weather: dict[str, Any]) -> dict[str, Any]:
    cfg = dash.ctx.config.chatgpt
    return scrub({"weather": weather, "time": _time(dash), "web_search": bool(cfg.web_search),
                  "tools_enabled": bool(cfg.tools_enabled)})


@router.get("")
async def info(dash: DashState = Depends(get_dash), lookup: bool = Query(False)) -> dict[str, Any]:
    """``lookup``: find home first if it isn't known yet (at most a few seconds); else only what is known."""
    svc = get_weather(dash.ctx)
    return _view(dash, await svc.lookup_status() if lookup else svc.status())


@router.put("")
async def set_info(body: InfoBody, dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    svc = get_weather(dash.ctx)
    if body.units is not None:
        svc.set_units(body.units)
    if body.location is not None:
        if body.location:
            try:
                await svc.set_home(body.location)
            except WeatherError as e:
                raise _fail(e) from None
        else:
            svc.clear_home()
            log.info("weather: home set back to automatic from the dashboard")
    return {"ok": True, **_view(dash, await svc.lookup_status())}


@router.get("/weather")
async def weather_now(dash: DashState = Depends(get_dash)) -> dict[str, Any]:
    """The weather at home now, as ChatGPT gets it (and one line for the parent)."""
    try:
        report = await get_weather(dash.ctx).report(days=2)
    except WeatherError as e:
        raise _fail(e) from None
    return scrub({"ok": True, "summary": summary_text(report), "report": report})

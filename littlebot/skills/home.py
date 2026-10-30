"""SmartHome: lights, plugs, fans, the thermostat, blinds, locks, TVs, scenes and scripts through Home Assistant.

Home Assistant's REST API (https://developers.home-assistant.io/docs/api/rest/) at ``skills.home_assistant_url``
with a long-lived access token (``HOME_ASSISTANT_TOKEN`` in .env, or data/home_assistant_token, chmod 600; read
with ``config.secret("home_assistant_token")``, never logged, never in a result):

    GET  /api/states                          every entity's state (kept 30 s)
    POST /api/services/<domain>/<service>     {"entity_id": ..., ...} does something

``home_devices(area_or_kind)`` lists the devices LittleBot may touch (the domains in ``DOMAINS``), narrowed to a
kind ("lights", "blinds") and/or the words of a room in their names ("kitchen"); a home card shows them (tap a
light or a plug to switch it). ``home_control(target, action, value)`` finds the device by its friendly name (or
entity id), loosely ("the kitchen lights" finds "Kitchen Light"), asks back when two fit equally, and calls the
service. Locking, unlocking and opening a garage door / gate need the user's explicit yes: without
``confirmed: true`` the tool only returns the question to ask.

The skill is offered only when the address and the token are set, never in offline mode, and (for an address that
isn't on the home network) only while cloud communication is on in Privacy. Errors are short kinds (timeout,
auth, not_found ...), never what the server said.
"""

from __future__ import annotations

import asyncio
import difflib
import ipaddress
import logging
import re
import time
from typing import Any, Callable

import httpx

from littlebot.skills.base import BOOL, STR, Skill, ToolSpec, as_json, err, ok, tool

log = logging.getLogger("littlebot.skills.home")

CACHE_S = 30.0
MAX_LISTED = 40
CARD_DEVICES = 12
MATCH_MIN = 0.62  # a name scoring less than this isn't the device they meant
AMBIGUOUS = 0.04  # another device within this of the best one: ask which

# Domains LittleBot may list and control, in listing order, with a plain word for each.
DOMAINS: dict[str, str] = {"light": "light", "switch": "switch", "fan": "fan", "climate": "thermostat",
                           "cover": "blind or door", "lock": "lock", "media_player": "media player",
                           "scene": "scene", "script": "script"}
KIND_WORDS: dict[str, tuple[str, ...]] = {
    "light": ("light", "lights", "lamp", "lamps", "bulb", "bulbs", "lighting"),
    "switch": ("switch", "switches", "plug", "plugs", "outlet", "outlets", "socket", "sockets"),
    "fan": ("fan", "fans"),
    "climate": ("thermostat", "thermostats", "heating", "heater", "heat", "ac", "aircon", "air conditioning",
                "air conditioner", "climate", "hvac", "temperature"),
    "cover": ("blind", "blinds", "curtain", "curtains", "shade", "shades", "shutter", "shutters", "cover", "covers",
              "garage", "garage door", "awning", "gate", "gates"),
    "lock": ("lock", "locks", "door lock", "door locks", "deadbolt"),
    "media_player": ("tv", "tvs", "television", "speaker", "speakers", "media", "media player", "media players"),
    "scene": ("scene", "scenes"),
    "script": ("script", "scripts", "automation script"),
}
ACTIONS = ("turn_on", "turn_off", "toggle", "set_brightness", "set_temperature", "open", "close", "lock", "unlock",
           "activate", "set_color")
# Which domains each action can apply to (narrows the name search: "open the kitchen" finds the kitchen blind).
ACTION_DOMAINS: dict[str, tuple[str, ...]] = {
    "turn_on": ("light", "switch", "fan", "media_player", "climate", "script", "scene", "cover"),
    "turn_off": ("light", "switch", "fan", "media_player", "climate", "script", "cover"),
    "toggle": ("light", "switch", "fan", "media_player", "cover"),
    "set_brightness": ("light", "fan", "cover"),
    "set_temperature": ("climate",),
    "open": ("cover",),
    "close": ("cover",),
    "lock": ("lock",),
    "unlock": ("lock",),
    "activate": ("scene", "script"),
    "set_color": ("light",),
}
TOGGLES = frozenset({"light", "switch", "fan"})  # a tap on the home card switches these
CONFIRM_COVERS = frozenset({"garage", "gate", "door"})  # opening these (device_class) asks first, like locks
WHITES = {"warm white": 2700, "soft white": 2700, "warm": 2700, "white": 4000, "neutral white": 4000,
          "cool white": 5500, "cool": 5500, "daylight": 6500, "bright white": 6500}
COLORS = frozenset((
    "red orange yellow green blue purple pink violet magenta cyan teal turquoise lime gold amber indigo lavender "
    "coral salmon crimson maroon navy aqua azure beige brown chocolate fuchsia khaki mint olive orchid peach plum "
    "skyblue tan tomato").split()) | {"sky blue", "hot pink", "light blue", "dark blue", "light green", "dark green",
                                      "deep pink", "royal blue", "sea green", "forest green", "light pink"}
_CSS_NAME = {"sky blue": "skyblue", "hot pink": "hotpink", "light blue": "lightblue", "dark blue": "darkblue",
             "light green": "lightgreen", "dark green": "darkgreen", "deep pink": "deeppink", "royal blue": "royalblue",
             "sea green": "seagreen", "forest green": "forestgreen", "light pink": "lightpink", "peach": "peachpuff",
             "amber": "orange"}
ERRORS = {
    "not_set_up": "the smart home isn't set up (Home Assistant's address and token)",
    "offline": "LittleBot is in offline mode, so it can't reach the smart home",
    "cloud_off": "cloud communication is off in Privacy, and the smart home is on the internet",
    "timeout": "the smart home didn't answer in time",
    "network": "the smart home couldn't be reached (is Home Assistant running?)",
    "auth": "Home Assistant refused LittleBot's token (it may need a new one)",
    "not_found": "Home Assistant doesn't know that",
    "bad_request": "Home Assistant couldn't do that with this device",
    "server": "Home Assistant had a problem",
    "bad_response": "Home Assistant gave an answer LittleBot couldn't read",
}
_FILLER = frozenset({"the", "my", "our", "a", "an", "in", "on", "of", "please", "all", "every", "whole", "room",
                     "area", "device", "devices", "littlebot", "little", "bot"})


class HomeError(Exception):
    def __init__(self, kind: str) -> None:
        self.kind = kind if kind in ERRORS else "bad_response"
        super().__init__(ERRORS[self.kind])


# ---------------------------------------------------------------- names
def words(text: Any) -> list[str]:
    """Lower-case words, apostrophes dropped, simple plurals made singular ("lights" -> "light")."""
    out = []
    for w in re.findall(r"[a-z0-9]+", str(text or "").lower().replace("'", "").replace("’", "")):
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def norm(text: Any) -> str:
    return " ".join(words(text))


def kind_of(filter_words: list[str]) -> tuple[str | None, list[str]]:
    """("light", ["kitchen"]) from ["kitchen", "light"]: the kind named (if any) and the other words."""
    text = " ".join(filter_words)
    for domain, names in KIND_WORDS.items():
        for name in sorted(names, key=len, reverse=True):
            n = norm(name)
            if re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", text):
                rest = re.sub(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", " ", text, count=1).split()
                return domain, rest
    return None, filter_words


def name_score(target: str, name: str) -> float:
    """How well ``target`` (what was said) fits a device ``name`` (0..1)."""
    t = [w for w in words(target) if w not in _FILLER]
    n = [w for w in words(name) if w not in _FILLER]
    if not t or not n:
        return 0.0
    ts, ns = " ".join(t), " ".join(n)
    if ts == ns:
        return 1.0
    if set(t) <= set(n):
        return 0.92 - 0.03 * (len(set(n)) - len(set(t)))
    if set(n) <= set(t):
        return 0.86 - 0.03 * (len(set(t)) - len(set(n)))
    return round(difflib.SequenceMatcher(None, ts, ns).ratio() * 0.88, 3)


def is_lan(url: str) -> bool:
    """The address is on the home network (a private / loopback IP, a .local name, a bare host name)."""
    try:
        host = (httpx.URL(url).host or "").lower().strip("[]")
    except (httpx.InvalidURL, TypeError, ValueError):
        return False
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return "." not in host or host.endswith((".local", ".lan", ".home", ".internal", ".home.arpa", ".localdomain"))
    return ip.is_private or ip.is_loopback or ip.is_link_local


def friendly(state: dict[str, Any]) -> str:
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    name = str(attrs.get("friendly_name") or "").strip()
    if name:
        return " ".join(name.split())[:60]
    obj = str(state.get("entity_id") or "").split(".", 1)[-1]
    return obj.replace("_", " ").title()[:60] or "Device"


def device(state: dict[str, Any]) -> dict[str, Any]:
    """One entity as the model and the card see it: {entity_id, name, kind, state, ...detail}."""
    eid = str(state.get("entity_id") or "")
    domain = eid.split(".", 1)[0]
    attrs = state.get("attributes") if isinstance(state.get("attributes"), dict) else {}
    out: dict[str, Any] = {"entity_id": eid, "name": friendly(state), "kind": domain,
                           "state": str(state.get("state") or "unknown")[:30]}
    if domain == "light" and isinstance(attrs.get("brightness"), (int, float)) and out["state"] == "on":
        out["brightness_percent"] = round(float(attrs["brightness"]) / 255 * 100)
    if domain == "climate":
        for key, new in (("current_temperature", "temperature_now"), ("temperature", "target_temperature")):
            if isinstance(attrs.get(key), (int, float)):
                out[new] = attrs[key]
    if domain == "cover" and isinstance(attrs.get("current_position"), (int, float)):
        out["position_percent"] = int(attrs["current_position"])
    if domain == "fan" and isinstance(attrs.get("percentage"), (int, float)) and out["state"] == "on":
        out["speed_percent"] = int(attrs["percentage"])
    if domain == "media_player" and attrs.get("media_title"):
        out["playing"] = str(attrs["media_title"])[:60]
    if domain == "cover" and attrs.get("device_class"):
        out["class"] = str(attrs["device_class"])[:20]
    return out


# ---------------------------------------------------------------- the skill
class SmartHome(Skill):
    name = "home"

    def __init__(self, ctx: Any, *, now: Callable[[], float] = time.time,
                 transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(ctx, now=now)
        self._transport = transport
        self._clock = clock
        self._states: list[dict[str, Any]] | None = None
        self._states_at = 0.0
        self._fetching: asyncio.Lock | None = None
        self._client: httpx.AsyncClient | None = None
        self._client_key: tuple[Any, ...] | None = None

    # ------------------------------------------------------------ set-up and gates
    @property
    def cfg(self) -> Any:
        return self.ctx.config.skills

    def url(self) -> str:
        raw = str(getattr(self.cfg, "home_assistant_url", "") or "").strip().rstrip("/")
        if raw.endswith("/api"):
            raw = raw[:-4]
        try:
            u = httpx.URL(raw)
        except (httpx.InvalidURL, TypeError, ValueError):
            return ""
        return raw if u.scheme in ("http", "https") and u.host else ""

    def _token(self) -> str:
        try:
            return str(self.ctx.config.secret("home_assistant_token") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def blocked_reason(self) -> str | None:
        if not self.url() or not self._token():
            return "not_set_up"
        if self.ctx.config.is_offline:
            return "offline"
        if not is_lan(self.url()) and not self.ctx.privacy.cloud_enabled:
            return "cloud_off"
        return None

    def available(self) -> bool:
        return self.blocked_reason() is None

    def tools(self) -> list[ToolSpec]:
        return [
            tool("home_devices", "The smart-home devices LittleBot can control (lights, plugs and switches, fans, "
                 "the thermostat, blinds and garage doors, locks, TVs and speakers, scenes, scripts) with their state "
                 "now. Give a kind ('lights', 'thermostat') or a room ('kitchen'), or both ('kitchen lights'), to "
                 "narrow it; leave it out for everything. It also shows them on the screen.",
                 {"area_or_kind": {**STR, "description": "optional: a room, a kind of device, or both"}}),
            tool("home_control", "Control one smart-home device: turn it on or off, toggle it, set a light's "
                 "brightness (0-100), the thermostat's temperature, a light's color, open or close a blind or "
                 "garage door, lock or unlock a lock, or activate a scene or script.",
                 {"target": {**STR, "description": "the device's name as the user says it ('kitchen light', "
                                                   "'movie night') or its entity id"},
                  "action": {"type": "string", "enum": list(ACTIONS)},
                  "value": {"description": "set_brightness: 0-100; set_temperature: degrees; set_color: a color "
                                           "name ('blue', 'warm white'); open/close: optional position 0-100"},
                  "confirmed": {**BOOL, "description": "true only after the user clearly said yes to locking, "
                                                       "unlocking or opening a garage door / gate"}},
                 ("target", "action")),
        ]

    def prompt_note(self) -> str:
        return ("Smart home (Home Assistant): home_devices lists the devices and their state (narrow it with a room "
                "or a kind); home_control does one thing to one device (turn_on, turn_off, toggle, set_brightness "
                "0-100, set_temperature, open, close, lock, unlock, activate a scene or script, set_color). Use the "
                "names it knows; when it asks which one, ask the user. Locking, unlocking and opening a garage door "
                "need the user's clear yes: ask, then call again with confirmed true. Confirm in a few words.")

    def status(self) -> dict[str, Any]:
        reason = self.blocked_reason()
        return {**super().status(), "set_up": reason != "not_set_up", "blocked": reason,
                "devices_known": len(self._states or [])}

    async def stop(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def run(self, name: str, args: dict[str, Any]) -> str:
        reason = self.blocked_reason()
        if reason:
            return err(ERRORS[reason])
        try:
            if name == "home_devices":
                return await self._home_devices(args)
            if name == "home_control":
                return await self._home_control(args)
        except HomeError as e:
            log.info("smart home: %s failed (%s)", name, e.kind)
            return err(str(e))
        return await super().run(name, args)

    # ------------------------------------------------------------ home_devices
    async def device_list(self, area_or_kind: str = "", *, fresh: bool = False) -> list[dict[str, Any]]:
        """The devices matching a room and/or kind (all of them for ""), listing order. Raises HomeError."""
        states = await self.states(fresh=fresh)
        domain, rest = kind_of(words(area_or_kind))
        rest = [w for w in rest if w not in _FILLER]
        out = []
        for s in states:
            d = device(s)
            if d["kind"] not in DOMAINS or (domain and d["kind"] != domain):
                continue
            if d["state"] == "unavailable" and d["kind"] in ("scene", "script"):
                continue
            if rest:
                have = set(words(d["name"])) | set(words(d["entity_id"].split(".", 1)[-1].replace("_", " ")))
                if not all(any(h == w or h.startswith(w) for h in have) for w in rest):
                    continue
            out.append(d)
        order = list(DOMAINS)
        out.sort(key=lambda d: (order.index(d["kind"]), d["name"].lower()))
        return out

    async def _home_devices(self, args: dict[str, Any]) -> str:
        wanted = " ".join(str(args.get("area_or_kind") or "").split())[:60]
        found = await self.device_list(wanted)
        if not found:
            return ok(f"no devices match '{wanted}'; home_devices without a filter lists them all" if wanted
                      else "Home Assistant has no lights, switches or other devices LittleBot can control")
        listed = [{k: v for k, v in d.items() if k != "entity_id"} for d in found[:MAX_LISTED]]
        out: dict[str, Any] = {"devices": listed, "count": len(found)}
        if len(found) > MAX_LISTED:
            out["more"] = len(found) - MAX_LISTED
        await self._show(wanted, found)
        return as_json(out)

    async def _show(self, wanted: str, found: list[dict[str, Any]]) -> None:
        if getattr(self.ctx, "screens", None) is None:
            return
        spec = {"card": "home", "title": card_title(wanted), "filter": wanted, "devices": found[:CARD_DEVICES]}
        task = asyncio.ensure_future(self.show(spec))
        try:
            await asyncio.wait_for(asyncio.shield(task), 0.5)
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------ home_control
    def resolve(self, target: str, action: str, states: list[dict[str, Any]]) -> tuple[dict[str, Any] | None,
                                                                                      list[str]]:
        """(the device meant, []) or (None, the names it could be: [] none, several = ask which). Only devices
        that can do ``action`` count ("*": any device)."""
        allowed = tuple(DOMAINS) if action == "*" else ACTION_DOMAINS.get(action, tuple(DOMAINS))
        devices = [device(s) for s in states]
        devices = [d for d in devices if d["kind"] in DOMAINS]
        raw = target.strip().lower()
        for d in devices:
            if d["entity_id"].lower() == raw:
                return d, []
        domain, rest = kind_of(words(target))
        scored = []
        for d in devices:
            if d["kind"] not in allowed:
                continue
            s = max(name_score(target, d["name"]),  # the friendly name first; the entity id's words as a fallback
                    name_score(target, d["entity_id"].split(".", 1)[-1].replace("_", " ")) - 0.1)
            if domain and d["kind"] == domain:
                s += 0.03
            scored.append((s, d))
        scored.sort(key=lambda x: -x[0])
        if not scored or scored[0][0] < MATCH_MIN:
            if domain and not [w for w in rest if w not in _FILLER]:  # only a kind: "the heating", "the thermostat"
                same = [d for _, d in scored if d["kind"] == domain]
                if len(same) == 1:
                    return same[0], []
                if same:
                    return None, [d["name"] for d in same[:4]]
            close = [d["name"] for s, d in scored[:3] if s >= 0.45]
            return None, close
        best = scored[0][0]
        tied = [d for s, d in scored if best - s <= AMBIGUOUS]
        if len(tied) > 1 and len({d["name"].lower() for d in tied}) > 1:
            return None, [d["name"] for d in tied[:4]]
        return scored[0][1], []

    async def _home_control(self, args: dict[str, Any]) -> str:
        target = " ".join(str(args.get("target") or "").split())[:80]
        action = str(args.get("action") or "").strip().lower()
        if not target:
            return err("say which device")
        if action not in ACTIONS:
            return err(f"unknown action; use one of {', '.join(ACTIONS)}")
        states = await self.states()
        found, options = self.resolve(target, action, states)
        if found is None:
            if len(options) > 1:
                return f"ask: which one: {_or(options)}? Ask the user, then call home_control again with that name."
            other, _ = self.resolve(target, "*", states)
            if other is not None and other["kind"] not in ACTION_DOMAINS.get(action, ()):
                # it exists, but can't do that ("turn off the front door": a lock)
                hint = "; say lock or unlock" if other["kind"] == "lock" else ""
                return err(f"{other['name']} is a {DOMAINS.get(other['kind'], 'device')}: it can't "
                           f"{action.replace('_', ' ')}{hint}")
            if options:
                return err(f"no device called '{target}' for that; did they mean {_or(options)}?")
            return err(f"no device called '{target}' can do that; home_devices lists the devices")
        try:
            plan = service_for(found, action, args.get("value"))
        except ValueError as e:
            return err(str(e))
        domain, service, data, done = plan
        if needs_yes(found, action) and args.get("confirmed") is not True:
            verb = {"lock": "lock", "unlock": "unlock"}.get(action, "open")
            return (f"ask: do you want me to {verb} {found['name']}? Ask the user to confirm, and only after a clear "
                    f"yes call home_control again with confirmed true.")
        await self.call(domain, service, {"entity_id": found["entity_id"], **data})
        log.info("smart home: %s.%s done", domain, service)
        return ok(done)

    async def toggle(self, entity_id: str) -> str | None:
        """Switch a light / plug / fan (the home card's tap); its new state ("on" / "off"), None when unknown.
        Raises HomeError."""
        domain = entity_id.split(".", 1)[0]
        if domain not in TOGGLES:
            raise HomeError("bad_request")
        reason = self.blocked_reason()
        if reason:
            raise HomeError(reason)
        changed = await self.call(domain, "toggle", {"entity_id": entity_id})
        for s in changed if isinstance(changed, list) else []:
            if isinstance(s, dict) and s.get("entity_id") == entity_id:
                return str(s.get("state") or "") or None
        return None

    # ------------------------------------------------------------ the REST API
    async def states(self, *, fresh: bool = False) -> list[dict[str, Any]]:
        if self._fetching is None:
            self._fetching = asyncio.Lock()
        async with self._fetching:
            if not fresh and self._states is not None and self._clock() - self._states_at < CACHE_S:
                return self._states
            data = await self._request("GET", "/api/states")
            if not isinstance(data, list):
                raise HomeError("bad_response")
            self._states = [s for s in data if isinstance(s, dict) and isinstance(s.get("entity_id"), str)]
            self._states_at = self._clock()
            return self._states

    async def call(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        if not re.fullmatch(r"[a-z_]+", domain) or not re.fullmatch(r"[a-z_]+", service):
            raise HomeError("bad_request")
        result = await self._request("POST", f"/api/services/{domain}/{service}", data)
        self._states_at = 0.0  # what was changed shows next time
        if isinstance(result, list) and self._states is not None:
            fresh = {s.get("entity_id"): s for s in result if isinstance(s, dict)}
            self._states = [fresh.get(s.get("entity_id"), s) for s in self._states]
        return result

    def _http(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        timeout = float(getattr(self.cfg, "home_timeout_seconds", 5.0))
        key = (loop, self.url(), timeout)
        if self._client is None or self._client.is_closed or self._client_key != key:
            from littlebot import __version__  # noqa: PLC0415

            self._client = httpx.AsyncClient(
                base_url=self.url(), transport=self._transport, follow_redirects=False,
                headers={"User-Agent": f"LittleBot/{__version__}", "Accept": "application/json"},
                timeout=httpx.Timeout(timeout, connect=min(3.0, timeout)))
            self._client_key = key
        return self._client

    async def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self._token()}"}
        timeout = float(getattr(self.cfg, "home_timeout_seconds", 5.0))
        try:
            resp = await asyncio.wait_for(self._http().request(method, path, json=body, headers=headers), timeout)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise HomeError("timeout") from None
        except httpx.HTTPError:
            raise HomeError("network") from None
        code = resp.status_code
        if code >= 400:
            log.info("smart home: %s %s answered HTTP %d", method, path.split("/")[2] if path.count("/") > 1 else path,
                     code)
            raise HomeError("auth" if code in (401, 403) else "not_found" if code == 404 else
                            "bad_request" if code in (400, 405, 422) else "server" if code >= 500 else "bad_response")
        try:
            return resp.json()
        except ValueError:
            raise HomeError("bad_response") from None


# ---------------------------------------------------------------- what each action does
def needs_yes(d: dict[str, Any], action: str) -> bool:
    if d["kind"] == "lock" and action in ("lock", "unlock", "toggle", "turn_on", "turn_off"):
        return True
    return (d["kind"] == "cover" and action in ("open", "turn_on", "toggle", "set_brightness")
            and (str(d.get("class") or "") in CONFIRM_COVERS
                 or any(w in words(d["name"]) for w in ("garage", "gate"))))


def _number(value: Any, lo: float, hi: float, what: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"give {what}")
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value)
        value = float(m.group(0)) if m else None
    if not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"give {what}")
    v = float(value)
    if not lo <= v <= hi:
        raise ValueError(f"{what} must be between {lo:g} and {hi:g}")
    return v


def color_data(value: Any) -> tuple[dict[str, Any], str]:
    name = " ".join(str(value or "").lower().split())[:30]
    if name in WHITES:
        return {"color_temp_kelvin": WHITES[name]}, name
    if name in COLORS:
        return {"color_name": _CSS_NAME.get(name, name.replace(" ", ""))}, name
    raise ValueError("give a color name, like red, blue, green, purple, pink, warm white or daylight")


def service_for(d: dict[str, Any], action: str, value: Any) -> tuple[str, str, dict[str, Any], str]:
    """(domain, service, extra data, what to say when done) for doing ``action`` to device ``d``. ValueError
    (a plain sentence) when that device can't do it or the value is wrong."""
    kind, name = d["kind"], d["name"]
    what = DOMAINS.get(kind, "device")
    if kind not in ACTION_DOMAINS.get(action, ()) and not (kind == "lock" and action in ("lock", "unlock")):
        raise ValueError(f"{name} is a {what}: it can't {action.replace('_', ' ')}")
    if action == "turn_on":
        if kind == "cover":
            return "cover", "open_cover", {}, f"opened {name}"
        if kind == "scene":
            return "scene", "turn_on", {}, f"activated {name}"
        if kind == "script":
            return "script", "turn_on", {}, f"started {name}"
        return kind, "turn_on", {}, f"turned on {name}"
    if action == "turn_off":
        if kind == "cover":
            return "cover", "close_cover", {}, f"closed {name}"
        return kind, "turn_off", {}, f"turned off {name}"
    if action == "toggle":
        return kind, "toggle", {}, f"switched {name} {'off' if d.get('state') in ('on', 'open') else 'on'}"
    if action == "set_brightness":
        pct = int(round(_number(value, 0, 100, "a brightness from 0 to 100")))
        if kind == "light":
            if pct == 0:
                return "light", "turn_off", {}, f"turned off {name}"
            return "light", "turn_on", {"brightness_pct": pct}, f"set {name} to {pct}%"
        if kind == "fan":
            return "fan", "set_percentage", {"percentage": pct}, f"set {name} to {pct}%"
        return "cover", "set_cover_position", {"position": pct}, f"set {name} to {pct}% open"
    if action == "set_temperature":
        t = _number(value, 4, 95, "a temperature")
        t = round(t * 2) / 2
        shown = f"{t:g}"
        return "climate", "set_temperature", {"temperature": t}, f"set {name} to {shown}°"
    if action in ("open", "close"):
        if value not in (None, "") and not isinstance(value, bool):
            pct = int(round(_number(value, 0, 100, "a position from 0 to 100")))
            return "cover", "set_cover_position", {"position": pct}, f"set {name} to {pct}% open"
        return ("cover", "open_cover", {}, f"opened {name}") if action == "open" else \
            ("cover", "close_cover", {}, f"closed {name}")
    if action in ("lock", "unlock"):
        return "lock", action, {}, f"{action}ed {name}"
    if action == "activate":
        return kind, "turn_on", {}, f"activated {name}" if kind == "scene" else f"started {name}"
    if action == "set_color":
        data, said = color_data(value)
        return "light", "turn_on", data, f"{name} is now {said}"
    raise ValueError("unknown action")


def card_title(wanted: str) -> str:
    domain, rest = kind_of(words(wanted))
    room = " ".join(w.capitalize() for w in rest if w not in _FILLER)
    label = {"light": "Lights", "switch": "Switches", "fan": "Fans", "climate": "Thermostat", "cover": "Blinds & doors",
             "lock": "Locks", "media_player": "Media", "scene": "Scenes", "script": "Scripts"}.get(domain or "", "")
    if room and label:
        return f"{room} {label.lower()}"
    return room or label or "Smart home"


def _or(names: list[str]) -> str:
    names = [f"'{n}'" for n in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


__all__ = ["ACTIONS", "DOMAINS", "ERRORS", "HomeError", "SmartHome", "card_title", "device", "is_lan", "kind_of",
           "name_score", "needs_yes", "service_for", "words"]

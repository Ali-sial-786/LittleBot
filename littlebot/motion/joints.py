"""Joint model: limits, angle <-> pulse mapping, config validation and calibration files.

Angle conventions (see docs/modules/motion.md):

* A **joint angle** is what config, gestures, the API and status use: degrees 0..180.
* The **servo angle** is joint angle, or ``180 - joint angle`` when ``inverted`` (a
  mirrored mounting). The pulse is linear in servo angle:
  ``pulse = min_pulse_us + servo_angle / 180 * (max_pulse_us - min_pulse_us)``.
* Every command is HARD-clamped to ``[min_angle, max_angle]`` and every pulse to
  ``[min_pulse_us, max_pulse_us]`` and to the absolute 500..2500 us window.
* Gestures use **normalised positions**: 0 = neutral, +1 = the limit named by
  ``positive_toward`` (arms: raised; head (pan): looking to the robot's left; head_tilt:
  looking up), -1 = the other.
* **Soft limits** (``soft_min`` .. ``soft_max``, ``soft_margin_deg`` inside the hard ones) bound
  look_at() and body-language targets; the hard limits bound everything.

Joint names: ``head`` is the head's PAN axis (kept as "head" for compatibility with configs,
calibration files and activities), ``head_tilt`` its TILT axis, ``left_arm`` / ``right_arm``
the shoulders. Any subset may be configured: gestures and look_at skip missing joints.

Calibration overrides live in a YAML file (``config/calibration.yaml``, git-ignored),
written by ``scripts/calibrate_servos.py``; the format is documented in
``config/calibration.example.yaml``.
"""

from __future__ import annotations

import datetime as _dt
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from littlebot.core.config import MotionConfig, ServoChannelConfig
from littlebot.core.paths import PROJECT_ROOT
from littlebot.motion.drivers import ABS_MAX_PULSE_US, ABS_MIN_PULSE_US, NUM_CHANNELS

SERVO_RANGE_DEG = 180.0
HARD_MAX_SPEED_DPS = 180.0  # no configuration can make a joint faster than this
HARD_MAX_ACCEL_DPS2 = 720.0
MAX_SOFT_MARGIN_DEG = 20.0

HEAD_PAN = "head"  # turn left / right (+ = the robot's left)
HEAD_TILT = "head_tilt"  # nod up / down (+ = up)
LEFT_ARM = "left_arm"
RIGHT_ARM = "right_arm"
HEAD_JOINTS = (HEAD_PAN, HEAD_TILT)
MIN_PWM_HZ, MAX_PWM_HZ = 40, 60  # analog hobby servos (MG90S) want ~50 Hz
MIN_UPDATE_HZ, MAX_UPDATE_HZ = 5.0, 200.0
CALIBRATION_VERSION = 1

_NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")

CALIBRATION_JOINT_KEYS = frozenset({
    "channel", "neutral", "min_angle", "max_angle", "max_speed_dps", "max_accel_dps2", "inverted",
    "min_pulse_us", "max_pulse_us", "positive_toward", "soft_margin_deg",
})
CALIBRATION_REQUIRED_KEYS = frozenset({"neutral", "min_angle", "max_angle"})
CALIBRATION_TOP_KEYS = frozenset({"version", "created", "tool", "notes", "pwm_frequency", "i2c_address", "servos"})


class MotionConfigError(ValueError):
    """Motion configuration is unsafe or inconsistent; motion stays disabled."""

    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors) or "invalid motion configuration")


class CalibrationError(ValueError):
    """Calibration file missing, unreadable or invalid."""


# --------------------------------------------------------------------------- joint spec
@dataclass(frozen=True)
class JointSpec:
    name: str
    channel: int
    neutral: float
    min_angle: float
    max_angle: float
    max_speed_dps: float = 90.0
    max_accel_dps2: float = 200.0
    inverted: bool = False
    min_pulse_us: float = 500.0
    max_pulse_us: float = 2500.0
    positive_toward: str = "max"
    soft_margin_deg: float = 0.0

    @classmethod
    def from_config(cls, name: str, cfg: ServoChannelConfig) -> "JointSpec":
        return cls(
            name=name, channel=int(cfg.channel), neutral=float(cfg.neutral),
            min_angle=float(cfg.min_angle), max_angle=float(cfg.max_angle),
            max_speed_dps=float(cfg.max_speed_dps), max_accel_dps2=float(cfg.max_accel_dps2),
            inverted=bool(cfg.inverted), min_pulse_us=float(cfg.min_pulse_us),
            max_pulse_us=float(cfg.max_pulse_us), positive_toward=str(cfg.positive_toward),
            soft_margin_deg=float(cfg.soft_margin_deg),
        )

    # -- limits -----------------------------------------------------------
    @property
    def speed_limit(self) -> float:
        """Effective max speed (deg/s): config value, never above the hard ceiling."""
        return max(1e-6, min(float(self.max_speed_dps), HARD_MAX_SPEED_DPS))

    @property
    def accel_limit(self) -> float:
        return max(1e-6, min(float(self.max_accel_dps2), HARD_MAX_ACCEL_DPS2))

    @property
    def pulse_floor(self) -> float:
        return max(ABS_MIN_PULSE_US, float(self.min_pulse_us))

    @property
    def pulse_ceiling(self) -> float:
        return min(ABS_MAX_PULSE_US, float(self.max_pulse_us))

    def clamp(self, angle: float) -> float:
        """Hard-clamp a joint angle to [min_angle, max_angle]. Non-finite values -> ValueError."""
        a = float(angle)
        if not math.isfinite(a):
            raise ValueError(f"{self.name}: angle must be a finite number")
        return min(self.max_angle, max(self.min_angle, a))

    def is_within(self, angle: float, tol: float = 1e-6) -> bool:
        return self.min_angle - tol <= angle <= self.max_angle + tol

    @property
    def soft_min(self) -> float:
        """Lower soft limit: ``soft_margin_deg`` inside min_angle (never past neutral)."""
        return min(self.neutral, self.min_angle + max(0.0, self.soft_margin_deg))

    @property
    def soft_max(self) -> float:
        return max(self.neutral, self.max_angle - max(0.0, self.soft_margin_deg))

    def soft_clamp(self, angle: float) -> float:
        """Clamp to the soft limits (look_at / body language targets). Non-finite -> ValueError."""
        a = self.clamp(angle)
        return min(self.soft_max, max(self.soft_min, a))

    def toward(self, angle: float, degrees: float) -> float:
        """``angle`` moved ``degrees`` in the positive (normalised) direction; not clamped."""
        return float(angle) + (float(degrees) if self.positive_toward == "max" else -float(degrees))

    # -- mapping --------------------------------------------------------------
    def servo_angle(self, angle: float) -> float:
        a = self.clamp(angle)
        return SERVO_RANGE_DEG - a if self.inverted else a

    def angle_to_pulse(self, angle: float) -> float:
        """Joint angle -> pulse width (us), clamped twice: angle limits, then pulse limits."""
        s = self.servo_angle(angle)
        pulse = self.min_pulse_us + (s / SERVO_RANGE_DEG) * (self.max_pulse_us - self.min_pulse_us)
        return min(self.pulse_ceiling, max(self.pulse_floor, pulse))

    def pulse_to_angle(self, pulse_us: float) -> float:
        """Inverse mapping (not clamped to joint limits; used for diagnostics)."""
        span = self.max_pulse_us - self.min_pulse_us
        s = (float(pulse_us) - self.min_pulse_us) / span * SERVO_RANGE_DEG
        return SERVO_RANGE_DEG - s if self.inverted else s

    def resolve(self, value: float) -> float:
        """Normalised gesture position (-1..1, 0 = neutral) -> joint angle (within limits)."""
        v = float(value)
        if not math.isfinite(v):
            raise ValueError(f"{self.name}: position must be finite")
        v = max(-1.0, min(1.0, v))
        pos_end = self.max_angle if self.positive_toward == "max" else self.min_angle
        neg_end = self.min_angle if self.positive_toward == "max" else self.max_angle
        end = pos_end if v >= 0 else neg_end
        return self.clamp(self.neutral + abs(v) * (end - self.neutral))

    def normalise(self, angle: float) -> float:
        """Inverse of ``resolve``: joint angle -> normalised position -1..1 (for drawing / UIs)."""
        a = self.clamp(angle)
        pos_end = self.max_angle if self.positive_toward == "max" else self.min_angle
        neg_end = self.min_angle if self.positive_toward == "max" else self.max_angle
        offset = a - self.neutral
        if offset == 0:
            return 0.0
        if (offset > 0) == (pos_end > self.neutral):
            return max(-1.0, min(1.0, offset / (pos_end - self.neutral)))
        return max(-1.0, min(1.0, -offset / (neg_end - self.neutral)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_joint(spec: JointSpec) -> list[str]:
    errs: list[str] = []
    n = spec.name
    if not _NAME_RE.match(n):
        errs.append(f"joint name {n!r} must be lowercase letters, digits or _")
    nums = {k: getattr(spec, k) for k in ("neutral", "min_angle", "max_angle", "max_speed_dps", "max_accel_dps2",
                                          "min_pulse_us", "max_pulse_us", "soft_margin_deg")}
    bad = [k for k, v in nums.items() if not isinstance(v, (int, float)) or not math.isfinite(float(v))]
    if bad:
        return errs + [f"{n}: {', '.join(bad)} must be finite numbers"]
    if not 0 <= spec.channel < NUM_CHANNELS:
        errs.append(f"{n}: channel {spec.channel} outside 0..{NUM_CHANNELS - 1}")
    if not 0.0 <= spec.min_angle < spec.neutral < spec.max_angle <= SERVO_RANGE_DEG:
        errs.append(f"{n}: need 0 <= min_angle < neutral < max_angle <= 180 "
                    f"(got min={spec.min_angle:g}, neutral={spec.neutral:g}, max={spec.max_angle:g})")
    if not 0 < spec.max_speed_dps <= HARD_MAX_SPEED_DPS:
        errs.append(f"{n}: max_speed_dps must be in (0, {HARD_MAX_SPEED_DPS:g}] (got {spec.max_speed_dps:g})")
    if not 0 < spec.max_accel_dps2 <= HARD_MAX_ACCEL_DPS2:
        errs.append(f"{n}: max_accel_dps2 must be in (0, {HARD_MAX_ACCEL_DPS2:g}] (got {spec.max_accel_dps2:g})")
    if not ABS_MIN_PULSE_US <= spec.min_pulse_us < spec.max_pulse_us <= ABS_MAX_PULSE_US:
        errs.append(f"{n}: need {ABS_MIN_PULSE_US:.0f} <= min_pulse_us < max_pulse_us <= {ABS_MAX_PULSE_US:.0f} "
                    f"(got {spec.min_pulse_us:g}..{spec.max_pulse_us:g})")
    if spec.positive_toward not in ("max", "min"):
        errs.append(f"{n}: positive_toward must be 'max' or 'min'")
    if not 0.0 <= spec.soft_margin_deg <= MAX_SOFT_MARGIN_DEG:
        errs.append(f"{n}: soft_margin_deg must be 0..{MAX_SOFT_MARGIN_DEG:g} (got {spec.soft_margin_deg:g})")
    return errs


def validate_specs(specs: Mapping[str, JointSpec], *, pwm_frequency: float = 50, update_hz: float = 50) -> list[str]:
    errs: list[str] = []
    if not specs:
        errs.append("no servos configured")
    channels: dict[int, str] = {}
    for name, spec in specs.items():
        if name != spec.name:
            errs.append(f"joint key {name!r} does not match spec name {spec.name!r}")
        errs.extend(validate_joint(spec))
        if spec.channel in channels:
            errs.append(f"{name}: channel {spec.channel} already used by {channels[spec.channel]}")
        channels.setdefault(spec.channel, name)
    if not MIN_PWM_HZ <= float(pwm_frequency) <= MAX_PWM_HZ:
        errs.append(f"pwm_frequency must be {MIN_PWM_HZ}..{MAX_PWM_HZ} Hz for analog servos (got {pwm_frequency})")
    if not MIN_UPDATE_HZ <= float(update_hz) <= MAX_UPDATE_HZ:
        errs.append(f"update_hz must be {MIN_UPDATE_HZ:g}..{MAX_UPDATE_HZ:g} (got {update_hz})")
    return errs


def validate_motion_settings(cfg: MotionConfig) -> list[str]:
    errs: list[str] = []
    checks = {
        "speed_scale": (cfg.speed_scale, 0.0, 1.0, False),
        "homing_speed_factor": (cfg.homing_speed_factor, 0.0, 1.0, False),
        "positions_hz": (cfg.positions_hz, 0.0, 60.0, False),
        "min_move_seconds": (cfg.min_move_seconds, 0.0, 10.0, True),
        "settle_seconds": (cfg.settle_seconds, 0.0, 10.0, True),
        "release_when_idle_seconds": (cfg.release_when_idle_seconds, 0.0, 3600.0, True),
    }
    for key, (v, lo, hi, lo_inclusive) in checks.items():
        ok = math.isfinite(v) and (lo <= v if lo_inclusive else lo < v) and v <= hi
        if not ok:
            errs.append(f"motion.{key}={v} outside {'[' if lo_inclusive else '('}{lo:g}, {hi:g}]")
    if not 0x03 <= int(cfg.i2c_address) <= 0x77:
        errs.append(f"motion.i2c_address 0x{int(cfg.i2c_address):02x} is not a valid 7-bit address")
    return errs


# --------------------------------------------------------------------------- calibration
@dataclass
class Calibration:
    path: Path
    servos: dict[str, dict[str, Any]]
    meta: dict[str, Any] = field(default_factory=dict)


def resolve_path(path: str | Path) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else PROJECT_ROOT / p


def load_calibration(path: str | Path, *, known_joints: Iterable[str] | None = None,
                     require_all: bool = False) -> Calibration:
    """Read and validate a calibration file (structure + types; limits are checked after merging)."""
    p = resolve_path(path)
    if not p.is_file():
        raise CalibrationError(f"calibration file {p} not found")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise CalibrationError(f"cannot read calibration file {p}: {e}") from e
    if not isinstance(data, dict):
        raise CalibrationError("calibration file must contain a mapping")
    unknown_top = set(data) - CALIBRATION_TOP_KEYS
    if unknown_top:
        raise CalibrationError(f"unknown top-level keys: {', '.join(sorted(map(str, unknown_top)))}")
    version = data.get("version", CALIBRATION_VERSION)
    if version != CALIBRATION_VERSION:
        raise CalibrationError(f"unsupported calibration version {version!r} (expected {CALIBRATION_VERSION})")
    servos = data.get("servos")
    if not isinstance(servos, dict) or not servos:
        raise CalibrationError("calibration file needs a non-empty 'servos' mapping")
    known = set(known_joints) if known_joints is not None else None
    out: dict[str, dict[str, Any]] = {}
    for name, entry in servos.items():
        if known is not None and name not in known:
            raise CalibrationError(f"calibration for unknown joint {name!r} (configured: {', '.join(sorted(known))})")
        if not isinstance(entry, dict):
            raise CalibrationError(f"{name}: calibration entry must be a mapping")
        extra = set(entry) - CALIBRATION_JOINT_KEYS
        if extra:
            raise CalibrationError(f"{name}: unknown keys {', '.join(sorted(map(str, extra)))}")
        missing = CALIBRATION_REQUIRED_KEYS - set(entry)
        if missing:
            raise CalibrationError(f"{name}: missing {', '.join(sorted(missing))}")
        for key, value in entry.items():
            if key == "inverted":
                if not isinstance(value, bool):
                    raise CalibrationError(f"{name}.inverted must be true/false")
            elif key == "positive_toward":
                if value not in ("max", "min"):
                    raise CalibrationError(f"{name}.positive_toward must be 'max' or 'min'")
            elif key == "channel":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise CalibrationError(f"{name}.channel must be an integer")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise CalibrationError(f"{name}.{key} must be a finite number")
        out[str(name)] = dict(entry)
    if require_all and known is not None:
        missing_joints = known - set(out)
        if missing_joints:
            raise CalibrationError(f"calibration missing joints: {', '.join(sorted(missing_joints))}")
    meta = {k: v for k, v in data.items() if k != "servos"}
    return Calibration(path=p, servos=out, meta=meta)


def apply_calibration(servos: Mapping[str, ServoChannelConfig],
                      calibration: Calibration | None) -> dict[str, ServoChannelConfig]:
    if calibration is None:
        return dict(servos)
    merged: dict[str, ServoChannelConfig] = {}
    for name, base in servos.items():
        override = calibration.servos.get(name)
        if not override:
            merged[name] = base
            continue
        try:
            merged[name] = ServoChannelConfig.model_validate({**base.model_dump(), **override})
        except Exception as e:  # pydantic ValidationError
            raise CalibrationError(f"{name}: {e}") from e
    return merged


def build_joint_specs(cfg: MotionConfig, calibration: Calibration | None = None) -> dict[str, JointSpec]:
    """Config (+ calibration overrides) -> validated joint specs. Raises MotionConfigError."""
    servos = apply_calibration(cfg.servos, calibration)
    specs = {name: JointSpec.from_config(name, sc) for name, sc in servos.items()}
    errs = validate_specs(specs, pwm_frequency=cfg.pwm_frequency, update_hz=cfg.update_hz)
    errs += validate_motion_settings(cfg)
    if errs:
        raise MotionConfigError(errs)
    return specs


def calibration_document(joints: Mapping[str, Mapping[str, Any]], *, pwm_frequency: int = 50,
                         i2c_address: int = 0x40, notes: str = "") -> dict[str, Any]:
    servos: dict[str, dict[str, Any]] = {}
    for name, values in joints.items():
        entry = {k: values[k] for k in ("channel", "neutral", "min_angle", "max_angle", "inverted", "positive_toward",
                                        "min_pulse_us", "max_pulse_us", "max_speed_dps", "max_accel_dps2",
                                        "soft_margin_deg")
                 if k in values}
        for k in ("neutral", "min_angle", "max_angle"):
            entry[k] = round(float(entry[k]), 1)
        servos[name] = entry
    doc: dict[str, Any] = {
        "version": CALIBRATION_VERSION,
        "created": _dt.datetime.now().replace(microsecond=0).isoformat(),
        "tool": "scripts/calibrate_servos.py",
        "pwm_frequency": int(pwm_frequency),
        "i2c_address": int(i2c_address),
    }
    if notes:
        doc["notes"] = notes
    doc["servos"] = servos
    return doc


def save_calibration(path: str | Path, joints: Mapping[str, Mapping[str, Any]], **kwargs: Any) -> Path:
    """Write a calibration file (validated by re-loading it)."""
    p = resolve_path(path)
    doc = calibration_document(joints, **kwargs)
    header = ("# LittleBot servo calibration - written by scripts/calibrate_servos.py.\n"
              "# Git-ignored, machine specific. Format: config/calibration.example.yaml.\n"
              "# Angles are joint angles in degrees; see docs/MOTION_SAFETY.md before editing.\n")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(header + yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    tmp.replace(p)
    load_calibration(p)
    return p

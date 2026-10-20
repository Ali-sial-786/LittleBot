#!/usr/bin/env python3
"""LittleBot camera + face-detector check (Raspberry Pi or desktop).

Lists video devices, opens one, reports the negotiated resolution / frame rate /
pixel format, measures the real capture rate and runs the face detector with
per-frame timings - the same downscale + detector the robot uses.

Privacy: frames are processed in memory and discarded. Nothing is written to
disk unless you pass ``--save PATH``, which saves exactly ONE frame.

    .venv/bin/python scripts/check_camera.py --list              # devices only (never opens one)
    .venv/bin/python scripts/check_camera.py                     # configured device, 5 s test
    .venv/bin/python scripts/check_camera.py --device /dev/video0 --seconds 10
    .venv/bin/python scripts/check_camera.py --width 1280 --height 720 --fps 30 --fourcc MJPG
    .venv/bin/python scripts/check_camera.py --detector yunet    # after scripts/download_models.py
    .venv/bin/python scripts/check_camera.py --fixture image.jpg # benchmark the detector, no camera needed
    .venv/bin/python scripts/check_camera.py --save /tmp/frame.jpg   # saves ONE frame (with boxes)
    .venv/bin/python scripts/check_camera.py --json

Exit status: 0 = ok, 1 = a camera was found but failed, 2 = no camera found.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HINTS = [
    "List cameras and their formats:  v4l2-ctl --list-devices   and   v4l2-ctl -d /dev/video0 --list-formats-ext",
    "  (sudo apt install v4l-utils). A UVC camera shows two nodes: the first (index 0) is the capture node.",
    "Plug the camera directly into the Pi (no unpowered hub). The U20CAM is USB 2.0: use MJPG - raw YUYV at",
    "  640x480@30 needs ~18 MB/s and larger raw modes drop to a few fps.",
    "Permission denied? Add your user to the 'video' group: sudo usermod -aG video $USER (then log in again).",
    "'Device busy'? Another program (or a running LittleBot) holds the camera: stop it first.",
    "Stable device name across re-plugging: use the /dev/v4l/by-id/...-video-index0 path as camera.device.",
]


def _stats(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    s = sorted(values)
    return {"n": len(s), "mean": round(statistics.fmean(s), 2), "median": round(statistics.median(s), 2),
            "p95": round(s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))], 2), "max": round(s[-1], 2)}


def _load_config(mode: str | None) -> Any:
    from littlebot.core.config import LittleBotConfig, load_config

    try:
        return load_config(mode)
    except Exception as e:  # noqa: BLE001 - a broken local.yaml must not break diagnostics
        print(f"! could not load config ({e}); using built-in defaults")
        return LittleBotConfig()


def _opencv_v4l2() -> str | None:
    from littlebot.vision.imaging import get_cv2

    cv2 = get_cv2()
    if cv2 is None:
        return None
    m = re.search(r"v4l/v4l2:\s*(\S+)", cv2.getBuildInformation(), re.IGNORECASE)
    return m.group(1) if m else "unknown"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Check the camera and face detector (no images are stored unless --save).")
    ap.add_argument("--list", action="store_true", help="only list video devices (never opens one) and exit")
    ap.add_argument("--mode", default=None, help="config mode for defaults (simulation | pi | offline)")
    ap.add_argument("--device", default=None, help="auto | index | /dev/videoN | /dev/v4l/by-id/... (default: config)")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--fps", type=float, default=None)
    ap.add_argument("--fourcc", default=None, help="pixel format to request, e.g. MJPG or YUYV ('' = driver default)")
    ap.add_argument("--seconds", type=float, default=5.0, help="measurement duration (default 5)")
    ap.add_argument("--detector", choices=["config", "haar", "yunet", "none"], default="config")
    ap.add_argument("--yunet-model", default=None, help="path to the YuNet ONNX model (default: config)")
    ap.add_argument("--process-width", type=int, default=None, help="downscale width before detection (default: config)")
    ap.add_argument("--detect-fps", type=float, default=0.0,
                    help="run the detector at most this often (0 = on every frame, default)")
    ap.add_argument("--threads", type=int, default=None, help="cv2.setNumThreads (default: config vision.opencv_threads)")
    ap.add_argument("--fixture", default=None, help="image / video / directory to use instead of a camera")
    ap.add_argument("--save", default=None, metavar="PATH",
                    help="save ONE frame (the last one, with detection boxes) - only when you ask for it")
    ap.add_argument("--json", action="store_true", help="print a JSON report instead of text")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    report: dict[str, Any] = {}

    def say(line: str = "") -> None:
        if not args.json:
            print(line, flush=True)

    def finish(code: int) -> int:
        report["exit_code"] = code
        if args.json:
            print(json.dumps(report, indent=2, default=str))
        return code

    from littlebot.hardware.platform import describe_platform
    from littlebot.vision.camera import CameraError, FixtureCamera, OpenCVCamera, list_video_devices, resolve_device
    from littlebot.vision.detector import configure_opencv_threads, create_detector, detector_support
    from littlebot.vision.imaging import get_cv2, resize_to_width

    cfg = _load_config(args.mode)
    cam_cfg, vis_cfg = cfg.camera, cfg.vision
    plat = describe_platform()
    support = detector_support()
    report["platform"] = plat
    report["opencv"] = {**support, "v4l2": _opencv_v4l2()}
    say("LittleBot camera check")
    say(f"  platform : {plat.get('pi_model') or plat.get('system')} {plat.get('machine')}, Python {plat.get('python')}")
    if support["opencv"] is None:
        say("  OpenCV   : NOT INSTALLED - pip install opencv-python-headless (or: sudo apt install python3-opencv)")
    else:
        say(f"  OpenCV   : {support['opencv']} (V4L2 support: {report['opencv']['v4l2']}); "
            f"Haar: {'yes' if support['haar_classifier'] and support['haar_cascade_file'] else 'no'}; "
            f"YuNet API: {'yes' if support['yunet'] else 'no'}")

    # ---------------------------------------------------------------- devices
    devices = list_video_devices()
    report["devices"] = devices
    say("")
    say("Video devices:")
    if not devices:
        say("  (none)")
    for d in devices:
        tags = []
        if d["candidate"]:
            tags.append("camera?")
        if d["usb"]:
            tags.append("usb")
        if d["platform_node"]:
            tags.append("SoC codec/ISP - not a camera")
        if d["node_index"] not in (0, None):
            tags.append("metadata node")
        say(f"  {d['path']:<14} {d['name'] or '?':<34} [{', '.join(tags)}]")
        if d.get("by_id"):
            say(f"  {'':<14} stable path: {d['by_id']}")
    candidates = [d for d in devices if d["candidate"]]

    if args.list:
        if not candidates:
            say("")
            say("No camera found.")
            for h in HINTS[:4]:
                say("  " + h)
            return finish(2)
        return finish(0)

    # ----------------------------------------------------------------- camera
    width = args.width or cam_cfg.capture_width
    height = args.height or cam_cfg.capture_height
    fps = args.fps if args.fps is not None else cam_cfg.capture_fps
    fourcc = cam_cfg.fourcc if args.fourcc is None else args.fourcc
    process_width = args.process_width or cam_cfg.process_width
    if args.fixture:
        cam: Any = FixtureCamera(args.fixture, loop=True)
        label = f"fixture {args.fixture}"
    else:
        spec = args.device if args.device is not None else cam_cfg.device
        dev = resolve_device(spec)
        path = dev if isinstance(dev, str) else f"/dev/video{dev}"
        if sys.platform.startswith("linux") and not Path(path).exists():
            say("")
            say(f"No camera found (device {spec!r} -> {path} does not exist).")
            say("Hints:")
            for h in HINTS:
                say("  " + h)
            report["camera"] = {"ok": False, "detail": "no camera found"}
            return finish(2)
        cam = OpenCVCamera(dev, width, height, fps, fourcc)
        label = f"{path} requesting {width}x{height} @ {fps:g} fps {fourcc or '(driver default format)'}"
    say("")
    say(f"Opening {label} ...")
    t_open = time.perf_counter()
    try:
        cam.open()
    except CameraError as e:
        say(f"  FAILED: {e}")
        say("Hints:")
        for h in HINTS:
            say("  " + h)
        report["camera"] = {"ok": False, "detail": str(e)}
        return finish(2 if "no camera" in str(e) else 1)
    open_ms = (time.perf_counter() - t_open) * 1000
    info = cam.info()
    report["camera"] = {"ok": True, "open_ms": round(open_ms, 1), **info}
    say(f"  opened in {open_ms:.0f} ms: {info.get('width')}x{info.get('height')} "
        f"@ {info.get('fps') or '?'} fps, format {info.get('fourcc') or '?'}")
    warnings: list[str] = []
    if not args.fixture:
        if (info.get("width"), info.get("height")) != (width, height):
            warnings.append(f"resolution negotiated as {info.get('width')}x{info.get('height')} (asked {width}x{height})")
        if fourcc and info.get("fourcc") and info["fourcc"].strip().upper() != fourcc.upper():
            warnings.append(f"format is {info['fourcc']} not {fourcc}: raw formats use much more USB 2.0 bandwidth")
        if info.get("fps") and fps and abs(float(info["fps"]) - float(fps)) > 0.5:
            warnings.append(f"driver reports {info['fps']} fps (asked {fps:g}); fine - LittleBot only processes "
                            f"{cam_cfg.detect_fps:g} frames/s")
    for w in warnings:
        say(f"  note: {w}")

    # --------------------------------------------------------------- detector
    if args.threads is not None:
        configure_opencv_threads(args.threads)
    else:
        configure_opencv_threads(vis_cfg.opencv_threads)
    kind = vis_cfg.detector if args.detector == "config" else args.detector
    detector = create_detector(kind, max_width=process_width, min_face_fraction=vis_cfg.min_face_fraction,
                               yunet_model=args.yunet_model or vis_cfg.yunet_model,
                               yunet_score_threshold=vis_cfg.yunet_score_threshold,
                               haar_cascade=vis_cfg.haar_cascade, haar_min_neighbors=vis_cfg.haar_min_neighbors,
                               haar_scale_factor=vis_cfg.haar_scale_factor)
    report["detector"] = {"requested": kind, "name": detector.name, "detail": detector.detail,
                          "process_width": process_width}
    say(f"Detector: {detector.name} ({detector.detail}); frames downscaled to {process_width} px wide")

    # ---------------------------------------------------------------- measure
    say(f"Measuring for {args.seconds:g} s ...")
    frames = misses = 0
    det_ms: list[float] = []
    resize_ms: list[float] = []
    faces_per_frame: list[int] = []
    last_frame = None
    last_faces: list[Any] = []
    first_frame_at = None
    t_end = time.monotonic() + max(0.1, args.seconds)
    next_detect = 0.0
    try:
        while time.monotonic() < t_end:
            frame = cam.read(timeout=1.0)
            now = time.monotonic()
            if frame is None:
                misses += 1
                if cam.failed or misses > 5:
                    say("  camera stopped delivering frames")
                    break
                continue
            frames += 1
            first_frame_at = first_frame_at or now
            if now >= next_detect:
                next_detect = now + (1.0 / args.detect_fps if args.detect_fps > 0 else 0.0)
                t0 = time.perf_counter()
                small = resize_to_width(frame, process_width)
                resize_ms.append((time.perf_counter() - t0) * 1000)
                faces = detector.detect(small)
                det_ms.append(detector.last_ms or 0.0)
                faces_per_frame.append(len(faces))
                last_faces = faces
            if args.save:
                last_frame = frame  # kept only to save the ONE frame the user asked for
    finally:
        elapsed = (time.monotonic() - first_frame_at) if first_frame_at else 0.0
        grab_fps = cam.measured_fps() if not args.fixture else None
        cam.release()

    delivered = (frames - 1) / elapsed if frames > 1 and elapsed > 0 else 0.0
    with_faces = sum(1 for n in faces_per_frame if n)
    report["capture"] = {"frames": frames, "misses": misses, "delivered_fps": round(delivered, 2),
                         "camera_fps": grab_fps}
    report["detection"] = {"runs": len(det_ms), "frames_with_faces": with_faces,
                           "max_faces": max(faces_per_frame, default=0), "detect_ms": _stats(det_ms),
                           "resize_ms": _stats(resize_ms),
                           "last_faces": [{"x": round(f.x, 3), "y": round(f.y, 3), "w": round(f.w, 3),
                                           "h": round(f.h, 3), "score": round(f.score, 3)} for f in last_faces]}
    if args.fixture:
        say(f"  frames processed: {frames} (fixture, no capture rate)")
    else:
        say(f"  frames delivered: {frames} ({delivered:.1f} fps to the reader; camera produced "
            f"{grab_fps or 0:.1f} fps); empty reads: {misses}")
    st = report["detection"]["detect_ms"]
    if st:
        say(f"  detection: {len(det_ms)} runs, faces in {with_faces} ({100 * with_faces / len(det_ms):.0f} %), "
            f"max {report['detection']['max_faces']} at once")
        say(f"  detect time per frame: mean {st['mean']} ms, median {st['median']} ms, p95 {st['p95']} ms, "
            f"max {st['max']} ms (+ resize {(_stats(resize_ms) or {}).get('mean', 0)} ms)")
        budget = st["mean"] * cam_cfg.detect_fps / 10.0
        say(f"  at camera.detect_fps={cam_cfg.detect_fps:g} that is ~{budget:.0f} % of one CPU core here")
    if detector.name == "none":
        say("  note: no face detector available - " + detector.detail)

    # ------------------------------------------------------------------- save
    if args.save:
        if last_frame is None:
            say("Not saving: no frame was captured.")
        else:
            cv2 = get_cv2()
            img = last_frame.copy()
            H, W = img.shape[:2]
            for f in last_faces:
                cv2.rectangle(img, (int(f.x * W), int(f.y * H)), (int((f.x + f.w) * W), int((f.y + f.h) * H)),
                              (0, 255, 0), 2)
            Path(args.save).expanduser().parent.mkdir(parents=True, exist_ok=True)
            ok = cv2.imwrite(str(Path(args.save).expanduser()), img)
            report["saved"] = str(args.save) if ok else None
            say(f"Saved ONE frame to {args.save} ({'ok' if ok else 'FAILED'}). It may show people: "
                "delete it when you are done.")
        last_frame = None

    code = 0 if frames > 0 else 1
    say("")
    say("Result: " + ("OK" if code == 0 else "camera opened but delivered no frames"))
    if code:
        for h in HINTS:
            say("  " + h)
    return finish(code)


if __name__ == "__main__":
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
    sys.exit(main())

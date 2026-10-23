#!/usr/bin/env python3
"""Check that ChatGPT can see LittleBot's photos (uses the robot's ChatGPT sign-in).

    .venv/bin/python scripts/check_vision.py            # send a test picture (a big "7" and 3 red dots)
    .venv/bin/python scripts/check_vision.py --camera   # also send one real camera photo and save it
                                                        # to data/vision_check.jpg so you can look at it
    .venv/bin/python scripts/check_vision.py --dry-run  # show the request shape, send nothing

The test picture tells two problems apart: if ChatGPT reads the 7 and counts the dots, images reach
it and any trouble is with the camera picture itself; if it can't, the endpoint isn't receiving our
images. --camera needs the camera free: it stops LittleBot for a few seconds
(systemctl --user stop littlebot) and starts it again afterwards. The saved photo is yours to delete.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TEST_QUESTION = "What number is written in the picture, and how many red dots are there?"
CAMERA_QUESTION = "Describe exactly what you can see in this photo."


def test_picture() -> bytes:
    import cv2
    import numpy as np

    img = np.full((480, 640, 3), 255, np.uint8)
    cv2.putText(img, "7", (220, 330), cv2.FONT_HERSHEY_SIMPLEX, 9, (0, 0, 0), 28, cv2.LINE_AA)
    for x in (120, 320, 520):
        cv2.circle(img, (x, 420), 30, (0, 0, 255), -1)  # BGR red
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    assert ok
    return buf.tobytes()


def camera_photo(device: object) -> bytes:
    import cv2

    cap = cv2.VideoCapture(device)
    try:
        if not cap.isOpened():
            raise SystemExit(f"Could not open the camera ({device!r}). Is LittleBot still running?")
        frame = None
        end = time.monotonic() + 2.5  # let auto-exposure and focus settle, like the running robot's camera
        while time.monotonic() < end:
            ok, f = cap.read()
            if ok:
                frame = f
        if frame is None:
            raise SystemExit("The camera gave no picture.")
    finally:
        cap.release()
    from littlebot.vision.imaging import brighten_if_dark

    print(f"   Camera brightness: {float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()):.0f} of 255 "
          "(under ~60 is dark: turn on a light)")
    ok, buf = cv2.imencode(".jpg", brighten_if_dark(frame), [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    assert ok
    return buf.tobytes()


async def ask(provider: object, jpeg: bytes, question: str) -> str:
    from littlebot.chatgpt.look import LOOK_SYSTEM, QUESTION

    t0 = time.monotonic()
    reply = await provider.reply([{"role": "user", "content": QUESTION.format(question=question)}],  # type: ignore[attr-defined]
                                 system=LOOK_SYSTEM.format(robot="LittleBot", words=40), max_tokens=200,
                                 timeout=30.0, images=[jpeg])
    return f"{reply.text!r}  (model {reply.model}, {time.monotonic() - t0:.1f} s, {len(jpeg) // 1024} KB image)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", action="store_true", help="also send a real camera photo (stops LittleBot briefly)")
    ap.add_argument("--dry-run", action="store_true", help="print the request shape and send nothing")
    args = ap.parse_args()

    from littlebot.chatgpt.auth import ChatGPTAuth
    from littlebot.chatgpt.responses import ChatGPTProvider
    from littlebot.core.config import load_config

    cfg = load_config(mode="pi")
    token_file = Path(cfg.chatgpt.token_file).expanduser()
    if not token_file.is_absolute():
        token_file = cfg.data_path / token_file
    auth = ChatGPTAuth(token_file, originator=cfg.chatgpt.originator, user_agent="LittleBot vision check")
    provider = ChatGPTProvider(auth, models=list(cfg.chatgpt.models), effort=cfg.chatgpt.reasoning_effort)
    jpeg = test_picture()
    if args.dry_run:
        body = provider.build_body([{"role": "user", "content": TEST_QUESTION}], system="(look instructions)",
                                   model=provider.model)
        print(json.dumps({k: v for k, v in body.items() if k != "input"}, indent=1))
        print("input:", json.dumps(body["input"])[:300], f"... + 1 image of {len(jpeg)} bytes")
        return 0
    if not auth.signed_in:
        print("Not signed in with ChatGPT on this robot. Sign in on the dashboard's AI page first.")
        return 1

    print(f"1) Test picture ({TEST_QUESTION})")
    try:
        print("   ChatGPT:", asyncio.run(ask(provider, jpeg, TEST_QUESTION)))
    except Exception as e:  # noqa: BLE001
        print(f"   FAILED: {type(e).__name__}: {e}  {getattr(e, 'api_error', '')}")
        return 1
    print("   Expected: the number 7 and 3 red dots.")

    if args.camera:
        print("2) Camera photo (stopping LittleBot for a moment)")
        subprocess.run(["systemctl", "--user", "stop", "littlebot"], check=False)
        try:
            time.sleep(1.5)
            photo = camera_photo(cfg.camera.device if not isinstance(cfg.camera.device, str) or
                                 cfg.camera.device.startswith("/dev/") else 0)
        finally:
            subprocess.run(["systemctl", "--user", "start", "littlebot"], check=False)
        out = cfg.data_path / "vision_check.jpg"
        out.write_bytes(photo)
        print(f"   Saved the photo to {out} (delete it when done).")
        try:
            print("   ChatGPT:", asyncio.run(ask(provider, photo, CAMERA_QUESTION)))
        except Exception as e:  # noqa: BLE001
            print(f"   FAILED: {type(e).__name__}: {e}  {getattr(e, 'api_error', '')}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

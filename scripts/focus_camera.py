#!/usr/bin/env python3
"""Live focus meter for the camera: twist the lens until the number is as high as it gets.

    .venv/bin/python scripts/focus_camera.py          # Ctrl+C to stop

Run it on the robot (or over ssh). It stops LittleBot while it uses the camera and starts it again
when you press Ctrl+C. Hold something with detail (your hand, a book, printed text) about 40-60 cm
in front of the camera and keep it still, then turn the lens a little at a time: the sharpness goes
up as the picture gets sharper. Go past the highest value and come back to it. Nothing is saved.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    import cv2

    from littlebot.core.config import load_config

    cfg = load_config(mode="pi")
    device = cfg.camera.device
    if isinstance(device, str) and not device.startswith("/dev/"):
        device = 0  # "auto"
    subprocess.run(["systemctl", "--user", "stop", "littlebot"], check=False)
    time.sleep(1.5)
    cap = cv2.VideoCapture(device)
    best = 0.0
    try:
        if not cap.isOpened():
            print(f"Could not open the camera ({device!r}).")
            return 1
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(cfg.camera.capture_width))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(cfg.camera.capture_height))
        print("Twist the lens slowly; higher = sharper. Ctrl+C when done.\n")
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.1)
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            centre = gray[h // 4: 3 * h // 4, w // 4: 3 * w // 4]  # what's in front of the lens
            sharp = float(cv2.Laplacian(centre, cv2.CV_64F).var())
            best = max(best, sharp)
            bar = "#" * min(40, int(40 * sharp / max(best, 1.0)))
            print(f"\r sharpness {sharp:7.0f}  best {best:7.0f}  brightness {gray.mean():3.0f}  |{bar:<40}|",
                  end="", flush=True)
            time.sleep(0.25)
    except KeyboardInterrupt:
        print(f"\n\nBest sharpness: {best:.0f} (in focus is usually well over 100 with a hand or book in view).")
    finally:
        cap.release()
        subprocess.run(["systemctl", "--user", "start", "littlebot"], check=False)
        print("LittleBot started again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

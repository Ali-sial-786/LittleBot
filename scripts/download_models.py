#!/usr/bin/env python3
"""Download the optional face-DETECTION models into models/ (git-ignored).

    .venv/bin/python scripts/download_models.py            # YuNet (recommended, ~230 KB)
    .venv/bin/python scripts/download_models.py haar       # Haar cascade XML (OpenCV 4.x builds without it)
    .venv/bin/python scripts/download_models.py all
    .venv/bin/python scripts/download_models.py --list     # what is installed / verified
    .venv/bin/python scripts/download_models.py --dest /some/dir --force

These models only find WHERE faces are. LittleBot never does face recognition,
identification or any other biometric analysis. Every file is checked against a
pinned SHA-256 hash; a mismatch is deleted and reported. Standard library only.

Exit status: 0 = all requested models present and verified, 1 = a download or check failed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = REPO_ROOT / "models"


@dataclass(frozen=True)
class Model:
    filename: str
    sha256: str
    size: int
    urls: list[str] = field(default_factory=list)
    license: str = ""
    note: str = ""


MODELS: dict[str, Model] = {
    "yunet": Model(
        filename="face_detection_yunet_2023mar.onnx",
        sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        size=232_589,
        urls=[
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/"
            "face_detection_yunet_2023mar.onnx",
            "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
        ],
        license="MIT (OpenCV Model Zoo)",
        note="YuNet face detector for cv2.FaceDetectorYN; needs OpenCV >= 4.8 (vision.detector: yunet)",
    ),
    "haar": Model(
        filename="haarcascade_frontalface_default.xml",
        sha256="0f7d4527844eb514d4a4948e822da90fbb16a34a0bbbbc6adc6498747a5aafb0",
        size=930_127,
        urls=["https://raw.githubusercontent.com/opencv/opencv/4.x/data/haarcascades/"
              "haarcascade_frontalface_default.xml"],
        license="Intel License Agreement (BSD-style, distributed with OpenCV)",
        note="frontal-face Haar cascade; only usable with OpenCV 4.x (cv2.CascadeClassifier)",
    ),
}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def is_verified(model: Model, dest: Path) -> bool:
    p = dest / model.filename
    return p.is_file() and sha256_of(p) == model.sha256


def download(model: Model, dest: Path, *, force: bool = False, timeout: float = 30.0, quiet: bool = False) -> bool:
    """Fetch ``model`` into ``dest`` (atomic replace after the hash matched). True on success."""
    def say(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    target = dest / model.filename
    if not force and target.exists():
        if is_verified(model, dest):
            say(f"  {model.filename}: already present (sha256 ok)")
            return True
        say(f"  {model.filename}: present but hash differs - downloading again")
    dest.mkdir(parents=True, exist_ok=True)
    limit = model.size * 2 + 1024
    for url in model.urls:
        say(f"  {model.filename}: downloading {url}")
        fd, tmp_name = tempfile.mkstemp(prefix=f".{model.filename}.", dir=dest)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                total = 0
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ValueError(f"response larger than expected ({total} bytes)")
                    out.write(chunk)
            digest = sha256_of(tmp)
            if digest != model.sha256:
                raise ValueError(f"sha256 mismatch ({digest[:16]}... != {model.sha256[:16]}...)")
            os.chmod(tmp, 0o644)  # mkstemp creates 0600; the robot may run as another user
            os.replace(tmp, target)
            say(f"  {model.filename}: ok ({total} bytes, sha256 verified) -> {target}")
            return True
        except Exception as e:  # noqa: BLE001 - try the next mirror
            say(f"  {model.filename}: failed from this mirror: {e}")
        finally:
            if tmp.exists():
                tmp.unlink()
    say(f"  {model.filename}: FAILED from every mirror. If you are behind a proxy set HTTPS_PROXY; "
        f"or download it manually into {dest} (sha256 {model.sha256}).")
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download optional face-detection models (detection only, no recognition).")
    ap.add_argument("names", nargs="*", default=["yunet"], help=f"models: {', '.join(MODELS)} or all (default: yunet)")
    ap.add_argument("--dest", default=str(DEFAULT_DEST), help="target directory (default: models/)")
    ap.add_argument("--force", action="store_true", help="download even if a verified copy exists")
    ap.add_argument("--list", action="store_true", help="show available models and their status")
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args(argv)
    dest = Path(args.dest).expanduser()

    if args.list:
        for name, m in MODELS.items():
            status = "verified" if is_verified(m, dest) else ("present, hash differs" if (dest / m.filename).exists()
                                                             else "not downloaded")
            print(f"{name:6} {m.filename:40} {status:22} {m.license}")
            print(f"{'':6} {m.note}")
        return 0

    names = list(MODELS) if "all" in args.names else args.names
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        ap.error(f"unknown model(s): {', '.join(unknown)} (choose from {', '.join(MODELS)}, all)")
    ok = True
    for name in names:
        ok &= download(MODELS[name], dest, force=args.force, timeout=args.timeout)
    if ok and "yunet" in names:
        print("Done. vision.detector: yunet (the default) will now use YuNet; check with scripts/check_camera.py.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

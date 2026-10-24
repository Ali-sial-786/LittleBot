#!/usr/bin/env python3
"""Download the optional vision models and the wake-word speech model into models/ (git-ignored).

    .venv/bin/python scripts/download_models.py            # YuNet (recommended, ~230 KB)
    .venv/bin/python scripts/download_models.py haar       # Haar cascade XML (OpenCV 4.x builds without it)
    .venv/bin/python scripts/download_models.py hands      # optional: count fingers on the robot (~8 MB)
    .venv/bin/python scripts/download_models.py faces      # optional: recognise family members (YuNet + SFace, 39 MB)
    .venv/bin/python scripts/download_models.py vosk-en    # wake word "Hey LittleBot" (Vosk small English, 40 MB)
    .venv/bin/python scripts/download_models.py all
    .venv/bin/python scripts/download_models.py --list     # what is installed / verified
    .venv/bin/python scripts/download_models.py --dest /some/dir --force

YuNet and Haar only find WHERE faces are. SFace (``faces``) is used only when a parent switches
on "Recognise family members" in Privacy: it tells apart the family members the parent added on
the dashboard, on the robot (littlebot/vision/faces.py); nothing else does any biometric analysis.
Every file is checked against a pinned SHA-256 hash; a mismatch is deleted and reported. Standard
library only.

The speech model (vosk-en, a zip) is unpacked into models/vosk-model-small-en-us-0.15/
and the zip deleted; it lets LittleBot hear its wake word on the robot, offline
(littlebot/audio/wakeword.py; also usable as audio.stt.vosk_model_path).

Exit status: 0 = all requested models present and verified, 1 = a download or check failed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
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
    # Optional: "how many fingers?" counted on the robot (littlebot/vision/hands.py) instead of by ChatGPT.
    # Hand joints only - no identity, nothing kept. Get both with: download_models.py hands
    "hand_palm": Model(
        filename="palm_detection_mediapipe_2023feb.onnx",
        sha256="78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c",
        size=3_905_734,
        urls=[
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/palm_detection_mediapipe/"
            "palm_detection_mediapipe_2023feb.onnx",
            "https://huggingface.co/opencv/palm_detection_mediapipe/resolve/main/palm_detection_mediapipe_2023feb.onnx",
        ],
        license="Apache-2.0 (OpenCV Model Zoo, from Google MediaPipe Hands)",
        note="palm detector for local finger counting (cv2.dnn); with hand_pose",
    ),
    "hand_pose": Model(
        filename="handpose_estimation_mediapipe_2023feb.onnx",
        sha256="db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c",
        size=4_099_621,
        urls=[
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/handpose_estimation_mediapipe/"
            "handpose_estimation_mediapipe_2023feb.onnx",
            "https://huggingface.co/opencv/handpose_estimation_mediapipe/resolve/main/"
            "handpose_estimation_mediapipe_2023feb.onnx",
        ],
        license="Apache-2.0 (OpenCV Model Zoo, from Google MediaPipe Hands)",
        note="hand landmarks for local finger counting (cv2.dnn); with hand_palm",
    ),
    # Optional: recognise the family members a parent added (littlebot/vision/faces.py, opt-in in Privacy).
    # 128 numbers per face, kept on the robot. Get it with YuNet: download_models.py faces
    "sface": Model(
        filename="face_recognition_sface_2021dec.onnx",
        sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        size=38_696_353,
        urls=[
            "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/"
            "face_recognition_sface_2021dec.onnx",
            "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
        ],
        license="Apache-2.0 (OpenCV Model Zoo)",
        note="SFace for cv2.FaceRecognizerSF: recognises family members (privacy.face_recognition_enabled); "
             "~110 MB RAM in use",
    ),
}
GROUPS: dict[str, list[str]] = {"hands": ["hand_palm", "hand_pose"], "faces": ["yunet", "sface"]}  # several models


@dataclass(frozen=True)
class Archive:
    """A zipped model directory: verified like a Model, then unpacked into ``dest/<dirname>``."""

    dirname: str  # the directory inside the zip, and under dest
    filename: str  # the zip
    sha256: str
    size: int
    urls: list[str] = field(default_factory=list)
    license: str = ""
    note: str = ""
    marker: str = ""  # a file that must exist in the unpacked directory
    max_unpacked: int = 200_000_000  # refuse zip bombs


STAMP = ".sha256"  # written into an unpacked directory: which verified archive it came from

ARCHIVES: dict[str, Archive] = {
    "vosk-en": Archive(
        dirname="vosk-model-small-en-us-0.15",
        filename="vosk-model-small-en-us-0.15.zip",
        sha256="30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498",
        size=41_205_931,
        urls=[
            "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip",
            "https://huggingface.co/rhasspy/vosk-models/resolve/main/en/vosk-model-small-en-us-0.15.zip",
        ],
        license="Apache-2.0 (Alpha Cephei Vosk models)",
        note="small English speech model for the wake word (wake.model_path); ~130 MB RAM while loaded",
        marker="conf/model.conf",
    ),
}


def archive_ready(archive: Archive, dest: Path) -> bool:
    """Unpacked from the verified zip (the stamp matches) and complete (the marker exists)."""
    folder = dest / archive.dirname
    stamp = folder / STAMP
    if not (folder.is_dir() and stamp.is_file()) or (archive.marker and not (folder / archive.marker).is_file()):
        return False
    return stamp.read_text(encoding="utf-8").strip() == archive.sha256


def _unpack(zip_path: Path, archive: Archive, staging: Path) -> Path:
    """Extract only files under ``<dirname>/`` (no absolute paths, no ``..``); returns the folder."""
    root = staging.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if sum(m.file_size for m in members) > archive.max_unpacked:
            raise ValueError("archive unpacks to more than expected")
        for m in members:
            name = m.filename
            if not name.startswith(archive.dirname + "/") or ".." in Path(name).parts or Path(name).is_absolute():
                raise ValueError(f"unexpected file in archive: {name!r}")
            target = (root / name).resolve()
            target.relative_to(root)
            if m.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 16)
            os.chmod(target, 0o644)
    folder = root / archive.dirname
    if archive.marker and not (folder / archive.marker).is_file():
        raise ValueError(f"{archive.marker} missing from the archive")
    return folder


def download_archive(archive: Archive, dest: Path, *, force: bool = False, timeout: float = 30.0,
                     quiet: bool = False) -> bool:
    """Fetch + verify the zip (like ``download``), unpack it next to the old copy, then swap it in."""
    def say(msg: str) -> None:
        if not quiet:
            print(msg, flush=True)

    if not force and archive_ready(archive, dest):
        say(f"  {archive.dirname}: already present (sha256 ok)")
        return True
    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{archive.dirname}.", dir=dest))
    try:
        zipped = Model(filename=archive.filename, sha256=archive.sha256, size=archive.size, urls=archive.urls)
        if not download(zipped, staging, force=True, timeout=timeout, quiet=quiet):
            return False
        try:
            folder = _unpack(staging / archive.filename, archive, staging)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            say(f"  {archive.dirname}: could not unpack {archive.filename}: {e}")
            return False
        (folder / STAMP).write_text(archive.sha256 + "\n", encoding="utf-8")
        os.chmod(folder, 0o755)
        target = dest / archive.dirname
        old = None
        if target.exists():
            old = staging / "old"
            os.replace(target, old)
        os.replace(folder, target)
        say(f"  {archive.dirname}: ok (unpacked, sha256 verified) -> {target}")
        return True
    finally:
        shutil.rmtree(staging, ignore_errors=True)  # the zip, and any replaced copy


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
    ap = argparse.ArgumentParser(description="Download optional vision models (face detection, finger counting, "
                                             "opt-in family recognition) and the wake-word speech model.")
    choices = [*MODELS, *GROUPS, *ARCHIVES]
    ap.add_argument("names", nargs="*", default=["yunet"], help=f"models: {', '.join(choices)} or all (default: yunet)")
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
        for name, a in ARCHIVES.items():
            status = "verified" if archive_ready(a, dest) else ("present, not verified" if (dest / a.dirname).exists()
                                                                else "not downloaded")
            print(f"{name:6} {a.dirname + '/':40} {status:22} {a.license}")
            print(f"{'':6} {a.note}")
        return 0

    names = [*MODELS, *ARCHIVES] if "all" in args.names else [m for n in args.names for m in GROUPS.get(n, [n])]
    unknown = [n for n in names if n not in MODELS and n not in ARCHIVES]
    if unknown:
        ap.error(f"unknown model(s): {', '.join(unknown)} (choose from {', '.join(choices)}, all)")
    ok = True
    for name in names:
        if name in ARCHIVES:
            ok &= download_archive(ARCHIVES[name], dest, force=args.force, timeout=args.timeout)
        else:
            ok &= download(MODELS[name], dest, force=args.force, timeout=args.timeout)
    if ok and "vosk-en" in names:
        print("Done. The wake word (\"Hey LittleBot\") uses it within a few seconds; see docs/CHATGPT.md.")
    if ok and "yunet" in names:
        print("Done. vision.detector: yunet (the default) will now use YuNet; check with scripts/check_camera.py.")
    if ok and "sface" in names:
        print("Done. To recognise family members: switch on \"Recognise family members\" in Privacy, then add "
              "people under Who's who on the Profile page.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

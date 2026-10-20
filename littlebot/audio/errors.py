"""Exceptions shared by the audio package."""

from __future__ import annotations


class AudioError(Exception):
    """Base class for audio problems that callers may want to handle."""


class AudioDeviceError(AudioError):
    """A speaker / microphone is missing, busy, or failed mid-stream."""


class TTSError(AudioError):
    """A text-to-speech provider could not produce audio."""


class STTError(AudioError):
    """A speech-to-text provider could not transcribe audio."""


class CloudNotAllowed(AudioError):
    """A cloud provider was asked to work while cloud use is disabled (privacy) or unconfigured."""

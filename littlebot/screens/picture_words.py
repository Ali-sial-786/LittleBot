"""Which view of a card's picture words ask for: "show the picture in full screen", "make it bigger", "zoom in".

Shared by the quick answer (littlebot/chatgpt/quick.py: the voice's hand-off "show the picture full screen" is done
at once, without the text model) and the screen director (littlebot/screens/director.py: the person's own words).
Only used while a card with a picture is up, so "make it bigger" means its picture. From a real call: "show the
picture full screen" made a new card instead, and asked again it opened Google.
"""

from __future__ import annotations

import re

MAX_WORDS = 14  # longer words are more than a request about the picture

_PIC = (r"(?:(?:(?:the|that|this|his|her|its|your) )?(?:pictures?|images?|photos?|pics?|drawings?|paintings?)|"
        r"it|that|this|him|her|them)")
_BACK = re.compile(r"\b(?:back to the (?:card|words|points|facts)|(?:show|see) (?:me )?the (?:whole )?(?:card|words|"
                   r"points|facts)(?: again)?|(?:exit|leave|close|out of|get out of|no more|not) (?:the )?full ?screen|"
                   r"normal size|(?:the )?(?:usual|normal) (?:size|view)|un ?zoom)\b")
_OUT = re.compile(rf"\b(?:zoom(?:ed)? (?:out|back)|less zoom(?:ed)?|(?:make|show) {_PIC} (?:a (?:bit|little) )?"
                  rf"smaller|shrink)\b")
_IN = re.compile(r"\b(?:zoom(?:ed)?(?: in(?:to)?| on| closer)?|magnify|(?:even )?bigger still|more zoom(?:ed)?)\b")
_FULL = re.compile(r"\b(?:full ?screen|full size|(?:the )?(?:whole|entire|full) screen|fills? (?:up )?the screen|"
                   r"takes? (?:over|up) the screen|as big as (?:the screen|you can|possible)|"
                   r"blow (?:it |that |this )?up|enlarge)\b")
_BIGGER = re.compile(rf"\b(?:(?:make|show|put|let me see|can i see) (?:me )?{_PIC} (?:a (?:bit|lot|little) )?"
                     rf"(?:bigger|larger|big|huge)|(?:a )?(?:bigger|larger) (?:picture|image|photo|pic|drawing|one|"
                     rf"view)|see {_PIC} (?:bigger|better|up close))\b")
# The words, the browser, a video or another picture: not this card's picture.
_NOT = re.compile(r"\b(?:don'?t|do not|never|no need|shouldn'?t|text|words?|writing|letters?|font|google|browser|web|"
                  r"online|internet|website|page|video|youtube|another|different|new|other|next|weather|forecast)\b")
_NOT_BACK = re.compile(r"\b(?:don'?t|do not|never|no need|shouldn'?t)\b")


def plain(text: str) -> str:
    """Lower case, letters, digits and apostrophes, single spaces."""
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", str(text or "").lower().replace("’", "'")).split())


def picture_view(text: str) -> str | None:
    """"fill", "in", "out" or "back" (the picture command, littlebot/screens/base.py PICTURE_ACTION), or None
    when the words aren't about the card's picture."""
    words = plain(text)
    if not words or len(words.split()) > MAX_WORDS:
        return None
    if _BACK.search(words):
        return None if _NOT_BACK.search(words) else "back"
    if _NOT.search(words):
        return None
    if _OUT.search(words):
        return "out"
    if _FULL.search(words) or _BIGGER.search(words):
        return "in" if re.search(r"\bzoom(?:ed)? in", words) else "fill"
    if _IN.search(words):
        return "in"
    return None


__all__ = ["picture_view", "plain"]

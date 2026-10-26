"""Fast decision models for LittleBot's small multiple-choice calls: TypeSafe's Jev or OpenAI's Decisions API.

They don't write anything: given a little text and a question with fixed answers, they pick one (with a
probability for each) in about 0.2 s. LittleBot uses them where it otherwise guesses from word lists or asks
the text model for a whole JSON answer. Each feature can be off, shadow (asked alongside LittleBot's own way,
both logged, nothing changes) or on (its answer is used when it is sure enough):

* ``cards``    - the screen director: no card, close, steer, or which card kind (only real kinds can come back)
* ``routing``  - a voice hand-off: a face, a camera look, who is here, or the text model with its tools
* ``answered`` - has LittleBot actually answered yet, or is it still "hold on, let me check" (the card waits)
* ``browser``  - is what they said about the web page on the screen (it then doesn't count as moving on)
* ``naming``   - did the voice talk to the grown-up as if they were the baby
* ``videos``   - which YouTube search result best fits what was asked for, and the child
* ``names``    - which activity or game they meant, when the name matches none closely
* ``concern``  - could someone be in danger (only ever adds an alert to LittleBot's own safety check)
* ``addressed`` - words said over the voice: to LittleBot, or people in the room talking to each other (the voice
  then carries on)

* ``base``    - the questions (Choice, YesNo, Score) and answers
* ``jev``     - TypeSafe's ``POST /v1/systemone`` (TYPESAFE_API_KEY)
* ``openai``  - OpenAI's ``POST /v1/decisions`` (OPENAI_API_KEY)
* ``service`` - ``get_decider(ctx)``: the provider, the modes, the gates, the limits, the stats
* ``uses``    - each feature's question

Off until a parent picks a provider (config ``decide.provider`` or the dashboard) and its key is in .env;
never with the Privacy page's cloud switch off or in offline mode. See docs/DECISIONS.md.
"""

from __future__ import annotations

from littlebot.decide.base import FEATURES, MODES, PROVIDERS, Answer, Choice, DecideError, Score, YesNo
from littlebot.decide.service import Decider, get_decider

__all__ = ["FEATURES", "MODES", "PROVIDERS", "Answer", "Choice", "DecideError", "Decider", "Score", "YesNo",
           "get_decider"]

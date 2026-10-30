"""Voice + screen games for grown-ups and the family: trivia, hangman, twenty questions, riddles, would you rather.

Modules
-------
* ``base``      - ``VoiceGame`` (the deterministic engine both voice paths drive) and ``Reply``
* ``answers``   - forgiving local answer matching ("bee", "the second one", "jupitor", "is there an S")
* ``scores``    - ``ScoreStore``: high scores, recently asked items, would-you-rather votes (SQLite)
* ``data``      - the built-in content (assets/games/*.json)
* one module per game: ``trivia``, ``word_game`` (hangman), ``twenty_questions``, ``riddles``, ``would_you_rather``
* ``skill``     - ``VoiceGames``, the skill (``ctx.skills.get("voice_games")``): tools for the AI, and
  ``handle_utterance`` / ``start`` / ``active`` for the fast voice pipeline

The cards live in littlebot/screens/cards (quiz.py, word_game.py, game_board.py). The toddler touch games
(littlebot/games/*.py) are separate and unchanged. See docs/GAMES.md.
"""

from __future__ import annotations

from littlebot.games.voice.base import Reply, VoiceGame

__all__ = ["Reply", "VoiceGame"]

"""LittleBot's screen as part of the conversation: info cards it presents while it talks.

* ``base``      - the Card interface, the host cards use, the card-type registry
* ``cards``     - the built-in cards (weather, words, explanations, recipes, timers, clocks, maths, ...)
* ``service``   - ScreenService: one card at a time over the face, the presenter's eyes, when it goes away
* ``presenter`` - LittleBot's eyes, small, lip-synced, presenting the card
* ``images``    - pictures for cards, fetched when needed and never kept
* ``director``  - decides during a voice chat what to show for what was just asked

See docs/SCREENS.md.
"""

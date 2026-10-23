"""Live information LittleBot can answer with (used by ChatGPT's tools, littlebot/chatgpt/tools.py).

* ``weather`` - current conditions and the forecast from Open-Meteo (free, no key), for home or any place;
  home is the parent's choice on the dashboard, else approximate from the robot's internet connection
* ``clock``   - the local time, date and day of the week (or somewhere else's)

Anything else that changes (news, scores, opening hours) is found by ChatGPT's own web search
(``chatgpt.web_search``, littlebot/chatgpt/responses.py).
"""

from __future__ import annotations

from littlebot.info.clock import now_info
from littlebot.info.weather import Place, WeatherError, WeatherService, get_weather

__all__ = ["Place", "WeatherError", "WeatherService", "get_weather", "now_info"]

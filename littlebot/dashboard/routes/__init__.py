"""Dashboard API routers (all under /api; auth is enforced by the app middleware)."""

from __future__ import annotations

from littlebot.dashboard.routes.activity_routes import router as activities
from littlebot.dashboard.routes.ai_routes import router as ai
from littlebot.dashboard.routes.auth_routes import router as auth
from littlebot.dashboard.routes.browser_routes import router as browser
from littlebot.dashboard.routes.camera_routes import router as camera
from littlebot.dashboard.routes.chatgpt_routes import router as chatgpt
from littlebot.dashboard.routes.device_routes import router as devices
from littlebot.dashboard.routes.face_routes import router as face
from littlebot.dashboard.routes.games_routes import router as games
from littlebot.dashboard.routes.info_routes import router as info
from littlebot.dashboard.routes.media_routes import router as media
from littlebot.dashboard.routes.motion_routes import router as motion
from littlebot.dashboard.routes.people_routes import router as people
from littlebot.dashboard.routes.power_routes import router as power
from littlebot.dashboard.routes.privacy_routes import router as privacy
from littlebot.dashboard.routes.profile_routes import router as profile
from littlebot.dashboard.routes.robot_routes import router as robot
from littlebot.dashboard.routes.screens_routes import router as screens
from littlebot.dashboard.routes.sim_routes import router as sim
from littlebot.dashboard.routes.voice_routes import router as voice_lines

ROUTERS = (auth, robot, face, camera, activities, games, screens, browser, media, profile, people, ai, chatgpt, info,
           privacy, devices, motion, sim, voice_lines, power)

__all__ = ["ROUTERS"]

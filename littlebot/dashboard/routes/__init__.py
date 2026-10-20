"""Dashboard API routers (all under /api; auth is enforced by the app middleware)."""

from __future__ import annotations

from littlebot.dashboard.routes.activity_routes import router as activities
from littlebot.dashboard.routes.ai_routes import router as ai
from littlebot.dashboard.routes.auth_routes import router as auth
from littlebot.dashboard.routes.device_routes import router as devices
from littlebot.dashboard.routes.face_routes import router as face
from littlebot.dashboard.routes.media_routes import router as media
from littlebot.dashboard.routes.motion_routes import router as motion
from littlebot.dashboard.routes.privacy_routes import router as privacy
from littlebot.dashboard.routes.profile_routes import router as profile
from littlebot.dashboard.routes.robot_routes import router as robot
from littlebot.dashboard.routes.sim_routes import router as sim

ROUTERS = (auth, robot, face, activities, media, profile, ai, privacy, devices, motion, sim)

__all__ = ["ROUTERS"]

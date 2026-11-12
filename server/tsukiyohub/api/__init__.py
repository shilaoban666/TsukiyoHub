"""API 路由包。"""

from tsukiyohub.api import (
    routes_chat,
    routes_gateway,
    routes_knowledge,
    routes_memory,
    routes_sessions,
    routes_skills,
    routes_system,
)

__all__ = [
    "routes_chat", "routes_gateway", "routes_knowledge", "routes_memory",
    "routes_sessions", "routes_skills", "routes_system",
]
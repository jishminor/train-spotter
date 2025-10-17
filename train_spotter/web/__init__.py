"""Web package exposing dashboard application factory."""

from .app import create_app
from .streaming import FrameBroadcaster

__all__ = ["create_app", "FrameBroadcaster"]

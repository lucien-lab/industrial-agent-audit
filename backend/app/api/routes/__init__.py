"""Router 统一出口。"""

from app.api.routes import equipment, faults, health, quality, readings, stats

__all__ = ["equipment", "faults", "health", "quality", "readings", "stats"]

from src.api.routes.assign import router as assign_router
from src.api.routes.admin_agents import router as admin_router
from src.api.routes.admin_departments import router as admin_departments_router

__all__ = [
    "assign_router",
    "admin_router",
    "admin_departments_router",
]

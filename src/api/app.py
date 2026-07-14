from pathlib import Path

from fastapi import FastAPI
from dotenv import load_dotenv
from src.api.routes import assign_router, admin_router, admin_departments_router

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)

app = FastAPI(title="CMX Auto-Assign")
app.include_router(router=assign_router, tags=["auto-assign"])
app.include_router(router=admin_router, tags=["admin"])
app.include_router(router=admin_departments_router, tags=["admin"])

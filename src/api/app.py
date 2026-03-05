from fastapi import FastAPI
from src.api import assign_router

app = FastAPI(title="CMX Auto-Assign")
app.include_router(router=assign_router, prefix="/agent", tags=["agent"])

@app.get("/")
def root():
    return "hey"
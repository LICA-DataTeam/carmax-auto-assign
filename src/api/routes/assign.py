from fastapi import APIRouter

router = APIRouter()

@router.get("/assign", response_model=None)
async def assign():
    return "Hi"
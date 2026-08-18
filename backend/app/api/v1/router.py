from fastapi import APIRouter

from app.api.v1 import auth, cohort, fis, health, patients, scans

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(patients.router)
api_router.include_router(scans.router)
api_router.include_router(fis.router)
api_router.include_router(cohort.router)

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .routers import admin, auth, common, member
from .seed import seed


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed()  # creates tables + default admin/packages on first run
    yield


app = FastAPI(title="Golden Apiece MLM API", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(common.router)
app.include_router(member.router)
app.include_router(admin.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}

import os

from cryptography.fernet import Fernet

os.environ.update({
    "ENV": "test",
    "DATABASE_URL": os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/pm5_test"),
    "MASTER_KEY": Fernet.generate_key().decode(),
    "JWT_SECRET": "t" * 40,
    "ADMIN_PASSWORD": "correct horse battery staple",
    "ALLOW_LIVE_TRADING": "true",
    "PRIVATE_KEY": "",
    "MARKET_DATA_ENABLED": "false",
})

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.database.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services.bootstrap import seed  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
async def schema():
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.drop_all)
        await c.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        await seed(db)
    yield


@pytest.fixture(scope="session")
async def client():
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            yield c


@pytest.fixture(scope="session")
async def auth(client):
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "correct horse battery staple"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}

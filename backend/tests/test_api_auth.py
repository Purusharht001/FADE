from httpx import AsyncClient


class TestRegisterAndLogin:
    async def test_register_returns_user(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "a@b.com", "password": "supersecret1", "full_name": "A B"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["email"] == "a@b.com"
        assert body["role"] == "clinician"
        assert "hashed_password" not in body

    async def test_duplicate_registration_conflicts(self, client: AsyncClient):
        payload = {"email": "dup@b.com", "password": "supersecret1", "full_name": "Dup"}
        first = await client.post("/api/v1/auth/register", json=payload)
        second = await client.post("/api/v1/auth/register", json=payload)
        assert first.status_code == 201
        assert second.status_code == 409

    async def test_login_returns_tokens(self, client: AsyncClient):
        await client.post(
            "/api/v1/auth/register",
            json={"email": "c@d.com", "password": "supersecret1", "full_name": "C D"},
        )
        resp = await client.post(
            "/api/v1/auth/login", json={"email": "c@d.com", "password": "supersecret1"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["accessToken"]
        assert body["refreshToken"]
        assert body["tokenType"] == "bearer"

    async def test_login_with_wrong_password_is_unauthorized(self, client: AsyncClient):
        await client.post(
            "/api/v1/auth/register",
            json={"email": "e@f.com", "password": "supersecret1", "full_name": "E F"},
        )
        resp = await client.post(
            "/api/v1/auth/login", json={"email": "e@f.com", "password": "wrong-password"}
        )
        assert resp.status_code == 401

    async def test_me_requires_a_token(self, client: AsyncClient):
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code == 401

    async def test_me_returns_current_user_with_valid_token(self, client: AsyncClient):
        await client.post(
            "/api/v1/auth/register",
            json={"email": "g@h.com", "password": "supersecret1", "full_name": "G H"},
        )
        login = await client.post(
            "/api/v1/auth/login", json={"email": "g@h.com", "password": "supersecret1"}
        )
        token = login.json()["accessToken"]
        resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["email"] == "g@h.com"

    async def test_refresh_rejects_an_access_token(self, client: AsyncClient):
        await client.post(
            "/api/v1/auth/register",
            json={"email": "i@j.com", "password": "supersecret1", "full_name": "I J"},
        )
        login = await client.post(
            "/api/v1/auth/login", json={"email": "i@j.com", "password": "supersecret1"}
        )
        access_token = login.json()["accessToken"]
        resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": access_token})
        assert resp.status_code == 401

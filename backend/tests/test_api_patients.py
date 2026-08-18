import uuid

from httpx import AsyncClient

from tests.conftest import register_and_login


class TestCreateAndListPatients:
    async def test_create_patient_requires_auth(self, client: AsyncClient):
        resp = await client.post("/api/v1/patients", json={"age": 70, "sex": "F"})
        assert resp.status_code == 401

    async def test_create_and_fetch_patient(self, client: AsyncClient):
        headers = await register_and_login(client)
        resp = await client.post(
            "/api/v1/patients", json={"age": 70, "sex": "F", "source": "Clinic"}, headers=headers
        )
        assert resp.status_code == 201
        patient = resp.json()
        assert patient["displayId"].startswith("PT-")
        assert patient["scans"] == []

        fetched = await client.get(f"/api/v1/patients/{patient['id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["id"] == patient["id"]

    async def test_get_unknown_patient_is_404(self, client: AsyncClient):
        headers = await register_and_login(client)
        resp = await client.get(f"/api/v1/patients/{uuid.uuid4()}", headers=headers)
        assert resp.status_code == 404

    async def test_list_patients_sorted_by_uncertainty_by_default(self, client: AsyncClient):
        headers = await register_and_login(client)
        for severity in (0.0, 0.5, 1.0):
            p = (
                await client.post("/api/v1/patients", json={"age": 70, "sex": "F"}, headers=headers)
            ).json()
            await client.post(
                f"/api/v1/patients/{p['id']}/scans/synthetic",
                json={"severity": severity, "seed": 1},
                headers=headers,
            )

        resp = await client.get("/api/v1/patients", headers=headers)
        assert resp.status_code == 200
        items = resp.json()
        uncertainties = [i["latestScan"]["uncertainty"] for i in items]
        assert uncertainties == sorted(uncertainties, reverse=True)


class TestSyntheticScans:
    async def test_synthetic_scan_produces_a_completed_result(self, client: AsyncClient):
        headers = await register_and_login(client)
        patient = (
            await client.post("/api/v1/patients", json={"age": 75, "sex": "M"}, headers=headers)
        ).json()

        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/synthetic",
            json={"severity": 0.9, "seed": 5},
            headers=headers,
        )
        assert resp.status_code == 201
        scan = resp.json()
        assert scan["status"] == "completed"
        assert len(scan["biomarkers"]) == 3
        assert scan["fisResult"]["stage"] in ("CN", "MCI", "AD")

    async def test_synthetic_scan_rejects_out_of_range_severity(self, client: AsyncClient):
        headers = await register_and_login(client)
        patient = (
            await client.post("/api/v1/patients", json={"age": 75, "sex": "M"}, headers=headers)
        ).json()
        resp = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/synthetic",
            json={"severity": 1.5},
            headers=headers,
        )
        assert resp.status_code == 422

    async def test_review_toggles_reviewed_flag(self, client: AsyncClient):
        headers = await register_and_login(client)
        patient = (
            await client.post("/api/v1/patients", json={"age": 75, "sex": "M"}, headers=headers)
        ).json()
        scan = (
            await client.post(
                f"/api/v1/patients/{patient['id']}/scans/synthetic",
                json={"severity": 0.5, "seed": 5},
                headers=headers,
            )
        ).json()
        assert scan["fisResult"]["reviewed"] is False

        reviewed = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/{scan['id']}/review",
            json={"reviewed": True},
            headers=headers,
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["fisResult"]["reviewed"] is True

        unreviewed = await client.post(
            f"/api/v1/patients/{patient['id']}/scans/{scan['id']}/review",
            json={"reviewed": False},
            headers=headers,
        )
        assert unreviewed.json()["fisResult"]["reviewed"] is False


class TestCohortStats:
    async def test_cohort_stats_reflect_created_patients(self, client: AsyncClient):
        headers = await register_and_login(client)
        for severity in (0.05, 0.95):
            p = (
                await client.post("/api/v1/patients", json={"age": 70, "sex": "F"}, headers=headers)
            ).json()
            await client.post(
                f"/api/v1/patients/{p['id']}/scans/synthetic",
                json={"severity": severity, "seed": 3},
                headers=headers,
            )

        resp = await client.get("/api/v1/cohort/stats", headers=headers)
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["totalPatients"] == 2
        assert stats["totalScans"] == 2
        assert stats["byStage"]["CN"] + stats["byStage"]["MCI"] + stats["byStage"]["AD"] == 2


class TestFISEndpoints:
    async def test_simulate_matches_direct_engine_shape(self, client: AsyncClient):
        headers = await register_and_login(client)
        resp = await client.post(
            "/api/v1/fis/simulate",
            json={
                "hippocampal_volume": 4.5,
                "ventricle_brain_ratio": 0.1,
                "cortical_thickness": 6.0,
            },
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["stage"] == "CN"
        assert body["reviewed"] is False

    async def test_list_rules_returns_all_ten(self, client: AsyncClient):
        headers = await register_and_login(client)
        resp = await client.get("/api/v1/fis/rules", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 10

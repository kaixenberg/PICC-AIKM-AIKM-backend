"""API-layer tests for /manageBucket — repositories are monkeypatched, no DB needed."""

from unittest.mock import AsyncMock, patch

_BUCKET = {
    "id": "11111111-1111-1111-1111-111111111111",
    "bucket_name": "ops_runbooks",
    "bucket_category": "Operations",
    "status": "PROVISIONING",
    "error_detail": None,
    "created_by": "test-user",
    "updated_by": "test-user",
}


# ---------------------------------------------------------------------------
# GET /manageBucket/getDetails/{account_id}
# ---------------------------------------------------------------------------

def test_get_details_returns_list(client):
    with patch("src.repositories.bucket_repo.get_by_account", return_value=[_BUCKET]):
        resp = client.get("/manageBucket/getDetails/ACC123")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["bucket_name"] == "ops_runbooks"


def test_get_details_empty_list(client):
    with patch("src.repositories.bucket_repo.get_by_account", return_value=[]):
        resp = client.get("/manageBucket/getDetails/UNKNOWN")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_details_include_deleted_param(client):
    with patch("src.repositories.bucket_repo.get_by_account", return_value=[]) as mock_fn:
        client.get("/manageBucket/getDetails/ACC123?includeDeleted=true")
    mock_fn.assert_called_once_with("ACC123", True)


# ---------------------------------------------------------------------------
# POST /manageBucket/createBucket
# ---------------------------------------------------------------------------

def test_create_bucket_202(client):
    with patch("src.repositories.bucket_repo.create", return_value=_BUCKET), \
         patch("src.services.milvus_client.create_collection", new_callable=AsyncMock, return_value=(True, None)), \
         patch("src.repositories.bucket_repo.set_provision_result", return_value={**_BUCKET, "status": "ACTIVE"}):
        resp = client.post(
            "/manageBucket/createBucket",
            json={"account_id": "ACC1", "bucket_name": "ops_runbooks"},
            headers={"User": "test-user"},
        )
    assert resp.status_code == 202
    assert resp.json()["status"] == "PROVISIONING"


def test_create_bucket_invalid_name_422(client):
    resp = client.post(
        "/manageBucket/createBucket",
        json={"account_id": "ACC1", "bucket_name": "my-invalid-bucket"},
    )
    assert resp.status_code == 422


def test_create_bucket_missing_account_id_422(client):
    resp = client.post(
        "/manageBucket/createBucket",
        json={"bucket_name": "valid_name"},
    )
    assert resp.status_code == 422


def test_create_bucket_uses_user_header(client):
    with patch("src.repositories.bucket_repo.create", return_value=_BUCKET) as mock_fn, \
         patch("src.services.milvus_client.create_collection", new_callable=AsyncMock, return_value=(True, None)), \
         patch("src.repositories.bucket_repo.set_provision_result", return_value={**_BUCKET, "status": "ACTIVE"}):
        client.post(
            "/manageBucket/createBucket",
            json={"account_id": "ACC1", "bucket_name": "ops_runbooks"},
            headers={"User": "alice"},
        )
    _, call_user = mock_fn.call_args.args
    assert call_user == "alice"


def test_create_bucket_milvus_failure(client):
    """When Milvus collection creation fails, the bucket row must be set to FAILED."""
    with patch("src.repositories.bucket_repo.create", return_value=_BUCKET), \
         patch("src.services.milvus_client.create_collection", new_callable=AsyncMock, return_value=(False, "connection refused")), \
         patch("src.repositories.bucket_repo.set_provision_result", return_value={**_BUCKET, "status": "FAILED"}) as mock_provision:
        resp = client.post(
            "/manageBucket/createBucket",
            json={"account_id": "ACC1", "bucket_name": "ops_runbooks"},
            headers={"User": "test-user"},
        )
    assert resp.status_code == 202
    mock_provision.assert_called_once_with(_BUCKET["id"], "FAILED", "connection refused")


# ---------------------------------------------------------------------------
# PUT /manageBucket/updateBucket/{id}
# ---------------------------------------------------------------------------

def test_update_bucket_200(client):
    updated = {**_BUCKET, "status": "ACTIVE"}
    with patch("src.repositories.bucket_repo.update", return_value=updated):
        resp = client.put(
            f"/manageBucket/updateBucket/{_BUCKET['id']}",
            json={"status": "ACTIVE"},
            headers={"User": "test-user"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ACTIVE"


def test_update_bucket_404_when_not_found(client):
    with patch("src.repositories.bucket_repo.update", return_value=None):
        resp = client.put(
            "/manageBucket/updateBucket/nonexistent",
            json={"status": "ACTIVE"},
        )
    assert resp.status_code == 404


def test_update_bucket_invalid_status_422(client):
    resp = client.put(
        f"/manageBucket/updateBucket/{_BUCKET['id']}",
        json={"status": "UNKNOWN_STATUS"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /manageBucket/provisionCallback
# ---------------------------------------------------------------------------

def test_provision_callback_200(client):
    with patch("src.repositories.bucket_repo.set_provision_result", return_value={**_BUCKET, "status": "ACTIVE"}):
        resp = client.post(
            "/manageBucket/provisionCallback",
            json={"bucket_id": _BUCKET["id"], "status": "ACTIVE"},
        )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ACTIVE"


def test_provision_callback_failed_status(client):
    failed = {**_BUCKET, "status": "FAILED", "error_detail": "collection exists"}
    with patch("src.repositories.bucket_repo.set_provision_result", return_value=failed):
        resp = client.post(
            "/manageBucket/provisionCallback",
            json={"bucket_id": _BUCKET["id"], "status": "FAILED", "error_detail": "collection exists"},
        )
    assert resp.status_code == 200
    assert resp.json()["error_detail"] == "collection exists"


def test_provision_callback_invalid_status_422(client):
    resp = client.post(
        "/manageBucket/provisionCallback",
        json={"bucket_id": _BUCKET["id"], "status": "READY"},
    )
    assert resp.status_code == 422


def test_provision_callback_404_when_not_found(client):
    with patch("src.repositories.bucket_repo.set_provision_result", return_value=None):
        resp = client.post(
            "/manageBucket/provisionCallback",
            json={"bucket_id": "missing-id", "status": "ACTIVE"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /manageBucket/deleteBucket/{id}
# ---------------------------------------------------------------------------

def test_delete_bucket_200(client):
    deleted = {**_BUCKET, "status": "DELETED"}
    with patch("src.repositories.bucket_repo.delete", return_value=deleted), \
         patch("src.services.milvus_client.delete_collection", new_callable=AsyncMock, return_value=(True, None)):
        resp = client.delete(f"/manageBucket/deleteBucket/{_BUCKET['id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "DELETED"


def test_delete_bucket_404_when_not_found(client):
    with patch("src.repositories.bucket_repo.delete", return_value=None):
        resp = client.delete("/manageBucket/deleteBucket/missing")
    assert resp.status_code == 404

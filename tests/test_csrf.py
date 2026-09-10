import re

from conftest import login


def get_csrf_token(client, path):
    resp = client.get(path)
    match = re.search(rb'name="csrf_token" value="([^"]+)"', resp.data)
    assert match, f"no csrf token found in {path}"
    return match.group(1).decode()


def test_post_without_csrf_token_is_rejected(app_module, client):
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    login(client)  # login itself needs a token too; expect this to fail closed
    resp = client.post(
        "/admin/orders/new",
        data={"name": "No Token", "mode": "รถ", "lot": "111"},
    )
    assert resp.status_code == 400


def test_post_with_valid_csrf_token_succeeds(app_module, client):
    app_module.app.config["WTF_CSRF_ENABLED"] = True
    token = get_csrf_token(client, "/admin/login")
    client.post("/admin/login", data={"password": "test-admin-password", "csrf_token": token})

    token = get_csrf_token(client, "/admin/match")
    resp = client.post(
        "/admin/orders/new",
        data={"name": "With Token", "mode": "รถ", "lot": "111", "csrf_token": token},
    )
    assert resp.status_code == 302

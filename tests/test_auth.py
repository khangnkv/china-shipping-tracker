from conftest import login


def test_orders_page_redirects_when_logged_out(client):
    resp = client.get("/admin/orders")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def test_wrong_password_does_not_log_in(client):
    resp = login(client, password="wrong-password")
    assert b"Wrong password" in resp.data


def test_correct_password_logs_in(client):
    resp = login(client)
    assert resp.status_code == 200
    with client.session_transaction() as sess:
        assert sess.get("logged_in") is True


def test_login_rate_limited_after_repeated_attempts(client):
    for _ in range(5):
        login(client, password="wrong-password")
    resp = client.post("/admin/login", data={"password": "wrong-password"})
    assert resp.status_code == 429

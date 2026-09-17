"""The signed-in TestClient helpers the web tests rely on."""
from src.sqlite_db import connect
from src.web.app import create_app
from src.web.auth import SESSION_COOKIE
from tests.auth_helpers import signed_in_client
from tests.settings_helpers import configured_stores


def test_signed_in_client_has_a_session_the_app_resolves():
    app = create_app(stores=configured_stores(connect(":memory:")))
    client = signed_in_client(app)
    assert app.state.auth.has_password() is True
    assert app.state.auth.resolve(client.cookies[SESSION_COOKIE]) is not None


def test_two_signed_in_clients_on_one_app_get_separate_sessions():
    app = create_app(stores=configured_stores(connect(":memory:")))
    a, b = signed_in_client(app), signed_in_client(app, follow_redirects=False)
    assert a.cookies[SESSION_COOKIE] != b.cookies[SESSION_COOKIE]
    assert app.state.auth.active_session_count() == 2

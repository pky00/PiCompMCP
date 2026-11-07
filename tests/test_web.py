# picomp/web.py with a stub controller: the token cookie, the Origin and Host
# checks, the error mapping and the endpoint table (PLAN.md section 9). One test
# uses the real Controller to show its idle-only mutations come back as 409.
import pytest
from fastapi.testclient import TestClient

from picomp.controller import Controller, NotFound, Refused
from picomp.web import COOKIE_NAME, create_app

TOKEN = "test-token_0123456789"
BASE = "http://127.0.0.1:8765"
COOKIE = {"Cookie": COOKIE_NAME + "=" + TOKEN}
MUTATE = dict(COOKIE, Origin=BASE)
FOREIGN = "http://127.0.0.1:3000"
NOT_AUTHORIZED = {"detail": "not authorized: open the URL printed in the server console"}

STATUS = {
    "board": "connected",
    "board_detail": "COM7, firmware 0.1.0",
    "state": "idle",
    "replay": None,
    "recording": None,
    "message": "",
}

RECORDING = {
    "id": 1,
    "name": "Recording 2026-10-21 14:02:11",
    "created_at": "2026-10-21T11:02:11Z",
    "screen_w": 1920,
    "screen_h": 1080,
    "dpi": 120,
    "duration_ms": 8450,
    "shots_mode": "each_click",
    "steps": 342,
    "shots": 7,
}

# method, path, JSON body, and the 409 detail while not idle
MUTATIONS = [
    ("PATCH", "/api/recordings/1", {"name": "x"}, "not allowed while {}"),
    ("DELETE", "/api/recordings/1", None, "not allowed while {}"),
    ("PUT", "/api/settings", {"screenshots": "none"}, "not allowed while {}"),
    ("POST", "/api/recordings/1/replay", None, "cannot replay while {}"),
]

POSTS = [
    ("PATCH", "/api/recordings/1", {"name": "x"}),
    ("DELETE", "/api/recordings/1", None),
    ("PUT", "/api/settings", {"screenshots": "none"}),
    ("POST", "/api/recordings/1/replay", None),
    ("POST", "/api/replay/resume", None),
    ("POST", "/api/replay/stop", None),
]


class FakeController:
    # Answers like the real Controller for the calls the web layer makes.

    def __init__(self, state="idle"):
        self.state = state
        self.calls = []
        self.lifecycle = []
        self.settings = {"screenshots": "each_click"}

    def startup(self):
        self.lifecycle.append("startup")

    def shutdown(self):
        self.lifecycle.append("shutdown")

    def _require_idle(self):
        if self.state != "idle":
            raise Refused("not allowed while %s" % self.state)

    def _find(self, rec_id):
        if rec_id != RECORDING["id"]:
            raise NotFound("no recording %s" % rec_id)

    def status(self):
        return dict(STATUS, state=self.state)

    def list_recordings(self):
        return [dict(RECORDING)]

    def rename_recording(self, rec_id, name):
        self.calls.append(("rename", rec_id, name))
        self._require_idle()
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("name must be 1 to 100 characters")
        self._find(rec_id)
        return {"id": rec_id, "name": name}

    def delete_recording(self, rec_id):
        self.calls.append(("delete", rec_id))
        self._require_idle()
        self._find(rec_id)

    def start_replay(self, rec_id):
        self.calls.append(("replay", rec_id))
        self._find(rec_id)
        if self.state != "idle":
            raise Refused("cannot replay while %s" % self.state)
        return {"id": rec_id, "total": RECORDING["steps"]}

    def resume_replay(self):
        self.calls.append(("resume",))
        if self.state != "paused":
            raise Refused("nothing to resume")

    def stop_replay(self):
        self.calls.append(("stop",))

    def get_settings(self):
        return dict(self.settings)

    def set_settings(self, values):
        self.calls.append(("settings", values))
        self._require_idle()
        if values.get("screenshots") not in ("each_click", "start_end", "none"):
            raise ValueError("screenshots must be one of each_click, start_end, none")
        self.settings.update(values)
        return dict(self.settings)


def client_for(controller, port=8765):
    # No "with": the lifespan (controller startup/shutdown) does not run.
    return TestClient(create_app(controller, TOKEN, port), base_url="http://127.0.0.1:%d" % port)


@pytest.fixture
def fake():
    return FakeController()


@pytest.fixture
def client(fake):
    c = client_for(fake)
    yield c
    c.close()


# --- token cookie ---

@pytest.mark.parametrize("path", ["/", "/api/status", "/api/recordings", "/api/settings", "/api/nope"])
def test_get_without_cookie_is_401(client, path):
    r = client.get(path)
    assert r.status_code == 401
    assert r.json() == NOT_AUTHORIZED


def test_wrong_cookie_is_401(client):
    r = client.get("/api/status", headers={"Cookie": COOKIE_NAME + "=not-the-token"})
    assert r.status_code == 401


def test_auth_sets_the_cookie_and_redirects(client):
    r = client.get("/auth", params={"t": TOKEN}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    cookies = r.headers.get_list("set-cookie")
    assert len(cookies) == 1
    parts = [p.strip() for p in cookies[0].split(";")]
    assert parts[0] == COOKIE_NAME + "=" + TOKEN
    assert {"httponly", "samesite=strict", "path=/"} <= {p.lower() for p in parts[1:]}
    assert client.get("/api/status", headers={"Cookie": parts[0]}).status_code == 200


@pytest.mark.parametrize("params", [{"t": "wrong"}, {"t": ""}, {}])
def test_auth_with_a_bad_token_is_401_without_a_cookie(client, params):
    r = client.get("/auth", params=params, follow_redirects=False)
    assert r.status_code == 401
    assert r.json() == NOT_AUTHORIZED
    assert "set-cookie" not in r.headers


def test_index_is_served_uncached(client):
    r = client.get("/", headers=COOKIE)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert r.headers["cache-control"] == "no-store"
    assert r.text.lstrip().lower().startswith("<!doctype html>")


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_api_docs_are_disabled(client, path):
    assert client.get(path, headers=COOKIE).status_code == 404


# --- Origin ---

@pytest.mark.parametrize("origin", [FOREIGN, "http://localhost:3000", "https://127.0.0.1:8765",
                                    "http://evil.example", "null"])
def test_post_from_a_foreign_origin_is_403(client, fake, origin):
    r = client.post("/api/replay/stop", headers=dict(COOKIE, Origin=origin))
    assert r.status_code == 403
    assert r.json() == {"detail": "bad origin"}
    assert fake.calls == []


def test_post_without_origin_is_403(client, fake):
    r = client.post("/api/replay/stop", headers=COOKIE)
    assert r.status_code == 403
    assert fake.calls == []


@pytest.mark.parametrize("method, path, body", POSTS)
def test_every_mutation_checks_the_origin(client, fake, method, path, body):
    r = client.request(method, path, json=body, headers=dict(COOKIE, Origin=FOREIGN))
    assert r.status_code == 403
    assert fake.calls == []


def test_origin_is_checked_before_the_cookie(client):
    assert client.post("/api/replay/stop", headers={"Origin": FOREIGN}).status_code == 403


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8765", "http://localhost:8765"])
def test_post_from_our_own_origin_is_allowed(client, fake, origin):
    r = client.post("/api/replay/stop", headers=dict(COOKIE, Origin=origin))
    assert r.status_code == 204
    assert fake.calls == [("stop",)]


def test_get_from_another_origin_is_allowed(client):
    r = client.get("/api/status", headers=dict(COOKIE, Origin="http://evil.example"))
    assert r.status_code == 200


def test_allowed_origin_follows_the_port(fake):
    c = client_for(fake, 9000)
    try:
        assert c.post("/api/replay/stop", headers=dict(COOKIE, Origin=BASE)).status_code == 403
        r = c.post("/api/replay/stop", headers=dict(COOKIE, Origin="http://127.0.0.1:9000"))
        assert r.status_code == 204
    finally:
        c.close()


# --- Host ---

@pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", "127.0.0.1.evil.example",
                                  "192.168.1.5:8765"])
def test_foreign_host_is_400(client, host):
    r = client.get("/api/status", headers=dict(COOKIE, Host=host))
    assert r.status_code == 400


def test_foreign_host_cannot_get_the_cookie(client):
    r = client.get("/auth", params={"t": TOKEN}, headers={"Host": "evil.example"}, follow_redirects=False)
    assert r.status_code == 400
    assert "set-cookie" not in r.headers


def test_localhost_host_is_allowed(client):
    assert client.get("/api/status", headers=dict(COOKIE, Host="localhost:8765")).status_code == 200


# --- endpoints ---

def test_status(client):
    r = client.get("/api/status", headers=COOKIE)
    assert r.status_code == 200
    assert r.json() == STATUS


def test_list_recordings(client):
    r = client.get("/api/recordings", headers=COOKIE)
    assert r.status_code == 200
    assert r.json() == [RECORDING]


def test_rename(client, fake):
    r = client.patch("/api/recordings/1", json={"name": "  Login flow "}, headers=MUTATE)
    assert r.status_code == 200
    assert r.json() == {"id": 1, "name": "Login flow"}
    assert fake.calls == [("rename", 1, "  Login flow ")]


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_bad_rename_is_400(client, name):
    r = client.patch("/api/recordings/1", json={"name": name}, headers=MUTATE)
    assert r.status_code == 400
    assert r.json() == {"detail": "name must be 1 to 100 characters"}


def test_rename_of_a_missing_recording_is_404(client):
    r = client.patch("/api/recordings/99", json={"name": "x"}, headers=MUTATE)
    assert r.status_code == 404
    assert r.json() == {"detail": "no recording 99"}


@pytest.mark.parametrize("body", [{}, {"title": "x"}, {"name": None}])
def test_malformed_rename_body_is_422(client, fake, body):
    r = client.patch("/api/recordings/1", json=body, headers=MUTATE)
    assert r.status_code == 422
    assert fake.calls == []


def test_non_integer_id_is_422(client, fake):
    assert client.delete("/api/recordings/abc", headers=MUTATE).status_code == 422
    assert fake.calls == []


def test_delete(client, fake):
    r = client.delete("/api/recordings/1", headers=MUTATE)
    assert r.status_code == 204
    assert r.content == b""
    assert fake.calls == [("delete", 1)]


def test_delete_of_a_missing_recording_is_404(client):
    r = client.delete("/api/recordings/7", headers=MUTATE)
    assert r.status_code == 404
    assert r.json() == {"detail": "no recording 7"}


def test_replay_is_202(client, fake):
    r = client.post("/api/recordings/1/replay", headers=MUTATE)
    assert r.status_code == 202
    assert r.json() == {"id": 1, "total": 342}
    assert fake.calls == [("replay", 1)]


def test_resume_when_paused_is_204(client, fake):
    fake.state = "paused"
    r = client.post("/api/replay/resume", headers=MUTATE)
    assert r.status_code == 204
    assert fake.calls == [("resume",)]


def test_resume_when_not_paused_is_409(client):
    r = client.post("/api/replay/resume", headers=MUTATE)
    assert r.status_code == 409
    assert r.json() == {"detail": "nothing to resume"}


@pytest.mark.parametrize("state", ["idle", "recording", "saving", "replaying", "paused"])
def test_stop_is_always_204(client, fake, state):
    fake.state = state
    assert client.post("/api/replay/stop", headers=MUTATE).status_code == 204
    assert fake.calls == [("stop",)]


def test_get_settings(client):
    r = client.get("/api/settings", headers=COOKIE)
    assert r.status_code == 200
    assert r.json() == {"screenshots": "each_click"}


def test_put_settings(client, fake):
    r = client.put("/api/settings", json={"screenshots": "none"}, headers=MUTATE)
    assert r.status_code == 200
    assert r.json() == {"screenshots": "none"}
    assert fake.calls == [("settings", {"screenshots": "none"})]


def test_put_unknown_setting_is_400(client):
    r = client.put("/api/settings", json={"screenshots": "sometimes"}, headers=MUTATE)
    assert r.status_code == 400
    assert r.json() == {"detail": "screenshots must be one of each_click, start_end, none"}


def test_put_malformed_settings_is_422(client, fake):
    assert client.put("/api/settings", json={}, headers=MUTATE).status_code == 422
    assert fake.calls == []


# --- 409 while not idle ---

@pytest.mark.parametrize("state", ["recording", "saving", "replaying", "paused"])
@pytest.mark.parametrize("method, path, body, detail", MUTATIONS)
def test_mutation_while_not_idle_is_409(client, fake, state, method, path, body, detail):
    fake.state = state
    r = client.request(method, path, json=body, headers=MUTATE)
    assert r.status_code == 409
    assert r.json() == {"detail": detail.format(state)}


@pytest.mark.parametrize("method, path, body", [m[:3] for m in MUTATIONS[:3]])
def test_real_controller_refuses_mutations_while_recording(tmp_path, method, path, body):
    db_file = tmp_path / "picomp.db"
    controller = Controller(db_path=db_file)
    controller._state = "recording"
    c = client_for(controller)
    try:
        r = c.request(method, path, json=body, headers=MUTATE)
    finally:
        c.close()
    assert r.status_code == 409
    assert r.json() == {"detail": "not allowed while recording"}
    assert controller.status()["message"] == "not allowed while recording"
    assert not db_file.exists()


# --- lifespan ---

def test_lifespan_starts_and_stops_the_controller(fake):
    with TestClient(create_app(fake, TOKEN), base_url=BASE) as c:
        assert fake.lifecycle == ["startup"]
        assert c.get("/api/status", headers=COOKIE).status_code == 200
    assert fake.lifecycle == ["startup", "shutdown"]

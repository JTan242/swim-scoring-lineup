"""
Pytest test suite for the swim scoring Flask application.
"""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "test"

import pytest
from app import create_app
from extensions import db
from models import User, Team, Swimmer
from services.scoring import format_time, score_for, parse_time_to_seconds, INDIV_SCORE

TEST_PASSWORD = "Testpass1"


@pytest.fixture
def app():
    """Create a test Flask app with in-memory SQLite and CSRF disabled."""
    app = create_app(test_config={
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,
        "SQLALCHEMY_DATABASE_URI": "sqlite://",
        "SECRET_KEY": "test",
        "CACHE_TYPE": "NullCache",
    })
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def app_context(app):
    with app.app_context():
        yield


@pytest.fixture
def db_session(app, app_context):
    db.create_all()
    yield db
    db.session.remove()
    db.drop_all()


def _register(client, username="testuser", password=TEST_PASSWORD):
    return client.post(
        "/register",
        data={
            "username": username,
            "password": password,
            "confirm": password,
        },
        follow_redirects=False,
    )


def _login(client, username="testuser", password=TEST_PASSWORD):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


@pytest.fixture
def auth_client(client, db_session, app):
    with app.app_context():
        _register(client)
        _login(client)
    return client


# ─── App factory ──────────────────────────────────────────────────────────────

def test_create_app_returns_flask_app(app):
    assert app is not None
    assert "app" in app.name


def test_create_app_loads_config(app):
    assert app.config["SECRET_KEY"] == "test"
    assert "sqlite" in app.config["SQLALCHEMY_DATABASE_URI"]


# ─── Auth flow ────────────────────────────────────────────────────────────────

def test_register_new_user_redirects_to_login(client, db_session, app):
    with app.app_context():
        response = _register(client, "newuser")
    assert response.status_code == 302
    assert "/login" in response.location


def test_register_weak_password_rejected(client, db_session, app):
    with app.app_context():
        response = client.post(
            "/register",
            data={"username": "weakpw", "password": "abc", "confirm": "abc"},
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert b"at least 8 characters" in response.data


def test_register_password_mismatch_rejected(client, db_session, app):
    with app.app_context():
        response = client.post(
            "/register",
            data={
                "username": "mismatch",
                "password": TEST_PASSWORD,
                "confirm": "Differentpass1",
            },
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert b"Passwords must match" in response.data


def test_login_valid_credentials_redirects_to_scrape(client, db_session, app):
    with app.app_context():
        _register(client, "validuser")
        response = _login(client, "validuser")
    assert response.status_code == 302
    assert "/scrape" in response.location


def test_login_invalid_credentials_shows_error(client, db_session, app):
    with app.app_context():
        response = client.post(
            "/login",
            data={"username": "baduser", "password": "wrongpass"},
            follow_redirects=True,
        )
    assert response.status_code == 200
    assert b"Invalid credentials" in response.data


def test_logout_redirects_to_login(auth_client, app):
    with app.app_context():
        response = auth_client.get("/logout", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.location


def test_protected_route_redirects_when_unauthenticated(client, app):
    with app.app_context():
        for path in ["/scrape", "/select", "/logout"]:
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 302, f"Expected redirect for {path}"
            assert "/login" in response.location


# ─── Models ───────────────────────────────────────────────────────────────────

def test_user_set_password_and_check_password(db_session, app):
    with app.app_context():
        user = User(username="pwtest")
        user.set_password("mypassword")
        assert user.check_password("mypassword") is True
        assert user.check_password("wrongpassword") is False


def test_user_duplicate_username_fails(db_session, app):
    from sqlalchemy.exc import IntegrityError
    with app.app_context():
        db.session.add(User(username="duplicate", password_hash="x"))
        db.session.commit()
        db.session.add(User(username="duplicate", password_hash="y"))
        with pytest.raises(IntegrityError):
            db.session.commit()


# ─── Scoring logic ────────────────────────────────────────────────────────────

def test_format_time_minutes():
    assert format_time(61.5) == "1:01.50"


def test_format_time_seconds_only():
    assert format_time(25.3) == "0:25.30"


def test_score_for_first_place():
    assert score_for(INDIV_SCORE, 1) == 20


def test_score_for_out_of_range():
    assert score_for(INDIV_SCORE, 17) == 0


def test_parse_time_to_seconds_with_minutes():
    assert parse_time_to_seconds("1:45.23") == pytest.approx(105.23)


def test_parse_time_to_seconds_seconds_only():
    assert parse_time_to_seconds("51.77") == 51.77


# ─── Seed data ────────────────────────────────────────────────────────────────

def test_seed_creates_teams_and_swimmers(auth_client, db_session, app):
    with app.app_context():
        response = auth_client.post("/seed", follow_redirects=False)
    assert response.status_code == 302
    with app.app_context():
        teams = Team.query.all()
        swimmers = Swimmer.query.all()
    assert len(teams) >= 2
    assert len(swimmers) > 0


# ─── Dashboard ────────────────────────────────────────────────────────────────

def test_select_returns_200_when_authenticated(auth_client, app):
    with app.app_context():
        response = auth_client.get("/select")
    assert response.status_code == 200


# ─── REST API ─────────────────────────────────────────────────────────────────

def test_api_teams_unauthenticated_redirects(client, app):
    with app.app_context():
        response = client.get("/api/teams")
    assert response.status_code == 302


def test_api_teams_returns_json(auth_client, app):
    with app.app_context():
        response = auth_client.get("/api/teams")
    assert response.status_code == 200
    assert response.content_type.startswith("application/json")


def test_api_events_returns_json(auth_client, app):
    with app.app_context():
        response = auth_client.get("/api/events")
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)


def test_api_results_returns_json(auth_client, app):
    with app.app_context():
        response = auth_client.get("/api/results")
    assert response.status_code == 200
    data = response.get_json()
    assert isinstance(data, list)


def test_api_import_missing_fields(auth_client, app):
    with app.app_context():
        response = auth_client.post(
            "/api/import",
            json={"gender": "M"},
        )
    assert response.status_code == 400
    assert "required" in response.get_json()["error"]


def test_health_endpoint(client, app):
    with app.app_context():
        response = client.get("/health")
    assert response.status_code == 200
    data = response.get_json()
    assert data["status"] == "ok"


# ─── Data isolation ───────────────────────────────────────────────────────────

def test_data_isolation_between_users(client, db_session, app):
    """User A's seeded teams should not appear on User B's dashboard."""
    with app.app_context():
        # User A registers, logs in, seeds data
        _register(client, "userA")
        _login(client, "userA")
        client.post("/seed", follow_redirects=True)
        client.get("/logout")

        # User B registers, logs in — should see no teams
        _register(client, "userB")
        _login(client, "userB")
        response = client.get("/select", follow_redirects=True)
        assert b"Pitt" not in response.data
        assert b"Penn State" not in response.data


def test_remove_sole_user_deletes_data(client, db_session, app):
    """When only one user has a team-season, removing it deletes the data."""
    with app.app_context():
        _register(client)
        _login(client)
        client.post("/seed", follow_redirects=True)

        team = Team.query.filter_by(name="Pitt Panthers").first()
        assert team is not None
        tid = team.id

        response = client.post("/select", data={
            "remove_ts": "1",
            "teams": f"{tid}:2025",
        }, follow_redirects=True)
        assert b"Removed" in response.data

        # Data deleted from DB because no other user has it
        team = Team.query.filter_by(name="Pitt Panthers").first()
        assert team is None
        response = client.get("/select")
        assert b"Pitt" not in response.data


def test_remove_shared_team_preserves_data(client, db_session, app):
    """When multiple users share a team-season, removing only unlinks."""
    from models import user_team_seasons
    with app.app_context():
        # User A seeds data
        _register(client, "userA")
        _login(client, "userA")
        client.post("/seed", follow_redirects=True)
        client.get("/logout")

        # User B also links the same team-season
        _register(client, "userB")
        _login(client, "userB")
        team = Team.query.filter_by(name="Pitt Panthers").first()
        assert team is not None
        userB = User.query.filter_by(username="userB").first()
        db.session.execute(
            user_team_seasons.insert().values(
                user_id=userB.id, team_id=team.id, season_year=2025,
            )
        )
        db.session.commit()

        # User A removes the team
        client.get("/logout")
        _login(client, "userA")
        response = client.post("/select", data={
            "remove_ts": "1",
            "teams": f"{team.id}:2025",
        }, follow_redirects=True)
        assert b"Removed" in response.data

        # Data preserved because User B still has it
        team = Team.query.filter_by(name="Pitt Panthers").first()
        assert team is not None

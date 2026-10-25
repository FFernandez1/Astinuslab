from __future__ import annotations

from importlib.resources import files

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


def test_browser_session_upgrade_and_downgrade_preserve_existing_data(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    cfg = Config(str(files("astinuslab.migrations").joinpath("alembic.ini")))
    with engine.connect() as connection:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "a039_playground_artifact_ownership")
        connection.execute(
            text(
                "INSERT INTO cases (id, name, status, created_at) VALUES ('case-test', 'test', 'open', CURRENT_TIMESTAMP)"
            )
        )
        connection.commit()
        command.upgrade(cfg, "a041_admin_browser_sessions")
        assert inspect(connection).has_table("admin_browser_sessions")
        connection.execute(
            text(
                "INSERT INTO admin_browser_sessions (token_hash, admin_token_hash, created_at, expires_at) "
                "VALUES (:token, :admin, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"token": "a" * 64, "admin": "b" * 64},
        )
        connection.commit()
        command.downgrade(cfg, "a039_playground_artifact_ownership")
        assert not inspect(connection).has_table("admin_browser_sessions")
        assert connection.execute(text("SELECT name FROM cases WHERE id = 'case-test'")).scalar_one() == "test"
        connection.commit()
    engine.dispose()

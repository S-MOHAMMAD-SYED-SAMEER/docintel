"""The migration itself, not the schema it happens to produce."""

from alembic import command
from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, inspect, text


def test_single_head(alembic_config: AlembicConfig) -> None:
    """Two heads mean a merge is missing and `upgrade head` is ambiguous."""
    assert len(ScriptDirectory.from_config(alembic_config).get_heads()) == 1


def test_upgrade_stamps_the_head_revision(
    migrated_engine: Engine, alembic_config: AlembicConfig
) -> None:
    head = ScriptDirectory.from_config(alembic_config).get_current_head()

    with migrated_engine.connect() as connection:
        stamped = connection.execute(
            text("select version_num from alembic_version")
        ).scalar_one()

    assert stamped == head


def test_downgrade_removes_the_table_and_the_enum_type(
    database_url: str, alembic_config: AlembicConfig, migrated_engine: Engine
) -> None:
    command.downgrade(alembic_config, "base")

    engine = create_engine(database_url)
    try:
        assert "documents" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            remaining = connection.execute(
                text("select count(*) from pg_type where typname = 'document_status'")
            ).scalar_one()
        assert remaining == 0
    finally:
        engine.dispose()

    # Leave the database at head so the fixture's own teardown is a no-op path
    # it can still run cleanly.
    command.upgrade(alembic_config, "head")


def test_upgrade_is_repeatable_after_a_downgrade(
    database_url: str, alembic_config: AlembicConfig, migrated_engine: Engine
) -> None:
    """The enum type must not survive a downgrade, or this raises."""
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")

    engine = create_engine(database_url)
    try:
        assert "documents" in inspect(engine).get_table_names()
    finally:
        engine.dispose()

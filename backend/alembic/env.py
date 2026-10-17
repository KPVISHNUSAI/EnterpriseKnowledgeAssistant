"""Alembic environment. Migrations run as the schema owner (MIGRATION_DATABASE_URL),
never as the runtime app role."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.models import Base

config = context.config
if config.config_file_name is not None:
    # Keep loggers created before migrations run in-process (tests, startup hooks).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    override = config.attributes.get("database_url")
    if override:
        return str(override)
    settings = get_settings()
    return settings.migration_database_url or settings.database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

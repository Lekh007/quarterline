"""Alembic environment wired to Quarterline settings and ORM metadata (SPEC §9)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from quarterline.config import Settings
from quarterline.store.models import Base

# This is the Alembic Config object, which provides access to the values
# within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Quarterline ORM metadata: every SPEC §9 table.
target_metadata = Base.metadata

# URL precedence: an explicit ini override (set programmatically by
# `quarterline db upgrade --database-url` or tests) wins; otherwise the fresh
# Settings instance reads DATABASE_URL from the environment/.env.
settings = Settings()
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.database_url)


def _is_sqlite() -> bool:
    return (config.get_main_option("sqlalchemy.url") or "").startswith("sqlite")


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DBAPI connection)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with an Engine connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Note: no PRAGMA here — executing one via exec_driver_sql would open a
        # pysqlite transaction that swallows alembic's version stamp. FK
        # enforcement lives in quarterline.store.db (application engine).
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_is_sqlite(),
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

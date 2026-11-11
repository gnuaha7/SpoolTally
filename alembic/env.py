from sqlalchemy import engine_from_config, pool

from alembic import context
from spooltally.db.models import Base

config = context.config


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            _run(connection)
        return
    _run(connection)


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        transaction_per_migration=False,
    )
    context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

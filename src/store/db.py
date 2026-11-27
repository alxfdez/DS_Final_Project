from __future__ import annotations

from flask import Flask, current_app, g
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _database_url(database: str) -> str:
    if database == ":memory:":
        return "sqlite+pysqlite:///:memory:"
    return f"sqlite+pysqlite:///{database}"


def init_app(app: Flask) -> None:
    from . import models  # noqa: F401 - registers mapped classes

    database = app.config["DATABASE"]
    engine = create_engine(
        _database_url(database),
        connect_args={"check_same_thread": False},
    )
    app.extensions["store_engine"] = engine
    app.extensions["store_session_factory"] = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )
    Base.metadata.create_all(engine)
    _upgrade_orders_table(engine)
    app.teardown_appcontext(close_session)


# Columns added to ``orders`` after the first release, in the order they were
# introduced. ``create_all`` never alters an existing table, so databases
# created by an older version get them here. Every step is safe to re-run.
_ORDERS_ADDED_COLUMNS = {
    "fulfillment_method": "VARCHAR(30) NOT NULL DEFAULT 'standard'",
    "source": "VARCHAR(20) NOT NULL DEFAULT 'storefront'",
    "external_order_id": "VARCHAR(80)",
}


def _upgrade_orders_table(engine) -> None:
    from .models import ORDER_ORIGIN_UNIQUE_INDEX

    existing = {column["name"] for column in inspect(engine).get_columns("orders")}
    with engine.begin() as connection:
        for name, ddl in _ORDERS_ADDED_COLUMNS.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE orders ADD COLUMN {name} {ddl}"))
        # SQLite cannot add a constraint with ALTER TABLE; a unique index gives
        # the same guarantee and matches what create_all builds for new databases.
        connection.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {ORDER_ORIGIN_UNIQUE_INDEX} "
                "ON orders (source, external_order_id)"
            )
        )

def get_session() -> Session:
    if "db_session" not in g:
        factory = current_app.extensions["store_session_factory"]
        g.db_session = factory()
    return g.db_session


def close_session(_error: BaseException | None = None) -> None:
    session = g.pop("db_session", None)
    if session is not None:
        session.close()

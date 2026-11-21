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
    _ensure_order_fulfillment_column(engine)
    app.teardown_appcontext(close_session)

def _ensure_order_fulfillment_column(engine) -> None:
    columns = {column["name"] for column in inspect(engine).get_columns("orders")}
    if "fulfillment_method" in columns:
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE orders ADD COLUMN fulfillment_method "
                "VARCHAR(30) NOT NULL DEFAULT 'standard'"
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

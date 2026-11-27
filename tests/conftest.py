from pathlib import Path

import pytest
from sqlalchemy import select

from store import create_app
from store.db import get_session
from store.models import Product


@pytest.fixture(autouse=True)
def dispose_engines(monkeypatch):
    """Close the SQLite engine of every app a test creates (including the
    "restarted" apps in persistence tests), so no connection leaks between
    tests as ResourceWarning."""
    import store

    created = []
    real_init = store.init_database

    def tracking_init(app):
        real_init(app)
        created.append(app.extensions["store_engine"])

    monkeypatch.setattr(store, "init_database", tracking_init)
    yield
    for engine in created:
        engine.dispose()


@pytest.fixture()
def app(tmp_path: Path):
    db_path = tmp_path / "test.db"
    app = create_app({
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DATABASE": str(db_path),
        "STORE_NAME": "Test Store",
    })
    with app.app_context():
        db = get_session()
        db.add_all([
            Product(sku="TEST-1", name="First Product", description="One", price_cents=10000, stock_quantity=5, image_url="/static/images/nissan-versa-2021.svg", active=1),
            Product(sku="TEST-2", name="Second Product", description="Two", price_cents=25000, stock_quantity=2, image_url="/static/images/mazda-3-2020.svg", active=1),
        ])
        db.commit()
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def first_product_id(app):
    with app.app_context():
        return get_session().scalar(select(Product.id).where(Product.sku == "TEST-1"))

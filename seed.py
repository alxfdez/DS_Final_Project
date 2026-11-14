from pathlib import Path

from sqlalchemy import func, select

from store import create_app
from store.db import get_session
from store.models import Product

# Semiusados.com seed catalog: used cars. Prices are in MXN cents.
# Stock is low on purpose: each listing represents a few near-identical units.
PRODUCTS = [
    ("SEMI-0001", "Nissan Versa Advance 2021",
     "2021 · 38,400 km · Automática · Gasolina · CDMX. Sedán de un solo dueño, servicios en agencia y llantas nuevas. Ideal para ciudad.",
     22990000, 3, "/static/images/nissan-versa-2021.svg"),
    ("SEMI-0002", "Mazda 3 Hatchback i Sport 2020",
     "2020 · 45,300 km · Automática · Gasolina · Guadalajara, Jal. Pantalla de 8.8\", cámara de reversa y Apple CarPlay. Interior impecable.",
     28990000, 2, "/static/images/mazda-3-2020.svg"),
    ("SEMI-0003", "Volkswagen Jetta Comfortline 2019",
     "2019 · 61,200 km · Automática · Gasolina · Monterrey, NL. Motor 1.4 turbo, quemacocos y factura original.",
     25990000, 2, "/static/images/vw-jetta-2019.svg"),
    ("SEMI-0004", "Toyota Corolla Base 2022",
     "2022 · 22,800 km · CVT · Gasolina · Querétaro, Qro. Garantía de agencia vigente y Toyota Safety Sense.",
     33990000, 1, "/static/images/toyota-corolla-2022.svg"),
    ("SEMI-0005", "Honda CR-V Turbo Plus 2018",
     "2018 · 74,500 km · Automática · Gasolina · Zapopan, Jal. SUV familiar con asientos de piel, techo eléctrico y 5 plazas amplias.",
     37990000, 1, "/static/images/honda-crv-2018.svg"),
    ("SEMI-0006", "Kia Rio Hatchback EX 2021",
     "2021 · 33,900 km · Automática · Gasolina · Puebla, Pue. Bajo consumo de combustible, cámara de reversa y control crucero.",
     24990000, 2, "/static/images/kia-rio-2021.svg"),
    ("SEMI-0007", "Mazda CX-5 i Grand Touring 2021",
     "2021 · 29,700 km · Automática · Gasolina · CDMX. Head-up display, sonido Bose y sensores de punto ciego.",
     46990000, 1, "/static/images/mazda-cx5-2021.svg"),
    ("SEMI-0008", "Ford Ranger XLT 4x2 2019",
     "2019 · 88,100 km · Manual · Diésel · León, Gto. Pickup de trabajo con caja protegida, enganche y mantenimiento al día.",
     44990000, 1, "/static/images/ford-ranger-2019.svg"),
]


def seed() -> None:
    app = create_app()
    with app.app_context():
        db = get_session()
        count = db.scalar(select(func.count()).select_from(Product))
        if count:
            print(f"Database already contains {count} products; nothing changed.")
            return
        for sku, name, description, price, stock, image_url in PRODUCTS:
            db.add(Product(
                sku=sku,
                name=name,
                description=description,
                price_cents=price,
                stock_quantity=stock,
                image_url=image_url,
                active=1,
            ))
        db.commit()
        print(f"Seeded {len(PRODUCTS)} products.")


if __name__ == "__main__":
    seed()

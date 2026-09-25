from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = "sqlite:///./app.db"

engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


# Columnas añadidas después de la primera puesta en marcha. `create_all` solo
# crea tablas que no existen: en una Raspberry que ya lleva meses corriendo, la
# tabla `dispatch` está creada y una columna nueva NO aparecería, así que el
# servicio arrancaría y fallaría en la primera consulta.
#
# SQLite admite ALTER TABLE ADD COLUMN con un valor por defecto constante, que
# es todo lo que hace falta aquí. Cada entrada es idempotente: si la columna ya
# está, no se toca nada.
ADDED_COLUMNS = {
    # Revisión del despacho: se incrementa en cada escritura. Es el número con
    # el que simtra-bus-monitor sabe que la pantalla recargó el itinerario y
    # debe adoptarlo, y el que impide que una carga antigua del monitor pise
    # una recarga más nueva (ver services/bus_monitor.py).
    "dispatch": [("revision", "INTEGER NOT NULL DEFAULT 0")],
    # Cola de subida de trazas GPS (ver models.Gps). Las Raspberry que ya
    # tienen la tabla `gps` necesitan estas columnas sin recrear la base.
    "gps": [
        ("upload", "BOOLEAN NOT NULL DEFAULT 0"),
        ("timestamp_unix", "INTEGER"),
        ("upload_error", "VARCHAR"),
    ],
}


# Sentencia que se ejecuta UNA vez, en la misma transacción, cuando la columna
# indicada se acaba de agregar a una tabla existente.
#
# gps.upload: las filas anteriores a la migración NO se encolan. Son la traza
# sin filtrar de meses (una por segundo, detenido incluido), con timestamps sin
# zona de origen conocida, y subirlas de golpe inundaría device-api con puntos
# cuyo instante no se puede garantizar. Se conservan intactas; el README indica
# cómo reencolarlas a mano si se decide subirlas.
BACKFILL = {
    ("gps", "upload"): "UPDATE gps SET upload = 1",
}


# Índices que `create_all` no crea en tablas existentes. Idempotentes.
ADDED_INDEXES = [
    # La cola pendiente se consulta cada ciclo del loader; sin índice sería un
    # recorrido completo de una tabla que crece ~30 000 filas por jornada.
    "CREATE INDEX IF NOT EXISTS ix_gps_pending ON gps (upload, upload_error, id)",
]


def ensure_schema(bind=None) -> list[str]:
    """
    Aplica las columnas de ADDED_COLUMNS que falten. Devuelve las aplicadas.

    Se llama al arrancar, justo después de `create_all`. No borra ni renombra
    nada: solo agrega, que es la única operación de esquema segura de ejecutar
    automáticamente en el equipo de un bus.
    """
    bind = bind or engine
    applied = []
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    with bind.begin() as connection:
        for table, columns in ADDED_COLUMNS.items():
            if table not in existing_tables:
                continue   # la acaba de crear create_all, ya trae la columna
            present = {column["name"] for column in inspector.get_columns(table)}
            for name, definition in columns:
                if name in present:
                    continue
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))
                applied.append(f"{table}.{name}")
                backfill = BACKFILL.get((table, name))
                if backfill:
                    connection.execute(text(backfill))

        tables_now = set(inspect(connection).get_table_names())
        for statement in ADDED_INDEXES:
            table = statement.split(" ON ", 1)[1].split(" ", 1)[0]
            if table in tables_now:
                connection.execute(text(statement))

    return applied

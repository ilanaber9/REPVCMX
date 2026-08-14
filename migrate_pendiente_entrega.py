"""Migración única: agrega estado 'pendiente_entrega' a solicitudes y tipo/'ausente' a paradas,
preservando todos los datos existentes (puntos importados, rutas, paradas)."""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")

db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA foreign_keys = OFF")

db.executescript(
    """
    ALTER TABLE solicitudes RENAME TO solicitudes_old;
    ALTER TABLE paradas RENAME TO paradas_old;

    CREATE TABLE solicitudes (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      cliente_id INTEGER REFERENCES users(id),
      nombre_contacto TEXT,
      direccion TEXT NOT NULL,
      material TEXT NOT NULL,
      notas TEXT,
      lat REAL,
      lon REAL,
      zona TEXT,
      estado TEXT NOT NULL DEFAULT 'pendiente' CHECK(estado IN ('pendiente','pendiente_entrega','programada','recolectada','incidencia','cancelada')),
      created_at TEXT DEFAULT (datetime('now')),
      CHECK (cliente_id IS NOT NULL OR nombre_contacto IS NOT NULL)
    );

    CREATE TABLE paradas (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ruta_id INTEGER NOT NULL REFERENCES rutas(id),
      solicitud_id INTEGER NOT NULL REFERENCES solicitudes(id),
      orden INTEGER NOT NULL,
      tipo TEXT NOT NULL DEFAULT 'recoleccion' CHECK(tipo IN ('recoleccion','entrega')),
      estado TEXT NOT NULL DEFAULT 'pendiente' CHECK(estado IN ('pendiente','completada','incidencia','ausente')),
      notas TEXT
    );

    INSERT INTO solicitudes (id, cliente_id, nombre_contacto, direccion, material, notas, lat, lon, zona, estado, created_at)
    SELECT id, cliente_id, nombre_contacto, direccion, material, notas, lat, lon, zona, estado, created_at FROM solicitudes_old;

    INSERT INTO paradas (id, ruta_id, solicitud_id, orden, tipo, estado, notas)
    SELECT id, ruta_id, solicitud_id, orden, 'recoleccion', estado, notas FROM paradas_old;

    DROP TABLE solicitudes_old;
    DROP TABLE paradas_old;
    """
)
db.commit()

n_sol = db.execute("SELECT COUNT(*) FROM solicitudes").fetchone()[0]
n_par = db.execute("SELECT COUNT(*) FROM paradas").fetchone()[0]
print(f"Migración completa. solicitudes={n_sol} paradas={n_par}")
db.close()

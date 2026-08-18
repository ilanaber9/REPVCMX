"""Migración única: agrega la columna users.telefono, hace nullable a users.email
(los pacientes ahora se identifican por WhatsApp en vez de correo), y rellena el
teléfono de cada cliente existente a partir de su solicitud más reciente."""
import os
import re
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")

db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA foreign_keys = OFF")

db.executescript(
    """
    ALTER TABLE users RENAME TO users_old;

    CREATE TABLE users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      email TEXT UNIQUE,
      telefono TEXT,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL CHECK(role IN ('admin','recolector','cliente','nef')),
      edad INTEGER,
      tipo_maquina TEXT CHECK(tipo_maquina IN ('maquina','manual')),
      marca TEXT CHECK(marca IN ('baxter','pisa')),
      frecuencia_semana INTEGER,
      causa_enfermedad TEXT CHECK(causa_enfermedad IN ('diabetes','hipertension','autoinmune','desconocida')),
      material_recolectado_kg REAL NOT NULL DEFAULT 0,
      reset_token TEXT,
      reset_token_expira TEXT,
      email_verificado INTEGER NOT NULL DEFAULT 1,
      verificacion_token TEXT,
      perfil_completo INTEGER NOT NULL DEFAULT 0,
      alta_completa INTEGER NOT NULL DEFAULT 0,
      terminos_aceptados INTEGER NOT NULL DEFAULT 0,
      aviso_privacidad_aceptado INTEGER NOT NULL DEFAULT 0,
      recibir_info_nef INTEGER NOT NULL DEFAULT 0,
      es_admin_general INTEGER NOT NULL DEFAULT 0,
      created_at TEXT DEFAULT (datetime('now','localtime'))
    );

    INSERT INTO users (
      id, name, email, password_hash, role, edad, tipo_maquina, marca, frecuencia_semana,
      causa_enfermedad, material_recolectado_kg, reset_token, reset_token_expira,
      email_verificado, verificacion_token, perfil_completo, alta_completa,
      terminos_aceptados, aviso_privacidad_aceptado, recibir_info_nef, es_admin_general, created_at
    )
    SELECT
      id, name, email, password_hash, role, edad, tipo_maquina, marca, frecuencia_semana,
      causa_enfermedad, material_recolectado_kg, reset_token, reset_token_expira,
      email_verificado, verificacion_token, perfil_completo, alta_completa,
      terminos_aceptados, aviso_privacidad_aceptado, recibir_info_nef, es_admin_general, created_at
    FROM users_old;

    DROP TABLE users_old;
    """
)
db.commit()

# --- Backfill: para cada cliente, el teléfono de su solicitud más reciente ---
db.row_factory = sqlite3.Row
clientes = db.execute("SELECT id FROM users WHERE role = 'cliente'").fetchall()
rellenados = 0
for c in clientes:
    fila = db.execute(
        "SELECT telefono FROM solicitudes WHERE cliente_id = ? AND telefono IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (c["id"],),
    ).fetchone()
    if fila is None or not fila["telefono"]:
        continue
    digitos = re.sub(r"\D", "", fila["telefono"])
    if len(digitos) != 10:
        continue
    db.execute("UPDATE users SET telefono = ? WHERE id = ?", (digitos, c["id"]))
    rellenados += 1
db.commit()

# --- Colisiones: dos clientes que hayan quedado con el mismo teléfono backfilleado ---
colisiones = db.execute(
    "SELECT telefono, COUNT(*) AS n FROM users WHERE role = 'cliente' AND telefono IS NOT NULL "
    "GROUP BY telefono HAVING COUNT(*) > 1"
).fetchall()
for col in colisiones:
    filas = db.execute(
        "SELECT id, name, created_at FROM users WHERE role = 'cliente' AND telefono = ? ORDER BY created_at DESC",
        (col["telefono"],),
    ).fetchall()
    print(f"[colisión] teléfono {col['telefono']} repetido en {col['n']} cuentas: "
          + ", ".join(f"#{f['id']} {f['name']}" for f in filas))
    # Se queda con la cuenta más reciente; a las demás se les quita el teléfono para no romper el índice único.
    for f in filas[1:]:
        db.execute("UPDATE users SET telefono = NULL WHERE id = ?", (f["id"],))
        rellenados -= 1
db.commit()

db.execute("CREATE UNIQUE INDEX idx_users_telefono ON users(telefono)")
db.commit()

sin_telefono = db.execute(
    "SELECT id, name, email FROM users WHERE role = 'cliente' AND telefono IS NULL"
).fetchall()

n_clientes = db.execute("SELECT COUNT(*) FROM users WHERE role = 'cliente'").fetchone()[0]
print(f"Migración completa. {n_clientes} clientes en total, {rellenados} con teléfono rellenado.")
if sin_telefono:
    print(f"\n{len(sin_telefono)} cliente(s) SIN teléfono (no podrán iniciar sesión hasta que se les asigne uno a mano):")
    for f in sin_telefono:
        print(f"  #{f['id']} {f['name']} — correo anterior: {f['email']}")

db.close()

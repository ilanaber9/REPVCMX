"""Importa los puntos de recolección desde el HTML de Google My Maps / Leaflet exportado.

Uso:
    ./venv/bin/python import_routes.py /ruta/al/archivo.html
"""
import json
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")


def extraer_routes_data(html_path):
    with open(html_path, encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("const ROUTES_DATA"):
                start = line.index("= [") + 2
                end = line.rindex("];")
                return json.loads(line[start:end + 1])
    raise ValueError("No se encontró ROUTES_DATA en el archivo.")


def main():
    if len(sys.argv) != 2:
        print("Uso: python import_routes.py /ruta/al/archivo.html")
        sys.exit(1)

    html_path = sys.argv[1]
    data = extraer_routes_data(html_path)

    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    ya_importado = db.execute(
        "SELECT COUNT(*) c FROM solicitudes WHERE zona LIKE 'Ruta %'"
    ).fetchone()["c"]
    if ya_importado:
        respuesta = input(
            f"Ya hay {ya_importado} puntos importados previamente. ¿Importar de nuevo de todas formas? (s/N): "
        )
        if respuesta.strip().lower() != "s":
            print("Cancelado.")
            return

    total = 0
    for ruta in data:
        zona = f"Ruta {ruta['route']:02d} ({ruta['dist_km']} km)"
        for stop in ruta["stops"]:
            nombre = stop["name"].strip() or "Sin nombre"
            direccion = stop["address"].strip() or nombre
            db.execute(
                "INSERT INTO solicitudes (cliente_id, nombre_contacto, direccion, material, lat, lon, zona, estado) "
                "VALUES (NULL, ?, ?, 'PVC', ?, ?, ?, 'pendiente')",
                (nombre, direccion, stop["lat"], stop["lon"], zona),
            )
            total += 1
    db.commit()
    db.close()
    print(f"Importados {total} puntos en {len(data)} zonas.")


if __name__ == "__main__":
    main()

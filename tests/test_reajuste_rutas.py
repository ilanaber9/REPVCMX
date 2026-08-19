# -*- coding: utf-8 -*-
"""
Pruebas del algoritmo de armado/reajuste de rutas (dividir_puntos_por_duracion,
reequilibrar_rutas_zona) contra el comportamiento pedido:

  1. Ninguna ruta debe superar DURACION_MAXIMA_RUTA_MIN (7:30 hrs), salvo la excepción de
     "zona lejana" (9:30 hrs) cuando la ruta ya incluye un paciente a más de 60 km del depósito.
  2. Cada ruta debe llenarse lo más posible (maximizar paradas) antes de abrir la siguiente,
     en vez de repartir parejo entre varias rutas a medio llenar.
  3. Al agregar un paciente nuevo a una zona que ya tiene rutas planificadas (sin iniciar),
     el sistema debe reajustarlas para incluirlo sin romper el tope de tiempo, sin tocar rutas
     ya en curso/completadas, y fusionando la parada si es el mismo cliente/dirección.

Usa una base de datos temporal (nunca la real database.db) y llama a las funciones reales de
app.py. Necesita conexión a internet: usa el servidor público de OSRM para calcular tiempos de
manejo reales por calle, igual que la app en producción.

Ejecutar con:
    ./venv/bin/python3 -m unittest tests.test_reajuste_rutas -v
"""
import os
import sqlite3
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

_tmp_db_fd, _tmp_db_path = tempfile.mkstemp(prefix="test_rutas_", suffix=".db")
os.close(_tmp_db_fd)
os.remove(_tmp_db_path)  # que app.py la cree vacía (sin schema ni seed) al primer connect
os.environ["DATABASE_PATH"] = _tmp_db_path

import app as appmod  # noqa: E402  (import después de fijar DATABASE_PATH a propósito)

# app.py llama a init_db() al importarse (no solo bajo `if __name__ == "__main__"`), así que la
# BD temporal ya quedó creada con el schema real y los ~574 puntos de seed_solicitudes.json. Eso
# no estorba a estas pruebas: cada una usa su propio nombre de zona ("Zona Prueba A/B/C"), que no
# choca con las zonas de la semilla.


def nueva_db_conn():
    """Abre una conexión nueva a la BD de prueba (schema y datos ya cargados por init_db())."""
    conn = sqlite3.connect(_tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


import atexit  # noqa: E402


@atexit.register
def _limpiar_db_temporal():
    try:
        os.remove(_tmp_db_path)
    except OSError:
        pass


DEPOT = (appmod.DEPOT_LAT, appmod.DEPOT_LON)


def punto_cerca_deposito(indice, paso=0.012):
    """Genera coordenadas en una rejilla alrededor del depósito, separadas ~1.3 km entre sí
    (quedan todas dentro de la Zona Metropolitana del Valle de México, con calles reales que
    OSRM puede enrutar)."""
    fila = indice // 6
    col = indice % 6
    return appmod.DEPOT_LAT + (fila - 3) * paso, appmod.DEPOT_LON + (col - 3) * paso


def punto_lejano(offset_km=90):
    """Un punto a ~offset_km en línea recta del depósito, para probar la excepción de zona
    lejana (> DISTANCIA_ZONA_LEJANA_KM)."""
    grados = offset_km / 111.0
    return appmod.DEPOT_LAT + grados, appmod.DEPOT_LON


def insertar_solicitud(conn, lat, lon, nombre="Paciente Prueba", zona=None, estado="pendiente",
                        cliente_id=None, direccion=None):
    cur = conn.execute(
        "INSERT INTO solicitudes (cliente_id, nombre_contacto, direccion, material, lat, lon, "
        "zona, estado) VALUES (?, ?, ?, 'PVC rígido', ?, ?, ?, ?)",
        (cliente_id, None if cliente_id else nombre, direccion or f"Calle de prueba {lat:.4f},{lon:.4f}",
         lat, lon, zona, estado),
    )
    conn.commit()
    return cur.lastrowid


class TestDividirPuntosPorDuracion(unittest.TestCase):
    """Pruebas puras del algoritmo de armado de tandas (sin base de datos)."""

    def test_topes_no_se_exceden(self):
        puntos = [dict(id=i, lat=lat, lon=lon) for i, (lat, lon) in
                  (( i, punto_cerca_deposito(i)) for i in range(30))]
        grupos = appmod.dividir_puntos_por_duracion(puntos)

        ids_cubiertos = set()
        for grupo in grupos:
            for p in grupo:
                ids_cubiertos.add(p["id"])
            estimado = appmod.estimar_ruta(grupo)
            tope = appmod._tope_efectivo_grupo(
                grupo, appmod.DURACION_MAXIMA_RUTA_MIN, appmod.DURACION_MAXIMA_RUTA_LEJANA_MIN
            )
            if len(grupo) > 1:
                self.assertLessEqual(
                    estimado["minutos"], tope,
                    f"Una tanda de {len(grupo)} paradas se pasó del tope ({estimado['minutos']} > {tope} min)",
                )

        # cobertura completa: ningún paciente se pierde ni se duplica
        self.assertEqual(ids_cubiertos, {p["id"] for p in puntos})
        total_asignado = sum(len(g) for g in grupos)
        self.assertEqual(total_asignado, len(puntos))

    def test_rutas_se_llenan_antes_de_abrir_otra(self):
        """Con suficientes pacientes cercanos entre sí para llenar más de una ruta, ninguna
        tanda (salvo quizá la última) debe quedar muy por debajo del tope de tiempo o del
        mínimo de paradas — si quedara corta habiendo pacientes disponibles para llenarla más,
        el algoritmo estaría repartiendo parejo en vez de maximizar."""
        puntos = [dict(id=i, lat=lat, lon=lon) for i, (lat, lon) in
                  ((i, punto_cerca_deposito(i)) for i in range(36))]
        grupos = appmod.dividir_puntos_por_duracion(puntos)
        self.assertGreaterEqual(len(grupos), 2, "se esperaban al menos 2 rutas con 36 pacientes cercanos")

        for grupo in grupos[:-1]:
            estimado = appmod.estimar_ruta(grupo)
            tope = appmod._tope_efectivo_grupo(
                grupo, appmod.DURACION_MAXIMA_RUTA_MIN, appmod.DURACION_MAXIMA_RUTA_LEJANA_MIN
            )
            llena_de_tiempo = estimado["minutos"] >= tope * 0.75
            llena_de_paradas = len(grupo) >= appmod.MIN_PARADAS_POR_RUTA
            self.assertTrue(
                llena_de_tiempo or llena_de_paradas,
                f"Tanda con solo {len(grupo)} paradas y {estimado['minutos']} min "
                f"(tope {tope} min) parece quedarse corta pudiendo llenarse más",
            )

    def test_un_paciente_solo_ya_excede_el_tope(self):
        """Si un solo punto ya excede el tope por sí mismo (caso extremo), debe quedar solo en
        su propia tanda en vez de intentar combinarlo o descartarlo."""
        muy_lejos = dict(id=999, lat=appmod.DEPOT_LAT + 3.0, lon=appmod.DEPOT_LON)  # ~330 km
        cercano = dict(id=1, lat=punto_cerca_deposito(0)[0], lon=punto_cerca_deposito(0)[1])
        grupos = appmod.dividir_puntos_por_duracion([muy_lejos, cercano])
        tandas_con_el_lejano = [g for g in grupos if any(p["id"] == 999 for p in g)]
        self.assertEqual(len(tandas_con_el_lejano), 1)
        self.assertEqual(len(tandas_con_el_lejano[0]), 1, "el paciente lejano debía quedar solo en su tanda")

    def test_excepcion_zona_lejana_usa_tope_extendido(self):
        """_tope_efectivo_grupo: una tanda con un paciente a más de DISTANCIA_ZONA_LEJANA_KM
        debe usar el tope extendido (9:30 hrs); una tanda sin pacientes lejanos usa el normal."""
        lat_lejos, lon_lejos = punto_lejano(90)
        grupo_lejano = [dict(id=1, lat=lat_lejos, lon=lon_lejos)]
        grupo_normal = [dict(id=2, lat=punto_cerca_deposito(0)[0], lon=punto_cerca_deposito(0)[1])]

        tope_lejano = appmod._tope_efectivo_grupo(
            grupo_lejano, appmod.DURACION_MAXIMA_RUTA_MIN, appmod.DURACION_MAXIMA_RUTA_LEJANA_MIN
        )
        tope_normal = appmod._tope_efectivo_grupo(
            grupo_normal, appmod.DURACION_MAXIMA_RUTA_MIN, appmod.DURACION_MAXIMA_RUTA_LEJANA_MIN
        )
        self.assertEqual(tope_lejano, appmod.DURACION_MAXIMA_RUTA_LEJANA_MIN)
        self.assertEqual(tope_normal, appmod.DURACION_MAXIMA_RUTA_MIN)


class TestReequilibrarRutasZona(unittest.TestCase):
    """Pruebas de extremo a extremo contra una base de datos temporal: simulan una zona con
    rutas ya planificadas y agregan un paciente nuevo, tal como pasa cuando alguien se registra
    o pide recolección en una zona con cobertura."""

    def setUp(self):
        self.conn = nueva_db_conn()

    def tearDown(self):
        self.conn.close()

    def _crear_ruta_planificada(self, zona, ids_solicitudes, iniciada=False):
        cur = self.conn.execute(
            "INSERT INTO rutas (nombre, zona, fecha, hora_salida, estado, hora_inicio_real) "
            "VALUES (?, ?, '2026-08-25', '08:00', ?, ?)",
            (zona, zona, "en_curso" if iniciada else "planificada", "08:05" if iniciada else None),
        )
        ruta_id = cur.lastrowid
        for i, sid in enumerate(ids_solicitudes, start=1):
            self.conn.execute(
                "INSERT INTO paradas (ruta_id, solicitud_id, orden, tipo) VALUES (?, ?, ?, 'recoleccion')",
                (ruta_id, sid, i),
            )
            self.conn.execute("UPDATE solicitudes SET estado='programada', zona=? WHERE id=?", (zona, sid))
        self.conn.commit()
        return ruta_id

    def test_paciente_nuevo_queda_integrado_sin_exceder_tope(self):
        zona = "Zona Prueba A"
        ids = [insertar_solicitud(self.conn, *punto_cerca_deposito(i), zona=zona, estado="programada")
               for i in range(10)]
        self._crear_ruta_planificada(zona, ids)

        nuevo_id = insertar_solicitud(self.conn, *punto_cerca_deposito(10), zona=zona, estado="pendiente")

        appmod.reequilibrar_rutas_zona(self.conn, zona, nueva_solicitud_id=nuevo_id)

        paradas = self.conn.execute(
            "SELECT p.*, r.zona AS ruta_zona FROM paradas p JOIN rutas r ON r.id = p.ruta_id "
            "WHERE r.zona = ?", (zona,),
        ).fetchall()
        ids_en_paradas = {p["solicitud_id"] for p in paradas} | {
            p["solicitud_extra_id"] for p in paradas if p["solicitud_extra_id"]
        }
        self.assertIn(nuevo_id, ids_en_paradas, "el paciente nuevo no quedó integrado a ninguna ruta")

        estado_nuevo = self.conn.execute("SELECT estado FROM solicitudes WHERE id=?", (nuevo_id,)).fetchone()
        self.assertEqual(estado_nuevo["estado"], "programada")

        for ruta in self.conn.execute("SELECT * FROM rutas WHERE zona=?", (zona,)).fetchall():
            puntos = self.conn.execute(
                "SELECT s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id=p.solicitud_id "
                "WHERE p.ruta_id=?", (ruta["id"],),
            ).fetchall()
            estimado = appmod.estimar_ruta(puntos)
            if estimado and len(puntos) > 1:
                self.assertLessEqual(
                    estimado["minutos"], appmod.DURACION_MAXIMA_RUTA_LEJANA_MIN,
                    f"ruta {ruta['nombre']} quedó en {estimado['minutos']} min tras el reajuste",
                )

    def test_rutas_ya_iniciadas_no_se_tocan(self):
        zona = "Zona Prueba B"
        ids_en_curso = [insertar_solicitud(self.conn, *punto_cerca_deposito(i), zona=zona, estado="programada")
                        for i in range(3)]
        ruta_en_curso_id = self._crear_ruta_planificada(zona, ids_en_curso, iniciada=True)

        ids_planificada = [insertar_solicitud(self.conn, *punto_cerca_deposito(i + 10), zona=zona, estado="programada")
                            for i in range(4)]
        self._crear_ruta_planificada(zona, ids_planificada)

        nuevo_id = insertar_solicitud(self.conn, *punto_cerca_deposito(20), zona=zona, estado="pendiente")
        appmod.reequilibrar_rutas_zona(self.conn, zona, nueva_solicitud_id=nuevo_id)

        paradas_en_curso = self.conn.execute(
            "SELECT solicitud_id FROM paradas WHERE ruta_id=? ORDER BY orden", (ruta_en_curso_id,)
        ).fetchall()
        self.assertEqual([p["solicitud_id"] for p in paradas_en_curso], ids_en_curso,
                          "la ruta ya iniciada no debía modificarse")

        ruta_en_curso = self.conn.execute("SELECT * FROM rutas WHERE id=?", (ruta_en_curso_id,)).fetchone()
        self.assertEqual(ruta_en_curso["estado"], "en_curso")

        # el paciente nuevo sí debió integrarse a alguna de las rutas planificadas (no a la en curso)
        rutas_planificadas_ids = {r["id"] for r in self.conn.execute(
            "SELECT id FROM rutas WHERE zona=? AND estado='planificada'", (zona,)
        ).fetchall()}
        paradas_nuevo = self.conn.execute(
            "SELECT ruta_id FROM paradas WHERE solicitud_id=? OR solicitud_extra_id=?",
            (nuevo_id, nuevo_id),
        ).fetchall()
        self.assertTrue(paradas_nuevo, "el paciente nuevo no quedó en ninguna parada")
        for p in paradas_nuevo:
            self.assertIn(p["ruta_id"], rutas_planificadas_ids)
            self.assertNotEqual(p["ruta_id"], ruta_en_curso_id)

    def test_mismo_cliente_se_fusiona_en_vez_de_agregar_parada(self):
        """Si el paciente nuevo es el mismo cliente que ya tiene una parada programada (p. ej.
        pidió redistribución de cajas además de su recolección de PVC), debe fusionarse como
        solicitud_extra_id en la parada existente, no crear una parada aparte."""
        zona = "Zona Prueba C"
        lat, lon = punto_cerca_deposito(0)
        cur = self.conn.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES ('Cliente X', 'x@test.local', 'x', 'cliente')"
        )
        cliente_id = cur.lastrowid
        self.conn.commit()

        id_original = insertar_solicitud(self.conn, lat, lon, zona=zona, estado="programada", cliente_id=cliente_id)
        otros_ids = [insertar_solicitud(self.conn, *punto_cerca_deposito(i), zona=zona, estado="programada")
                     for i in range(1, 5)]
        self._crear_ruta_planificada(zona, [id_original] + otros_ids)

        id_extra = insertar_solicitud(self.conn, lat, lon, zona=zona, estado="pendiente", cliente_id=cliente_id)

        paradas_antes = self.conn.execute(
            "SELECT COUNT(*) AS n FROM paradas p JOIN rutas r ON r.id=p.ruta_id WHERE r.zona=?", (zona,)
        ).fetchone()["n"]

        appmod.reequilibrar_rutas_zona(self.conn, zona, nueva_solicitud_id=id_extra)

        paradas_despues = self.conn.execute(
            "SELECT * FROM paradas p JOIN rutas r ON r.id=p.ruta_id WHERE r.zona=?", (zona,)
        ).fetchall()
        self.assertEqual(len(paradas_despues), paradas_antes,
                          "debía fusionarse en la parada existente, no agregar una parada nueva")

        parada_fusionada = [p for p in paradas_despues if p["solicitud_id"] == id_original]
        self.assertEqual(len(parada_fusionada), 1)
        self.assertEqual(parada_fusionada[0]["solicitud_extra_id"], id_extra)


if __name__ == "__main__":
    unittest.main()

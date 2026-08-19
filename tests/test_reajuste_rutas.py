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
    """Abre una conexión nueva a la BD compartida de prueba (schema y datos ya cargados por
    init_db() al importar app.py). Solo la usan las pruebas que pasan por el servidor Flask real
    (get_db() lee la ruta fija DATABASE_PATH), donde no hay forma de aislar por test."""
    conn = sqlite3.connect(_tmp_db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


_temp_files_aislados = []


def nueva_db_aislada():
    """Crea una base de datos temporal propia (schema real, sin datos) para un solo test,
    completamente aislada de las demás. Necesario desde que existe la fusión con ruta vecina
    (fusionar_grupo_pequeno_con_ruta_vecina): busca entre TODAS las rutas planificadas sin
    filtrar por zona, así que si varias pruebas compartieran la misma BD, una podría acabar
    fusionándose por accidente con sobrantes de otra prueba anterior."""
    fd, path = tempfile.mkstemp(prefix="test_rutas_aislada_", suffix=".db")
    os.close(fd)
    os.remove(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    with open(appmod.SCHEMA_PATH) as f:
        conn.executescript(f.read())
    _temp_files_aislados.append(path)
    return conn


import atexit  # noqa: E402


@atexit.register
def _limpiar_db_temporal():
    for path in [_tmp_db_path] + _temp_files_aislados:
        try:
            os.remove(path)
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


def punto_en_cluster_denso(indice, ancho=10, paso=0.006, centro=None):
    """Rejilla más densa (~650 m entre puntos) que punto_cerca_deposito, para simular una zona
    real con muchos pacientes concentrados en una colonia y forzar que haga falta más de una
    ruta."""
    centro_lat, centro_lon = centro or (appmod.DEPOT_LAT + 0.05, appmod.DEPOT_LON + 0.05)
    fila = indice // ancho
    col = indice % ancho
    return centro_lat + (fila - ancho / 2) * paso, centro_lon + (col - ancho / 2) * paso


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
        self.conn = nueva_db_aislada()

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

    def test_paciente_nuevo_provoca_mover_a_otros_a_la_ruta_que_les_toca(self):
        """Caso central del pedido: mientras se arman las rutas de una zona pueden llegar
        inscripciones nuevas. Si las rutas ya planificadas quedaron mal agrupadas (p. ej. mezcladas
        entre dos colonias separadas, como podría pasar si se crearon a mano o en otro momento) y
        llega un paciente nuevo, reequilibrar_rutas_zona no debe limitarse a acomodar solo al
        nuevo donde quepa: tiene que recalcular TODA la zona, así que cualquier paciente que no
        quedó agrupado por cercanía se mueve a la ruta que sí le corresponde geográficamente."""
        zona = "Zona Prueba D"
        centro_a = (appmod.DEPOT_LAT + 0.05, appmod.DEPOT_LON + 0.09)
        centro_b = (appmod.DEPOT_LAT + 0.05, appmod.DEPOT_LON - 0.09)  # ~19 km de la colonia A

        cluster_de = {}
        ids_a, ids_b = [], []
        for i in range(20):
            lat, lon = punto_en_cluster_denso(i, ancho=5, paso=0.006, centro=centro_a)
            sid = insertar_solicitud(self.conn, lat, lon, nombre=f"Colonia A {i}", zona=zona, estado="programada")
            ids_a.append(sid)
            cluster_de[sid] = "A"
        for i in range(20):
            lat, lon = punto_en_cluster_denso(i, ancho=5, paso=0.006, centro=centro_b)
            sid = insertar_solicitud(self.conn, lat, lon, nombre=f"Colonia B {i}", zona=zona, estado="programada")
            ids_b.append(sid)
            cluster_de[sid] = "B"

        # Rutas iniciales deliberadamente mal agrupadas: cada una mezcla mitad de la colonia A
        # con mitad de la colonia B, como si nunca se hubieran ordenado por cercanía.
        self._crear_ruta_planificada(zona, ids_a[:10] + ids_b[:10])
        self._crear_ruta_planificada(zona, ids_a[10:] + ids_b[10:])

        lat_nuevo, lon_nuevo = punto_en_cluster_denso(20, ancho=5, paso=0.006, centro=centro_a)
        nuevo_id = insertar_solicitud(self.conn, lat_nuevo, lon_nuevo, nombre="Colonia A nuevo",
                                       zona=zona, estado="pendiente")
        cluster_de[nuevo_id] = "A"

        id_max_antes = self.conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM rutas").fetchone()["n"]
        appmod.reequilibrar_rutas_zona(self.conn, zona, nueva_solicitud_id=nuevo_id)

        # Las rutas 2+ que resultan del reequilibrio se renombran "Ruta NN (km)" y ese mismo
        # nombre se les pone también como zona (ver reequilibrar_rutas_zona) — así que solo la
        # primera conserva zona == zona original. Para traerlas todas, comparamos por id.
        rutas = self.conn.execute("SELECT * FROM rutas WHERE id > ?", (id_max_antes,)).fetchall()
        self.assertGreaterEqual(len(rutas), 2, "41 pacientes en dos colonias separadas deberían necesitar 2+ rutas")

        ids_en_paradas = set()
        print(f"\n[Reequilibrio con colonias mezcladas] -> {len(rutas)} ruta(s) tras integrar al nuevo:")
        for ruta in rutas:
            paradas = self.conn.execute(
                "SELECT solicitud_id FROM paradas WHERE ruta_id=?", (ruta["id"],)
            ).fetchall()
            colonias_por_parada = [cluster_de[p["solicitud_id"]] for p in paradas]
            ids_en_paradas.update(p["solicitud_id"] for p in paradas)
            conteo = {c: colonias_por_parada.count(c) for c in set(colonias_por_parada)}
            print(f"  - {ruta['nombre']}: {len(paradas)} paradas, colonias {conteo}")
            # Tolera cuando mucho 1 paciente "frontera" de la colonia minoritaria por ruta (el
            # vecino más cercano puede caer justo en el límite de tiempo entre una colonia y la
            # otra); lo que no debe pasar es que una ruta quede genuinamente mitad y mitad.
            minoria = min(conteo.values()) if len(conteo) > 1 else 0
            self.assertLessEqual(
                minoria, 1,
                f"{ruta['nombre']} quedó mezclando colonias casi parejo {conteo} — no se reagrupó por cercanía",
            )

        # nadie se perdió y el paciente nuevo quedó incluido
        self.assertEqual(ids_en_paradas, set(cluster_de.keys()))
        self.assertIn(nuevo_id, ids_en_paradas)

        # como las rutas originales mezclaban A y B a propósito, para que ahora cada ruta sea de
        # una sola colonia, forzosamente varios pacientes tuvieron que cambiar de ruta.
        ids_originales_ruta1 = set(ids_a[:10] + ids_b[:10])
        rutas_finales_por_id = {}
        for ruta in rutas:
            for p in self.conn.execute("SELECT solicitud_id FROM paradas WHERE ruta_id=?", (ruta["id"],)):
                rutas_finales_por_id[p["solicitud_id"]] = ruta["id"]
        rutas_distintas_dentro_del_grupo_original = len(
            {rutas_finales_por_id[sid] for sid in ids_originales_ruta1}
        )
        self.assertGreater(
            rutas_distintas_dentro_del_grupo_original, 1,
            "los pacientes que antes compartían ruta (mal agrupados) deberían haberse separado "
            "en rutas distintas al reagruparse por colonia",
        )


class _BaseFlaskAislado(unittest.TestCase):
    """Base para pruebas que necesitan pasar por el servidor Flask real (test_client), no solo
    llamar las funciones directo. get_db() lee la ruta fija appmod.DB_PATH, así que cada test la
    apunta a su propia base temporal aislada (con su propio admin y recolector) para no
    contaminarse con rutas que hayan quedado de otra prueba — importante desde que existe la
    fusión con ruta vecina, que busca entre TODAS las rutas planificadas de la base."""

    def setUp(self):
        fd, path = tempfile.mkstemp(prefix="test_rutas_flask_", suffix=".db")
        os.close(fd)
        os.remove(path)
        self._db_path_original = appmod.DB_PATH
        appmod.DB_PATH = path
        _temp_files_aislados.append(path)

        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        with open(appmod.SCHEMA_PATH) as f:
            self.conn.executescript(f.read())
        self.conn.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES "
            "('Administrador', 'admin@rutas.local', ?, 'admin')",
            (appmod.generate_password_hash("admin123", method="pbkdf2:sha256"),),
        )
        cur = self.conn.execute(
            "INSERT INTO users (name, email, password_hash, role) VALUES "
            "('Recolector Test', 'recolector.test@rutas.local', ?, 'recolector')",
            (appmod.generate_password_hash("x", method="pbkdf2:sha256"),),
        )
        self.recolector_id = cur.lastrowid
        self.conn.commit()

        self.client = appmod.app.test_client()
        resp = self.client.post(
            "/login", data={"email": "admin@rutas.local", "password": "admin123"}, follow_redirects=False
        )
        self.assertEqual(resp.status_code, 302, "no se pudo iniciar sesión como admin para la prueba")

    def tearDown(self):
        self.conn.close()
        appmod.DB_PATH = self._db_path_original


class TestZonaConMuchosPacientes(_BaseFlaskAislado):
    """Simula el caso real: una zona con muchísimos pacientes pendientes, todos concentrados en
    la misma colonia, y usa el flujo real de "Crear rutas por zona" (POST a
    /admin/rutas/masivas, el mismo botón que usa el admin) para verificar que se resuelve
    abriendo varias rutas por cercanía en vez de una sola gigante o de repartir parejo."""

    N_PACIENTES = 80
    ZONA = "Zona Masiva Test"

    def setUp(self):
        super().setUp()
        for i in range(self.N_PACIENTES):
            lat, lon = punto_en_cluster_denso(i)
            insertar_solicitud(self.conn, lat, lon, nombre=f"Paciente masivo {i}", zona=self.ZONA)

    def test_zona_densa_se_reparte_en_varias_rutas_maximizadas(self):
        resp = self.client.post(
            "/admin/rutas/masivas",
            data={
                "zonas": [self.ZONA],
                "fecha": "2026-08-25",
                "hora_salida": "08:00",
                f"recolector_id__{self.ZONA}": str(self.recolector_id),
            },
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 302, "el POST a /admin/rutas/masivas no redirigió (¿falló?)")

        rutas = self.conn.execute(
            "SELECT * FROM rutas WHERE zona = ? OR nombre = ? ORDER BY id", (self.ZONA, self.ZONA)
        ).fetchall()
        # Las tandas 2, 3... se renombran "Ruta NN (km)" y también quedan con zona propia (ver
        # admin_rutas_masivas), así que hay que traerlas todas por el rango de ids de esta corrida.
        primera = self.conn.execute("SELECT MIN(id) AS n FROM rutas WHERE zona = ?", (self.ZONA,)).fetchone()["n"]
        todas_las_rutas = self.conn.execute("SELECT * FROM rutas WHERE id >= ? ORDER BY id", (primera,)).fetchall()

        print(f"\n[Zona con {self.N_PACIENTES} pacientes] -> {len(todas_las_rutas)} ruta(s) generada(s):")
        total_paradas = 0
        for ruta in todas_las_rutas:
            paradas = self.conn.execute(
                "SELECT s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
                "WHERE p.ruta_id = ?", (ruta["id"],),
            ).fetchall()
            total_paradas += len(paradas)
            estimado = appmod.estimar_ruta(paradas)
            print(f"  - {ruta['nombre']}: {len(paradas)} paradas, {estimado['duracion'] if estimado else '?'}")

            self.assertGreaterEqual(
                len(paradas), 1, f"la ruta {ruta['nombre']} se creó sin paradas"
            )
            if estimado and len(paradas) > 1:
                self.assertLessEqual(
                    estimado["minutos"], appmod.DURACION_MAXIMA_RUTA_MIN,
                    f"{ruta['nombre']} se pasó de las 7:30 hrs ({estimado['duracion']})",
                )

        # cobertura completa: todos los pacientes de la zona quedaron programados en alguna ruta
        self.assertEqual(total_paradas, self.N_PACIENTES)

        # se abrieron varias rutas en vez de una sola o de una por paciente
        self.assertGreater(len(todas_las_rutas), 1, "80 pacientes cercanos deberían requerir más de 1 ruta")
        self.assertLess(len(todas_las_rutas), self.N_PACIENTES // appmod.MIN_PARADAS_POR_RUTA + 3,
                         "salieron demasiadas rutas para la cantidad de pacientes: no se está maximizando cada una")

        # cada ruta —salvo quizá la última— debe quedar bien llena de tiempo o de paradas; si
        # varias tandas quedan muy por debajo del tope habiendo pacientes cercanos disponibles,
        # es señal de que no se está agrupando por cercanía antes de dividir en tandas.
        for ruta in todas_las_rutas[:-1]:
            paradas = self.conn.execute(
                "SELECT s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
                "WHERE p.ruta_id = ?", (ruta["id"],),
            ).fetchall()
            estimado = appmod.estimar_ruta(paradas)
            llena_de_tiempo = estimado and estimado["minutos"] >= appmod.DURACION_MAXIMA_RUTA_MIN * 0.75
            llena_de_paradas = len(paradas) >= appmod.MIN_PARADAS_POR_RUTA
            self.assertTrue(
                llena_de_tiempo or llena_de_paradas,
                f"{ruta['nombre']} quedó con solo {len(paradas)} paradas "
                f"({estimado['duracion'] if estimado else '?'}) pudiendo llenarse más",
            )


class TestMinimoDeDespacho(_BaseFlaskAislado):
    """No debe crearse (ni despacharse) una ruta de muy pocos pacientes —MIN_PARADAS_DESPACHO,
    hoy 8— porque no es rentable mandar la camioneta un día entero por eso. Cubre las 3 salidas
    posibles cuando una tanda queda por debajo del mínimo al crear rutas desde cero: se fusiona
    con la ruta planificada más cercana, se queda pendiente si no hay ninguna cerca, o —si es un
    paciente aislado y lejano que nunca va a juntar el mínimo por sí solo— se avisa al admin en
    vez de dejarlo esperando en silencio."""

    def test_grupo_chico_se_fusiona_con_ruta_vecina_con_espacio(self):
        centro = (appmod.DEPOT_LAT + 0.03, appmod.DEPOT_LON + 0.03)
        ids_ruta_existente = []
        for i in range(6):
            lat, lon = punto_en_cluster_denso(i, ancho=3, paso=0.004, centro=centro)
            sid = insertar_solicitud(self.conn, lat, lon, nombre=f"Ya en ruta {i}", zona="Zona Vecina", estado="programada")
            ids_ruta_existente.append(sid)
        cur = self.conn.execute(
            "INSERT INTO rutas (nombre, zona, fecha, hora_salida, recolector_id, estado) "
            "VALUES ('Zona Vecina', 'Zona Vecina', '2026-08-25', '08:00', ?, 'planificada')",
            (self.recolector_id,),
        )
        ruta_vecina_id = cur.lastrowid
        for i, sid in enumerate(ids_ruta_existente, start=1):
            self.conn.execute(
                "INSERT INTO paradas (ruta_id, solicitud_id, orden, tipo) VALUES (?, ?, ?, 'recoleccion')",
                (ruta_vecina_id, sid, i),
            )
        self.conn.commit()

        zona_chica = "Zona Chica"
        ids_chicos = []
        for i in range(4):
            lat, lon = punto_en_cluster_denso(i, ancho=2, paso=0.004, centro=centro)
            sid = insertar_solicitud(self.conn, lat, lon, nombre=f"Grupo chico {i}", zona=zona_chica)
            ids_chicos.append(sid)

        resp = self.client.post(
            "/admin/rutas/masivas",
            data={
                "zonas": [zona_chica],
                "fecha": "2026-08-25",
                "hora_salida": "08:00",
                f"recolector_id__{zona_chica}": str(self.recolector_id),
            },
        )
        self.assertEqual(resp.status_code, 302)

        rutas_nuevas_de_zona_chica = self.conn.execute(
            "SELECT * FROM rutas WHERE zona = ?", (zona_chica,)
        ).fetchall()
        self.assertEqual(len(rutas_nuevas_de_zona_chica), 0,
                          "no debía crearse una ruta aparte para un grupo de 4 (por debajo del mínimo)")

        paradas_vecina = self.conn.execute(
            "SELECT solicitud_id FROM paradas WHERE ruta_id = ?", (ruta_vecina_id,)
        ).fetchall()
        ids_en_vecina = {p["solicitud_id"] for p in paradas_vecina}
        self.assertTrue(
            set(ids_chicos).issubset(ids_en_vecina),
            "los 4 pacientes del grupo chico debían integrarse a la ruta vecina existente",
        )
        for sid in ids_chicos:
            estado = self.conn.execute("SELECT estado FROM solicitudes WHERE id=?", (sid,)).fetchone()["estado"]
            self.assertEqual(estado, "programada")

    def test_grupo_chico_sin_vecino_queda_pendiente(self):
        zona_chica = "Zona Chica Sin Vecinos"
        centro = (appmod.DEPOT_LAT - 0.03, appmod.DEPOT_LON - 0.03)
        ids_chicos = []
        for i in range(3):
            lat, lon = punto_en_cluster_denso(i, ancho=2, paso=0.004, centro=centro)
            sid = insertar_solicitud(self.conn, lat, lon, nombre=f"Sin vecino {i}", zona=zona_chica)
            ids_chicos.append(sid)

        resp = self.client.post(
            "/admin/rutas/masivas",
            data={
                "zonas": [zona_chica],
                "fecha": "2026-08-25",
                "hora_salida": "08:00",
                f"recolector_id__{zona_chica}": str(self.recolector_id),
            },
        )
        self.assertEqual(resp.status_code, 302)

        rutas_creadas = self.conn.execute("SELECT * FROM rutas WHERE zona = ?", (zona_chica,)).fetchall()
        self.assertEqual(len(rutas_creadas), 0, "no debía crearse ruta para un grupo de 3 sin ninguna vecina")

        for sid in ids_chicos:
            estado = self.conn.execute("SELECT estado FROM solicitudes WHERE id=?", (sid,)).fetchone()["estado"]
            self.assertEqual(estado, "pendiente", "debían quedarse pendientes para la siguiente corrida")

    def test_paciente_aislado_lejano_avisa_al_admin_en_vez_de_quedar_en_silencio(self):
        zona_lejana = "Zona Lejana Sola"
        lat, lon = punto_lejano(300)  # a velocidad de carretera real (no la de manejo local que
        # usa el resto del algoritmo), 90 km no bastaba para exceder el tope por sí solo
        sid = insertar_solicitud(self.conn, lat, lon, nombre="Paciente Muy Lejos", zona=zona_lejana)

        notificaciones_antes = self.conn.execute("SELECT COUNT(*) AS n FROM notificaciones_admin").fetchone()["n"]

        resp = self.client.post(
            "/admin/rutas/masivas",
            data={
                "zonas": [zona_lejana],
                "fecha": "2026-08-25",
                "hora_salida": "08:00",
                f"recolector_id__{zona_lejana}": str(self.recolector_id),
            },
        )
        self.assertEqual(resp.status_code, 302)

        rutas_creadas = self.conn.execute("SELECT * FROM rutas WHERE zona = ?", (zona_lejana,)).fetchall()
        self.assertEqual(len(rutas_creadas), 0, "un paciente aislado y lejano no debía despacharse solo")

        estado = self.conn.execute("SELECT estado FROM solicitudes WHERE id=?", (sid,)).fetchone()["estado"]
        self.assertEqual(estado, "pendiente")

        notificaciones_despues = self.conn.execute(
            "SELECT * FROM notificaciones_admin ORDER BY id DESC LIMIT 1"
        ).fetchone()
        notificaciones_count = self.conn.execute("SELECT COUNT(*) AS n FROM notificaciones_admin").fetchone()["n"]
        self.assertEqual(notificaciones_count, notificaciones_antes + 1,
                          "debía crearse una notificación para el admin sobre este paciente aislado")
        self.assertIn("Paciente Muy Lejos", notificaciones_despues["mensaje"])


if __name__ == "__main__":
    unittest.main()

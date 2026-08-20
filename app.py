import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import socket
import sqlite3
import subprocess
import threading
import time as time_module
from datetime import date, datetime, timedelta
from datetime import time as dtime
from email.mime.text import MIMEText
from functools import wraps
from math import atan2, cos, radians, sin, sqrt
from zoneinfo import ZoneInfo
import urllib.error
import urllib.request
from urllib.parse import urlencode

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for, flash
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
ENV_PATH = os.path.join(BASE_DIR, ".env")

if os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        for linea in f:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                clave, valor = linea.split("=", 1)
                os.environ.setdefault(clave.strip(), valor.strip())

# En Render, DATABASE_PATH apunta al disco persistente (/var/data/database.db) para que la base
# sobreviva cada deploy — sin esta variable (como en desarrollo local) se queda junto al código.
DB_PATH = os.environ.get("DATABASE_PATH", os.path.join(BASE_DIR, "database.db"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-cambiar-en-produccion")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200 MB por archivo subido

NEF_VIDEOS_DIR = os.path.join(BASE_DIR, "static", "videos", "nef")
NEF_VIDEO_EXTENSIONES = {"mp4", "mov", "webm", "m4v"}
ADMIN_VIDEOS_DIR = os.path.join(BASE_DIR, "static", "videos", "admin")

ESTADO_LABELS = {
    "pendiente": "Pendiente de recolección",
    "pendiente_entrega": "Pendiente de entrega",
    "entregado": "Bote entregado",
    "programada": "Programada",
    "recolectada": "Recolectada",
    "incidencia": "Incidencia",
    "ausente": "Nadie en casa",
    "cancelada": "Cancelada",
    "lista_espera": "En lista de espera",
}

# Se usa para toda decisión que dependa de "qué hora es" (el corte de las 9pm para avisos, entre
# otras) en vez de confiar en la hora local del sistema — en Render el servidor corre en UTC, así
# que datetime.now() o datetime('now','localtime') ahí NO son la hora de Ciudad de México.
ZONA_HORARIA_NEGOCIO = ZoneInfo("America/Mexico_City")
HORA_CORTE_AVISOS_NOCTURNO = dtime(21, 0)  # después de esta hora, un aviso de ruta programada se
# pospone hasta la mañana siguiente en vez de mandarse de inmediato, para no interrumpir el
# descanso del paciente.
HORA_ENVIO_AVISOS_MATUTINO = dtime(7, 0)


def ahora_negocio():
    return datetime.now(ZONA_HORARIA_NEGOCIO)


# Filiberto Gómez 279, Tlalnepantla de Baz, Estado de México — punto de partida/regreso.
DEPOT_LAT = 19.5438982
DEPOT_LON = -99.2055009
VELOCIDAD_PROMEDIO_KMH = 22
MINUTOS_POR_PARADA = 15
# OSRM calcula el tiempo de manejo en condiciones ideales (sin tráfico). Este factor ajusta
# ese tiempo hacia condiciones reales de tráfico en la Zona Metropolitana del Valle de México.
FACTOR_TRAFICO = 1.4
MAX_PACIENTES_ACTIVOS = 700
MATERIALES_PRODUCTO_TERMINADO = ["PEMOFLEX BOLSA", "PEMOFLEX MANGUERA"]
TIPOS_CAJAS = [
    f"{tipo} {marca} {color}"
    for tipo in ("Manual", "Máquina")
    for marca in ("Baxter", "Pisa")
    for color in ("amarilla", "verde", "roja", "morada")
]
PERSONAS_PRODUCTIVIDAD = ["Gabriela", "Paola", "Monserrat"]
ACTIVIDADES_PRODUCTIVIDAD = ["moler", "cortar", "secar", "envasar"]
ACTIVIDADES_PRODUCTIVIDAD_LABELS = {
    "moler": "Moler", "cortar": "Cortar", "secar": "Secar", "envasar": "Envasar",
}
ZONA_BOOTSTRAP_DEFAULT = "Zona 1"
DIAS_ESPERA_DONACION = 30  # pacientes en modalidad 'donacion': cada cuántos días se vuelven a
# programar después de su última recolección (o de entregado el bote, si es la primera).
DIAS_ESPERA_COMPRA = 60  # pacientes en modalidad 'compra': ídem, pero cada 60 días.
PERSONAS_VACACIONES = ["Lety", "Martin", "Gaby", "Paola", "Monserrat"]
DIAS_VACACIONES_DEFAULT = 12
DURACION_MAXIMA_RUTA_MIN = 7 * 60 + 30  # 7:30 hrs por ruta antes de dividirla en otra
MIN_PARADAS_POR_RUTA = 12  # buscamos que cada ruta traiga al menos este número de pacientes,
# para juntar más material por día y ser más rentables en vez de mandar camionetas a medio llenar
MIN_PARADAS_DESPACHO = 8  # piso real: por debajo de esto no es rentable mandar la camioneta solo
# por esa tanda (a diferencia de MIN_PARADAS_POR_RUTA, que es solo la aspiración de llenado). Si
# una tanda queda por debajo, primero se intenta fusionar con la ruta planificada más cercana
# (de cualquier zona, ver fusionar_grupo_pequeno_con_ruta_vecina); si no cabe en ninguna, esos
# pacientes se quedan pendientes para la siguiente corrida —salvo el caso de un paciente aislado
# y lejano sin ninguna ruta cercana, que en vez de quedar esperando en silencio le avisa al admin
# (ver intentar_despachar_grupo_pequeno) para que decida si vale la pena mandar la camioneta.
DISTANCIA_ZONA_LEJANA_KM = 60  # en línea recta desde el depósito: más allá de esto, ya de por sí
# hay que manejar mucho para llegar, así que conviene la excepción de abajo en vez de mandar la
# camioneta varias veces a medio llenar
DURACION_MAXIMA_RUTA_LEJANA_MIN = DURACION_MAXIMA_RUTA_MIN + 2 * 60  # 9:30 hrs: excepción para
# rutas que ya de por sí incluyen algún paciente lejano (más de DISTANCIA_ZONA_LEJANA_KM del
# depósito) — como el viaje ya es largo, conviene juntar ahí la mayor cantidad posible de
# pacientes de esa zona aunque la ruta se pase del tope normal de 7:30
CAJAS_MAX_POR_SOLICITUD = 10  # no caben más en la camioneta en una sola solicitud
CAJAS_MAX_ENTREGA_RUTA = 15  # máximo de cajas a entregar (recibir) por ruta
CAJAS_MAX_RECEPCION_RUTA = 15  # máximo de cajas a recoger (donar) por ruta
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{}?overview=full&geometries=geojson"

_osrm_cache = {}


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))


def formatear_duracion(minutos):
    minutos = round(minutos)
    horas, mins = divmod(minutos, 60)
    if horas:
        return f"{horas} h {mins} min" if mins else f"{horas} h"
    return f"{mins} min"


def _http_get(url, headers=None, timeout=8):
    """Hace un GET y regresa el cuerpo de la respuesta (bytes), o None si falla. Intenta primero
    con urllib (siempre disponible, no depende de que 'curl' esté instalado — importante para
    plataformas como Render, cuya imagen puede no traer curl). Si falla —por ejemplo en Macs con
    el Python de Apple, cuyo LibreSSL a veces no completa el handshake TLS con estos servidores—
    cae a curl como respaldo, si está disponible."""
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        pass
    try:
        cmd = ["curl", "-s", "--max-time", str(timeout)]
        for k, v in (headers or {}).items():
            cmd += ["-H", f"{k}: {v}"]
        cmd.append(url)
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout + 2, check=True)
        return proc.stdout
    except Exception:
        return None


def osrm_distancia_duracion(secuencia):
    """secuencia: lista de (lat, lon) en orden de visita. Devuelve (km, minutos_manejo, geometria, tramos_min)
    usando calles reales (OSRM público, sin API key) o None si falla/no hay internet.
    geometria es la lista de puntos [lat, lon] que sigue la ruta real por calles, para dibujar en el mapa.
    tramos_min son los minutos de manejo (sin factor de tráfico) de cada tramo entre puntos consecutivos
    de la secuencia, para poder calcular a qué hora se llega a cada parada."""
    clave = tuple(secuencia)
    if clave in _osrm_cache:
        return _osrm_cache[clave]
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in secuencia)
    url = OSRM_URL.format(coords_str)
    try:
        body = _http_get(url, timeout=8)
        data = json.loads(body)
        ruta = data["routes"][0]
        geometria = [[lat, lon] for lon, lat in ruta["geometry"]["coordinates"]]
        tramos_min = [leg["duration"] / 60 for leg in ruta["legs"]]
        resultado = (ruta["distance"] / 1000, ruta["duration"] / 60, geometria, tramos_min)
    except Exception:
        return None
    _osrm_cache[clave] = resultado
    return resultado


def estimar_ruta(puntos):
    """puntos: filas con 'lat'/'lon'. Ida y vuelta desde el depósito, en el orden dado.
    Usa distancia/tiempo de manejo real por calles (OSRM); si no hay conexión, cae a línea recta."""
    coords = [(p["lat"], p["lon"]) for p in puntos if p["lat"] is not None and p["lon"] is not None]
    if not coords:
        return None
    secuencia = [(DEPOT_LAT, DEPOT_LON)] + coords + [(DEPOT_LAT, DEPOT_LON)]

    osrm = osrm_distancia_duracion(secuencia)
    if osrm:
        distancia_km, minutos_manejo, geometria, _tramos_min = osrm
        minutos_manejo *= FACTOR_TRAFICO
        fuente = "calles"
    else:
        distancia_km = sum(
            haversine_km(*secuencia[i], *secuencia[i + 1]) for i in range(len(secuencia) - 1)
        )
        minutos_manejo = distancia_km / VELOCIDAD_PROMEDIO_KMH * 60
        geometria = None
        fuente = "linea_recta"

    minutos = minutos_manejo + (len(coords) * MINUTOS_POR_PARADA)
    return {
        "distancia_km": round(distancia_km, 1),
        "minutos": round(minutos),
        "duracion": formatear_duracion(minutos),
        "puntos_sin_coords": len(puntos) - len(coords),
        "fuente": fuente,
        "geometria": geometria,
    }


def _duracion_aproximada_paquete(puntos):
    """Estimación rápida (sin red, en línea recta) de la duración de una ruta ida y vuelta con
    estos puntos. Se usa solo para decidir, punto por punto, cuántas paradas caben todavía en la
    tanda que se está armando en dividir_puntos_por_duracion — llamar a estimar_ruta (que sí
    consulta calles reales por OSRM) en cada paso sería demasiado lento con rutas de cientos de
    paradas. Aplica FACTOR_TRAFICO como margen de seguridad porque la distancia en línea recta
    subestima la distancia real por calles; la duración final de cada tanda ya armada se valida
    con estimar_ruta antes de dejarla así."""
    coords = [(p["lat"], p["lon"]) for p in puntos if p["lat"] is not None and p["lon"] is not None]
    if not coords:
        return len(puntos) * MINUTOS_POR_PARADA
    secuencia = [(DEPOT_LAT, DEPOT_LON)] + coords + [(DEPOT_LAT, DEPOT_LON)]
    km = sum(haversine_km(*secuencia[i], *secuencia[i + 1]) for i in range(len(secuencia) - 1))
    minutos_manejo = km / VELOCIDAD_PROMEDIO_KMH * 60 * FACTOR_TRAFICO
    return minutos_manejo + len(coords) * MINUTOS_POR_PARADA


def _tope_efectivo_grupo(grupo, minutos_max, minutos_max_lejano):
    """El tope de duración que le aplica a esta tanda: el extendido (zona lejana) si ya incluye
    algún paciente a más de DISTANCIA_ZONA_LEJANA_KM en línea recta del depósito —ya que ese
    viaje largo conviene aprovecharlo al máximo—, o el normal si no."""
    for p in grupo:
        if p["lat"] is None or p["lon"] is None:
            continue
        if haversine_km(DEPOT_LAT, DEPOT_LON, p["lat"], p["lon"]) > DISTANCIA_ZONA_LEJANA_KM:
            return minutos_max_lejano
    return minutos_max


def dividir_puntos_por_duracion(
    puntos,
    minutos_max=DURACION_MAXIMA_RUTA_MIN,
    min_paradas=MIN_PARADAS_POR_RUTA,
    minutos_max_lejano=DURACION_MAXIMA_RUTA_LEJANA_MIN,
):
    """Agrupa puntos (en el orden dado, p. ej. por cercanía) en tandas que quepan en minutos_max
    de manejo ida y vuelta al depósito, llenando cada tanda lo más posible antes de abrir la
    siguiente —en vez de repartir parejo entre muchas tandas a medio llenar— para minimizar
    cuántas rutas hacen falta y que cada una se acerque lo más posible al tope de tiempo. Si un
    solo punto ya excede minutos_max por sí mismo, queda solo en su tanda.

    Una tanda que ya incluye algún paciente lejano (ver _tope_efectivo_grupo) usa
    minutos_max_lejano en su lugar: como ya hay que manejar lejos para llegar, conviene juntar ahí
    a la mayor cantidad de pacientes de esa zona posible en vez de mandar la camioneta varias
    veces a medio llenar.

    Al final, si la última tanda quedó con menos de min_paradas, la funde con la anterior (o
    reparte parejo entre ambas) para acercarlas al mínimo de pacientes por ruta que buscamos."""
    if not puntos:
        return []

    grupos = []
    resto = list(puntos)
    while resto:
        grupo = [resto.pop(0)]
        tope = _tope_efectivo_grupo(grupo, minutos_max, minutos_max_lejano)
        while resto:
            candidato = grupo + [resto[0]]
            tope = _tope_efectivo_grupo(candidato, minutos_max, minutos_max_lejano)
            if _duracion_aproximada_paquete(candidato) > tope:
                break
            grupo.append(resto.pop(0))
        grupos.append(grupo)

    # Ajuste fino con duración real por calles (OSRM): la estimación rápida de arriba puede
    # quedarse corta frente a las calles reales. Si una tanda ya armada se pasa del tope al
    # medirla con precisión, le regresa paradas del final a la siguiente tanda (o abre una nueva
    # si era la última) hasta que quepa.
    idx = 0
    while idx < len(grupos):
        grupo = grupos[idx]
        while len(grupo) > 1:
            tope = _tope_efectivo_grupo(grupo, minutos_max, minutos_max_lejano)
            estimado = estimar_ruta(grupo)
            if not estimado or estimado["minutos"] <= tope:
                break
            sobrante = grupo.pop()
            if idx + 1 < len(grupos):
                grupos[idx + 1].insert(0, sobrante)
            else:
                grupos.append([sobrante])
        idx += 1

    # Si la última tanda quedó corta de pacientes, la funde con la anterior: si ambas juntas
    # caben en una sola ruta, se combinan en una sola (menos rutas todavía, y más llena); si no
    # caben, se reparten parejas entre las dos para que ninguna quede tan corta como la original.
    # (Pedirle paradas de una en una solo a la tanda anterior dejaría a esa por debajo del
    # mínimo en su lugar, sin resolver el problema.)
    if len(grupos) > 1 and len(grupos[-1]) < min_paradas:
        combinado = grupos[-2] + grupos[-1]
        tope_combinado = _tope_efectivo_grupo(combinado, minutos_max, minutos_max_lejano)
        estimado = estimar_ruta(combinado)
        if estimado and estimado["minutos"] <= tope_combinado:
            grupos[-2:] = [combinado]
        else:
            mitad = len(combinado) // 2
            nuevo_par = [combinado[:mitad], combinado[mitad:]]
            topes_par = [_tope_efectivo_grupo(g, minutos_max, minutos_max_lejano) for g in nuevo_par]
            if all(
                (estimar_ruta(g) or {"minutos": 0})["minutos"] <= t
                for g, t in zip(nuevo_par, topes_par)
            ):
                grupos[-2:] = nuevo_par

    return grupos


def fusionar_puntos_mismo_cliente(puntos):
    """Agrupa puntos (filas/dicts con id, estado, cliente_id, lat, lon, direccion) que pertenecen
    al mismo cliente_id —o, si no tienen cuenta (cliente_id NULL, paciente agregado por el admin o
    importado), a la misma dirección— en una sola unidad de parada, para no duplicar la visita
    cuando el mismo domicilio tiene a la vez una solicitud de recolección/entrega de PVC y una de
    redistribución de cajas. Devuelve una lista de dicts: id, lat, lon, tipo, extra_id, tipo_extra."""
    por_clave = {}
    unidades = []
    for p in puntos:
        tipo = "entrega" if p["estado"] == "pendiente_entrega" else "recoleccion"
        cid = p["cliente_id"]
        if cid is not None:
            clave = ("cliente", cid)
        else:
            direccion_norm = (p["direccion"] or "").strip().lower()
            clave = ("direccion", direccion_norm) if direccion_norm else None
        if clave is not None and clave in por_clave:
            por_clave[clave]["extra_id"] = p["id"]
            por_clave[clave]["tipo_extra"] = tipo
            continue
        unidad = {
            "id": p["id"], "lat": p["lat"], "lon": p["lon"], "tipo": tipo,
            "extra_id": None, "tipo_extra": None,
        }
        if clave is not None:
            por_clave[clave] = unidad
        unidades.append(unidad)
    return unidades


def limitar_cajas_grupo(db, grupo):
    """Recibe los puntos (ya fusionados) que formarán UNA ruta y quita los que exceden los
    máximos de cajas por ruta (CAJAS_MAX_ENTREGA_RUTA a entregar/recibir, CAJAS_MAX_RECEPCION_RUTA
    a recoger/donar), dando prioridad a las solicitudes de recepción (donar) sobre las de entrega
    (recibir). Los puntos sin cajas de por medio nunca se filtran. Devuelve (grupo_filtrado,
    sobrantes); los sobrantes quedan sin tocar (pendientes) para una futura ruta."""
    ids = set()
    for p in grupo:
        ids.add(p["id"])
        if p.get("extra_id"):
            ids.add(p["extra_id"])
    info = {}
    if ids:
        marcadores = ",".join("?" * len(ids))
        filas = db.execute(
            f"SELECT id, tipo_redistribucion, cantidad_cajas FROM solicitudes WHERE id IN ({marcadores})",
            tuple(ids),
        ).fetchall()
        info = {f["id"]: f for f in filas}

    def cajas_de(p):
        entrega = recepcion = 0
        for sid in (p["id"], p.get("extra_id")):
            fila = info.get(sid)
            if not fila:
                continue
            cantidad = fila["cantidad_cajas"] or 0
            if fila["tipo_redistribucion"] == "material":
                entrega += cantidad
            elif fila["tipo_redistribucion"] == "donar":
                recepcion += cantidad
        return entrega, recepcion

    con_indice = list(enumerate(grupo))
    con_indice.sort(key=lambda item: (0 if cajas_de(item[1])[1] > 0 else 1, item[0]))

    incluidos = set()
    sobrantes = []
    total_entrega = total_recepcion = 0
    for idx, p in con_indice:
        entrega, recepcion = cajas_de(p)
        if entrega == 0 and recepcion == 0:
            incluidos.add(idx)
            continue
        if total_entrega + entrega > CAJAS_MAX_ENTREGA_RUTA or total_recepcion + recepcion > CAJAS_MAX_RECEPCION_RUTA:
            sobrantes.append(p)
            continue
        total_entrega += entrega
        total_recepcion += recepcion
        incluidos.add(idx)

    grupo_filtrado = [p for idx, p in enumerate(grupo) if idx in incluidos]
    return grupo_filtrado, sobrantes


def _centroide(puntos):
    con_coords = [p for p in puntos if p.get("lat") is not None and p.get("lon") is not None]
    if not con_coords:
        return None
    return (
        sum(p["lat"] for p in con_coords) / len(con_coords),
        sum(p["lon"] for p in con_coords) / len(con_coords),
    )


def fusionar_grupo_pequeno_con_ruta_vecina(db, grupo):
    """grupo quedó por debajo de MIN_PARADAS_DESPACHO. Busca, entre las rutas ya planificadas y
    sin iniciar (de cualquier zona), la más cercana geográficamente a este grupo donde quepan
    estos pacientes sin pasar el tope de duración que le corresponda a esa ruta combinada. Si
    encuentra una, agrega ahí las paradas (reordenadas por cercanía) y regresa la lista de ids de
    las paradas nuevas (las de este grupo; las que ya tenía la ruta no cambian). Si ninguna tiene
    espacio, no toca nada y regresa None."""
    centro = _centroide(grupo)
    if centro is None:
        return None
    candidatas = db.execute(
        "SELECT * FROM rutas WHERE estado = 'planificada' AND hora_inicio_real IS NULL"
    ).fetchall()
    if not candidatas:
        return None

    def puntos_de_ruta(ruta_id):
        return [dict(p) for p in db.execute(
            "SELECT s.id, s.lat, s.lon, p.tipo, p.solicitud_extra_id AS extra_id, p.tipo_extra "
            "FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
            "WHERE p.ruta_id = ? ORDER BY p.orden",
            (ruta_id,),
        ).fetchall()]

    def distancia_a_ruta(ruta):
        centro_ruta = _centroide(puntos_de_ruta(ruta["id"]))
        if centro_ruta is None:
            return float("inf")
        return haversine_km(centro[0], centro[1], centro_ruta[0], centro_ruta[1])

    for ruta in sorted(candidatas, key=distancia_a_ruta):
        puntos_ruta = puntos_de_ruta(ruta["id"])
        combinado = ordenar_por_cercania(puntos_ruta + grupo)
        estimado = estimar_ruta(combinado)
        tope = _tope_efectivo_grupo(combinado, DURACION_MAXIMA_RUTA_MIN, DURACION_MAXIMA_RUTA_LEJANA_MIN)
        if not estimado or estimado["minutos"] > tope:
            continue
        ids_del_grupo = {p["id"] for p in grupo}
        db.execute("DELETE FROM paradas WHERE ruta_id = ?", (ruta["id"],))
        parada_ids_nuevas = []
        for i, p in enumerate(combinado, start=1):
            cur_parada = db.execute(
                "INSERT INTO paradas (ruta_id, solicitud_id, solicitud_extra_id, tipo_extra, orden, tipo) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ruta["id"], p["id"], p.get("extra_id"), p.get("tipo_extra"), i, p["tipo"]),
            )
            if p["id"] in ids_del_grupo:
                parada_ids_nuevas.append(cur_parada.lastrowid)
            db.execute("UPDATE solicitudes SET estado = 'programada', zona = ? WHERE id = ?", (ruta["zona"], p["id"]))
            if p.get("extra_id"):
                db.execute(
                    "UPDATE solicitudes SET estado = 'programada', zona = ? WHERE id = ?",
                    (ruta["zona"], p["extra_id"]),
                )
        return parada_ids_nuevas
    return None


def _grupo_aislado_lejano(grupo):
    """True si el grupo es un único paciente cuya ruta, aunque fuera solo, ya excede el tope
    normal de 7:30 por sí mismo —el caso que dividir_puntos_por_duracion deja solo en su tanda
    porque no hay forma de combinarlo con nadie más sin pasarse del tope."""
    if len(grupo) != 1:
        return False
    estimado = estimar_ruta(grupo)
    return bool(estimado and estimado["minutos"] > DURACION_MAXIMA_RUTA_MIN)


def intentar_despachar_grupo_pequeno(db, grupo):
    """grupo quedó por debajo de MIN_PARADAS_DESPACHO. Primero intenta fusionarlo con la ruta
    planificada más cercana (ver fusionar_grupo_pequeno_con_ruta_vecina). Si no cupo en ninguna:
    un paciente aislado y lejano (sin vecinos ni ruta cercana posible) le avisa al admin en vez de
    dejarlo esperando en silencio, porque nunca va a juntar el mínimo por sí solo; cualquier otro
    caso simplemente se queda pendiente para la siguiente corrida. Devuelve
    (resultado, parada_ids_nuevas) donde resultado es 'fusionado', 'aviso' o 'pendiente'."""
    parada_ids_nuevas = fusionar_grupo_pequeno_con_ruta_vecina(db, grupo)
    if parada_ids_nuevas is not None:
        return "fusionado", parada_ids_nuevas
    if _grupo_aislado_lejano(grupo):
        p = grupo[0]
        sol = db.execute(
            "SELECT cliente_id, nombre_contacto, direccion FROM solicitudes WHERE id = ?", (p["id"],)
        ).fetchone()
        nombre = sol["nombre_contacto"] if sol else None
        cliente_id = sol["cliente_id"] if sol else None
        if cliente_id:
            cliente = db.execute("SELECT name FROM users WHERE id = ?", (cliente_id,)).fetchone()
            nombre = cliente["name"] if cliente else nombre
        crear_notificacion_admin(
            db, cliente_id,
            f"{nombre or 'Un paciente'} está demasiado lejos de cualquier otra ruta o paciente "
            f"(dirección: {sol['direccion'] if sol else '?'}) para juntar el mínimo de "
            f"{MIN_PARADAS_DESPACHO} paradas. No se le creó ruta automáticamente — revisa si vale "
            "la pena mandar la camioneta solo por él o esperar a que se sume alguien más cerca.",
        )
        return "aviso", []
    return "pendiente", []


def siguiente_numero_ruta(db):
    """Siguiente número consecutivo libre para nombrar una ruta como 'Ruta NN (...)', tomando
    el máximo usado tanto en zonas importadas como en nombres de rutas ya creadas."""
    maximo = 0
    filas = db.execute(
        "SELECT zona AS nombre FROM solicitudes WHERE zona IS NOT NULL "
        "UNION SELECT nombre FROM rutas UNION SELECT zona AS nombre FROM zonas_referencia"
    ).fetchall()
    for f in filas:
        m = re.match(r"Ruta (\d+)", f["nombre"] or "")
        if m:
            maximo = max(maximo, int(m.group(1)))
    return maximo + 1


def estimar_ruta_por_id(db, ruta_id):
    filas = db.execute(
        "SELECT s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
        "WHERE p.ruta_id = ? ORDER BY p.orden",
        (ruta_id,),
    ).fetchall()
    return estimar_ruta(filas)


def horario_estimado_siguiente(db, parada_id):
    """Para la parada que sigue AHORA MISMO en una ruta ya en curso: estima la hora de llegada
    a partir de la hora actual + el tiempo de manejo real desde la última parada ya resuelta (o
    el depósito, si es la primera) hasta esta. A diferencia de horario_estimado_parada —que
    proyecta desde el inicio de la ruta sumando un presupuesto de minutos por cada parada
    anterior— esto no se desfasa aunque la ruta ya lleve muchas paradas resueltas, porque no
    depende de cuánto se suponía que iban a tardar sino de dónde está el recolector ahora."""
    parada = db.execute(
        "SELECT p.*, s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id WHERE p.id = ?",
        (parada_id,),
    ).fetchone()
    if parada is None or parada["lat"] is None or parada["lon"] is None:
        return None

    anterior = db.execute(
        "SELECT s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
        "WHERE p.ruta_id = ? AND p.orden < ? AND p.estado != 'pendiente' AND s.lat IS NOT NULL "
        "AND s.lon IS NOT NULL ORDER BY p.orden DESC LIMIT 1",
        (parada["ruta_id"], parada["orden"]),
    ).fetchone()
    origen_lat, origen_lon = (anterior["lat"], anterior["lon"]) if anterior else (DEPOT_LAT, DEPOT_LON)

    osrm = osrm_distancia_duracion([(origen_lat, origen_lon), (parada["lat"], parada["lon"])])
    if osrm:
        minutos = osrm[1] * FACTOR_TRAFICO
    else:
        minutos = haversine_km(origen_lat, origen_lon, parada["lat"], parada["lon"]) / VELOCIDAD_PROMEDIO_KMH * 60

    llegada = datetime.now() + timedelta(minutes=minutos)
    salida_de_ahi = llegada + timedelta(minutes=30)
    return f"{llegada.strftime('%-I:%M %p')} – {salida_de_ahi.strftime('%-I:%M %p')}"


def horario_estimado_parada(db, parada_id):
    """Calcula una ventana aproximada ('9:40 am - 10:10 am') de a qué hora pasarán por esta
    parada específica, según el orden de la ruta, la hora de salida y el tráfico real (OSRM).
    Devuelve None si la parada no existe, no tiene coordenadas, o falla el cálculo de ruta."""
    parada = db.execute(
        "SELECT p.*, r.fecha, r.hora_salida, r.hora_inicio_real FROM paradas p "
        "JOIN rutas r ON r.id = p.ruta_id WHERE p.id = ?",
        (parada_id,),
    ).fetchone()
    if parada is None:
        return None

    paradas_ruta = db.execute(
        "SELECT p.id, s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
        "WHERE p.ruta_id = ? ORDER BY p.orden",
        (parada["ruta_id"],),
    ).fetchall()
    con_coords = [p for p in paradas_ruta if p["lat"] is not None and p["lon"] is not None]
    if not con_coords or parada["id"] not in [p["id"] for p in con_coords]:
        return None

    secuencia = [(DEPOT_LAT, DEPOT_LON)] + [(p["lat"], p["lon"]) for p in con_coords] + [(DEPOT_LAT, DEPOT_LON)]
    osrm = osrm_distancia_duracion(secuencia)
    if not osrm:
        return None
    _km, _min, _geom, tramos_min = osrm

    inicio = None
    if parada["hora_inicio_real"]:
        try:
            inicio = datetime.strptime(parada["hora_inicio_real"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            inicio = None
    if inicio is None:
        try:
            hora_h, hora_m = (int(x) for x in parada["hora_salida"].split(":"))
        except (ValueError, AttributeError):
            hora_h, hora_m = 8, 0
        inicio = datetime.combine(date.today(), dtime(hora_h, hora_m))

    indice = [p["id"] for p in con_coords].index(parada["id"])
    minutos_acumulados = sum(t * FACTOR_TRAFICO for t in tramos_min[: indice + 1]) + indice * MINUTOS_POR_PARADA
    llegada = inicio + timedelta(minutes=minutos_acumulados)
    salida_de_ahi = llegada + timedelta(minutes=30)
    return f"{llegada.strftime('%-I:%M %p')} – {salida_de_ahi.strftime('%-I:%M %p')}"


def _notificar_paradas_programadas(parada_ids):
    """Manda a cada paciente un correo con la información de su recolección recién programada
    (fecha, horario estimado, recolector) y dos ligas para confirmar si podrá recibir la
    recolección ese día o no. Corre en un hilo aparte con su propia conexión a la base de
    datos, así que no bloquea la respuesta de quien creó la ruta.
    Arma las ligas con url_absoluta() en vez de tomar el host de la petición que creó la ruta
    (request.host_url) — ese host es el del navegador del admin/recolector, no le sirve de nada
    al paciente que abre el enlace desde su propio teléfono."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        for parada_id in parada_ids:
            parada = conn.execute(
                "SELECT p.id, p.ruta_id, r.fecha, r.hora_salida, u.name AS recolector_nombre, "
                "s.direccion, s.cliente_id "
                "FROM paradas p JOIN rutas r ON r.id = p.ruta_id JOIN solicitudes s ON s.id = p.solicitud_id "
                "LEFT JOIN users u ON u.id = r.recolector_id WHERE p.id = ?",
                (parada_id,),
            ).fetchone()
            if parada is None or not parada["cliente_id"]:
                continue
            paciente = conn.execute(
                "SELECT name, telefono FROM users WHERE id = ?", (parada["cliente_id"],)
            ).fetchone()
            if not paciente or not paciente["telefono"]:
                continue

            token = secrets.token_urlsafe(24)
            conn.execute("UPDATE paradas SET confirmacion_token = ? WHERE id = ?", (token, parada_id))
            conn.commit()

            horario = horario_estimado_parada(conn, parada_id)
            with app.test_request_context():
                link_si = url_absoluta("parada_confirmar", token=token, respuesta="si")
                link_no = url_absoluta("parada_confirmar", token=token, respuesta="no")

            fecha_horario = parada["fecha"] + (f", entre {horario}" if horario else "")
            cuerpo = (
                f"Hola {paciente['name']},\n\n"
                f"Ya programamos tu recolección para el {fecha_horario}.\n"
                f"Recolector: {parada['recolector_nombre'] or 'por asignar'}\n"
                f"Dirección: {parada['direccion']}\n\n"
                "¿Vas a poder recibir la recolección ese día?\n\n"
                f"Sí puedo: {link_si}\n"
                f"No puedo: {link_no}\n"
            )
            enviar_whatsapp_primer_contacto_respetando_horario(
                conn,
                telefono_whatsapp_e164(paciente["telefono"]),
                "TWILIO_TEMPLATE_RUTA_PROGRAMADA_SID",
                {
                    "1": paciente["name"], "2": fecha_horario,
                    "3": parada["recolector_nombre"] or "por asignar", "4": parada["direccion"],
                    "5": link_si, "6": link_no,
                },
                cuerpo,
            )
            conn.commit()
    finally:
        conn.close()


def _notificar_siguiente_parada(ruta_id):
    """Avisa por correo al paciente de la próxima parada pendiente de una ruta (la de menor
    `orden` que siga sin resolverse) que el recolector va en camino. Corre en un hilo aparte
    con su propia conexión a la base de datos."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        siguiente = conn.execute(
            "SELECT id, solicitud_id FROM paradas WHERE ruta_id = ? AND estado = 'pendiente' "
            "ORDER BY orden LIMIT 1",
            (ruta_id,),
        ).fetchone()
        if siguiente is None:
            return
        sol = conn.execute(
            "SELECT direccion, cliente_id FROM solicitudes WHERE id = ?", (siguiente["solicitud_id"],)
        ).fetchone()
        if sol is None or not sol["cliente_id"]:
            return
        paciente = conn.execute(
            "SELECT name, telefono FROM users WHERE id = ?", (sol["cliente_id"],)
        ).fetchone()
        if not paciente or not paciente["telefono"]:
            return

        horario = horario_estimado_siguiente(conn, siguiente["id"])
        horario_texto = f"entre {horario}" if horario else "en cualquier momento de hoy"
        cuerpo = (
            f"Hola {paciente['name']},\n\n"
            f"Prepárate, el recolector se encuentra en camino a tu domicilio, {horario_texto}.\n\n"
            f"Dirección registrada: {sol['direccion']}\n\n"
            "Ten tu material listo para cuando llegue."
        )
        enviar_whatsapp_primer_contacto(
            telefono_whatsapp_e164(paciente["telefono"]),
            "TWILIO_TEMPLATE_RECOLECTOR_EN_CAMINO_SID",
            {"1": paciente["name"], "2": horario_texto, "3": sol["direccion"]},
            cuerpo,
        )
    finally:
        conn.close()


def url_absoluta(endpoint, **kwargs):
    """Arma un enlace completo para mandar por WhatsApp. url_for(_external=True) usa el host con
    el que se hizo la petición que disparó el envío (por ejemplo 127.0.0.1 si el admin está en su
    propia laptop) — eso rompe el enlace para quien lo reciba en otro dispositivo. Si PUBLIC_BASE_URL
    está configurada en .env (IP de red local para pruebas, o el dominio real en producción), se usa
    esa en vez del host de la petición."""
    base = os.environ.get("PUBLIC_BASE_URL")
    if base:
        return base.rstrip("/") + url_for(endpoint, **kwargs)
    return url_for(endpoint, _external=True, **kwargs)


def _url_publica_actual():
    """La URL exacta que Twilio usó para llamar a este webhook, para validar su firma. Detrás de
    un proxy (como en Render) request.url puede reportar "http://" aunque Twilio realmente haya
    llamado por "https://" —eso rompería la validación de la firma—, así que si PUBLIC_BASE_URL
    está configurada se arma la URL a partir de ella en vez de confiar en el host/esquema que ve
    Flask."""
    base = os.environ.get("PUBLIC_BASE_URL")
    if base:
        return base.rstrip("/") + request.path
    return request.url


def telefono_identidad(raw):
    """Valida el teléfono que un cliente usa como identidad de cuenta (login, registro,
    recuperación). Devuelve los 10 dígitos tal cual (sin +52) o None si no son exactamente 10."""
    digitos = re.sub(r"\D", "", raw or "")
    if len(digitos) != 10:
        return None
    return digitos


def telefono_whatsapp_e164(telefono_10_digitos):
    """Arma la dirección que espera la API de WhatsApp para un número mexicano de 10 dígitos.
    WhatsApp usa un "1" extra después del 52 para México (no se marca así al llamar, pero así
    quedó registrado el wa_id) — confirmado al probar contra el sandbox de Twilio."""
    digitos = re.sub(r"\D", "", telefono_10_digitos or "")
    if len(digitos) != 10:
        return None
    return f"+521{digitos}"


def crear_notificacion_admin(db, cliente_id, mensaje):
    db.execute(
        "INSERT INTO notificaciones_admin (cliente_id, mensaje) VALUES (?, ?)", (cliente_id, mensaje)
    )


def enviar_email(destinatario, asunto, cuerpo):
    """Envía un correo por Gmail SMTP usando las credenciales de .env.
    Devuelve True si se envió, False si falló (sin conexión, credenciales inválidas, etc.)."""
    remitente = os.environ.get("SMTP_EMAIL")
    clave = os.environ.get("SMTP_APP_PASSWORD")
    if not remitente or not clave:
        print("[enviar_email] SMTP_EMAIL/SMTP_APP_PASSWORD no están configurados — no se envió el correo.")
        return False
    msg = MIMEText(cuerpo)
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = destinatario
    # Render no tiene salida IPv6, y smtp.gmail.com sí publica un registro AAAA — sin esto,
    # smtplib intenta conectar por IPv6 primero y falla con "Network is unreachable".
    # Forzamos la resolución a IPv4 solo mientras dure esta conexión.
    getaddrinfo_original = socket.getaddrinfo

    def _getaddrinfo_ipv4(host, port, family=0, type=0, proto=0, flags=0):
        return getaddrinfo_original(host, port, socket.AF_INET, type, proto, flags)

    try:
        socket.getaddrinfo = _getaddrinfo_ipv4
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(remitente, clave)
            server.sendmail(remitente, [destinatario], msg.as_string())
        return True
    except Exception as e:
        print(f"[enviar_email] Falló el envío a {destinatario}: {e}")
        return False
    finally:
        socket.getaddrinfo = getaddrinfo_original


def enviar_whatsapp(destinatario, cuerpo):
    """Envía un WhatsApp por la API REST de Twilio usando las credenciales de .env.
    `destinatario` es el número en formato E.164 (ej. "+525512345678"), sin el prefijo "whatsapp:".
    Devuelve True si Twilio aceptó el mensaje, False si falló (credenciales faltantes, número no
    unido al sandbox, error de red, etc.)."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    numero_from = os.environ.get("TWILIO_WHATSAPP_FROM")
    if not account_sid or not auth_token or not numero_from:
        print("[enviar_whatsapp] TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_WHATSAPP_FROM no están configurados — no se envió el mensaje.")
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urlencode(
        {
            "From": f"whatsapp:{numero_from}" if not numero_from.startswith("whatsapp:") else numero_from,
            "To": f"whatsapp:{destinatario}" if not destinatario.startswith("whatsapp:") else destinatario,
            "Body": cuerpo,
        }
    ).encode("utf-8")
    credenciales = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {credenciales}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", errors="replace")
        print(f"[enviar_whatsapp] Falló el envío a {destinatario}: {e.code} {detalle}")
        return False
    except Exception as e:
        print(f"[enviar_whatsapp] Falló el envío a {destinatario}: {e}")
        return False


def enviar_whatsapp_template(destinatario, content_sid, content_variables=None):
    """Envía un WhatsApp usando una plantilla (Content Template) ya aprobada por Meta, en vez de
    texto libre. Fuera del sandbox, WhatsApp solo deja mandar texto libre si la conversación ya
    la abrió el usuario en las últimas 24h — para escribirle primero a alguien (como al paciente
    que se acaba de registrar) hay que usar una plantilla aprobada. `content_sid` es el ID que
    Twilio le da a la plantilla (empieza con "HX...") una vez aprobada; `content_variables` es un
    diccionario con las variables numeradas de la plantilla, p. ej. {"1": nombre_paciente}."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    numero_from = os.environ.get("TWILIO_WHATSAPP_FROM")
    if not account_sid or not auth_token or not numero_from:
        print("[enviar_whatsapp_template] TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_WHATSAPP_FROM no están configurados — no se envió el mensaje.")
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    campos = {
        "From": f"whatsapp:{numero_from}" if not numero_from.startswith("whatsapp:") else numero_from,
        "To": f"whatsapp:{destinatario}" if not destinatario.startswith("whatsapp:") else destinatario,
        "ContentSid": content_sid,
    }
    if content_variables:
        campos["ContentVariables"] = json.dumps(content_variables)
    data = urlencode(campos).encode("utf-8")
    credenciales = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {credenciales}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except urllib.error.HTTPError as e:
        detalle = e.read().decode("utf-8", errors="replace")
        print(f"[enviar_whatsapp_template] Falló el envío a {destinatario}: {e.code} {detalle}")
        return False
    except Exception as e:
        print(f"[enviar_whatsapp_template] Falló el envío a {destinatario}: {e}")
        return False


def enviar_whatsapp_primer_contacto(destinatario, content_sid_env, content_variables, cuerpo_libre):
    """Manda el primer mensaje de una conversación de WhatsApp (uno que el negocio inicia, sin que
    el paciente haya escrito antes): si ya hay una plantilla aprobada configurada en .env
    (content_sid_env, p. ej. "TWILIO_TEMPLATE_VERIFICACION_SID"), la usa —obligatorio fuera del
    sandbox para escribirle primero a alguien—; si no está configurada, cae al texto libre de
    siempre, que es lo único que hace falta mientras se sigue probando en el sandbox (ahí no hay
    restricción de plantillas). Así el código no necesita tocarse de nuevo: en cuanto se registre
    la plantilla en Twilio y se agregue su SID a las variables de entorno, el envío cambia solo."""
    content_sid = os.environ.get(content_sid_env)
    if content_sid:
        return enviar_whatsapp_template(destinatario, content_sid, content_variables)
    return enviar_whatsapp(destinatario, cuerpo_libre)


def enviar_whatsapp_primer_contacto_respetando_horario(db, destinatario, content_sid_env, content_variables, cuerpo_libre):
    """Como enviar_whatsapp_primer_contacto(), pero si ya pasan de las 9:00pm (hora de Ciudad de
    México) no manda el mensaje de inmediato — lo deja guardado para enviarse a primera hora del
    día siguiente, para no interrumpir el descanso del paciente con una notificación tardía."""
    ahora = ahora_negocio()
    if ahora.time() < HORA_CORTE_AVISOS_NOCTURNO:
        return enviar_whatsapp_primer_contacto(destinatario, content_sid_env, content_variables, cuerpo_libre)
    manana = (ahora + timedelta(days=1)).replace(
        hour=HORA_ENVIO_AVISOS_MATUTINO.hour, minute=HORA_ENVIO_AVISOS_MATUTINO.minute,
        second=0, microsecond=0,
    )
    db.execute(
        "INSERT INTO avisos_programados (telefono, content_sid_env, content_variables_json, cuerpo_libre, enviar_despues_de) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            destinatario, content_sid_env,
            json.dumps(content_variables) if content_variables else None,
            cuerpo_libre, manana.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    return None


def procesar_avisos_programados():
    """Manda los avisos que se pospusieron por generarse después de las 9:00pm (ver
    enviar_whatsapp_primer_contacto_respetando_horario) y ya les llegó su hora. Corre desde un
    hilo en segundo plano que se revisa periódicamente (ver _hilo_avisos_programados) — así
    sobrevive aunque el servidor se reinicie entre que se guardó el aviso y que le tocaba salir.
    Reclama cada aviso con un UPDATE condicionado antes de mandarlo, para que si hay más de un
    proceso de gunicorn corriendo este mismo chequeo a la vez, cada aviso solo se mande una vez."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 20000")
    try:
        ahora_texto = ahora_negocio().strftime("%Y-%m-%d %H:%M:%S")
        pendientes = conn.execute(
            "SELECT * FROM avisos_programados WHERE enviado = 0 AND enviar_despues_de <= ?", (ahora_texto,)
        ).fetchall()
        for aviso in pendientes:
            cur = conn.execute(
                "UPDATE avisos_programados SET enviado = 1 WHERE id = ? AND enviado = 0", (aviso["id"],)
            )
            conn.commit()
            if cur.rowcount == 0:
                continue
            content_variables = json.loads(aviso["content_variables_json"]) if aviso["content_variables_json"] else None
            enviar_whatsapp_primer_contacto(
                aviso["telefono"], aviso["content_sid_env"], content_variables, aviso["cuerpo_libre"],
            )
    finally:
        conn.close()


def _hilo_avisos_programados():
    while True:
        try:
            procesar_avisos_programados()
        except Exception as e:
            print(f"[avisos_programados] error: {e}")
        time_module.sleep(300)


def validar_firma_twilio(url, parametros_post, firma_recibida):
    """Confirma que una petición al webhook de WhatsApp realmente viene de Twilio y no de
    cualquiera que le mande un POST falso a esta URL pública. Sigue el algoritmo de Twilio:
    HMAC-SHA1 de la URL completa más cada par clave+valor del POST (ordenados alfabéticamente por
    clave, concatenados sin separador), usando el auth token como llave, en base64 — y lo compara
    contra el header X-Twilio-Signature con una comparación de tiempo constante."""
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not auth_token or not firma_recibida:
        return False
    base = url
    for clave in sorted(parametros_post.keys()):
        base += clave + parametros_post[clave]
    firma_calculada = base64.b64encode(
        hmac.new(auth_token.encode("utf-8"), base.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")
    return hmac.compare_digest(firma_calculada, firma_recibida)


def geocodificar_direccion(direccion, limite=5, codigo_postal=None):
    """Busca una dirección con Nominatim (OpenStreetMap, gratis, sin API key). Una misma calle y
    número puede existir en varias colonias/alcaldías (p. ej. Av. de los Bosques 1515 existe tanto
    en Tecamachalco como en Miguel Hidalgo), así que devuelve hasta `limite` candidatos —
    [{"lat", "lon", "etiqueta"}, ...] — en vez de adivinar uno solo. Si se da codigo_postal, se usa
    para acotar la búsqueda a esa zona exacta. Lista vacía si no se encuentra nada o falla la
    conexión."""
    params = {"format": "json", "limit": limite, "countrycodes": "mx"}
    if codigo_postal:
        params["street"] = direccion
        params["postalcode"] = codigo_postal
    else:
        params["q"] = direccion
    query = urlencode(params)
    url = f"https://nominatim.openstreetmap.org/search?{query}"
    try:
        body = _http_get(url, headers={"User-Agent": "rutas-recoleccion-app/1.0"}, timeout=8)
        data = json.loads(body)
        return [
            {"lat": float(d["lat"]), "lon": float(d["lon"]), "etiqueta": d.get("display_name", direccion)}
            for d in data
        ]
    except Exception:
        return []


def geocodificar_codigo_postal(codigo_postal, limite=5):
    """Busca un código postal mexicano con Nominatim y devuelve hasta `limite` candidatos —
    [{"lat", "lon", "etiqueta"}, ...]. Lista vacía si no se encuentra nada o falla la conexión."""
    query = urlencode({"postalcode": codigo_postal, "country": "Mexico", "format": "json", "limit": limite})
    url = f"https://nominatim.openstreetmap.org/search?{query}"
    try:
        body = _http_get(url, headers={"User-Agent": "rutas-recoleccion-app/1.0"}, timeout=8)
        data = json.loads(body)
        return [
            {"lat": float(d["lat"]), "lon": float(d["lon"]), "etiqueta": d.get("display_name", codigo_postal)}
            for d in data
        ]
    except Exception:
        return []


def geocodificar_inverso(lat, lon):
    """Convierte coordenadas GPS a una dirección legible con Nominatim (geocodificación
    inversa). Devuelve el texto de la dirección o None si falla."""
    query = urlencode({"lat": lat, "lon": lon, "format": "json"})
    url = f"https://nominatim.openstreetmap.org/reverse?{query}"
    try:
        body = _http_get(url, headers={"User-Agent": "rutas-recoleccion-app/1.0"}, timeout=8)
        data = json.loads(body)
        return data.get("display_name")
    except Exception:
        return None


def condicion_lista_para_recoleccion(alias=""):
    """Fragmento SQL (booleano) que indica si una solicitud ya está lista para volver a
    programarse: nunca se ha recolectado (fecha_reinicio_espera NULL, incluye pacientes nuevos
    en su primera recolección) o ya pasó el intervalo que le toca según su modalidad —30 días
    si es donación, 60 si es compra— contado desde su última recolección."""
    p = f"{alias}." if alias else ""
    dias_caso = f"CASE WHEN {p}modalidad = 'compra' THEN {DIAS_ESPERA_COMPRA} ELSE {DIAS_ESPERA_DONACION} END"
    return (
        f"({p}fecha_reinicio_espera IS NULL OR "
        f"datetime({p}fecha_reinicio_espera, '+' || ({dias_caso}) || ' days') <= datetime('now', 'localtime'))"
    )


def ordenar_por_cercania(puntos):
    """Reordena los puntos con el heurístico del vecino más cercano, empezando desde el
    depósito y encadenando siempre el punto no visitado más cercano al actual. Así las paradas
    consecutivas de una ruta quedan geográficamente juntas en vez de en orden arbitrario. Los
    puntos sin coordenadas se dejan al final, en su orden original."""
    con_coords = [p for p in puntos if p.get("lat") is not None and p.get("lon") is not None]
    sin_coords = [p for p in puntos if p.get("lat") is None or p.get("lon") is None]
    restantes = con_coords[:]
    ordenados = []
    lat_actual, lon_actual = DEPOT_LAT, DEPOT_LON
    while restantes:
        siguiente = min(restantes, key=lambda p: haversine_km(lat_actual, lon_actual, p["lat"], p["lon"]))
        ordenados.append(siguiente)
        restantes.remove(siguiente)
        lat_actual, lon_actual = siguiente["lat"], siguiente["lon"]
    return ordenados + sin_coords


def ordenar_grupo_por_cercania(grupo, minutos_max=DURACION_MAXIMA_RUTA_MIN):
    """Aplica ordenar_por_cercania a un grupo ya armado (mismas paradas, sin agregar ni quitar
    ninguna) para que el recorrido dentro de la ruta sea eficiente. Si por alguna razón el
    reordenamiento resultara en una duración mayor a minutos_max, se conserva el orden
    original en su lugar."""
    reordenado = ordenar_por_cercania(grupo)
    estimado = estimar_ruta(reordenado)
    if estimado and estimado["minutos"] > minutos_max:
        return grupo
    return reordenado


def zona_mas_cercana(db, lat, lon):
    """Busca, entre los puntos que ya tienen zona asignada (solicitudes reales de hoy, más los
    puntos de referencia guardados en zonas_referencia para no depender solo de pacientes activos),
    cuál está más cerca de (lat, lon) y devuelve (zona, distancia_km), o None si no hay ningún
    punto con zona y coordenadas."""
    filas = db.execute(
        "SELECT zona, lat, lon FROM solicitudes WHERE zona IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL "
        "UNION ALL SELECT zona, lat, lon FROM zonas_referencia"
    ).fetchall()
    mejor = None
    for f in filas:
        d = haversine_km(lat, lon, f["lat"], f["lon"])
        if mejor is None or d < mejor[1]:
            mejor = (f["zona"], d)
    return mejor


LIMITE_MINUTOS_COBERTURA = 20


def fuera_de_cobertura(db, lat, lon):
    """True si no hay ningún punto ya cubierto (con zona asignada, ya sea una solicitud real de
    hoy o un punto de referencia guardado en zonas_referencia) a menos de LIMITE_MINUTOS_COBERTURA
    minutos de manejo real desde (lat, lon). Si todavía no hay ningún punto con zona en el sistema
    (arranque en frío, p. ej. justo después de vaciar la base de datos), se compara contra el
    depósito en su lugar — así la primera zona que se cree de forma automática sigue respetando un
    radio real de cobertura, en vez de aceptar cualquier lugar."""
    filas = db.execute(
        "SELECT lat, lon FROM solicitudes WHERE zona IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL "
        "UNION ALL SELECT lat, lon FROM zonas_referencia"
    ).fetchall()
    if not filas:
        mejor = {"lat": DEPOT_LAT, "lon": DEPOT_LON}
    else:
        mejor = min(filas, key=lambda f: haversine_km(lat, lon, f["lat"], f["lon"]))
    osrm = osrm_distancia_duracion([(lat, lon), (mejor["lat"], mejor["lon"])])
    if osrm:
        minutos = osrm[1] * FACTOR_TRAFICO
    else:
        minutos = haversine_km(lat, lon, mejor["lat"], mejor["lon"]) / VELOCIDAD_PROMEDIO_KMH * 60
    return minutos > LIMITE_MINUTOS_COBERTURA


RADIO_DIRECCION_DUPLICADA_KM = 0.03  # ~30 metros: se considera la misma dirección


def direccion_ya_registrada(db, lat, lon):
    """True si ya existe un paciente (solicitud con dirección de recolección, no cancelada)
    a menos de RADIO_DIRECCION_DUPLICADA_KM de (lat, lon) — es decir, la misma dirección."""
    filas = db.execute(
        "SELECT lat, lon FROM solicitudes WHERE lat IS NOT NULL AND lon IS NOT NULL "
        "AND estado != 'cancelada'"
    ).fetchall()
    return any(haversine_km(lat, lon, f["lat"], f["lon"]) <= RADIO_DIRECCION_DUPLICADA_KM for f in filas)


def reequilibrar_rutas_zona(db, zona, nueva_solicitud_id=None):
    """Si la zona ya tiene rutas planificadas (aún sin iniciar), las recalcula —incluyendo,
    si se indica, una solicitud recién agregada— para que las paradas queden repartidas sin
    que ninguna ruta exceda DURACION_MAXIMA_RUTA_MIN. No toca rutas ya iniciadas o completadas."""
    rutas_zona = db.execute(
        "SELECT * FROM rutas WHERE estado = 'planificada' AND hora_inicio_real IS NULL "
        "AND zona = ? ORDER BY id",
        (zona,),
    ).fetchall()
    if not rutas_zona:
        return

    puntos = []
    for r in rutas_zona:
        puntos.extend(dict(p) for p in db.execute(
            "SELECT s.id, s.lat, s.lon, p.tipo, p.solicitud_extra_id AS extra_id, p.tipo_extra, "
            "s.cliente_id, s.direccion "
            "FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id WHERE p.ruta_id = ? ORDER BY p.orden",
            (r["id"],),
        ).fetchall())

    ids_ya_programados = set()
    for pt in puntos:
        ids_ya_programados.add(pt["id"])
        if pt.get("extra_id"):
            ids_ya_programados.add(pt["extra_id"])

    if nueva_solicitud_id is not None:
        nueva = db.execute(
            "SELECT id, lat, lon, estado, cliente_id, direccion FROM solicitudes WHERE id = ?",
            (nueva_solicitud_id,),
        ).fetchone()
        if nueva:
            tipo = "entrega" if nueva["estado"] == "pendiente_entrega" else "recoleccion"
            fusionado = False
            nueva_direccion = (nueva["direccion"] or "").strip().lower()
            for pt in puntos:
                if pt.get("extra_id"):
                    continue
                if nueva["cliente_id"] is not None:
                    coincide = pt.get("cliente_id") == nueva["cliente_id"]
                else:
                    coincide = (
                        pt.get("cliente_id") is None
                        and nueva_direccion
                        and (pt.get("direccion") or "").strip().lower() == nueva_direccion
                    )
                if coincide:
                    pt["extra_id"] = nueva["id"]
                    pt["tipo_extra"] = tipo
                    fusionado = True
                    break
            if not fusionado:
                puntos.append({
                    "id": nueva["id"], "lat": nueva["lat"], "lon": nueva["lon"], "tipo": tipo,
                    "extra_id": None, "tipo_extra": None, "cliente_id": nueva["cliente_id"],
                    "direccion": nueva["direccion"],
                })

    if not puntos:
        return

    # Reordena por cercanía real (vecino más cercano desde el depósito) antes de dividir en
    # tandas: así, si se agregó una solicitud nueva, queda intercalada en la posición que le
    # corresponde por cercanía en vez de ir siempre al final.
    puntos = ordenar_por_cercania(puntos)

    grupos_sin_filtrar = dividir_puntos_por_duracion(puntos)
    grupos = []
    sobrantes_cajas = []
    for grupo in grupos_sin_filtrar:
        grupo_filtrado, sobrantes = limitar_cajas_grupo(db, grupo)
        if grupo_filtrado:
            grupos.append(grupo_filtrado)
        sobrantes_cajas.extend(sobrantes)
    ruta_base = rutas_zona[0]

    for r in rutas_zona:
        db.execute("DELETE FROM paradas WHERE ruta_id = ?", (r["id"],))
        db.execute("DELETE FROM rutas WHERE id = ?", (r["id"],))

    for p in sobrantes_cajas:
        estado_previo = "pendiente_entrega" if p["tipo"] == "entrega" else "pendiente"
        db.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (estado_previo, p["id"]))
        if p.get("extra_id"):
            estado_previo_extra = "pendiente_entrega" if p.get("tipo_extra") == "entrega" else "pendiente"
            db.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (estado_previo_extra, p["extra_id"]))

    proximo_numero_ruta = siguiente_numero_ruta(db)
    parada_ids_nuevas = []
    # A diferencia de admin_rutas_masivas (que crea rutas desde cero), aquí NO se aplica el piso
    # MIN_PARADAS_DESPACHO: esta zona ya tenía al menos una ruta planificada con pacientes que
    # probablemente ya fueron notificados ("tu recolección quedó programada..."). Aunque el
    # reajuste deje una tanda por debajo del mínimo, se sigue creando su ruta en vez de arriesgarse
    # a desprogramar a alguien que ya avisamos.
    for idx, grupo in enumerate(grupos, start=1):
        if idx == 1:
            nombre_ruta = zona
        else:
            estimado_grupo = estimar_ruta(grupo)
            km = estimado_grupo["distancia_km"] if estimado_grupo else 0
            nombre_ruta = f"Ruta {proximo_numero_ruta:02d} ({km} km)"
            proximo_numero_ruta += 1
            for p in grupo:
                db.execute("UPDATE solicitudes SET zona = ? WHERE id = ?", (nombre_ruta, p["id"]))
                if p.get("extra_id"):
                    db.execute("UPDATE solicitudes SET zona = ? WHERE id = ?", (nombre_ruta, p["extra_id"]))
        cur = db.execute(
            "INSERT INTO rutas (nombre, zona, fecha, hora_salida, recolector_id) VALUES (?, ?, ?, ?, ?)",
            (nombre_ruta, nombre_ruta, ruta_base["fecha"], ruta_base["hora_salida"], ruta_base["recolector_id"]),
        )
        ruta_id = cur.lastrowid
        for i, p in enumerate(grupo, start=1):
            cur_parada = db.execute(
                "INSERT INTO paradas (ruta_id, solicitud_id, solicitud_extra_id, tipo_extra, orden, tipo) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ruta_id, p["id"], p.get("extra_id"), p.get("tipo_extra"), i, p["tipo"]),
            )
            es_nueva = p["id"] not in ids_ya_programados or (
                p.get("extra_id") and p["extra_id"] not in ids_ya_programados
            )
            if es_nueva:
                parada_ids_nuevas.append(cur_parada.lastrowid)
            db.execute("UPDATE solicitudes SET estado = 'programada' WHERE id = ?", (p["id"],))
            if p.get("extra_id"):
                db.execute("UPDATE solicitudes SET estado = 'programada' WHERE id = ?", (p["extra_id"],))

    if parada_ids_nuevas:
        threading.Thread(
            target=_notificar_paradas_programadas, args=(parada_ids_nuevas,), daemon=True
        ).start()


def contar_pacientes_activos(db):
    """Cuenta pacientes distintos con al menos una solicitud activa (no en lista de espera).
    Cada cliente_id cuenta una sola vez aunque tenga varias solicitudes; cada solicitud sin
    cuenta de usuario (nombre_contacto) cuenta como un paciente distinto."""
    con_cuenta = db.execute(
        "SELECT COUNT(DISTINCT cliente_id) AS n FROM solicitudes "
        "WHERE cliente_id IS NOT NULL AND estado != 'lista_espera'"
    ).fetchone()["n"]
    sin_cuenta = db.execute(
        "SELECT COUNT(*) AS n FROM solicitudes WHERE cliente_id IS NULL AND estado != 'lista_espera'"
    ).fetchone()["n"]
    return con_cuenta + sin_cuenta


def promover_lista_espera(db):
    """Si hay lugar libre (menos de MAX_PACIENTES_ACTIVOS pacientes activos), activa al
    paciente que lleva más tiempo en la lista de espera por cupo lleno: le asigna zona y lo
    deja pendiente de entrega, igual que una alta nueva. No toca a quienes están pendientes de
    ruta por falta de cobertura (fuera_cobertura=1) — a esos solo los puede activar un admin
    cuando de verdad haya ruta en su zona."""
    if contar_pacientes_activos(db) >= MAX_PACIENTES_ACTIVOS:
        return
    siguiente = db.execute(
        "SELECT * FROM solicitudes WHERE estado = 'lista_espera' AND fuera_cobertura = 0 "
        "ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    if siguiente is None:
        return

    zona = None
    if siguiente["lat"] is not None and siguiente["lon"] is not None:
        cercana = zona_mas_cercana(db, siguiente["lat"], siguiente["lon"])
        zona = cercana[0] if cercana else ZONA_BOOTSTRAP_DEFAULT

    db.execute(
        "UPDATE solicitudes SET estado = 'pendiente_entrega', zona = ? WHERE id = ?",
        (zona, siguiente["id"]),
    )
    if zona:
        reequilibrar_rutas_zona(db, zona, siguiente["id"])
        zona = db.execute("SELECT zona FROM solicitudes WHERE id = ?", (siguiente["id"],)).fetchone()["zona"]

    nombre = siguiente["nombre_contacto"]
    telefono_paciente = None
    if siguiente["cliente_id"]:
        u = db.execute("SELECT name, telefono FROM users WHERE id = ?", (siguiente["cliente_id"],)).fetchone()
        if u:
            nombre = u["name"]
            telefono_paciente = u["telefono"]
    mensaje = f"'{nombre}' salió de la lista de espera y ya quedó activo"
    mensaje += f", asignado a {zona}." if zona else "."
    crear_notificacion_admin(db, siguiente["cliente_id"], mensaje)

    if telefono_paciente:
        texto_zona = f"quedaste integrado a {zona}." if zona else "en breve te asignaremos una ruta."
        enviar_whatsapp_primer_contacto(
            telefono_whatsapp_e164(telefono_paciente),
            "TWILIO_TEMPLATE_SALIO_ESPERA_SID",
            {"1": nombre, "2": texto_zona},
            f"Hola {nombre},\n\n"
            f"¡Buenas noticias! Ya se liberó un lugar y saliste de la lista de espera: {texto_zona}\n"
            "Te avisaremos con la fecha y el horario aproximado en cuanto tu recolección quede programada.",
        )


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # Si dos peticiones escriben casi al mismo tiempo (p. ej. un doble clic en "generar
        # rutas"), que la segunda espere a que la primera termine su transacción en vez de fallar
        # de inmediato con "database is locked". 20s da margen de sobra incluso cuando la primera
        # tiene que esperar varias llamadas al servicio de mapas (OSRM) antes de guardar.
        g.db.execute("PRAGMA busy_timeout = 20000")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


SEED_SOLICITUDES_PATH = os.path.join(BASE_DIR, "seed_solicitudes.json")


def init_db():
    is_new = not os.path.exists(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    if is_new:
        with open(SCHEMA_PATH) as f:
            db.executescript(f.read())
        db.execute(
            "INSERT INTO users (name, email, password_hash, role, es_admin_general) VALUES (?, ?, ?, ?, 1)",
            ("Administrador", "admin@rutas.local", generate_password_hash("admin123", method="pbkdf2:sha256"), "admin"),
        )
        if os.path.exists(SEED_SOLICITUDES_PATH):
            # Estos puntos no son pacientes reales — son solo la referencia de cobertura con la
            # que se delimitaron las zonas (ver zonas_referencia y fuera_de_cobertura()). Por eso
            # se cargan como referencia, no como solicitudes: así una base nueva arranca con la
            # misma cobertura ya delimitada, sin ensuciar la lista de pacientes con ejemplos.
            with open(SEED_SOLICITUDES_PATH, encoding="utf-8") as f:
                puntos = json.load(f)
            for p in puntos:
                db.execute(
                    "INSERT INTO zonas_referencia (zona, lat, lon) VALUES (?, ?, ?)",
                    (p["zona"], p["lat"], p["lon"]),
                )
            print(f"Se cargaron {len(puntos)} puntos de referencia de cobertura desde seed_solicitudes.json.")
        db.commit()
        print("Base de datos creada. Login admin -> admin@rutas.local / admin123")
    db.close()
    aplicar_migraciones_pendientes()


def aplicar_migraciones_pendientes():
    """Aplica a una base de datos YA EXISTENTE los cambios de esquema que se agregaron después de
    que Render pasó a usar disco persistente — antes, cada deploy recreaba la base desde cero con
    schema.sql ya actualizado, así que nunca hacía falta esto; ahora la base sobrevive entre
    deploys, así que un cambio nuevo en schema.sql no llega solo a producción. Cada paso se
    protege para poder correr en cada arranque sin problema (columna/tabla ya existente se
    ignora), así que agregar aquí un paso nuevo cada vez que se toque el esquema es suficiente
    para que se aplique solo, tanto en local como en Render, sin depender de correr nada a mano."""
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    columnas_users = {r["name"] for r in db.execute("PRAGMA table_info(users)")}
    for columna, definicion in [
        ("telefono", "TEXT"),
        ("es_admin_general", "INTEGER NOT NULL DEFAULT 0"),
        ("nef_ultima_vista", "TEXT"),
    ]:
        if columna not in columnas_users:
            db.execute(f"ALTER TABLE users ADD COLUMN {columna} {definicion}")

    tablas = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    if "horas_extra" not in tablas:
        db.execute(
            "CREATE TABLE horas_extra ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  recolector_id INTEGER NOT NULL REFERENCES users(id),"
            "  fecha TEXT NOT NULL DEFAULT (date('now','localtime')),"
            "  hora_inicio TEXT NOT NULL,"
            "  hora_salida TEXT NOT NULL,"
            "  horas_trabajadas REAL NOT NULL,"
            "  horas_extra REAL NOT NULL,"
            "  created_at TEXT DEFAULT (datetime('now','localtime'))"
            ")"
        )

    if "avisos_programados" not in tablas:
        db.execute(
            "CREATE TABLE avisos_programados ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  telefono TEXT NOT NULL,"
            "  content_sid_env TEXT NOT NULL,"
            "  content_variables_json TEXT,"
            "  cuerpo_libre TEXT NOT NULL,"
            "  enviar_despues_de TEXT NOT NULL,"
            "  enviado INTEGER NOT NULL DEFAULT 0,"
            "  created_at TEXT DEFAULT (datetime('now','localtime'))"
            ")"
        )

    if "zonas_referencia" not in tablas:
        db.execute(
            "CREATE TABLE zonas_referencia ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  zona TEXT NOT NULL,"
            "  lat REAL NOT NULL,"
            "  lon REAL NOT NULL,"
            "  created_at TEXT DEFAULT (datetime('now','localtime'))"
            ")"
        )
        # Migración única, justo al crear la tabla por primera vez: los puntos de ejemplo
        # (importados sin cliente_id, nunca fueron pacientes reales) que se usaron para delimitar
        # la cobertura actual quedan guardados aquí como referencia permanente —así
        # fuera_de_cobertura()/zona_mas_cercana() los siguen usando— y se eliminan de solicitudes.
        db.execute(
            "INSERT INTO zonas_referencia (zona, lat, lon) "
            "SELECT zona, lat, lon FROM solicitudes "
            "WHERE cliente_id IS NULL AND zona IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL"
        )
        db.execute("DELETE FROM solicitudes WHERE cliente_id IS NULL")

    db.commit()
    db.close()


# ---------- Auth helpers ----------

def current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()


def login_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                return redirect(url_for("login"))
            if roles and user["role"] not in roles:
                flash("No tienes acceso a esa sección.", "error")
                return redirect(url_for("home"))
            return view(user, *args, **kwargs)
        return wrapped
    return decorator


@app.context_processor
def inject_user():
    return {"current_user": current_user(), "ESTADO_LABELS": ESTADO_LABELS}


# ---------- General ----------

@app.route("/")
def home():
    user = current_user()
    if user is None:
        return render_template("landing.html")
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    if user["role"] == "recolector":
        return redirect(url_for("recolector_dashboard"))
    if user["role"] == "nef":
        return redirect(url_for("nef_dashboard"))
    if user["role"] == "cliente":
        if not user["email_verificado"]:
            return redirect(url_for("cliente_verificar_correo"))
        if not user["aviso_privacidad_aceptado"]:
            return redirect(url_for("cliente_privacidad"))
        if not user["perfil_completo"]:
            return redirect(url_for("cliente_bienvenida"))
        if not user["alta_completa"]:
            return redirect(url_for("cliente_alta"))
    return redirect(url_for("cliente_dashboard"))


TIPO_LOGIN_ROLES = {"admin": "admin", "recolector": "recolector", "cliente": "cliente", "nef": "nef"}
TIPO_LOGIN_LABELS = {"admin": "Administrador", "recolector": "Recolector", "cliente": "Paciente", "nef": "NEF"}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form["password"]
        tipo = request.form.get("tipo") or None
        db = get_db()
        if tipo == "cliente":
            telefono = telefono_identidad(request.form.get("telefono", ""))
            user = db.execute(
                "SELECT * FROM users WHERE telefono = ? AND role = 'cliente'", (telefono,)
            ).fetchone()
            error_no_existe = "Ese número de WhatsApp no está registrado."
        else:
            email = request.form["email"].strip().lower()
            user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            error_no_existe = "Este correo no está registrado."
        if user is None:
            flash(error_no_existe, "error")
            return render_template("login.html", tipo=tipo, tipo_label=TIPO_LOGIN_LABELS.get(tipo))
        if not check_password_hash(user["password_hash"], password):
            error_password = "Número de WhatsApp o contraseña incorrectos." if tipo == "cliente" else "Correo o contraseña incorrectos."
            flash(error_password, "error")
            return render_template("login.html", tipo=tipo, tipo_label=TIPO_LOGIN_LABELS.get(tipo))
        if tipo and TIPO_LOGIN_ROLES.get(tipo) != user["role"]:
            flash(f"Esa cuenta no es de {TIPO_LOGIN_LABELS.get(tipo, tipo)}.", "error")
            return render_template("login.html", tipo=tipo, tipo_label=TIPO_LOGIN_LABELS.get(tipo))
        session.clear()
        session["user_id"] = user["id"]
        return redirect(url_for("home"))
    tipo = request.args.get("tipo") or None
    return render_template("login.html", tipo=tipo, tipo_label=TIPO_LOGIN_LABELS.get(tipo))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/olvide-password", methods=["GET", "POST"])
def olvide_password():
    if request.method == "POST":
        tipo = request.form.get("tipo") or None
        db = get_db()
        if tipo == "cliente":
            telefono = telefono_identidad(request.form.get("telefono", ""))
            user = db.execute(
                "SELECT * FROM users WHERE telefono = ? AND role = 'cliente'", (telefono,)
            ).fetchone()
        else:
            email = request.form.get("email", "").strip().lower()
            user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            expira = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "UPDATE users SET reset_token = ?, reset_token_expira = ? WHERE id = ?",
                (token, expira, user["id"]),
            )
            db.commit()
            link = url_absoluta("restablecer_password", token=token)
            cuerpo = (
                f"Hola {user['name']},\n\n"
                "Recibimos una solicitud para restablecer tu contraseña en RE-PVC.\n"
                f"Entra a este enlace para poner una nueva (válido por 1 hora):\n{link}\n\n"
                "Si tú no pediste esto, ignora este mensaje."
            )
            if tipo == "cliente":
                enviar_whatsapp_primer_contacto(
                    telefono_whatsapp_e164(user["telefono"]),
                    "TWILIO_TEMPLATE_RESET_PASSWORD_SID",
                    {"1": user["name"], "2": link},
                    cuerpo,
                )
            else:
                enviar_email(user["email"], "Recuperar contraseña — RE-PVC", cuerpo)
        mensaje = (
            "Si ese número de WhatsApp está registrado, te enviamos un enlace para restablecer tu contraseña."
            if tipo == "cliente"
            else "Si ese correo está registrado, te enviamos un enlace para restablecer tu contraseña."
        )
        flash(mensaje, "success")
        return redirect(url_for("login", tipo=tipo))
    tipo = request.args.get("tipo") or None
    return render_template("olvide_password.html", tipo=tipo)


@app.route("/restablecer-password/<token>", methods=["GET", "POST"])
def restablecer_password(token):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE reset_token = ?", (token,)).fetchone()
    valido = user is not None
    if valido and user["reset_token_expira"]:
        try:
            expira = datetime.strptime(user["reset_token_expira"], "%Y-%m-%d %H:%M:%S")
            valido = datetime.now() <= expira
        except ValueError:
            valido = False
    if not valido:
        flash("Ese enlace ya no es válido o expiró. Solicita uno nuevo.", "error")
        return redirect(url_for("olvide_password"))

    if request.method == "POST":
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if len(password) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "error")
            return render_template("restablecer_password.html", token=token)
        if password != password2:
            flash("Las contraseñas no coinciden.", "error")
            return render_template("restablecer_password.html", token=token)
        db.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expira = NULL WHERE id = ?",
            (generate_password_hash(password, method="pbkdf2:sha256"), user["id"]),
        )
        db.commit()
        flash("Contraseña actualizada. Ya puedes iniciar sesión.", "success")
        return redirect(url_for("login"))
    return render_template("restablecer_password.html", token=token)


def marcar_parada_ausente_por_rechazo(db, parada_id):
    """Cuando el paciente confirma DE ANTEMANO que NO va a poder recibir la recolección/entrega
    programada (antes de que el recolector siquiera salga), la quita por completo de la ruta de
    ese día —a diferencia de cuando el recolector llega y no encuentra a nadie, aquí ni falta
    hacer que pase por ahí— y regresa la(s) solicitud(es) a pendiente para que se puedan
    reprogramar en otra ruta. No toca kg ni movimientos de inventario porque no se llegó a
    recoger/entregar nada. Devuelve (ruta_id, lat, lon, solicitud_id) de la parada eliminada —para
    poder intentar llenar el hueco después, ya que la parada en sí ya no existe— o None si no
    hizo nada."""
    parada = db.execute(
        "SELECT p.*, s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id WHERE p.id = ?",
        (parada_id,),
    ).fetchone()
    if parada is None or parada["estado"] != "pendiente":
        return None
    es_entrega = parada["tipo"] == "entrega"
    solicitud_estado = "pendiente_entrega" if es_entrega else "pendiente"
    db.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (solicitud_estado, parada["solicitud_id"]))
    if parada["solicitud_extra_id"]:
        es_entrega_extra = parada["tipo_extra"] == "entrega"
        solicitud_estado_extra = "pendiente_entrega" if es_entrega_extra else "pendiente"
        db.execute(
            "UPDATE solicitudes SET estado = ? WHERE id = ?",
            (solicitud_estado_extra, parada["solicitud_extra_id"]),
        )
    db.execute("DELETE FROM paradas WHERE id = ?", (parada_id,))
    return parada["ruta_id"], parada["lat"], parada["lon"], parada["solicitud_id"]


@app.route("/parada/<token>/confirmar", methods=["GET", "POST"])
def parada_confirmar(token):
    """El GET solo muestra la página con el botón de confirmar, sin actualizar nada — ver el
    comentario de verificar_correo() sobre por qué (el robot de vista previa de WhatsApp
    precarga el link antes de que el paciente lo abra)."""
    respuesta = request.args.get("respuesta")
    if respuesta not in ("si", "no"):
        return render_template("parada_confirmar.html", valido=False), 400
    db = get_db()
    parada = db.execute(
        "SELECT p.*, s.direccion, r.fecha FROM paradas p "
        "JOIN solicitudes s ON s.id = p.solicitud_id JOIN rutas r ON r.id = p.ruta_id "
        "WHERE p.confirmacion_token = ?",
        (token,),
    ).fetchone()
    if parada is None:
        return render_template("parada_confirmar.html", valido=False), 404
    if request.method == "GET":
        return render_template("parada_confirmar.html", valido=True, pendiente=True, respuesta=respuesta, parada=parada)
    db.execute("UPDATE paradas SET confirmado_paciente = ? WHERE id = ?", (respuesta, parada["id"]))
    if respuesta == "no":
        resultado = marcar_parada_ausente_por_rechazo(db, parada["id"])
        db.commit()
        if resultado:
            ruta_id, lat, lon, solicitud_id = resultado
            intentar_llenar_hueco_ausente(
                db, ruta_id, lat, lon, solicitud_id_ausente=solicitud_id
            )
    else:
        db.commit()
    return render_template("parada_confirmar.html", valido=True, respuesta=respuesta, parada=parada)


@app.route("/solicitud/<token>/existencia", methods=["GET", "POST"])
def solicitud_confirmar_existencia(token):
    """El GET solo muestra la página con el botón de confirmar, sin actualizar nada — ver el
    comentario de verificar_correo() sobre por qué (el robot de vista previa de WhatsApp
    precarga el link antes de que el paciente lo abra)."""
    respuesta = request.args.get("respuesta")
    if respuesta not in ("si", "cancelar"):
        return render_template("solicitud_existencia_confirmar.html", valido=False), 400
    db = get_db()
    sol = db.execute("SELECT * FROM solicitudes WHERE token_existencia = ?", (token,)).fetchone()
    if sol is None:
        return render_template("solicitud_existencia_confirmar.html", valido=False), 404
    if sol["confirmado_existencia"] or sol["estado"] == "cancelada":
        return render_template(
            "solicitud_existencia_confirmar.html", valido=True, respuesta="ya_resuelto", solicitud=sol
        )
    if request.method == "GET":
        return render_template(
            "solicitud_existencia_confirmar.html", valido=True, pendiente=True, respuesta=respuesta, solicitud=sol
        )

    if respuesta == "cancelar":
        db.execute(
            "UPDATE solicitudes SET estado = 'cancelada', token_existencia = NULL WHERE id = ?", (sol["id"],)
        )
        db.commit()
        return render_template(
            "solicitud_existencia_confirmar.html", valido=True, respuesta="cancelar", solicitud=sol
        )

    necesita = sol["cantidad_cajas"] or 0
    if existencia_caja(db, sol["material"]) < necesita:
        db.execute(
            "UPDATE solicitudes SET notificado_existencia = 0, token_existencia = NULL WHERE id = ?",
            (sol["id"],),
        )
        db.commit()
        return render_template(
            "solicitud_existencia_confirmar.html", valido=True, respuesta="agotado", solicitud=sol
        )

    db.execute(
        "UPDATE solicitudes SET confirmado_existencia = 1, token_existencia = NULL WHERE id = ?", (sol["id"],)
    )
    if sol["recoger_en_sitio"]:
        registrar_movimiento_cajas(
            db, sol["material"], "entrega", -necesita, f"Recolección en sitio — solicitud #{sol['id']}",
        )
    db.commit()
    return render_template("solicitud_existencia_confirmar.html", valido=True, respuesta="si", solicitud=sol)


@app.route("/cliente/paradas/<int:parada_id>/confirmar", methods=["POST"])
@login_required("cliente")
def cliente_confirmar_parada(user, parada_id):
    respuesta = request.form.get("respuesta")
    if respuesta not in ("si", "no"):
        flash("Respuesta inválida.", "error")
        return redirect(url_for("cliente_dashboard"))
    db = get_db()
    parada = db.execute(
        "SELECT p.id FROM paradas p "
        "JOIN solicitudes s ON s.id = p.solicitud_id "
        "LEFT JOIN solicitudes s2 ON s2.id = p.solicitud_extra_id "
        "WHERE p.id = ? AND (s.cliente_id = ? OR s2.cliente_id = ?)",
        (parada_id, user["id"], user["id"]),
    ).fetchone()
    if parada is None:
        flash("No puedes confirmar esa recolección.", "error")
        return redirect(url_for("cliente_dashboard"))
    db.execute("UPDATE paradas SET confirmado_paciente = ? WHERE id = ?", (respuesta, parada_id))
    if respuesta == "no":
        resultado = marcar_parada_ausente_por_rechazo(db, parada_id)
        db.commit()
        if resultado:
            ruta_id, lat, lon, solicitud_id = resultado
            intentar_llenar_hueco_ausente(
                db, ruta_id, lat, lon, solicitud_id_ausente=solicitud_id
            )
    else:
        db.commit()
    flash(
        "Gracias, confirmaste que sí podrás recibir la recolección." if respuesta == "si"
        else "Gracias, avisamos que no podrás recibir la recolección ese día — se reprogramará para otro día.",
        "success",
    )
    return redirect(url_for("cliente_dashboard"))


@app.route("/paciente")
def paciente_intro():
    return render_template("paciente_intro.html")


@app.route("/paciente/verificar-zona")
def paciente_verificar_zona():
    codigo_postal = request.args.get("cp", "").strip()
    if not codigo_postal:
        return jsonify({"error": "Escribe tu código postal."}), 400
    if not codigo_postal.isdigit() or len(codigo_postal) != 5:
        return jsonify({"error": "El código postal debe tener 5 dígitos."}), 400
    resultados = geocodificar_codigo_postal(codigo_postal)
    if not resultados:
        return jsonify({"error": "No se encontró ese código postal."}), 404
    db = get_db()
    for r in resultados:
        r["cubierto"] = not fuera_de_cobertura(db, r["lat"], r["lon"])
    return jsonify({"resultados": resultados})


@app.route("/registro", methods=["GET", "POST"])
def registro():
    if request.method == "POST":
        name = request.form["name"].strip()
        telefono = telefono_identidad(request.form.get("telefono", ""))
        password = request.form["password"]
        if telefono is None:
            flash("Escribe un número de WhatsApp válido de 10 dígitos.", "error")
            return render_template("registro.html")
        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE telefono = ?", (telefono,)).fetchone()
        if existing:
            flash("Ese número de WhatsApp ya está registrado.", "error")
            return render_template("registro.html")
        token = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO users (name, telefono, password_hash, role, email_verificado, verificacion_token) "
            "VALUES (?, ?, ?, 'cliente', 0, ?)",
            (name, telefono, generate_password_hash(password, method="pbkdf2:sha256"), token),
        )
        db.commit()
        link = url_absoluta("verificar_correo", token=token)
        enviado = enviar_whatsapp_primer_contacto(
            telefono_whatsapp_e164(telefono),
            "TWILIO_TEMPLATE_VERIFICACION_SID",
            {"1": name, "2": link},
            f"Hola {name},\n\nGracias por registrarte en RE-PVC. Confirma tu cuenta entrando a este enlace:\n{link}\n\n"
            "Si tú no creaste esta cuenta, ignora este mensaje.",
        )
        if enviado:
            flash("Cuenta creada. Revisa tu WhatsApp para verificarla antes de continuar.", "success")
        else:
            flash(
                "Cuenta creada, pero no pudimos enviarte el mensaje de verificación en este momento. "
                "Inicia sesión y usa la opción de reenviar el mensaje desde tu cuenta.",
                "error",
            )
        return redirect(url_for("login", tipo="cliente"))
    return render_template("registro.html")


@app.route("/verificar-correo/<token>", methods=["GET", "POST"])
def verificar_correo(token):
    """El GET solo muestra la página con el botón de confirmar, sin tocar la base de datos —
    WhatsApp manda un robot (facebookexternalhit) a precargar el link para armar la vista previa
    en cuanto se envía el mensaje, antes de que la persona lo abra. Si el GET ya consumiera el
    token (como antes), el robot lo gastaba primero y el paciente se encontraba el enlace
    'inválido' segundos después. Por eso la acción real solo pasa en el POST, que solo dispara un
    clic humano en el botón, nunca el robot de vista previa."""
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE verificacion_token = ?", (token,)).fetchone()
    if user is None:
        if request.method == "GET":
            return render_template("verificar_correo.html", valido=False)
        flash("Ese enlace de verificación ya no es válido.", "error")
        return redirect(url_for("login"))
    if request.method == "GET":
        return render_template("verificar_correo.html", valido=True, nombre=user["name"])
    db.execute(
        "UPDATE users SET email_verificado = 1, verificacion_token = NULL WHERE id = ?", (user["id"],)
    )
    db.commit()
    flash("¡Cuenta verificada! Ya puedes continuar.", "success")
    session.clear()
    session["user_id"] = user["id"]
    return redirect(url_for("home"))


# ---------- Cliente ----------

@app.route("/cliente/verificar-correo", methods=["GET", "POST"])
@login_required("cliente")
def cliente_verificar_correo(user):
    if user["email_verificado"]:
        return redirect(url_for("home"))
    if request.method == "POST":
        token = secrets.token_urlsafe(32)
        db = get_db()
        db.execute("UPDATE users SET verificacion_token = ? WHERE id = ?", (token, user["id"]))
        db.commit()
        link = url_absoluta("verificar_correo", token=token)
        enviado = enviar_whatsapp_primer_contacto(
            telefono_whatsapp_e164(user["telefono"]),
            "TWILIO_TEMPLATE_VERIFICACION_SID",
            {"1": user["name"], "2": link},
            f"Hola {user['name']},\n\nConfirma tu cuenta entrando a este enlace:\n{link}",
        )
        if enviado:
            flash("Te reenviamos el mensaje de verificación por WhatsApp.", "success")
        else:
            flash("No pudimos enviar el mensaje en este momento. Intenta de nuevo en unos minutos.", "error")
    return render_template("cliente_verificar_correo.html")


@app.route("/cliente/privacidad", methods=["GET", "POST"])
@login_required("cliente")
def cliente_privacidad(user):
    if not user["email_verificado"]:
        return redirect(url_for("cliente_verificar_correo"))
    if user["aviso_privacidad_aceptado"]:
        return redirect(url_for("home"))
    if request.method == "POST":
        db = get_db()
        db.execute("UPDATE users SET aviso_privacidad_aceptado = 1 WHERE id = ?", (user["id"],))
        db.commit()
        return redirect(url_for("home"))
    return render_template("cliente_privacidad.html")


@app.route("/cliente/bienvenida", methods=["GET", "POST"])
@login_required("cliente")
def cliente_bienvenida(user):
    if not user["email_verificado"]:
        return redirect(url_for("cliente_verificar_correo"))
    if not user["aviso_privacidad_aceptado"]:
        return redirect(url_for("cliente_privacidad"))
    if user["perfil_completo"]:
        return redirect(url_for("cliente_alta"))

    if request.method == "POST":
        edad = request.form.get("edad", "").strip()
        tipo_maquina = request.form.get("tipo_maquina")
        marca = request.form.get("marca")
        frecuencia_semana = request.form.get("frecuencia_semana", "").strip()
        causa_enfermedad = request.form.get("causa_enfermedad")
        recibir_info_nef = request.form.get("recibir_info_nef")

        if tipo_maquina not in ("maquina", "manual") or marca not in ("baxter", "pisa"):
            flash("Selecciona el tipo y la marca.", "error")
            return render_template("cliente_bienvenida.html")
        if causa_enfermedad not in ("diabetes", "hipertension", "autoinmune", "desconocida"):
            flash("Selecciona la causa de la enfermedad renal.", "error")
            return render_template("cliente_bienvenida.html")
        if recibir_info_nef not in ("0", "1"):
            flash("Dinos si quieres recibir información de NEF.", "error")
            return render_template("cliente_bienvenida.html")
        try:
            edad = int(edad)
            frecuencia_semana = int(frecuencia_semana)
        except ValueError:
            flash("Edad y frecuencia deben ser números.", "error")
            return render_template("cliente_bienvenida.html")

        db = get_db()
        db.execute(
            "UPDATE users SET edad = ?, tipo_maquina = ?, marca = ?, frecuencia_semana = ?, "
            "causa_enfermedad = ?, recibir_info_nef = ?, perfil_completo = 1 WHERE id = ?",
            (edad, tipo_maquina, marca, frecuencia_semana, causa_enfermedad, recibir_info_nef, user["id"]),
        )
        db.commit()
        if (tipo_maquina, marca) in VIDEOS_PACIENTE:
            return redirect(url_for("cliente_video"))
        return redirect(url_for("cliente_alta"))
    return render_template("cliente_bienvenida.html")


VIDEOS_PACIENTE = {
    ("manual", "baxter"): "baxter-manual.mp4",
    ("maquina", "baxter"): "baxter-maquina.mp4",
    ("maquina", "pisa"): "pisa-maquina.mp4",
    ("manual", "pisa"): "pisa-manual.mp4",
}


@app.route("/cliente/video")
@login_required("cliente")
def cliente_video(user):
    if not user["perfil_completo"]:
        return redirect(url_for("cliente_bienvenida"))
    if user["alta_completa"]:
        return redirect(url_for("cliente_dashboard"))
    video = VIDEOS_PACIENTE.get((user["tipo_maquina"], user["marca"]))
    if not video:
        return redirect(url_for("cliente_alta"))
    return render_template("cliente_video.html", video=video)


@app.route("/cliente/alta", methods=["GET", "POST"])
@login_required("cliente")
def cliente_alta(user):
    if not user["email_verificado"]:
        return redirect(url_for("cliente_verificar_correo"))
    if not user["aviso_privacidad_aceptado"]:
        return redirect(url_for("cliente_privacidad"))
    if not user["perfil_completo"]:
        return redirect(url_for("cliente_bienvenida"))
    if user["alta_completa"]:
        return redirect(url_for("cliente_dashboard"))

    if request.method == "POST":
        direccion = request.form["direccion"].strip()
        codigo_postal = request.form.get("codigo_postal", "").strip() or None
        lat = request.form.get("lat", "").strip()
        lon = request.form.get("lon", "").strip()
        try:
            lat = float(lat) if lat else None
            lon = float(lon) if lon else None
        except ValueError:
            lat = lon = None

        db = get_db()
        zona = None
        sin_cobertura = False
        if lat is not None and lon is not None:
            if direccion_ya_registrada(db, lat, lon):
                flash("Esa dirección ya está registrada con otro paciente.", "error")
                return render_template("cliente_alta.html")
            sin_cobertura = fuera_de_cobertura(db, lat, lon)

        en_espera = contar_pacientes_activos(db) >= MAX_PACIENTES_ACTIVOS
        if not en_espera and not sin_cobertura and lat is not None and lon is not None:
            cercana = zona_mas_cercana(db, lat, lon)
            zona = cercana[0] if cercana else ZONA_BOOTSTRAP_DEFAULT

        estado_inicial = "lista_espera" if (en_espera or sin_cobertura) else "pendiente_entrega"
        cur = db.execute(
            "INSERT INTO solicitudes (cliente_id, direccion, codigo_postal, material, lat, lon, zona, "
            "estado, fuera_cobertura) VALUES (?, ?, ?, 'PVC', ?, ?, ?, ?, ?)",
            (user["id"], direccion, codigo_postal, lat, lon, zona, estado_inicial,
             1 if sin_cobertura else 0),
        )
        db.execute("UPDATE users SET alta_completa = 1, terminos_aceptados = 1 WHERE id = ?", (user["id"],))
        if zona:
            reequilibrar_rutas_zona(db, zona, cur.lastrowid)
            zona = db.execute("SELECT zona FROM solicitudes WHERE id = ?", (cur.lastrowid,)).fetchone()["zona"]
        if en_espera:
            mensaje = (
                f"'{user['name']}' se dio de alta — {direccion}. "
                f"Cupo lleno ({MAX_PACIENTES_ACTIVOS} pacientes activos): quedó en lista de espera."
            )
        elif sin_cobertura:
            mensaje = (
                f"'{user['name']}' se dio de alta — {direccion}. "
                "No hay ruta en su zona: quedó pendiente de ruta."
            )
        else:
            mensaje = f"'{user['name']}' se dio de alta — {direccion}. Pendiente de entrega de bote."
            if zona:
                mensaje += f" Asignado a {zona}."
        crear_notificacion_admin(db, user["id"], mensaje)
        db.commit()
        if en_espera:
            enviar_whatsapp_primer_contacto(
                telefono_whatsapp_e164(user["telefono"]),
                "TWILIO_TEMPLATE_LISTA_ESPERA_SID",
                {"1": user["name"], "2": str(MAX_PACIENTES_ACTIVOS)},
                f"Hola {user['name']},\n\n"
                f"Por ahora llegamos al cupo máximo de {MAX_PACIENTES_ACTIVOS} pacientes activos, "
                "así que tu registro quedó en la lista de espera.\n"
                "En cuanto se libere un lugar te integraremos a una ruta automáticamente y te "
                "avisaremos por este medio — no necesitas hacer nada más por ahora.",
            )
            flash(
                "¡Registro completo! Por ahora llegamos al cupo máximo de pacientes activos, "
                "así que quedaste en la lista de espera — te avisaremos en cuanto haya lugar.",
                "success",
            )
        elif sin_cobertura:
            enviar_whatsapp_primer_contacto(
                telefono_whatsapp_e164(user["telefono"]),
                "TWILIO_TEMPLATE_SIN_COBERTURA_SID",
                {"1": user["name"]},
                f"Hola {user['name']},\n\n"
                "Tu registro quedó completo. Por ahora no tenemos ruta en tu zona, así que tu recolección "
                "queda pendiente — en cuanto tengamos cobertura ahí te avisaremos y te integraremos a una ruta.",
            )
            flash(
                "¡Registro completo! Por ahora no tenemos ruta en tu zona — te avisaremos en cuanto la tengamos.",
                "success",
            )
        else:
            numero_paciente = db.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = 'cliente' AND id <= ?", (user["id"],)
            ).fetchone()["n"]
            flash(f"¡Bienvenido a la familia RE-PVC! Eres el paciente #{numero_paciente}.", "success")
        return redirect(url_for("cliente_dashboard"))
    return render_template("cliente_alta.html")


@app.route("/cliente")
@login_required("cliente")
def cliente_dashboard(user):
    if not user["email_verificado"]:
        return redirect(url_for("cliente_verificar_correo"))
    if not user["aviso_privacidad_aceptado"]:
        return redirect(url_for("cliente_privacidad"))
    if not user["perfil_completo"]:
        return redirect(url_for("cliente_bienvenida"))
    if not user["alta_completa"]:
        return redirect(url_for("cliente_alta"))

    db = get_db()
    solicitudes_rows = db.execute(
        "SELECT * FROM solicitudes WHERE cliente_id = ? ORDER BY created_at DESC", (user["id"],)
    ).fetchall()
    solicitudes = []
    for s in solicitudes_rows:
        sol = dict(s)
        sol["fecha_ruta"] = None
        sol["horario"] = None
        sol["recolector_nombre"] = None
        sol["parada_id"] = None
        sol["confirmado_paciente"] = None
        if sol["estado"] == "programada":
            parada = db.execute(
                "SELECT p.id, p.confirmado_paciente, r.fecha, r.hora_inicio_real, "
                "u.name AS recolector_nombre FROM paradas p "
                "JOIN rutas r ON r.id = p.ruta_id LEFT JOIN users u ON u.id = r.recolector_id "
                "WHERE (p.solicitud_id = ? OR p.solicitud_extra_id = ?) AND p.estado = 'pendiente' "
                "ORDER BY p.id DESC LIMIT 1",
                (s["id"], s["id"]),
            ).fetchone()
            if parada:
                sol["fecha_ruta"] = parada["fecha"]
                if parada["hora_inicio_real"]:
                    sol["horario"] = horario_estimado_siguiente(db, parada["id"])
                else:
                    sol["horario"] = horario_estimado_parada(db, parada["id"])
                sol["recolector_nombre"] = parada["recolector_nombre"]
                sol["parada_id"] = parada["id"]
                sol["confirmado_paciente"] = parada["confirmado_paciente"]
        solicitudes.append(sol)
    nef_publicaciones = []
    nef_confirmados = set()
    nef_nuevas = 0
    if user["recibir_info_nef"]:
        nef_publicaciones = db.execute(
            "SELECT * FROM nef_publicaciones ORDER BY created_at DESC"
        ).fetchall()
        nef_confirmados = {
            r["publicacion_id"] for r in db.execute(
                "SELECT publicacion_id FROM nef_confirmaciones WHERE cliente_id = ?", (user["id"],)
            ).fetchall()
        }
        nef_nuevas = db.execute(
            "SELECT COUNT(*) AS n FROM nef_publicaciones WHERE created_at > ?",
            (user["nef_ultima_vista"] or "1970-01-01",),
        ).fetchone()["n"]
    admin_videos = db.execute("SELECT * FROM admin_videos ORDER BY created_at DESC").fetchall()

    cajas_donadas = db.execute(
        "SELECT material, SUM(cantidad_cajas) AS total FROM solicitudes "
        "WHERE cliente_id = ? AND tipo_redistribucion = 'donar' AND estado = 'recolectada' "
        "AND cantidad_cajas IS NOT NULL GROUP BY material ORDER BY material",
        (user["id"],),
    ).fetchall()
    cajas_recibidas = db.execute(
        "SELECT material, SUM(cantidad_cajas) AS total FROM solicitudes "
        "WHERE cliente_id = ? AND tipo_redistribucion = 'material' AND cantidad_cajas IS NOT NULL "
        "AND (estado = 'pendiente' OR (recoger_en_sitio = 1 AND confirmado_existencia = 1)) "
        "GROUP BY material ORDER BY material",
        (user["id"],),
    ).fetchall()
    cajas_donadas_total = sum(r["total"] for r in cajas_donadas)
    cajas_recibidas_total = sum(r["total"] for r in cajas_recibidas)

    solicitud_principal = next((s for s in solicitudes if not s["tipo_redistribucion"]), None)

    return render_template(
        "cliente_dashboard.html", solicitudes=solicitudes,
        solicitud_principal=solicitud_principal,
        nef_publicaciones=nef_publicaciones, nef_confirmados=nef_confirmados, nef_nuevas=nef_nuevas,
        admin_videos=admin_videos,
        cajas_donadas=cajas_donadas, cajas_recibidas=cajas_recibidas,
        cajas_donadas_total=cajas_donadas_total, cajas_recibidas_total=cajas_recibidas_total,
        tipos_cajas=TIPOS_CAJAS,
    )


@app.route("/cliente/nef/marcar-visto", methods=["POST"])
@login_required("cliente")
def cliente_nef_marcar_visto(user):
    db = get_db()
    db.execute(
        "UPDATE users SET nef_ultima_vista = datetime('now','localtime') WHERE id = ?", (user["id"],)
    )
    db.commit()
    return ("", 204)


@app.route("/cliente/solicitudes/nueva", methods=["POST"])
@login_required("cliente")
def cliente_nueva_solicitud(user):
    material = request.form["material"].strip()
    notas = request.form.get("notas", "").strip()
    tipo = request.form.get("tipo", "donar")
    recoger_en_sitio = request.form.get("recoger_en_sitio") == "1"
    cantidad_cajas = request.form.get("cantidad_cajas", "").strip()
    try:
        cantidad_cajas = int(cantidad_cajas) if cantidad_cajas else None
    except ValueError:
        cantidad_cajas = None
    if cantidad_cajas and cantidad_cajas > CAJAS_MAX_POR_SOLICITUD:
        flash(f"El máximo por solicitud es de {CAJAS_MAX_POR_SOLICITUD} cajas — no caben más en la camioneta.", "error")
        return redirect(url_for("cliente_dashboard", tab="redistribucion"))
    estado_inicial = "pendiente_entrega" if tipo == "material" else "pendiente"
    db = get_db()
    alta = db.execute(
        "SELECT direccion, lat, lon, zona FROM solicitudes WHERE cliente_id = ? AND lat IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    if alta is None:
        alta = db.execute(
            "SELECT direccion, lat, lon, zona FROM solicitudes WHERE cliente_id = ? ORDER BY created_at DESC LIMIT 1",
            (user["id"],),
        ).fetchone()
    direccion = alta["direccion"] if alta else ""
    if recoger_en_sitio:
        lat = lon = zona = None
    else:
        lat = alta["lat"] if alta else None
        lon = alta["lon"] if alta else None
        zona = alta["zona"] if alta else None
    hay_existencia = False
    if tipo == "material" and cantidad_cajas:
        hay_existencia = existencia_caja(db, material) >= cantidad_cajas

    cur = db.execute(
        "INSERT INTO solicitudes (cliente_id, direccion, material, notas, cantidad_cajas, "
        "tipo_redistribucion, lat, lon, zona, estado, recoger_en_sitio, confirmado_existencia) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user["id"], direccion, material, notas, cantidad_cajas, tipo, lat, lon, zona, estado_inicial,
         1 if recoger_en_sitio else 0, 1 if hay_existencia else 0),
    )
    nueva_solicitud_id = cur.lastrowid
    cajas_texto = f"{cantidad_cajas} caja(s)" if cantidad_cajas else "cajas"
    if recoger_en_sitio:
        if hay_existencia:
            registrar_movimiento_cajas(
                db, material, "entrega", -cantidad_cajas, f"Recolección en sitio — solicitud #{nueva_solicitud_id}",
            )
            crear_notificacion_admin(
                db, user["id"],
                f"'{user['name']}' quiere pasar a recoger {cajas_texto} de {material} en RE-PVC "
                "— ya hay existencia, se le avisó que puede pasar.",
            )
            direccion_repvc = "Filiberto Gómez 279, Tlaxcopan, Tlalnepantla de Baz, Estado de México, CP 54030"
            enviar_whatsapp_primer_contacto(
                telefono_whatsapp_e164(user["telefono"]),
                "TWILIO_TEMPLATE_EXISTENCIA_RECOGER_SID",
                {"1": user["name"], "2": cajas_texto, "3": material, "4": direccion_repvc},
                f"Hola {user['name']},\n\nYa tenemos existencia de {cajas_texto} de {material}. "
                f"Ya puedes pasar a recolectarlas a:\n{direccion_repvc}",
            )
        else:
            crear_notificacion_admin(
                db, user["id"],
                f"'{user['name']}' quiere pasar a recoger {cajas_texto} de {material} en RE-PVC "
                "— por ahora no hay existencia suficiente.",
            )
            enviar_whatsapp_primer_contacto(
                telefono_whatsapp_e164(user["telefono"]),
                "TWILIO_TEMPLATE_SIN_EXISTENCIA_SID",
                {"1": user["name"], "2": cajas_texto, "3": material},
                f"Hola {user['name']},\n\nRecibimos tu solicitud de {cajas_texto} de {material}. "
                "Por ahora no tenemos existencia suficiente — en cuanto la tengamos te escribiremos "
                "para confirmar si sigues necesitándolas.",
            )
    elif tipo == "material":
        if hay_existencia:
            crear_notificacion_admin(
                db, user["id"],
                f"'{user['name']}' solicita recibir {cajas_texto} de {material} — hay existencia, "
                "queda programada la entrega.",
            )
            enviar_whatsapp_primer_contacto(
                telefono_whatsapp_e164(user["telefono"]),
                "TWILIO_TEMPLATE_ENTREGA_PROGRAMADA_SID",
                {"1": user["name"], "2": cajas_texto, "3": material},
                f"Hola {user['name']},\n\nYa tenemos existencia de {cajas_texto} de {material}. "
                "Tu entrega quedó programada, te avisaremos la fecha y el horario aproximado.",
            )
        else:
            crear_notificacion_admin(
                db, user["id"],
                f"'{user['name']}' solicita recibir {cajas_texto} de {material} — por ahora no hay "
                "existencia suficiente.",
            )
            enviar_whatsapp_primer_contacto(
                telefono_whatsapp_e164(user["telefono"]),
                "TWILIO_TEMPLATE_SIN_EXISTENCIA_SID",
                {"1": user["name"], "2": cajas_texto, "3": material},
                f"Hola {user['name']},\n\nRecibimos tu solicitud de {cajas_texto} de {material}. "
                "Por ahora no tenemos existencia suficiente — en cuanto la tengamos te escribiremos "
                "para confirmar si sigues necesitándolas.",
            )
    else:
        crear_notificacion_admin(
            db, user["id"], f"'{user['name']}' quiere donar {cajas_texto} de {material}.",
        )
    db.commit()
    if recoger_en_sitio:
        if hay_existencia:
            flash("¡Ya tenemos existencia! Ya puedes pasar a recolectarlas a RE-PVC.", "success")
        else:
            flash("Solicitud enviada. Por ahora no tenemos existencia — te avisaremos en cuanto haya.", "success")
    elif tipo == "material":
        if hay_existencia:
            flash("¡Ya tenemos existencia! Tu entrega quedó programada.", "success")
        else:
            flash("Solicitud registrada. Por ahora no tenemos existencia suficiente — te avisaremos en cuanto haya.", "success")
    else:
        flash("Solicitud de recolección creada.", "success")
    return redirect(url_for("cliente_dashboard", tab="notificaciones"))


@app.route("/cliente/regresar-bote", methods=["POST"])
@login_required("cliente")
def cliente_regresar_bote(user):
    db = get_db()
    sol = db.execute(
        "SELECT id, bote_a_devolver FROM solicitudes WHERE cliente_id = ? AND tipo_redistribucion IS NULL "
        "ORDER BY created_at DESC LIMIT 1",
        (user["id"],),
    ).fetchone()
    if sol is None:
        flash("No encontramos tu solicitud — contáctanos directamente.", "error")
    elif sol["bote_a_devolver"]:
        flash("Ya habíamos registrado que vas a regresar el bote — lo recogeremos en tu próxima ruta.", "success")
    else:
        db.execute("UPDATE solicitudes SET bote_a_devolver = 1 WHERE id = ?", (sol["id"],))
        crear_notificacion_admin(db, user["id"], f"'{user['name']}' avisó que va a regresar el bote.")
        db.commit()
        flash("Listo, marcamos que vas a regresar el bote — lo recogeremos en tu próxima ruta.", "success")
    return redirect(url_for("cliente_dashboard", tab="notificaciones"))


# ---------- Admin ----------

@app.route("/admin")
@login_required("admin")
def admin_dashboard(user):
    db = get_db()
    solicitudes_clientes = db.execute(
        "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre FROM solicitudes s "
        "LEFT JOIN users u ON u.id = s.cliente_id "
        f"WHERE s.estado = 'pendiente' AND s.cliente_id IS NOT NULL AND {condicion_lista_para_recoleccion('s')}"
        " ORDER BY s.created_at"
    ).fetchall()

    pendientes_entrega = db.execute(
        "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre, "
        "(SELECT r.nombre FROM paradas p JOIN rutas r ON r.id = p.ruta_id "
        " WHERE (p.solicitud_id = s.id OR p.solicitud_extra_id = s.id) AND p.tipo = 'entrega' "
        " AND p.estado = 'pendiente' ORDER BY p.id DESC LIMIT 1) AS ruta_programada "
        "FROM solicitudes s LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.recoger_en_sitio = 0 AND ("
        "  s.estado = 'pendiente_entrega'"
        "  OR (s.estado = 'programada' AND EXISTS ("
        "    SELECT 1 FROM paradas p WHERE (p.solicitud_id = s.id OR p.solicitud_extra_id = s.id) "
        "    AND p.tipo = 'entrega' AND p.estado = 'pendiente'"
        "  ))"
        ") ORDER BY s.zona IS NULL, s.zona, s.created_at"
    ).fetchall()

    zonas = [
        row["zona"] for row in db.execute(
            "SELECT DISTINCT zona FROM solicitudes "
            f"WHERE estado IN ('pendiente', 'pendiente_entrega') AND zona IS NOT NULL "
            f"AND {condicion_lista_para_recoleccion()} ORDER BY zona"
        ).fetchall()
    ]
    zona_actual = request.args.get("zona") or (zonas[0] if zonas else None)
    puntos_zona = []
    if zona_actual:
        puntos_zona = db.execute(
            "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre FROM solicitudes s "
            "LEFT JOIN users u ON u.id = s.cliente_id "
            f"WHERE s.estado IN ('pendiente', 'pendiente_entrega') AND s.zona = ? "
            f"AND {condicion_lista_para_recoleccion('s')} "
            "ORDER BY COALESCE(s.fecha_reinicio_espera, s.created_at)",
            (zona_actual,),
        ).fetchall()

    rutas_rows = db.execute(
        "SELECT r.*, u.name AS recolector_nombre, "
        "(SELECT COUNT(*) FROM paradas p WHERE p.ruta_id = r.id) AS total_paradas, "
        "(SELECT COUNT(*) FROM paradas p WHERE p.ruta_id = r.id AND p.estado != 'pendiente') AS paradas_hechas, "
        "(SELECT COUNT(*) FROM paradas p WHERE p.ruta_id = r.id AND p.confirmado_paciente = 'si') AS confirmadas_si, "
        "(SELECT COUNT(*) FROM paradas p WHERE p.ruta_id = r.id AND p.confirmado_paciente = 'no') AS confirmadas_no "
        "FROM rutas r LEFT JOIN users u ON u.id = r.recolector_id ORDER BY r.fecha DESC"
    ).fetchall()
    rutas = []
    for r in rutas_rows:
        ruta = dict(r)
        ruta["estimado"] = estimar_ruta_por_id(db, r["id"])
        if ruta["estado"] == "completada":
            tiempo_real = None
            if ruta["hora_inicio_real"] and ruta["hora_fin_real"]:
                try:
                    inicio = datetime.strptime(ruta["hora_inicio_real"], "%Y-%m-%d %H:%M:%S")
                    fin = datetime.strptime(ruta["hora_fin_real"], "%Y-%m-%d %H:%M:%S")
                    tiempo_real = formatear_duracion((fin - inicio).total_seconds() / 60)
                except ValueError:
                    tiempo_real = None
            ruta["tiempo_real"] = tiempo_real
            ruta["kg_total"] = db.execute(
                "SELECT COALESCE(SUM(kg_recolectados), 0) AS kg FROM paradas WHERE ruta_id = ?", (r["id"],)
            ).fetchone()["kg"]
        rutas.append(ruta)
    recolectores = db.execute("SELECT * FROM users WHERE role = 'recolector' ORDER BY name").fetchall()
    cuentas_nef = db.execute("SELECT * FROM users WHERE role = 'nef' ORDER BY name").fetchall()
    cuentas_admin_general = db.execute(
        "SELECT * FROM users WHERE role = 'admin' AND es_admin_general = 1 ORDER BY name"
    ).fetchall()
    cuentas_admin = db.execute(
        "SELECT * FROM users WHERE role = 'admin' AND es_admin_general = 0 ORDER BY name"
    ).fetchall()
    horas_extra_registros = db.execute(
        "SELECT h.*, u.name AS recolector_nombre FROM horas_extra h "
        "JOIN users u ON u.id = h.recolector_id ORDER BY h.fecha DESC, h.hora_inicio DESC"
    ).fetchall()
    horas_extra_por_recolector = db.execute(
        "SELECT u.name AS recolector_nombre, COALESCE(SUM(h.horas_extra), 0) AS total "
        "FROM users u LEFT JOIN horas_extra h ON h.recolector_id = u.id "
        "WHERE u.role = 'recolector' GROUP BY u.id ORDER BY u.name"
    ).fetchall()
    rutas_activas = [r for r in rutas if r["estado"] != "completada"]
    rutas_finalizadas = [r for r in rutas if r["estado"] == "completada"]

    notificaciones = db.execute(
        "SELECT * FROM notificaciones_admin ORDER BY leida, created_at DESC"
    ).fetchall()
    notificaciones_sin_leer = sum(1 for n in notificaciones if not n["leida"])

    pacientes_rows = db.execute(
        "SELECT * FROM users WHERE role = 'cliente' AND id NOT IN ("
        "SELECT cliente_id FROM solicitudes WHERE estado = 'lista_espera' AND fuera_cobertura = 1 "
        "AND cliente_id IS NOT NULL) ORDER BY perfil_completo, name"
    ).fetchall()
    pacientes = []
    for p in pacientes_rows:
        paciente = dict(p)
        ruta_sol = db.execute(
            "SELECT s.id AS solicitud_id, s.zona, s.direccion, s.telefono, s.modalidad, "
            "s.bote_a_devolver, r.nombre AS ruta_nombre FROM solicitudes s "
            "LEFT JOIN paradas pa ON pa.solicitud_id = s.id "
            "LEFT JOIN rutas r ON r.id = pa.ruta_id "
            "WHERE s.cliente_id = ? "
            "ORDER BY (r.nombre IS NULL), (s.zona IS NULL), s.created_at DESC, pa.id DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        paciente["ruta_actual"] = (ruta_sol["ruta_nombre"] or ruta_sol["zona"]) if ruta_sol else None
        paciente["direccion_actual"] = ruta_sol["direccion"] if ruta_sol else None
        paciente["telefono_actual"] = (ruta_sol["telefono"] if ruta_sol else None) or paciente["telefono"]
        paciente["modalidad_actual"] = ruta_sol["modalidad"] if ruta_sol else None
        paciente["solicitud_id_actual"] = ruta_sol["solicitud_id"] if ruta_sol else None
        paciente["bote_a_devolver"] = ruta_sol["bote_a_devolver"] if ruta_sol else False
        ultima_visita = db.execute(
            "SELECT r.fecha FROM paradas p "
            "JOIN rutas r ON r.id = p.ruta_id JOIN solicitudes s ON s.id = p.solicitud_id "
            "WHERE s.cliente_id = ? AND p.estado != 'pendiente' ORDER BY r.fecha DESC LIMIT 1",
            (p["id"],),
        ).fetchone()
        paciente["ultima_visita"] = ultima_visita["fecha"] if ultima_visita else None
        pacientes.append(paciente)

    lista_espera = db.execute(
        "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre FROM solicitudes s "
        "LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.estado = 'lista_espera' AND s.fuera_cobertura = 0 ORDER BY s.created_at ASC"
    ).fetchall()
    pendientes_ruta = db.execute(
        "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre FROM solicitudes s "
        "LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.estado = 'lista_espera' AND s.fuera_cobertura = 1 ORDER BY s.created_at ASC"
    ).fetchall()
    pacientes_activos = contar_pacientes_activos(db)

    ingresos = db.execute(
        "SELECT * FROM movimientos_dinero WHERE tipo = 'ingreso' ORDER BY created_at DESC"
    ).fetchall()
    egresos = db.execute(
        "SELECT * FROM movimientos_dinero WHERE tipo = 'egreso' ORDER BY created_at DESC"
    ).fetchall()
    total_ingresos = sum(r["monto"] for r in ingresos)
    total_egresos = sum(r["monto"] for r in egresos)

    kg_por_dia = db.execute(
        "SELECT r.fecha AS fecha, SUM(p.kg_recolectados) AS kg "
        "FROM paradas p JOIN rutas r ON r.id = p.ruta_id "
        "WHERE p.kg_recolectados IS NOT NULL "
        "GROUP BY r.fecha ORDER BY r.fecha DESC"
    ).fetchall()
    total_kg_recolectados = db.execute(
        "SELECT COALESCE(SUM(kg_recolectados), 0) AS kg FROM paradas"
    ).fetchone()["kg"]

    almacen_entradas = db.execute(
        "SELECT * FROM almacen_movimientos WHERE tipo = 'entrada' ORDER BY created_at DESC"
    ).fetchall()
    almacen_salidas = db.execute(
        "SELECT * FROM almacen_movimientos WHERE tipo = 'salida' ORDER BY created_at DESC"
    ).fetchall()
    existencia_por_material = dict(db.execute(
        "SELECT material, "
        "SUM(CASE WHEN tipo = 'entrada' THEN cantidad ELSE -cantidad END) AS cantidad "
        "FROM almacen_movimientos GROUP BY material"
    ).fetchall())
    existencia_almacen = [
        {"material": m, "cantidad": existencia_por_material.get(m, 0) or 0}
        for m in MATERIALES_PRODUCTO_TERMINADO
    ]

    admin_videos = db.execute("SELECT * FROM admin_videos ORDER BY created_at DESC").fetchall()

    pedidos_pendientes = db.execute(
        "SELECT * FROM pedidos_material WHERE estado = 'pendiente' ORDER BY fecha_pedido"
    ).fetchall()
    pedidos_recibidos = db.execute(
        "SELECT * FROM pedidos_material WHERE estado = 'recibido' ORDER BY fecha_recibido DESC"
    ).fetchall()

    existencia_botes = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN tipo IN ('compra','devolucion') THEN cantidad ELSE -cantidad END), 0) AS n "
        "FROM inventario_botes"
    ).fetchone()["n"]
    movimientos_botes = db.execute(
        "SELECT * FROM inventario_botes ORDER BY created_at DESC LIMIT 100"
    ).fetchall()

    existencia_por_caja = dict(db.execute(
        "SELECT material, SUM(cantidad) AS n FROM inventario_cajas GROUP BY material"
    ).fetchall())
    existencia_cajas = [
        {"material": m, "cantidad": existencia_por_caja.get(m, 0) or 0}
        for m in TIPOS_CAJAS
    ]
    movimientos_cajas = db.execute(
        "SELECT * FROM inventario_cajas ORDER BY created_at DESC LIMIT 100"
    ).fetchall()

    fecha_productividad = request.args.get("fecha") or date.today().isoformat()
    productividad_dia = db.execute(
        "SELECT * FROM productividad WHERE fecha = ? ORDER BY created_at DESC", (fecha_productividad,)
    ).fetchall()
    resumen_por_persona = db.execute(
        "SELECT persona, COALESCE(SUM(cantidad_kg), 0) AS kg, COUNT(*) AS n FROM productividad "
        "WHERE fecha = ? GROUP BY persona ORDER BY persona",
        (fecha_productividad,),
    ).fetchall()
    resumen_por_actividad = db.execute(
        "SELECT actividad, COALESCE(SUM(cantidad_kg), 0) AS kg, COUNT(*) AS n FROM productividad "
        "WHERE fecha = ? GROUP BY actividad ORDER BY actividad",
        (fecha_productividad,),
    ).fetchall()

    semana_ref_str = request.args.get("semana") or fecha_productividad
    try:
        semana_ref = date.fromisoformat(semana_ref_str)
    except ValueError:
        semana_ref = date.today()
    lunes = semana_ref - timedelta(days=semana_ref.weekday())
    dias_semana = [lunes + timedelta(days=i) for i in range(5)]
    viernes = dias_semana[-1]
    nombres_dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes"]
    dias_semana_info = [
        {"fecha": d.isoformat(), "label": f"{nombres_dias[i]} {d.day}"} for i, d in enumerate(dias_semana)
    ]

    productividad_semana = db.execute(
        "SELECT * FROM productividad WHERE fecha BETWEEN ? AND ? ORDER BY fecha, created_at",
        (lunes.isoformat(), viernes.isoformat()),
    ).fetchall()

    matriz_semana = {p: {d.isoformat(): 0.0 for d in dias_semana} for p in PERSONAS_PRODUCTIVIDAD}
    totales_persona_semana = {p: 0.0 for p in PERSONAS_PRODUCTIVIDAD}
    totales_actividad_semana = {a: 0.0 for a in ACTIVIDADES_PRODUCTIVIDAD}
    for r in productividad_semana:
        kg = r["cantidad_kg"] or 0
        if r["persona"] in matriz_semana and r["fecha"] in matriz_semana[r["persona"]]:
            matriz_semana[r["persona"]][r["fecha"]] += kg
        if r["persona"] in totales_persona_semana:
            totales_persona_semana[r["persona"]] += kg
        if r["actividad"] in totales_actividad_semana:
            totales_actividad_semana[r["actividad"]] += kg
    total_semana_kg = sum(totales_persona_semana.values())

    saldo_vacaciones = {
        r["persona"]: r["dias_totales"]
        for r in db.execute("SELECT persona, dias_totales FROM vacaciones_saldo").fetchall()
    }
    tomados_vacaciones = {
        r["persona"]: r["dias"]
        for r in db.execute(
            "SELECT persona, COALESCE(SUM(dias), 0) AS dias FROM vacaciones_registros GROUP BY persona"
        ).fetchall()
    }
    resumen_vacaciones = []
    for p in PERSONAS_VACACIONES:
        totales = saldo_vacaciones.get(p, DIAS_VACACIONES_DEFAULT)
        tomados = tomados_vacaciones.get(p, 0)
        resumen_vacaciones.append({
            "persona": p, "dias_totales": totales, "dias_tomados": tomados,
            "dias_restantes": totales - tomados,
        })
    vacaciones_registros = db.execute(
        "SELECT * FROM vacaciones_registros ORDER BY fecha_inicio DESC"
    ).fetchall()

    auditoria_sin_zona = db.execute(
        "SELECT s.id, COALESCE(u.name, s.nombre_contacto) AS nombre, s.direccion, s.material, "
        "s.tipo_redistribucion, s.estado FROM solicitudes s LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.estado IN ('pendiente', 'pendiente_entrega') AND s.zona IS NULL "
        "ORDER BY s.created_at"
    ).fetchall()
    auditoria_sin_coordenadas = db.execute(
        "SELECT s.id, COALESCE(u.name, s.nombre_contacto) AS nombre, s.direccion, s.zona, s.estado "
        "FROM solicitudes s LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.estado IN ('pendiente', 'pendiente_entrega') AND s.zona IS NOT NULL "
        "AND (s.lat IS NULL OR s.lon IS NULL) ORDER BY s.created_at"
    ).fetchall()
    auditoria_programada_sin_ruta = db.execute(
        "SELECT s.id, COALESCE(u.name, s.nombre_contacto) AS nombre, s.direccion, s.zona, s.estado "
        "FROM solicitudes s LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.estado = 'programada' AND NOT EXISTS ("
        "  SELECT 1 FROM paradas p JOIN rutas r ON r.id = p.ruta_id "
        "  WHERE r.estado != 'completada' AND (p.solicitud_id = s.id OR p.solicitud_extra_id = s.id)"
        ") ORDER BY s.created_at"
    ).fetchall()
    total_auditoria_rutas = (
        len(auditoria_sin_zona) + len(auditoria_sin_coordenadas) + len(auditoria_programada_sin_ruta)
    )

    return render_template(
        "admin_dashboard.html",
        pendientes=solicitudes_clientes,
        pendientes_entrega=pendientes_entrega,
        auditoria_sin_zona=auditoria_sin_zona,
        auditoria_sin_coordenadas=auditoria_sin_coordenadas,
        auditoria_programada_sin_ruta=auditoria_programada_sin_ruta,
        total_auditoria_rutas=total_auditoria_rutas,
        zonas=zonas,
        zona_actual=zona_actual,
        puntos_zona=puntos_zona,
        rutas=rutas_activas,
        rutas_finalizadas=rutas_finalizadas,
        recolectores=recolectores,
        cuentas_nef=cuentas_nef,
        cuentas_admin_general=cuentas_admin_general,
        cuentas_admin=cuentas_admin,
        horas_extra_registros=horas_extra_registros,
        horas_extra_por_recolector=horas_extra_por_recolector,
        notificaciones=notificaciones,
        notificaciones_sin_leer=notificaciones_sin_leer,
        pacientes=pacientes,
        lista_espera=lista_espera,
        pendientes_ruta=pendientes_ruta,
        pacientes_activos=pacientes_activos,
        max_pacientes_activos=MAX_PACIENTES_ACTIVOS,
        ingresos=ingresos,
        egresos=egresos,
        total_ingresos=total_ingresos,
        total_egresos=total_egresos,
        total_efectivo=total_ingresos - total_egresos,
        kg_por_dia=kg_por_dia,
        total_kg_recolectados=total_kg_recolectados,
        almacen_entradas=almacen_entradas,
        almacen_salidas=almacen_salidas,
        existencia_almacen=existencia_almacen,
        materiales_producto_terminado=MATERIALES_PRODUCTO_TERMINADO,
        admin_videos=admin_videos,
        pedidos_pendientes=pedidos_pendientes,
        pedidos_recibidos=pedidos_recibidos,
        existencia_botes=existencia_botes,
        movimientos_botes=movimientos_botes,
        existencia_cajas=existencia_cajas,
        movimientos_cajas=movimientos_cajas,
        tipos_cajas=TIPOS_CAJAS,
        fecha_productividad=fecha_productividad,
        productividad_dia=productividad_dia,
        resumen_por_persona=resumen_por_persona,
        resumen_por_actividad=resumen_por_actividad,
        personas_productividad=PERSONAS_PRODUCTIVIDAD,
        actividades_productividad=ACTIVIDADES_PRODUCTIVIDAD,
        actividades_productividad_labels=ACTIVIDADES_PRODUCTIVIDAD_LABELS,
        semana_ref_str=semana_ref_str,
        dias_semana=dias_semana,
        dias_semana_info=dias_semana_info,
        matriz_semana=matriz_semana,
        totales_persona_semana=totales_persona_semana,
        totales_actividad_semana=totales_actividad_semana,
        total_semana_kg=total_semana_kg,
        personas_vacaciones=PERSONAS_VACACIONES,
        resumen_vacaciones=resumen_vacaciones,
        vacaciones_registros=vacaciones_registros,
    )


@app.route("/admin/notificaciones/<int:notificacion_id>/leer", methods=["POST"])
@login_required("admin")
def admin_marcar_notificacion_leida(user, notificacion_id):
    db = get_db()
    db.execute("UPDATE notificaciones_admin SET leida = 1 WHERE id = ?", (notificacion_id,))
    db.commit()
    return redirect(url_for("admin_dashboard", tab="notificaciones"))


@app.route("/admin/notificaciones/leer_todas", methods=["POST"])
@login_required("admin")
def admin_marcar_todas_notificaciones_leidas(user):
    db = get_db()
    db.execute("UPDATE notificaciones_admin SET leida = 1 WHERE leida = 0")
    db.commit()
    return redirect(url_for("admin_dashboard", tab="notificaciones"))


@app.route("/admin/pacientes/invitar", methods=["POST"])
@login_required("admin")
def admin_invitar_paciente(user):
    """En vez de que el admin capture aquí todos los datos médicos y la dirección del paciente
    (como se hacía antes), solo da de alta el correo y le manda una invitación: el paciente crea
    su propia contraseña en /invitacion/<token> y de ahí lo manda el flujo normal de cliente
    (cliente_privacidad -> cliente_bienvenida -> cliente_alta) a llenar su perfil médico y su
    dirección él mismo — así queda inscrito con su propia cuenta desde el inicio, en vez de una
    solicitud sin dueño (cliente_id NULL) que solo el admin puede ver/editar."""
    nombre = request.form["nombre"].strip()
    telefono = telefono_identidad(request.form.get("telefono", ""))

    if telefono is None:
        flash("Escribe un número de WhatsApp válido de 10 dígitos. No se envió invitación.", "error")
        return redirect(url_for("admin_dashboard", tab="paciente"))

    db = get_db()
    existente = db.execute("SELECT id FROM users WHERE telefono = ?", (telefono,)).fetchone()
    if existente:
        flash(f"Ese número de WhatsApp ya tiene una cuenta ({existente['id']}). No se envió invitación.", "error")
        return redirect(url_for("admin_dashboard", tab="paciente"))

    token = secrets.token_urlsafe(32)
    # Contraseña provisional e imposible de adivinar: nadie puede entrar con ella, el paciente
    # tiene que pasar por /invitacion/<token> para poner la suya y activar la cuenta.
    password_provisional = generate_password_hash(secrets.token_urlsafe(24), method="pbkdf2:sha256")
    db.execute(
        "INSERT INTO users (name, telefono, password_hash, role, email_verificado, verificacion_token) "
        "VALUES (?, ?, ?, 'cliente', 0, ?)",
        (nombre, telefono, password_provisional, token),
    )
    db.commit()
    link = url_absoluta("invitacion_paciente", token=token)
    enviado = enviar_whatsapp_primer_contacto(
        telefono_whatsapp_e164(telefono),
        "TWILIO_TEMPLATE_INVITACION_SID",
        {"1": nombre, "2": link},
        f"Hola {nombre},\n\nTe dimos de alta en RE-PVC para que puedas programar tus recolecciones "
        f"de material PVC desde la app. Entra a este enlace para crear tu contraseña y activar tu "
        f"cuenta:\n{link}\n\nAhí mismo vas a poder completar tu perfil y tu dirección de recolección.\n\n"
        "Si no esperabas este mensaje, ignóralo.",
    )
    if enviado:
        flash(f"Se invitó a '{nombre}' — le enviamos un WhatsApp para que active su cuenta.", "success")
    else:
        flash(
            f"'{nombre}' quedó registrado, pero no pudimos enviarle la invitación por WhatsApp en este "
            "momento. Vuelve a intentar en unos minutos desde la lista de pacientes.",
            "error",
        )
    return redirect(url_for("admin_dashboard", tab="paciente"))


@app.route("/invitacion/<token>", methods=["GET", "POST"])
def invitacion_paciente(token):
    """El paciente llega aquí desde el correo que le mandó admin_invitar_paciente. A diferencia
    de verificar_correo (que solo confirma un correo que el propio paciente ya usó para
    registrarse con su contraseña), aquí el paciente todavía no tiene contraseña — la pone en
    este paso, y eso mismo cuenta como verificar que el correo es suyo."""
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE verificacion_token = ? AND role = 'cliente'", (token,)
    ).fetchone()
    if user is None:
        flash("Ese enlace de invitación ya no es válido.", "error")
        return redirect(url_for("login"))
    if request.method == "POST":
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        if len(password) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "error")
            return render_template("invitacion_paciente.html", token=token, nombre=user["name"])
        if password != password2:
            flash("Las contraseñas no coinciden.", "error")
            return render_template("invitacion_paciente.html", token=token, nombre=user["name"])
        db.execute(
            "UPDATE users SET password_hash = ?, email_verificado = 1, verificacion_token = NULL "
            "WHERE id = ?",
            (generate_password_hash(password, method="pbkdf2:sha256"), user["id"]),
        )
        db.commit()
        session.clear()
        session["user_id"] = user["id"]
        flash("¡Cuenta activada! Termina de completar tu perfil para programar tu recolección.", "success")
        return redirect(url_for("home"))
    return render_template("invitacion_paciente.html", token=token, nombre=user["name"])


@app.route("/admin/solicitudes/<int:solicitud_id>/entregar", methods=["POST"])
@login_required("admin")
def admin_marcar_bote_entregado(user, solicitud_id):
    db = get_db()
    sol = db.execute("SELECT * FROM solicitudes WHERE id = ?", (solicitud_id,)).fetchone()
    if sol is None or sol["estado"] != "pendiente_entrega":
        flash("Ese paciente ya no está pendiente de entrega.", "error")
        return redirect(url_for("admin_dashboard", tab="solicitudes"))
    db.execute("UPDATE solicitudes SET estado = 'pendiente' WHERE id = ?", (solicitud_id,))
    registrar_movimiento_botes(db, "entrega", 1, f"Entregado a {sol['nombre_contacto'] or sol['direccion']}")
    db.commit()
    flash(
        f"Bote entregado a '{sol['nombre_contacto'] or sol['direccion']}'. "
        "Sigue integrado en su ruta, ahora como punto pendiente de recolección.",
        "success",
    )
    return redirect(url_for("admin_dashboard", tab="solicitudes"))


@app.route("/admin/geocodificar")
@login_required("admin")
def admin_geocodificar(user):
    direccion = request.args.get("direccion", "").strip()
    codigo_postal = request.args.get("cp", "").strip()
    if not direccion:
        return jsonify({"error": "Escribe una dirección."}), 400
    resultados = geocodificar_direccion(direccion, codigo_postal=codigo_postal or None)
    if not resultados:
        return jsonify({"error": "No se encontró esa dirección."}), 404
    return jsonify({"resultados": resultados})


@app.route("/cliente/geocodificar")
@login_required("cliente")
def cliente_geocodificar(user):
    direccion = request.args.get("direccion", "").strip()
    codigo_postal = request.args.get("cp", "").strip()
    if not direccion:
        return jsonify({"error": "Escribe una dirección."}), 400
    resultados = geocodificar_direccion(direccion, codigo_postal=codigo_postal or None)
    if not resultados:
        return jsonify({"error": "No se encontró esa dirección."}), 404
    return jsonify({"resultados": resultados})


@app.route("/cliente/geocodificar-inverso")
@login_required("cliente")
def cliente_geocodificar_inverso(user):
    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except ValueError:
        return jsonify({"error": "Coordenadas inválidas."}), 400
    direccion = geocodificar_inverso(lat, lon)
    return jsonify({"direccion": direccion})


@app.route("/admin/mapa")
@login_required("admin")
def admin_mapa(user):
    db = get_db()
    puntos = db.execute(
        "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre FROM solicitudes s "
        "LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.lat IS NOT NULL AND s.lon IS NOT NULL AND s.estado != 'cancelada' "
        "ORDER BY s.zona, s.id"
    ).fetchall()
    puntos_json = json.dumps([dict(p) for p in puntos])
    return render_template("admin_mapa.html", puntos=puntos, puntos_json=puntos_json)


@app.route("/admin/zonas/<zona>/mapa")
@login_required("admin")
def admin_zona_mapa(user, zona):
    db = get_db()
    puntos = db.execute(
        "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre FROM solicitudes s "
        "LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.zona = ? AND s.estado IN ('pendiente', 'pendiente_entrega') "
        f"AND {condicion_lista_para_recoleccion('s')} "
        "ORDER BY COALESCE(s.fecha_reinicio_espera, s.created_at)",
        (zona,),
    ).fetchall()
    puntos_json = json.dumps([dict(p) for p in puntos])
    estimado = estimar_ruta(puntos)
    return render_template(
        "admin_zona_mapa.html", zona=zona, puntos=puntos, puntos_json=puntos_json, estimado=estimado
    )


@app.route("/admin/rutas/<int:ruta_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_ruta(user, ruta_id):
    db = get_db()
    ruta = db.execute("SELECT * FROM rutas WHERE id = ?", (ruta_id,)).fetchone()
    if ruta is None:
        flash("Esa ruta ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="rutas"))

    paradas_ruta = db.execute("SELECT * FROM paradas WHERE ruta_id = ?", (ruta_id,)).fetchall()
    for p in paradas_ruta:
        if p["estado"] == "pendiente":
            estado_previo = "pendiente_entrega" if p["tipo"] == "entrega" else "pendiente"
            db.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (estado_previo, p["solicitud_id"]))
        if p["solicitud_extra_id"] and p["estado_extra"] == "pendiente":
            estado_previo_extra = "pendiente_entrega" if p["tipo_extra"] == "entrega" else "pendiente"
            db.execute(
                "UPDATE solicitudes SET estado = ? WHERE id = ?", (estado_previo_extra, p["solicitud_extra_id"])
            )

    db.execute("DELETE FROM paradas WHERE ruta_id = ?", (ruta_id,))
    db.execute("DELETE FROM rutas WHERE id = ?", (ruta_id,))
    db.commit()
    flash(f"Ruta '{ruta['nombre']}' eliminada de la planeación.", "success")
    return redirect(url_for("admin_dashboard", tab="rutas"))


@app.route("/admin/paradas/<int:parada_id>/quitar", methods=["POST"])
@login_required("admin")
def admin_quitar_parada(user, parada_id):
    db = get_db()
    parada = db.execute("SELECT * FROM paradas WHERE id = ?", (parada_id,)).fetchone()
    if parada is None:
        flash("Esa parada ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="rutas"))

    ruta_id = parada["ruta_id"]
    sol = db.execute("SELECT * FROM solicitudes WHERE id = ?", (parada["solicitud_id"],)).fetchone()

    if parada["estado"] == "pendiente":
        estado_previo = "pendiente_entrega" if parada["tipo"] == "entrega" else "pendiente"
        db.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (estado_previo, parada["solicitud_id"]))
    if parada["solicitud_extra_id"] and parada["estado_extra"] == "pendiente":
        estado_previo_extra = "pendiente_entrega" if parada["tipo_extra"] == "entrega" else "pendiente"
        db.execute(
            "UPDATE solicitudes SET estado = ? WHERE id = ?", (estado_previo_extra, parada["solicitud_extra_id"])
        )

    db.execute("DELETE FROM paradas WHERE id = ?", (parada_id,))
    db.commit()
    nombre = sol["nombre_contacto"] if sol else "el paciente"
    flash(f"Se quitó a '{nombre}' de la ruta.", "success")
    return redirect(url_for("admin_ver_ruta", ruta_id=ruta_id))


@app.route("/admin/solicitudes/<int:solicitud_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_solicitud(user, solicitud_id):
    db = get_db()
    sol = db.execute("SELECT * FROM solicitudes WHERE id = ?", (solicitud_id,)).fetchone()
    if sol is None:
        flash("Ese paciente ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="solicitudes"))

    nombre = sol["nombre_contacto"] or sol["direccion"]
    zona = sol["zona"]
    cliente_id = sol["cliente_id"]

    if cliente_id:
        otras_solicitudes = db.execute(
            "SELECT id FROM solicitudes WHERE cliente_id = ?", (cliente_id,)
        ).fetchall()
        for otra in otras_solicitudes:
            db.execute("DELETE FROM paradas WHERE solicitud_id = ?", (otra["id"],))
        db.execute("DELETE FROM solicitudes WHERE cliente_id = ?", (cliente_id,))
        db.execute("DELETE FROM notificaciones_admin WHERE cliente_id = ?", (cliente_id,))
        db.execute("DELETE FROM users WHERE id = ?", (cliente_id,))
        promover_lista_espera(db)
        db.commit()
        flash(f"'{nombre}' se eliminó permanentemente del sistema, junto con su cuenta de paciente.", "success")
    else:
        db.execute("DELETE FROM paradas WHERE solicitud_id = ?", (solicitud_id,))
        db.execute("DELETE FROM solicitudes WHERE id = ?", (solicitud_id,))
        promover_lista_espera(db)
        db.commit()
        flash(f"'{nombre}' se eliminó permanentemente del sistema.", "success")
    if zona:
        return redirect(url_for("admin_zona_mapa", zona=zona))
    return redirect(url_for("admin_dashboard", tab="solicitudes"))


@app.route("/admin/pacientes/<int:cliente_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_paciente(user, cliente_id):
    db = get_db()
    paciente = db.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'cliente'", (cliente_id,)
    ).fetchone()
    if paciente is None:
        flash("Ese paciente ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="pacientes"))

    solicitudes = db.execute("SELECT id FROM solicitudes WHERE cliente_id = ?", (cliente_id,)).fetchall()
    for s in solicitudes:
        db.execute("DELETE FROM paradas WHERE solicitud_id = ?", (s["id"],))
    db.execute("DELETE FROM solicitudes WHERE cliente_id = ?", (cliente_id,))
    db.execute("DELETE FROM notificaciones_admin WHERE cliente_id = ?", (cliente_id,))
    db.execute("DELETE FROM users WHERE id = ?", (cliente_id,))
    promover_lista_espera(db)
    db.commit()
    flash(f"'{paciente['name']}' se eliminó permanentemente del sistema.", "success")
    return redirect(url_for("admin_dashboard", tab="pacientes"))


@app.route("/admin/pacientes/<int:cliente_id>/modalidad", methods=["POST"])
@login_required("admin")
def admin_actualizar_modalidad(user, cliente_id):
    modalidad = request.form.get("modalidad")
    if modalidad not in ("compra", "donacion"):
        flash("Modalidad inválida.", "error")
        return redirect(url_for("admin_dashboard", tab="pacientes"))
    db = get_db()
    paciente = db.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'cliente'", (cliente_id,)
    ).fetchone()
    if paciente is None:
        flash("Ese paciente ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="pacientes"))
    db.execute("UPDATE solicitudes SET modalidad = ? WHERE cliente_id = ?", (modalidad, cliente_id))
    db.commit()
    flash(f"'{paciente['name']}' quedó marcado como {'Compra' if modalidad == 'compra' else 'Donación'}.", "success")
    return redirect(url_for("admin_dashboard", tab="pacientes"))


@app.route("/admin/solicitudes/<int:solicitud_id>/activar-pendiente-ruta", methods=["POST"])
@login_required("admin")
def admin_activar_pendiente_ruta(user, solicitud_id):
    db = get_db()
    sol = db.execute(
        "SELECT * FROM solicitudes WHERE id = ? AND estado = 'lista_espera' AND fuera_cobertura = 1",
        (solicitud_id,),
    ).fetchone()
    if sol is None:
        flash("Esa solicitud ya no está pendiente de ruta.", "error")
        return redirect(url_for("admin_dashboard", tab="espera"))

    if sol["lat"] is None or sol["lon"] is None or fuera_de_cobertura(db, sol["lat"], sol["lon"]):
        flash("Todavía no encontramos ninguna zona con ruta cerca de esa dirección.", "error")
        return redirect(url_for("admin_dashboard", tab="espera"))

    cercana = zona_mas_cercana(db, sol["lat"], sol["lon"])
    zona = cercana[0] if cercana else ZONA_BOOTSTRAP_DEFAULT

    db.execute(
        "UPDATE solicitudes SET estado = 'pendiente_entrega', fuera_cobertura = 0, zona = ? WHERE id = ?",
        (zona, solicitud_id),
    )
    reequilibrar_rutas_zona(db, zona, solicitud_id)
    zona = db.execute("SELECT zona FROM solicitudes WHERE id = ?", (solicitud_id,)).fetchone()["zona"]
    nombre = sol["nombre_contacto"]
    telefono = None
    if sol["cliente_id"]:
        u = db.execute("SELECT name, telefono FROM users WHERE id = ?", (sol["cliente_id"],)).fetchone()
        if u:
            nombre = u["name"]
            telefono = u["telefono"]
    db.commit()
    if telefono:
        zona_texto = f" ({zona})" if zona else ""
        enviar_whatsapp_primer_contacto(
            telefono_whatsapp_e164(telefono),
            "TWILIO_TEMPLATE_ACTIVAR_RUTA_SID",
            {"1": nombre, "2": zona_texto or "tu zona"},
            f"Hola {nombre},\n\n¡Buenas noticias! Ya tenemos ruta en tu zona{zona_texto} y quedaste integrado.\n"
            "Te avisaremos con la fecha y el horario aproximado en cuanto tu recolección quede programada.",
        )
    flash(f"'{nombre}' se activó — asignado a {zona}.", "success")
    return redirect(url_for("admin_dashboard", tab="espera"))


@app.route("/admin/solicitudes/<int:solicitud_id>/regresar-bote", methods=["POST"])
@login_required("admin")
def admin_marcar_bote_devolver(user, solicitud_id):
    db = get_db()
    sol = db.execute("SELECT * FROM solicitudes WHERE id = ?", (solicitud_id,)).fetchone()
    if sol is None:
        flash("Esa solicitud ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="pacientes"))
    db.execute("UPDATE solicitudes SET bote_a_devolver = 1 WHERE id = ?", (solicitud_id,))
    db.commit()
    nombre = sol["nombre_contacto"] or sol["direccion"]
    flash(f"Se marcó que '{nombre}' debe regresar el bote — aparecerá en su próxima ruta.", "success")
    return redirect(url_for("admin_dashboard", tab="pacientes"))


def registrar_movimiento_botes(db, tipo, cantidad, notas=None):
    """Registra un movimiento en el inventario de botes. 'compra' y 'devolucion' suman al
    inventario; 'entrega' resta (un bote que sale del almacén hacia un paciente)."""
    if cantidad <= 0:
        return
    db.execute(
        "INSERT INTO inventario_botes (tipo, cantidad, notas) VALUES (?, ?, ?)",
        (tipo, cantidad, notas),
    )


@app.route("/admin/botes/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_bote_inventario(user):
    cantidad = request.form.get("cantidad", "").strip()
    notas = request.form.get("notas", "").strip() or None
    try:
        cantidad = int(cantidad)
    except ValueError:
        cantidad = None
    if not cantidad or cantidad <= 0:
        flash("Pon una cantidad válida.", "error")
        return redirect(url_for("admin_dashboard", tab="botes"))
    db = get_db()
    registrar_movimiento_botes(db, "compra", cantidad, notas)
    db.commit()
    flash(f"Se agregaron {cantidad} bote(s) al inventario.", "success")
    return redirect(url_for("admin_dashboard", tab="botes"))


def existencia_caja(db, material):
    """Existencia actual (suma de movimientos) de un tipo de caja en el inventario."""
    fila = db.execute(
        "SELECT COALESCE(SUM(cantidad), 0) AS n FROM inventario_cajas WHERE material = ?", (material,)
    ).fetchone()
    return fila["n"]


def avisar_solicitudes_pendientes_por_existencia(db, material):
    """Cuando sube el inventario de un tipo de caja, revisa si hay solicitudes de 'recibir' que
    estaban esperando existencia (confirmado_existencia=0) y ya alcanzan con lo que hay ahora.
    A cada una que alcance le manda un correo con un enlace para confirmar si sigue
    necesitándolas o prefiere cancelar la solicitud — no se confirman solas, porque ya pasó
    tiempo desde que se pidieron y puede que ya no las necesiten."""
    disponible = existencia_caja(db, material)
    if disponible <= 0:
        return
    pendientes = db.execute(
        "SELECT * FROM solicitudes WHERE tipo_redistribucion = 'material' AND confirmado_existencia = 0 "
        "AND notificado_existencia = 0 AND material = ? "
        "AND estado NOT IN ('cancelada', 'recolectada') ORDER BY created_at ASC",
        (material,),
    ).fetchall()
    for sol in pendientes:
        necesita = sol["cantidad_cajas"] or 0
        if necesita <= 0 or disponible < necesita:
            continue
        nombre = sol["nombre_contacto"]
        telefono = None
        if sol["cliente_id"]:
            u = db.execute("SELECT name, telefono FROM users WHERE id = ?", (sol["cliente_id"],)).fetchone()
            if u:
                nombre = u["name"]
                telefono = u["telefono"]
        if not telefono:
            continue
        disponible -= necesita
        token = secrets.token_urlsafe(24)
        db.execute(
            "UPDATE solicitudes SET notificado_existencia = 1, token_existencia = ? WHERE id = ?",
            (token, sol["id"]),
        )
        cajas_texto = f"{necesita} caja(s)" if necesita else "cajas"
        link_si = url_absoluta("solicitud_confirmar_existencia", token=token, respuesta="si")
        link_cancelar = url_absoluta(
            "solicitud_confirmar_existencia", token=token, respuesta="cancelar"
        )
        enviar_whatsapp_primer_contacto(
            telefono_whatsapp_e164(telefono),
            "TWILIO_TEMPLATE_EXISTENCIA_DISPONIBLE_SID",
            {"1": nombre, "2": cajas_texto, "3": material, "4": link_si, "5": link_cancelar},
            f"Hola {nombre},\n\n"
            f"Ya tenemos existencia de {cajas_texto} de {material} que nos habías pedido.\n\n"
            "¿Sigues necesitándolas?\n\n"
            f"Sí, sigo necesitándolas: {link_si}\n"
            f"Ya no las necesito, cancelar solicitud: {link_cancelar}\n",
        )


def registrar_movimiento_cajas(db, material, tipo, cantidad, notas=None):
    """Registra un movimiento en el inventario de cajas para un tipo de material específico.
    `cantidad` puede ser positiva (suma, p.ej. donación o ajuste al alza) o negativa (resta,
    p.ej. entrega a un paciente o ajuste a la baja)."""
    if not cantidad:
        return
    db.execute(
        "INSERT INTO inventario_cajas (material, tipo, cantidad, notas) VALUES (?, ?, ?, ?)",
        (material, tipo, cantidad, notas),
    )
    if cantidad > 0:
        avisar_solicitudes_pendientes_por_existencia(db, material)


@app.route("/admin/cajas/ajuste", methods=["POST"])
@login_required("admin")
def admin_ajuste_cajas(user):
    material = request.form.get("material")
    cantidad = request.form.get("cantidad", "").strip()
    notas = request.form.get("notas", "").strip() or None
    if material not in TIPOS_CAJAS:
        flash("Selecciona un tipo de caja válido.", "error")
        return redirect(url_for("admin_dashboard", tab="cajas"))
    try:
        cantidad = int(cantidad)
    except ValueError:
        cantidad = None
    if not cantidad:
        flash("Pon una cantidad válida (puede ser negativa para restar).", "error")
        return redirect(url_for("admin_dashboard", tab="cajas"))
    db = get_db()
    registrar_movimiento_cajas(db, material, "ajuste", cantidad, notas)
    db.commit()
    flash(f"Ajuste registrado: {'+' if cantidad > 0 else ''}{cantidad} {material}.", "success")
    return redirect(url_for("admin_dashboard", tab="cajas"))


@app.route("/admin/productividad/nueva", methods=["POST"])
@login_required("admin")
def admin_nueva_productividad(user):
    persona = request.form.get("persona")
    actividad = request.form.get("actividad")
    fecha = request.form.get("fecha") or date.today().isoformat()
    cantidad_kg = request.form.get("cantidad_kg", "").strip()
    notas = request.form.get("notas", "").strip() or None
    if persona not in PERSONAS_PRODUCTIVIDAD:
        flash("Selecciona quién lo hizo.", "error")
        return redirect(url_for("admin_dashboard", tab="productividad"))
    if actividad not in ACTIVIDADES_PRODUCTIVIDAD:
        flash("Selecciona la actividad.", "error")
        return redirect(url_for("admin_dashboard", tab="productividad"))
    try:
        cantidad_kg = float(cantidad_kg) if cantidad_kg else None
    except ValueError:
        cantidad_kg = None
    db = get_db()
    db.execute(
        "INSERT INTO productividad (fecha, persona, actividad, cantidad_kg, notas) VALUES (?, ?, ?, ?, ?)",
        (fecha, persona, actividad, cantidad_kg, notas),
    )
    db.commit()
    flash("Actividad registrada.", "success")
    return redirect(url_for("admin_dashboard", tab="productividad", fecha=fecha))


@app.route("/admin/productividad/<int:productividad_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_productividad(user, productividad_id):
    db = get_db()
    reg = db.execute("SELECT fecha FROM productividad WHERE id = ?", (productividad_id,)).fetchone()
    db.execute("DELETE FROM productividad WHERE id = ?", (productividad_id,))
    db.commit()
    flash("Registro eliminado.", "success")
    return redirect(url_for("admin_dashboard", tab="productividad", fecha=reg["fecha"] if reg else None))


@app.route("/admin/vacaciones/nueva", methods=["POST"])
@login_required("admin")
def admin_nueva_vacacion(user):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede editar vacaciones.", "error")
        return redirect(url_for("admin_dashboard", tab="vacaciones"))
    persona = request.form.get("persona")
    fecha_inicio = request.form.get("fecha_inicio", "").strip()
    fecha_fin = request.form.get("fecha_fin", "").strip()
    notas = request.form.get("notas", "").strip() or None
    if persona not in PERSONAS_VACACIONES:
        flash("Selecciona el trabajador.", "error")
        return redirect(url_for("admin_dashboard", tab="vacaciones"))
    try:
        d_inicio = date.fromisoformat(fecha_inicio)
        d_fin = date.fromisoformat(fecha_fin)
    except ValueError:
        flash("Pon fechas válidas.", "error")
        return redirect(url_for("admin_dashboard", tab="vacaciones"))
    if d_fin < d_inicio:
        flash("La fecha final no puede ser antes de la fecha de inicio.", "error")
        return redirect(url_for("admin_dashboard", tab="vacaciones"))
    dias = (d_fin - d_inicio).days + 1
    db = get_db()
    db.execute(
        "INSERT INTO vacaciones_registros (persona, fecha_inicio, fecha_fin, dias, notas) VALUES (?, ?, ?, ?, ?)",
        (persona, fecha_inicio, fecha_fin, dias, notas),
    )
    db.commit()
    flash(f"Vacaciones registradas para {persona} ({dias} día(s)).", "success")
    return redirect(url_for("admin_dashboard", tab="vacaciones"))


@app.route("/admin/vacaciones/<int:vacacion_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_vacacion(user, vacacion_id):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede editar vacaciones.", "error")
        return redirect(url_for("admin_dashboard", tab="vacaciones"))
    db = get_db()
    db.execute("DELETE FROM vacaciones_registros WHERE id = ?", (vacacion_id,))
    db.commit()
    flash("Registro de vacaciones eliminado.", "success")
    return redirect(url_for("admin_dashboard", tab="vacaciones"))


@app.route("/admin/vacaciones/saldo", methods=["POST"])
@login_required("admin")
def admin_actualizar_saldo_vacaciones(user):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede editar vacaciones.", "error")
        return redirect(url_for("admin_dashboard", tab="vacaciones"))
    persona = request.form.get("persona")
    dias_totales = request.form.get("dias_totales", "").strip()
    if persona not in PERSONAS_VACACIONES:
        flash("Selecciona el trabajador.", "error")
        return redirect(url_for("admin_dashboard", tab="vacaciones"))
    try:
        dias_totales = int(dias_totales)
    except ValueError:
        flash("Pon un número de días válido.", "error")
        return redirect(url_for("admin_dashboard", tab="vacaciones"))
    if dias_totales < 0:
        flash("Los días no pueden ser negativos.", "error")
        return redirect(url_for("admin_dashboard", tab="vacaciones"))
    db = get_db()
    db.execute(
        "INSERT INTO vacaciones_saldo (persona, dias_totales) VALUES (?, ?) "
        "ON CONFLICT(persona) DO UPDATE SET dias_totales = excluded.dias_totales",
        (persona, dias_totales),
    )
    db.commit()
    flash(f"Días totales de {persona} actualizados a {dias_totales}.", "success")
    return redirect(url_for("admin_dashboard", tab="vacaciones"))


@app.route("/admin/dinero/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_movimiento(user):
    tipo = request.form.get("tipo")
    if tipo not in ("ingreso", "egreso"):
        flash("Tipo de movimiento inválido.", "error")
        return redirect(url_for("admin_dashboard", tab="dinero"))
    motivo = request.form.get("motivo", "").strip()
    monto_raw = request.form.get("monto", "").strip()
    try:
        monto = float(monto_raw)
    except ValueError:
        monto = None
    if not monto or monto <= 0 or not motivo:
        flash("Pon una cantidad válida y el motivo.", "error")
        return redirect(url_for("admin_dashboard", tab=tipo))
    db = get_db()
    db.execute(
        "INSERT INTO movimientos_dinero (tipo, monto, motivo) VALUES (?, ?, ?)", (tipo, monto, motivo)
    )
    db.commit()
    flash(f"{'Ingreso' if tipo == 'ingreso' else 'Egreso'} registrado.", "success")
    return redirect(url_for("admin_dashboard", tab=tipo))


@app.route("/admin/almacen/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_movimiento_almacen(user):
    tipo = request.form.get("tipo")
    if tipo not in ("entrada", "salida"):
        flash("Tipo de movimiento inválido.", "error")
        return redirect(url_for("admin_dashboard", tab="almacen"))
    material = request.form.get("material", "").strip()
    motivo = request.form.get("motivo", "").strip() or None
    cantidad_raw = request.form.get("cantidad", "").strip()
    try:
        cantidad = float(cantidad_raw)
    except ValueError:
        cantidad = None
    subtab = "existencia" if tipo == "entrada" else "entregado"
    if material not in MATERIALES_PRODUCTO_TERMINADO or not cantidad or cantidad <= 0:
        flash("Selecciona el material y pon una cantidad válida.", "error")
        return redirect(url_for("admin_dashboard", tab=subtab))
    db = get_db()
    if tipo == "salida":
        existencia = db.execute(
            "SELECT COALESCE(SUM(CASE WHEN tipo = 'entrada' THEN cantidad ELSE -cantidad END), 0) AS cantidad "
            "FROM almacen_movimientos WHERE material = ?",
            (material,),
        ).fetchone()["cantidad"]
        if cantidad > existencia:
            flash(f"No hay en existencia. Existencia actual de {material}: {existencia:.1f} kg.", "error")
            return redirect(url_for("admin_dashboard", tab=subtab))
    db.execute(
        "INSERT INTO almacen_movimientos (tipo, material, cantidad, motivo) VALUES (?, ?, ?, ?)",
        (tipo, material, cantidad, motivo),
    )
    db.commit()
    flash("Entrada a almacén registrada." if tipo == "entrada" else "Salida de almacén registrada.", "success")
    return redirect(url_for("admin_dashboard", tab=subtab))


@app.route("/admin/rutas/masivas", methods=["GET", "POST"])
@login_required("admin")
def admin_rutas_masivas(user):
    db = get_db()
    recolectores = db.execute("SELECT * FROM users WHERE role = 'recolector' ORDER BY name").fetchall()

    if request.method == "POST":
        # Toma el lock de escritura desde el inicio (antes de leer qué solicitudes están
        # pendientes) para que, si el formulario se envía dos veces casi al mismo tiempo, la
        # segunda petición espere a que la primera termine y confirme sus cambios, y así vea las
        # solicitudes ya marcadas 'programada' en vez de volver a programarlas por duplicado.
        try:
            db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            flash(
                "Ya se está generando otra ruta en este momento (probablemente un envío duplicado "
                "del mismo formulario). No se creó nada por duplicado — espera unos segundos y "
                "revisa el panel antes de reintentar.",
                "error",
            )
            return redirect(url_for("admin_dashboard"))
        zonas_seleccionadas = request.form.getlist("zonas")
        fecha = request.form.get("fecha") or date.today().isoformat()
        hora_salida = request.form.get("hora_salida") or "08:00"
        rutas_creadas = 0
        paradas_creadas = 0
        zonas_omitidas = []
        solicitudes_cajas_omitidas = 0
        pacientes_fusionados_a_vecina = 0
        pacientes_bajo_minimo_pendientes = 0
        pacientes_aislados_avisados = 0
        proximo_numero_ruta = siguiente_numero_ruta(db)
        parada_ids_nuevas = []
        for zona in zonas_seleccionadas:
            recolector_id = request.form.get(f"recolector_id__{zona}") or None
            if not recolector_id:
                zonas_omitidas.append(zona)
                continue
            puntos_raw = db.execute(
                "SELECT id, estado, lat, lon, cliente_id, direccion FROM solicitudes "
                "WHERE estado IN ('pendiente', 'pendiente_entrega') AND zona = ? "
                f"AND {condicion_lista_para_recoleccion()} "
                "ORDER BY COALESCE(fecha_reinicio_espera, created_at)",
                (zona,),
            ).fetchall()
            if not puntos_raw:
                continue
            # Se reordena por cercanía real (vecino más cercano desde el depósito) antes de
            # dividir en tandas: así cada ruta agrupa pacientes que ya están juntos en el
            # trayecto, en vez de que quién cae en qué tanda dependa de su fecha de alta/última
            # recolección —eso dejaba tandas finales a medio llenar aunque hubiera pacientes
            # cercanos disponibles para completarlas—. Dentro de cada tanda ya armada,
            # ordenar_grupo_por_cercania todavía reacomoda el orden de visita para el manejo.
            # Nota: como limitar_cajas_grupo (abajo) usa el orden de cada tanda para decidir a
            # quién le toca prioridad cuando no caben todas las solicitudes de cajas, esa
            # prioridad ahora es por cercanía dentro de la tanda en vez de por tiempo de espera.
            puntos = fusionar_puntos_mismo_cliente(puntos_raw)
            puntos = ordenar_por_cercania(puntos)
            grupos_sin_filtrar = dividir_puntos_por_duracion(puntos)
            grupos = []
            for grupo_crudo in grupos_sin_filtrar:
                grupo_filtrado, sobrantes = limitar_cajas_grupo(db, grupo_crudo)
                solicitudes_cajas_omitidas += len(sobrantes)
                if grupo_filtrado:
                    grupos.append(ordenar_grupo_por_cercania(grupo_filtrado))
            n_rutas_zona = 0
            for grupo in grupos:
                if len(grupo) < MIN_PARADAS_DESPACHO:
                    resultado, paradas_fusion_nuevas = intentar_despachar_grupo_pequeno(db, grupo)
                    if resultado == "fusionado":
                        pacientes_fusionados_a_vecina += len(grupo)
                        parada_ids_nuevas.extend(paradas_fusion_nuevas)
                    elif resultado == "aviso":
                        pacientes_aislados_avisados += len(grupo)
                    else:
                        pacientes_bajo_minimo_pendientes += len(grupo)
                    continue
                n_rutas_zona += 1
                if n_rutas_zona == 1:
                    nombre_ruta = zona
                else:
                    estimado_grupo = estimar_ruta(grupo)
                    km = estimado_grupo["distancia_km"] if estimado_grupo else 0
                    nombre_ruta = f"Ruta {proximo_numero_ruta:02d} ({km} km)"
                    proximo_numero_ruta += 1
                    for p in grupo:
                        db.execute("UPDATE solicitudes SET zona = ? WHERE id = ?", (nombre_ruta, p["id"]))
                        if p.get("extra_id"):
                            db.execute("UPDATE solicitudes SET zona = ? WHERE id = ?", (nombre_ruta, p["extra_id"]))
                cur = db.execute(
                    "INSERT INTO rutas (nombre, zona, fecha, hora_salida, recolector_id) VALUES (?, ?, ?, ?, ?)",
                    (nombre_ruta, nombre_ruta, fecha, hora_salida, recolector_id),
                )
                ruta_id = cur.lastrowid
                for i, p in enumerate(grupo, start=1):
                    cur_parada = db.execute(
                        "INSERT INTO paradas (ruta_id, solicitud_id, solicitud_extra_id, tipo_extra, orden, tipo) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (ruta_id, p["id"], p.get("extra_id"), p.get("tipo_extra"), i, p["tipo"]),
                    )
                    parada_ids_nuevas.append(cur_parada.lastrowid)
                    db.execute("UPDATE solicitudes SET estado = 'programada' WHERE id = ?", (p["id"],))
                    if p.get("extra_id"):
                        db.execute("UPDATE solicitudes SET estado = 'programada' WHERE id = ?", (p["extra_id"],))
                rutas_creadas += 1
                paradas_creadas += len(grupo)
        db.commit()
        if parada_ids_nuevas:
            threading.Thread(
                target=_notificar_paradas_programadas, args=(parada_ids_nuevas,), daemon=True
            ).start()
        mensaje = f"{rutas_creadas} ruta(s) creada(s) con {paradas_creadas} parada(s) en total."
        if zonas_omitidas:
            mensaje += (
                f" No se programaron {len(zonas_omitidas)} zona(s) por no tener recolector asignado: "
                + ", ".join(zonas_omitidas) + "."
            )
        if solicitudes_cajas_omitidas:
            mensaje += (
                f" {solicitudes_cajas_omitidas} solicitud(es) de cajas no se incluyeron por exceder el "
                f"máximo de {CAJAS_MAX_ENTREGA_RUTA} cajas de entrega o {CAJAS_MAX_RECEPCION_RUTA} de "
                "recepción por ruta; quedaron pendientes para otra ruta."
            )
        if pacientes_fusionados_a_vecina:
            mensaje += (
                f" {pacientes_fusionados_a_vecina} paciente(s) no juntaban el mínimo de "
                f"{MIN_PARADAS_DESPACHO} en su zona y se integraron a la ruta planificada más cercana."
            )
        if pacientes_bajo_minimo_pendientes:
            mensaje += (
                f" {pacientes_bajo_minimo_pendientes} paciente(s) quedaron pendientes por no juntar "
                f"el mínimo de {MIN_PARADAS_DESPACHO} ni tener una ruta cercana con espacio; se "
                "revisan en la siguiente corrida."
            )
        if pacientes_aislados_avisados:
            mensaje += (
                f" {pacientes_aislados_avisados} paciente(s) están demasiado aislados para juntar el "
                "mínimo — se te avisó en notificaciones para que decidas caso por caso."
            )
        hubo_pendientes = pacientes_bajo_minimo_pendientes or pacientes_aislados_avisados
        flash(
            mensaje,
            "success" if not zonas_omitidas and not solicitudes_cajas_omitidas and not hubo_pendientes else "error",
        )
        return redirect(url_for("admin_dashboard", tab="rutas"))

    zonas = db.execute(
        "SELECT zona, COUNT(*) AS n FROM solicitudes WHERE estado IN ('pendiente', 'pendiente_entrega') "
        f"AND zona IS NOT NULL AND {condicion_lista_para_recoleccion()} GROUP BY zona ORDER BY zona"
    ).fetchall()
    return render_template("admin_rutas_masivas.html", zonas=zonas, recolectores=recolectores)


@app.route("/admin/rutas/<int:ruta_id>")
@login_required("admin")
def admin_ver_ruta(user, ruta_id):
    db = get_db()
    ruta = db.execute(
        "SELECT r.*, u.name AS recolector_nombre FROM rutas r "
        "LEFT JOIN users u ON u.id = r.recolector_id WHERE r.id = ?",
        (ruta_id,),
    ).fetchone()
    paradas = db.execute(
        "SELECT p.*, s.direccion, s.material, s.modalidad, s.notas AS notas_solicitud, "
        "s.cantidad_cajas, s.lat, s.lon, s.bote_a_devolver, "
        "COALESCE(u.name, s.nombre_contacto) AS cliente_nombre, "
        "s2.material AS material_extra, s2.cantidad_cajas AS cantidad_cajas_extra "
        "FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
        "LEFT JOIN solicitudes s2 ON s2.id = p.solicitud_extra_id "
        "LEFT JOIN users u ON u.id = s.cliente_id WHERE p.ruta_id = ? ORDER BY p.orden",
        (ruta_id,),
    ).fetchall()
    paradas_json = json.dumps([dict(p) for p in paradas])
    estimado = estimar_ruta(paradas)
    tiempo_real = None
    if ruta["hora_inicio_real"]:
        try:
            inicio_real = datetime.strptime(ruta["hora_inicio_real"], "%Y-%m-%d %H:%M:%S")
            fin_real = (
                datetime.strptime(ruta["hora_fin_real"], "%Y-%m-%d %H:%M:%S")
                if ruta["hora_fin_real"] else datetime.now()
            )
            tiempo_real = formatear_duracion((fin_real - inicio_real).total_seconds() / 60)
        except ValueError:
            tiempo_real = None
    kg_total = db.execute(
        "SELECT COALESCE(SUM(kg_recolectados), 0) AS kg FROM paradas WHERE ruta_id = ?", (ruta_id,)
    ).fetchone()["kg"]

    candidatos = []
    aviso_exceso = None
    if ruta["estado"] != "completada" and ruta["zona"]:
        ids_en_ruta = set()
        for p in paradas:
            ids_en_ruta.add(p["solicitud_id"])
            if p["solicitud_extra_id"]:
                ids_en_ruta.add(p["solicitud_extra_id"])
        candidatos = db.execute(
            "SELECT s.id, COALESCE(u.name, s.nombre_contacto) AS nombre, s.direccion "
            "FROM solicitudes s LEFT JOIN users u ON u.id = s.cliente_id "
            "WHERE s.zona = ? AND s.estado IN ('pendiente', 'pendiente_entrega') "
            f"AND {condicion_lista_para_recoleccion('s')} ORDER BY s.created_at",
            (ruta["zona"],),
        ).fetchall()
        candidatos = [c for c in candidatos if c["id"] not in ids_en_ruta]

        confirmar_id = request.args.get("confirmar_agregar", type=int)
        if confirmar_id:
            candidato_preview = next((c for c in candidatos if c["id"] == confirmar_id), None)
            if candidato_preview:
                sol_preview = db.execute(
                    "SELECT lat, lon FROM solicitudes WHERE id = ?", (confirmar_id,)
                ).fetchone()
                puntos_prueba = [dict(p) for p in paradas] + [dict(sol_preview)]
                estimado_prueba = estimar_ruta(puntos_prueba)
                aviso_exceso = {
                    "solicitud_id": confirmar_id, "nombre": candidato_preview["nombre"],
                    "duracion": estimado_prueba["duracion"] if estimado_prueba else "desconocida",
                }

    return render_template(
        "admin_ruta.html", ruta=ruta, paradas=paradas, paradas_json=paradas_json, estimado=estimado,
        tiempo_real=tiempo_real, kg_total=kg_total, candidatos=candidatos, aviso_exceso=aviso_exceso,
    )


@app.route("/admin/rutas/<int:ruta_id>/agregar-paciente", methods=["POST"])
@login_required("admin")
def admin_agregar_paciente_ruta(user, ruta_id):
    db = get_db()
    ruta = db.execute("SELECT * FROM rutas WHERE id = ?", (ruta_id,)).fetchone()
    if ruta is None:
        flash("Esa ruta ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="rutas"))
    if ruta["estado"] == "completada":
        flash("Esta ruta ya se completó, no se puede modificar.", "error")
        return redirect(url_for("admin_ver_ruta", ruta_id=ruta_id))

    solicitud_id = request.form.get("solicitud_id", type=int)
    confirmar_exceso = request.form.get("confirmar_exceso") == "1"
    candidato = db.execute(
        "SELECT * FROM solicitudes WHERE id = ? AND estado IN ('pendiente', 'pendiente_entrega')",
        (solicitud_id,),
    ).fetchone()
    if candidato is None:
        flash("Selecciona un paciente pendiente válido.", "error")
        return redirect(url_for("admin_ver_ruta", ruta_id=ruta_id))

    paradas_actuales = db.execute(
        "SELECT s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id WHERE p.ruta_id = ?",
        (ruta_id,),
    ).fetchall()
    puntos_prueba = [dict(p) for p in paradas_actuales] + [{"lat": candidato["lat"], "lon": candidato["lon"]}]
    estimado_prueba = estimar_ruta(puntos_prueba)
    if estimado_prueba and estimado_prueba["minutos"] > DURACION_MAXIMA_RUTA_MIN and not confirmar_exceso:
        flash(
            f"Agregar a este paciente deja la ruta en {estimado_prueba['duracion']}, por encima del límite de "
            f"{formatear_duracion(DURACION_MAXIMA_RUTA_MIN)}. Confirma si quieres agregarlo de todas formas.",
            "error",
        )
        return redirect(url_for("admin_ver_ruta", ruta_id=ruta_id, confirmar_agregar=solicitud_id))

    tipo_nuevo = "entrega" if candidato["estado"] == "pendiente_entrega" else "recoleccion"
    no_arranco = ruta["estado"] == "planificada" and ruta["hora_inicio_real"] is None
    if no_arranco:
        # La ruta no ha salido: mete la parada nueva y reacomoda todo el recorrido por cercanía
        # real, para que quede en la posición que le toca en vez de siempre al final.
        db.execute(
            "INSERT INTO paradas (ruta_id, solicitud_id, orden, tipo) VALUES (?, ?, 0, ?)",
            (ruta_id, candidato["id"], tipo_nuevo),
        )
        todas = [dict(p) for p in db.execute(
            "SELECT p.id AS parada_id, s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
            "WHERE p.ruta_id = ?",
            (ruta_id,),
        ).fetchall()]
        todas_ordenadas = ordenar_por_cercania(todas)
        for i, p in enumerate(todas_ordenadas, start=1):
            db.execute("UPDATE paradas SET orden = ? WHERE id = ?", (i, p["parada_id"]))
    else:
        # La ruta ya va en curso: se agrega al final para no reordenar paradas ya resueltas.
        siguiente_orden = db.execute(
            "SELECT COALESCE(MAX(orden), 0) + 1 AS n FROM paradas WHERE ruta_id = ?", (ruta_id,)
        ).fetchone()["n"]
        db.execute(
            "INSERT INTO paradas (ruta_id, solicitud_id, orden, tipo) VALUES (?, ?, ?, ?)",
            (ruta_id, candidato["id"], siguiente_orden, tipo_nuevo),
        )
    db.execute("UPDATE solicitudes SET estado = 'programada' WHERE id = ?", (candidato["id"],))
    db.commit()

    nombre = candidato["nombre_contacto"]
    telefono = None
    if candidato["cliente_id"]:
        u = db.execute("SELECT name, telefono FROM users WHERE id = ?", (candidato["cliente_id"],)).fetchone()
        if u:
            nombre = u["name"]
            telefono = u["telefono"]
    if telefono:
        parada_nueva = db.execute(
            "SELECT id FROM paradas WHERE ruta_id = ? AND solicitud_id = ?", (ruta_id, candidato["id"])
        ).fetchone()
        threading.Thread(
            target=_notificar_paradas_programadas, args=([parada_nueva["id"]],), daemon=True
        ).start()

    flash(f"Se agregó a '{nombre}' a la ruta.", "success")
    return redirect(url_for("admin_ver_ruta", ruta_id=ruta_id))


@app.route("/admin/administradores/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_admin(user):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede crear cuentas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    name = request.form["name"].strip()
    email = request.form["email"].strip().lower()
    password = request.form["password"]
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        flash("Ese correo ya está registrado.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute(
        "INSERT INTO users (name, email, password_hash, role, es_admin_general) VALUES (?, ?, ?, 'admin', 0)",
        (name, email, generate_password_hash(password, method="pbkdf2:sha256")),
    )
    db.commit()
    flash(f"Cuenta de administrador '{name}' creada.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/administradores/<int:user_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_admin(user, user_id):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede dar de baja cuentas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db = get_db()
    r = db.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'admin' AND es_admin_general = 0", (user_id,)
    ).fetchone()
    if r is None:
        flash("Esa cuenta ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Cuenta de administrador '{r['name']}' eliminada.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/administradores/<int:user_id>/restablecer-password", methods=["POST"])
@login_required("admin")
def admin_restablecer_password_admin(user, user_id):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede restablecer contraseñas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db = get_db()
    r = db.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'admin' AND es_admin_general = 0", (user_id,)
    ).fetchone()
    if r is None:
        flash("Esa cuenta ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    password = request.form.get("password", "")
    if len(password) < 4:
        flash("La contraseña debe tener al menos 4 caracteres.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(password, method="pbkdf2:sha256"), user_id),
    )
    db.commit()
    flash(f"Contraseña de '{r['name']}' actualizada.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/usuarios/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_recolector(user):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede crear cuentas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    name = request.form["name"].strip()
    email = request.form["email"].strip().lower()
    password = request.form["password"]
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        flash("Ese correo ya está registrado.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'recolector')",
        (name, email, generate_password_hash(password, method="pbkdf2:sha256")),
    )
    db.commit()
    flash(f"Recolector '{name}' creado.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/usuarios/<int:user_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_recolector(user, user_id):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede dar de baja cuentas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db = get_db()
    r = db.execute("SELECT * FROM users WHERE id = ? AND role = 'recolector'", (user_id,)).fetchone()
    if r is None:
        flash("Ese recolector ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    rutas_activas = db.execute(
        "SELECT COUNT(*) AS n FROM rutas WHERE recolector_id = ? AND estado != 'completada'", (user_id,)
    ).fetchone()["n"]
    if rutas_activas:
        flash(
            f"No puedes eliminar a '{r['name']}' — tiene {rutas_activas} ruta(s) activa(s) asignada(s). "
            "Reasígnalas a otro recolector o elimínalas primero.",
            "error",
        )
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute("UPDATE rutas SET recolector_id = NULL WHERE recolector_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Recolector '{r['name']}' eliminado.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/usuarios/<int:user_id>/restablecer-password", methods=["POST"])
@login_required("admin")
def admin_restablecer_password_recolector(user, user_id):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede restablecer contraseñas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db = get_db()
    r = db.execute("SELECT * FROM users WHERE id = ? AND role = 'recolector'", (user_id,)).fetchone()
    if r is None:
        flash("Ese recolector ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    password = request.form.get("password", "")
    if len(password) < 4:
        flash("La contraseña debe tener al menos 4 caracteres.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(password, method="pbkdf2:sha256"), user_id),
    )
    db.commit()
    flash(f"Contraseña de '{r['name']}' actualizada.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/nef/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_nef(user):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede crear cuentas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    name = request.form["name"].strip()
    email = request.form["email"].strip().lower()
    password = request.form["password"]
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        flash("Ese correo ya está registrado.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'nef')",
        (name, email, generate_password_hash(password, method="pbkdf2:sha256")),
    )
    db.commit()
    flash(f"Cuenta de NEF '{name}' creada.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/nef/<int:user_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_nef(user, user_id):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede dar de baja cuentas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db = get_db()
    r = db.execute("SELECT * FROM users WHERE id = ? AND role = 'nef'", (user_id,)).fetchone()
    if r is None:
        flash("Esa cuenta de NEF ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Cuenta de NEF '{r['name']}' eliminada.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/nef/<int:user_id>/restablecer-password", methods=["POST"])
@login_required("admin")
def admin_restablecer_password_nef(user, user_id):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede restablecer contraseñas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db = get_db()
    r = db.execute("SELECT * FROM users WHERE id = ? AND role = 'nef'", (user_id,)).fetchone()
    if r is None:
        flash("Esa cuenta de NEF ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    password = request.form.get("password", "")
    if len(password) < 4:
        flash("La contraseña debe tener al menos 4 caracteres.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(password, method="pbkdf2:sha256"), user_id),
    )
    db.commit()
    flash(f"Contraseña de '{r['name']}' actualizada.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/administradores-generales/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_admin_general(user):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede crear cuentas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    name = request.form["name"].strip()
    email = request.form["email"].strip().lower()
    password = request.form["password"]
    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        flash("Ese correo ya está registrado.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute(
        "INSERT INTO users (name, email, password_hash, role, es_admin_general) VALUES (?, ?, ?, 'admin', 1)",
        (name, email, generate_password_hash(password, method="pbkdf2:sha256")),
    )
    db.commit()
    flash(f"Cuenta de administrador general '{name}' creada.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/administradores-generales/<int:user_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_admin_general(user, user_id):
    if not user["es_admin_general"]:
        flash("Solo el administrador general puede dar de baja cuentas.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db = get_db()
    r = db.execute(
        "SELECT * FROM users WHERE id = ? AND role = 'admin' AND es_admin_general = 1", (user_id,)
    ).fetchone()
    if r is None:
        flash("Esa cuenta ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    if r["id"] == user["id"]:
        flash("No puedes eliminar tu propia cuenta.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Cuenta de administrador general '{r['name']}' eliminada.", "success")
    return redirect(url_for("admin_dashboard", tab="recolectores"))


@app.route("/admin/videos/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_video(user):
    titulo = request.form.get("titulo", "").strip()
    descripcion = request.form.get("descripcion", "").strip() or None
    archivo = request.files.get("archivo")
    if not titulo:
        flash("Ponle un título al video.", "error")
        return redirect(url_for("admin_dashboard", tab="videos"))
    if not archivo or not archivo.filename:
        flash("Sube un video.", "error")
        return redirect(url_for("admin_dashboard", tab="videos"))
    extension = archivo.filename.rsplit(".", 1)[-1].lower() if "." in archivo.filename else ""
    if extension not in NEF_VIDEO_EXTENSIONES:
        flash("Formato de video no soportado. Usa mp4, mov, webm o m4v.", "error")
        return redirect(url_for("admin_dashboard", tab="videos"))
    os.makedirs(ADMIN_VIDEOS_DIR, exist_ok=True)
    nombre_archivo = f"{secrets.token_hex(8)}_{secure_filename(archivo.filename)}"
    archivo.save(os.path.join(ADMIN_VIDEOS_DIR, nombre_archivo))

    db = get_db()
    db.execute(
        "INSERT INTO admin_videos (titulo, descripcion, archivo) VALUES (?, ?, ?)",
        (titulo, descripcion, nombre_archivo),
    )
    db.commit()
    flash("Video subido.", "success")
    return redirect(url_for("admin_dashboard", tab="videos"))


@app.route("/admin/videos/<int:video_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_video(user, video_id):
    db = get_db()
    video = db.execute("SELECT * FROM admin_videos WHERE id = ?", (video_id,)).fetchone()
    if video:
        ruta_archivo = os.path.join(ADMIN_VIDEOS_DIR, video["archivo"])
        if os.path.exists(ruta_archivo):
            os.remove(ruta_archivo)
        db.execute("DELETE FROM admin_videos WHERE id = ?", (video_id,))
        db.commit()
        flash("Video eliminado.", "success")
    return redirect(url_for("admin_dashboard", tab="videos"))


@app.route("/admin/pedidos/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_pedido(user):
    material = request.form.get("material", "").strip()
    cantidad = request.form.get("cantidad", "").strip()
    unidad = request.form.get("unidad", "").strip() or None
    proveedor = request.form.get("proveedor", "").strip() or None
    notas = request.form.get("notas", "").strip() or None
    if cantidad:
        try:
            cantidad = float(cantidad)
        except ValueError:
            flash("La cantidad debe ser un número.", "error")
            return redirect(url_for("admin_dashboard", tab="pedidos"))
        if cantidad <= 0:
            flash("La cantidad debe ser mayor a cero.", "error")
            return redirect(url_for("admin_dashboard", tab="pedidos"))
    else:
        cantidad = None
    if not material:
        flash("Pon el material.", "error")
        return redirect(url_for("admin_dashboard", tab="pedidos"))
    db = get_db()
    db.execute(
        "INSERT INTO pedidos_material (material, cantidad, unidad, proveedor, notas) VALUES (?, ?, ?, ?, ?)",
        (material, cantidad, unidad, proveedor, notas),
    )
    db.commit()
    flash("Pedido registrado.", "success")
    return redirect(url_for("admin_dashboard", tab="pedidos"))


@app.route("/admin/pedidos/<int:pedido_id>/recibido", methods=["POST"])
@login_required("admin")
def admin_pedido_recibido(user, pedido_id):
    db = get_db()
    pedido = db.execute(
        "SELECT * FROM pedidos_material WHERE id = ? AND estado = 'pendiente'", (pedido_id,)
    ).fetchone()
    if pedido is None:
        flash("Ese pedido ya no está pendiente.", "error")
        return redirect(url_for("admin_dashboard", tab="pedidos"))
    db.execute(
        "UPDATE pedidos_material SET estado = 'recibido', fecha_recibido = datetime('now','localtime') "
        "WHERE id = ?",
        (pedido_id,),
    )
    db.commit()
    flash(f"Pedido de {pedido['material']} marcado como recibido.", "success")
    return redirect(url_for("admin_dashboard", tab="pedidos"))


@app.route("/admin/pedidos/<int:pedido_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_pedido(user, pedido_id):
    db = get_db()
    db.execute("DELETE FROM pedidos_material WHERE id = ?", (pedido_id,))
    db.commit()
    flash("Pedido eliminado.", "success")
    return redirect(url_for("admin_dashboard", tab="pedidos"))


# ---------- Recolector ----------

@app.route("/recolector")
@login_required("recolector")
def recolector_dashboard(user):
    db = get_db()
    rutas = db.execute(
        "SELECT r.*, "
        "(SELECT COUNT(*) FROM paradas p WHERE p.ruta_id = r.id) AS total_paradas, "
        "(SELECT COUNT(*) FROM paradas p WHERE p.ruta_id = r.id AND p.estado != 'pendiente') AS paradas_hechas "
        "FROM rutas r WHERE r.recolector_id = ? ORDER BY r.fecha DESC",
        (user["id"],),
    ).fetchall()
    horas_extra_registros = db.execute(
        "SELECT * FROM horas_extra WHERE recolector_id = ? ORDER BY fecha DESC, hora_inicio DESC",
        (user["id"],),
    ).fetchall()
    horas_extra_total = sum(r["horas_extra"] for r in horas_extra_registros)
    return render_template(
        "recolector_dashboard.html", rutas=rutas, hoy=date.today().isoformat(),
        horas_extra_registros=horas_extra_registros, horas_extra_total=horas_extra_total,
    )


@app.route("/recolector/horas-extra", methods=["POST"])
@login_required("recolector")
def recolector_registrar_horas_extra(user):
    fecha = request.form.get("fecha", "").strip()
    hora_inicio = request.form.get("hora_inicio", "").strip()
    hora_salida = request.form.get("hora_salida", "").strip()
    if not fecha or not hora_inicio or not hora_salida:
        flash("Completa la fecha, la hora de entrada y la hora de salida.", "error")
        return redirect(url_for("recolector_dashboard"))
    try:
        inicio = datetime.strptime(hora_inicio, "%H:%M")
        salida = datetime.strptime(hora_salida, "%H:%M")
    except ValueError:
        flash("Hora inválida.", "error")
        return redirect(url_for("recolector_dashboard"))
    horas_trabajadas = (salida - inicio).total_seconds() / 3600
    if horas_trabajadas <= 0:
        horas_trabajadas += 24  # turno que cruza medianoche
    horas_extra = max(0, horas_trabajadas - 8)
    db = get_db()
    db.execute(
        "INSERT INTO horas_extra (recolector_id, fecha, hora_inicio, hora_salida, horas_trabajadas, horas_extra) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], fecha, hora_inicio, hora_salida, horas_trabajadas, horas_extra),
    )
    db.commit()
    flash("Horas registradas.", "success")
    return redirect(url_for("recolector_dashboard"))


@app.route("/recolector/rutas/<int:ruta_id>")
@login_required("recolector")
def recolector_ver_ruta(user, ruta_id):
    db = get_db()
    ruta = db.execute(
        "SELECT * FROM rutas WHERE id = ? AND recolector_id = ?", (ruta_id, user["id"])
    ).fetchone()
    if ruta is None:
        flash("Esa ruta no está asignada a tu cuenta.", "error")
        return redirect(url_for("recolector_dashboard"))
    paradas = db.execute(
        "SELECT p.*, s.direccion, s.material, s.modalidad, s.notas AS notas_solicitud, "
        "s.cantidad_cajas, s.lat, s.lon, COALESCE(s.telefono, u.telefono) AS telefono, "
        "s.tipo_redistribucion, s.bote_a_devolver, "
        "COALESCE(u.name, s.nombre_contacto) AS cliente_nombre, "
        "s2.material AS material_extra, s2.cantidad_cajas AS cantidad_cajas_extra, "
        "s2.tipo_redistribucion AS tipo_redistribucion_extra "
        "FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
        "LEFT JOIN solicitudes s2 ON s2.id = p.solicitud_extra_id "
        "LEFT JOIN users u ON u.id = s.cliente_id WHERE p.ruta_id = ? ORDER BY p.orden",
        (ruta_id,),
    ).fetchall()
    paradas_json = json.dumps([dict(p) for p in paradas])
    estimado = estimar_ruta(paradas)
    tiempo_real = None
    if ruta["hora_inicio_real"]:
        try:
            inicio_real = datetime.strptime(ruta["hora_inicio_real"], "%Y-%m-%d %H:%M:%S")
            fin_real = (
                datetime.strptime(ruta["hora_fin_real"], "%Y-%m-%d %H:%M:%S")
                if ruta["hora_fin_real"] else datetime.now()
            )
            tiempo_real = formatear_duracion((fin_real - inicio_real).total_seconds() / 60)
        except ValueError:
            tiempo_real = None
    botes_pendientes = sum(
        1 for p in paradas
        if p["estado"] == "pendiente" and (
            (p["tipo"] == "entrega" and p["tipo_redistribucion"] is None)
            or (p["tipo_extra"] == "entrega" and p["tipo_redistribucion_extra"] is None)
        )
    )
    cajas_por_material = {}
    for p in paradas:
        if p["estado"] != "pendiente":
            continue
        if p["tipo_redistribucion"] == "material" and p["cantidad_cajas"]:
            cajas_por_material[p["material"]] = cajas_por_material.get(p["material"], 0) + p["cantidad_cajas"]
        if p["tipo_redistribucion_extra"] == "material" and p["cantidad_cajas_extra"]:
            cajas_por_material[p["material_extra"]] = (
                cajas_por_material.get(p["material_extra"], 0) + p["cantidad_cajas_extra"]
            )
    return render_template(
        "recolector_ruta.html", ruta=ruta, paradas=paradas, paradas_json=paradas_json,
        estimado=estimado, tiempo_real=tiempo_real, botes_pendientes=botes_pendientes,
        cajas_por_material=cajas_por_material,
    )


@app.route("/recolector/rutas/<int:ruta_id>/iniciar", methods=["POST"])
@login_required("recolector")
def recolector_iniciar_ruta(user, ruta_id):
    db = get_db()
    ruta = db.execute(
        "SELECT * FROM rutas WHERE id = ? AND recolector_id = ?", (ruta_id, user["id"])
    ).fetchone()
    if ruta is None:
        flash("Esa ruta no está asignada a tu cuenta.", "error")
        return redirect(url_for("recolector_dashboard"))
    if ruta["hora_inicio_real"]:
        flash("Esta ruta ya se había iniciado.", "error")
        return redirect(url_for("recolector_ver_ruta", ruta_id=ruta_id))

    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    nuevo_estado = "en_curso" if ruta["estado"] == "planificada" else ruta["estado"]
    db.execute(
        "UPDATE rutas SET hora_inicio_real = ?, estado = ? WHERE id = ?", (ahora, nuevo_estado, ruta_id)
    )
    db.commit()
    threading.Thread(target=_notificar_siguiente_parada, args=(ruta_id,), daemon=True).start()
    flash("Ruta iniciada. Los horarios de los pacientes se recalculan desde ahora.", "success")
    return redirect(url_for("recolector_ver_ruta", ruta_id=ruta_id))


@app.route("/recolector/rutas/<int:ruta_id>/finalizar", methods=["POST"])
@login_required("recolector")
def recolector_finalizar_ruta(user, ruta_id):
    db = get_db()
    ruta = db.execute(
        "SELECT * FROM rutas WHERE id = ? AND recolector_id = ?", (ruta_id, user["id"])
    ).fetchone()
    if ruta is None:
        flash("Esa ruta no está asignada a tu cuenta.", "error")
        return redirect(url_for("recolector_dashboard"))
    if not ruta["hora_inicio_real"]:
        flash("Primero tienes que iniciar la ruta.", "error")
        return redirect(url_for("recolector_ver_ruta", ruta_id=ruta_id))
    if ruta["hora_fin_real"]:
        flash("Esta ruta ya se había finalizado.", "error")
        return redirect(url_for("recolector_ver_ruta", ruta_id=ruta_id))

    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE rutas SET hora_fin_real = ? WHERE id = ?", (ahora, ruta_id))
    db.execute(
        "UPDATE solicitudes SET estado = 'pendiente' WHERE estado IN ('recolectada', 'incidencia') "
        "AND id IN (SELECT solicitud_id FROM paradas WHERE ruta_id = ?)",
        (ruta_id,),
    )
    db.commit()
    flash("Ruta finalizada. El reporte queda guardado y todos sus pacientes vuelven a quedar disponibles para programarse el próximo mes.", "success")
    return redirect(url_for("recolector_ver_ruta", ruta_id=ruta_id))


@app.route("/recolector/rutas/<int:ruta_id>/suspender", methods=["POST"])
@login_required("recolector")
def recolector_suspender_ruta(user, ruta_id):
    db = get_db()
    ruta = db.execute(
        "SELECT * FROM rutas WHERE id = ? AND recolector_id = ?", (ruta_id, user["id"])
    ).fetchone()
    if ruta is None:
        flash("Esa ruta no está asignada a tu cuenta.", "error")
        return redirect(url_for("recolector_dashboard"))
    if not ruta["hora_inicio_real"]:
        flash("Primero tienes que iniciar la ruta.", "error")
        return redirect(url_for("recolector_ver_ruta", ruta_id=ruta_id))
    if ruta["hora_fin_real"]:
        flash("Esta ruta ya se había finalizado.", "error")
        return redirect(url_for("recolector_ver_ruta", ruta_id=ruta_id))

    motivo = request.form.get("motivo", "").strip()
    nota = f"Ruta suspendida: {motivo}" if motivo else "Ruta suspendida por una eventualidad."

    paradas = db.execute(
        "SELECT p.*, s.cliente_id AS cliente_id, s2.cliente_id AS cliente_id_extra "
        "FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
        "LEFT JOIN solicitudes s2 ON s2.id = p.solicitud_extra_id "
        "WHERE p.ruta_id = ?",
        (ruta_id,),
    ).fetchall()

    afectados = {}
    for p in paradas:
        if p["estado"] == "pendiente":
            estado_previo = "pendiente_entrega" if p["tipo"] == "entrega" else "pendiente"
            db.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (estado_previo, p["solicitud_id"]))
            db.execute("UPDATE paradas SET estado = 'incidencia', notas = ? WHERE id = ?", (nota, p["id"]))
            if p["cliente_id"]:
                u = db.execute("SELECT name, telefono FROM users WHERE id = ?", (p["cliente_id"],)).fetchone()
                if u and u["telefono"]:
                    afectados[u["telefono"]] = u["name"]
        if p["solicitud_extra_id"] and p["estado_extra"] == "pendiente":
            estado_previo_extra = "pendiente_entrega" if p["tipo_extra"] == "entrega" else "pendiente"
            db.execute(
                "UPDATE solicitudes SET estado = ? WHERE id = ?", (estado_previo_extra, p["solicitud_extra_id"])
            )
            db.execute("UPDATE paradas SET estado_extra = 'incidencia' WHERE id = ?", (p["id"],))
            if p["cliente_id_extra"]:
                u2 = db.execute("SELECT name, telefono FROM users WHERE id = ?", (p["cliente_id_extra"],)).fetchone()
                if u2 and u2["telefono"]:
                    afectados[u2["telefono"]] = u2["name"]

    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute("UPDATE rutas SET hora_fin_real = ?, estado = 'completada' WHERE id = ?", (ahora, ruta_id))
    crear_notificacion_admin(
        db, None,
        f"El recolector '{user['name']}' suspendió la ruta '{ruta['nombre']}' — {nota} "
        f"({len(afectados)} paciente(s) afectado(s), quedaron disponibles para reprogramar).",
    )
    db.commit()

    for telefono, nombre in afectados.items():
        enviar_whatsapp_primer_contacto(
            telefono_whatsapp_e164(telefono),
            "TWILIO_TEMPLATE_RUTA_SUSPENDIDA_SID",
            {"1": nombre},
            f"Hola {nombre},\n\n"
            "Lamentamos informarte que la ruta de recolección de hoy tuvo que suspenderse por una "
            "eventualidad. Te ofrecemos una disculpa por las molestias.\n\n"
            "Vamos a reprogramar tu recolección lo antes posible y te avisaremos en cuanto quede lista "
            "una nueva fecha.\n\nGracias por tu paciencia.",
        )

    flash(
        f"Ruta suspendida. Se avisó a {len(afectados)} paciente(s) — sus recolecciones quedaron "
        "disponibles para reprogramar.",
        "success",
    )
    return redirect(url_for("recolector_dashboard"))


def _resolver_resultado_parte(db, solicitud_id, tipo, tipo_redistribucion, material, cantidad_cajas,
                               resultado, parada_id, sufijo_notas=""):
    """Aplica el resultado (completada/ausente/incidencia) que el recolector reportó para UNA
    parte de una parada —la solicitud principal, o la fusionada como 'extra' en la misma
    visita— actualizando el estado de esa solicitud y, si corresponde, registrando el
    movimiento de inventario de botes o cajas. tipo/tipo_redistribucion/material/cantidad_cajas
    ya vienen resueltos para esa parte específica."""
    es_entrega = tipo == "entrega"
    if resultado == "completada":
        if tipo_redistribucion is None:
            fecha_reinicio = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "UPDATE solicitudes SET estado = 'pendiente', fecha_reinicio_espera = ? WHERE id = ?",
                (fecha_reinicio, solicitud_id),
            )
            if es_entrega:
                registrar_movimiento_botes(db, "entrega", 1, f"Parada #{parada_id}{sufijo_notas}")
        else:
            db.execute("UPDATE solicitudes SET estado = 'recolectada' WHERE id = ?", (solicitud_id,))
            if cantidad_cajas:
                signo = 1 if tipo_redistribucion == "donar" else -1
                registrar_movimiento_cajas(
                    db, material, "donacion" if signo > 0 else "entrega",
                    signo * cantidad_cajas, f"Parada #{parada_id}{sufijo_notas}",
                )
    elif resultado == "ausente":
        solicitud_estado = "pendiente_entrega" if es_entrega else "pendiente"
        db.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (solicitud_estado, solicitud_id))
    else:
        db.execute("UPDATE solicitudes SET estado = 'incidencia' WHERE id = ?", (solicitud_id,))


def intentar_llenar_hueco_ausente(
    db, ruta_id, lat_ausente, lon_ausente, excluir_parada_id=None, solicitud_id_ausente=None,
):
    """Cuando una parada queda 'ausente' —el recolector no encontró a nadie, o el paciente avisó
    de antemano en la plataforma que no va a poder recibir la recolección (en cuyo caso esa
    parada ya se eliminó y solo llega su ubicación, no su id)— revisa si añadir una parada más
    sigue cabiendo dentro de DURACION_MAXIMA_RUTA_MIN y, si es así, busca en la misma zona al
    paciente pendiente más cercano a esa dirección (que ya le toque recolección, y que no esté ya
    en esta ruta) y lo agrega, para no desperdiciar el hueco que dejó. excluir_parada_id se usa
    solo cuando la parada ausente TODAVÍA existe en la ruta (el caso del recolector) para no
    contarla al estimar cuánto sobra ni al reacomodar; solicitud_id_ausente excluye al paciente
    que acaba de rechazar de ser su propio "candidato cercano" —si no, como queda pendiente de
    nuevo justo antes de esta búsqueda, se seleccionaría a sí mismo (distancia cero) y quedaría
    otra vez en la misma ruta que acaba de rechazar. Si la ruta todavía no arranca, reacomoda
    TODAS sus paradas por cercanía real para intercalar la nueva en la posición que le
    corresponde; si ya está en curso, la agrega al final para no reordenar paradas ya resueltas.
    No hace nada si la ruta ya terminó, no hay margen de tiempo, o no hay ningún candidato
    disponible en la zona. Devuelve la dirección del paciente agregado, o None si no se agregó a
    nadie."""
    ruta = db.execute(
        "SELECT nombre, zona, estado, hora_inicio_real, hora_fin_real FROM rutas WHERE id = ?", (ruta_id,)
    ).fetchone()
    # hora_fin_real solo se llena cuando el recolector cierra la ruta explícitamente — a
    # diferencia de 'estado', que puede haber quedado en 'completada' nada más porque ya no
    # quedaban paradas pendientes en ese momento, sin que la ruta esté realmente cerrada.
    if ruta is None or ruta["hora_fin_real"] is not None or not ruta["zona"]:
        return None
    if lat_ausente is None or lon_ausente is None:
        return None
    excluir_parada_id = excluir_parada_id or -1

    ids_en_ruta = set()
    for r in db.execute(
        "SELECT solicitud_id, solicitud_extra_id FROM paradas WHERE ruta_id = ?", (ruta_id,)
    ).fetchall():
        ids_en_ruta.add(r["solicitud_id"])
        if r["solicitud_extra_id"]:
            ids_en_ruta.add(r["solicitud_extra_id"])
    if solicitud_id_ausente is not None:
        ids_en_ruta.add(solicitud_id_ausente)

    # Busca candidatos por la zona REAL de la ruta (ruta.zona), no por el campo zona de la
    # solicitud ausente — así, si esa solicitud se hubiera quedado con un valor de zona viejo,
    # de todos modos se busca y se coloca en la ruta en la que en verdad está parada.
    candidatos = db.execute(
        "SELECT id, estado, lat, lon, direccion FROM solicitudes WHERE zona = ? "
        "AND estado IN ('pendiente', 'pendiente_entrega') "
        f"AND {condicion_lista_para_recoleccion()} AND lat IS NOT NULL AND lon IS NOT NULL",
        (ruta["zona"],),
    ).fetchall()
    candidatos = [c for c in candidatos if c["id"] not in ids_en_ruta]
    if not candidatos:
        return None
    candidato = min(
        candidatos, key=lambda c: haversine_km(lat_ausente, lon_ausente, c["lat"], c["lon"])
    )

    puntos_ruta = db.execute(
        "SELECT s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
        "WHERE p.ruta_id = ? AND p.id != ? ORDER BY p.orden",
        (ruta_id, excluir_parada_id),
    ).fetchall()
    puntos_prueba = [dict(p) for p in puntos_ruta] + [{"lat": candidato["lat"], "lon": candidato["lon"]}]
    estimado = estimar_ruta(puntos_prueba)
    if estimado and estimado["minutos"] > DURACION_MAXIMA_RUTA_MIN:
        return None

    tipo_nuevo = "entrega" if candidato["estado"] == "pendiente_entrega" else "recoleccion"
    no_arranco = ruta["estado"] == "planificada" and ruta["hora_inicio_real"] is None
    if no_arranco:
        # La ruta no ha salido: mete la parada nueva y reacomoda todo el recorrido por cercanía
        # real, para que quede en la posición que le toca en vez de siempre al final.
        cur = db.execute(
            "INSERT INTO paradas (ruta_id, solicitud_id, orden, tipo) VALUES (?, ?, ?, ?)",
            (ruta_id, candidato["id"], 0, tipo_nuevo),
        )
        nueva_parada_id = cur.lastrowid
        todas = [dict(p) for p in db.execute(
            "SELECT p.id AS parada_id, s.lat, s.lon FROM paradas p JOIN solicitudes s ON s.id = p.solicitud_id "
            "WHERE p.ruta_id = ? AND p.id != ?",
            (ruta_id, excluir_parada_id),
        ).fetchall()]
        todas_ordenadas = ordenar_por_cercania(todas)
        for i, p in enumerate(todas_ordenadas, start=1):
            db.execute("UPDATE paradas SET orden = ? WHERE id = ?", (i, p["parada_id"]))
        if excluir_parada_id != -1:
            # Si la parada ausente todavía existe (caso recolector), sácala de la numeración
            # activa (que empieza en 1) para que no comparta orden con ninguna pendiente real.
            db.execute("UPDATE paradas SET orden = 0 WHERE id = ?", (excluir_parada_id,))
    else:
        # La ruta ya va en curso: se agrega al final para no reordenar paradas ya resueltas.
        siguiente_orden = db.execute(
            "SELECT COALESCE(MAX(orden), 0) + 1 AS n FROM paradas WHERE ruta_id = ?", (ruta_id,)
        ).fetchone()["n"]
        cur = db.execute(
            "INSERT INTO paradas (ruta_id, solicitud_id, orden, tipo) VALUES (?, ?, ?, ?)",
            (ruta_id, candidato["id"], siguiente_orden, tipo_nuevo),
        )
        if ruta["estado"] != "en_curso":
            db.execute("UPDATE rutas SET estado = 'en_curso' WHERE id = ?", (ruta_id,))

    # Deja la zona del paciente agregado igual a la de la ruta real donde quedó su parada —así
    # la lista de pacientes y cualquier futuro "generar rutas" lo reconocen en su ruta real en
    # vez de la que tenía antes de agregarse aquí.
    db.execute(
        "UPDATE solicitudes SET estado = 'programada', zona = ? WHERE id = ?",
        (ruta["zona"], candidato["id"]),
    )
    db.commit()
    threading.Thread(
        target=_notificar_paradas_programadas, args=([cur.lastrowid],), daemon=True
    ).start()
    return candidato["direccion"]


@app.route("/recolector/paradas/<int:parada_id>/actualizar", methods=["POST"])
@login_required("recolector")
def recolector_actualizar_parada(user, parada_id):
    resultado = request.form.get("estado")
    parte = request.form.get("parte", "principal")
    if resultado not in ("completada", "incidencia", "ausente"):
        flash("Estado inválido.", "error")
        return redirect(url_for("recolector_dashboard"))

    db = get_db()
    parada = db.execute(
        "SELECT p.*, r.recolector_id, r.id AS ruta_id, s.cliente_id, "
        "s.tipo_redistribucion, s.cantidad_cajas, s.material AS material_cajas, "
        "s2.tipo_redistribucion AS tipo_redistribucion_extra, s2.cantidad_cajas AS cantidad_cajas_extra, "
        "s2.material AS material_cajas_extra "
        "FROM paradas p JOIN rutas r ON r.id = p.ruta_id JOIN solicitudes s ON s.id = p.solicitud_id "
        "LEFT JOIN solicitudes s2 ON s2.id = p.solicitud_extra_id WHERE p.id = ?",
        (parada_id,),
    ).fetchone()
    if parada is None or parada["recolector_id"] != user["id"]:
        flash("No puedes editar esa parada.", "error")
        return redirect(url_for("recolector_dashboard"))

    resolviendo_extra = parte == "extra" and parada["solicitud_extra_id"] is not None
    campo_estado = "estado_extra" if resolviendo_extra else "estado"
    if parada[campo_estado] != "pendiente":
        flash("Esa parte ya quedó registrada y no se puede cambiar.", "error")
        return redirect(url_for("recolector_ver_ruta", ruta_id=parada["ruta_id"]))

    notas = request.form.get("notas", "").strip()
    if notas:
        db.execute("UPDATE paradas SET notas = ? WHERE id = ?", (notas, parada_id))

    if "kg" in request.form:
        kg_raw = request.form.get("kg", "").strip()
        kg_final = 0.0
        if resultado == "completada" and kg_raw:
            try:
                kg_final = max(0.0, float(kg_raw))
            except ValueError:
                kg_final = 0.0
        kg_anterior = parada["kg_recolectados"] or 0.0
        db.execute("UPDATE paradas SET kg_recolectados = ? WHERE id = ?", (kg_final, parada_id))
        delta_kg = kg_final - kg_anterior
        if parada["cliente_id"] and delta_kg:
            db.execute(
                "UPDATE users SET material_recolectado_kg = material_recolectado_kg + ? WHERE id = ?",
                (delta_kg, parada["cliente_id"]),
            )

    if "cajas" in request.form:
        cajas_raw = request.form.get("cajas", "").strip()
        cajas_final = None
        if resultado == "completada" and cajas_raw:
            try:
                cajas_final = max(0, int(cajas_raw))
            except ValueError:
                cajas_final = None
        campo_cajas = "cajas_reales_extra" if resolviendo_extra else "cajas_reales"
        db.execute(f"UPDATE paradas SET {campo_cajas} = ? WHERE id = ?", (cajas_final, parada_id))

    if resolviendo_extra:
        _resolver_resultado_parte(
            db, parada["solicitud_extra_id"], parada["tipo_extra"], parada["tipo_redistribucion_extra"],
            parada["material_cajas_extra"], parada["cantidad_cajas_extra"], resultado, parada_id, " (extra)",
        )
    else:
        _resolver_resultado_parte(
            db, parada["solicitud_id"], parada["tipo"], parada["tipo_redistribucion"],
            parada["material_cajas"], parada["cantidad_cajas"], resultado, parada_id, "",
        )

    db.execute(f"UPDATE paradas SET {campo_estado} = ? WHERE id = ?", (resultado, parada_id))

    paradas_pendientes = db.execute(
        "SELECT COUNT(*) c FROM paradas WHERE ruta_id = ? AND ("
        "  estado = 'pendiente' OR (solicitud_extra_id IS NOT NULL AND estado_extra = 'pendiente')"
        ")",
        (parada["ruta_id"],),
    ).fetchone()["c"]
    nuevo_estado_ruta = "completada" if paradas_pendientes == 0 else "en_curso"
    db.execute("UPDATE rutas SET estado = ? WHERE id = ?", (nuevo_estado_ruta, parada["ruta_id"]))

    db.commit()
    parada_ya_resuelta = db.execute(
        "SELECT (estado != 'pendiente') AND (solicitud_extra_id IS NULL OR estado_extra != 'pendiente') AS r "
        "FROM paradas WHERE id = ?",
        (parada_id,),
    ).fetchone()["r"]
    if parada_ya_resuelta and paradas_pendientes > 0:
        threading.Thread(
            target=_notificar_siguiente_parada, args=(parada["ruta_id"],), daemon=True
        ).start()
    if resultado == "ausente" and not resolviendo_extra:
        sol_ausente = db.execute(
            "SELECT lat, lon FROM solicitudes WHERE id = ?", (parada["solicitud_id"],)
        ).fetchone()
        direccion_agregada = intentar_llenar_hueco_ausente(
            db, parada["ruta_id"], sol_ausente["lat"], sol_ausente["lon"],
            excluir_parada_id=parada_id,
        )
        if direccion_agregada:
            flash(f"Como sobraba tiempo en la ruta, se agregó una parada cercana: {direccion_agregada}.", "success")
    flash("Segunda solicitud de esta parada actualizada." if resolviendo_extra else "Parada actualizada.", "success")
    return redirect(url_for("recolector_ver_ruta", ruta_id=parada["ruta_id"]))


@app.route("/recolector/paradas/<int:parada_id>/bote-devuelto", methods=["POST"])
@login_required("recolector")
def recolector_bote_devuelto(user, parada_id):
    db = get_db()
    parada = db.execute(
        "SELECT p.*, r.recolector_id, s.cliente_id, s.direccion, s.nombre_contacto FROM paradas p "
        "JOIN rutas r ON r.id = p.ruta_id JOIN solicitudes s ON s.id = p.solicitud_id WHERE p.id = ?",
        (parada_id,),
    ).fetchone()
    if parada is None or parada["recolector_id"] != user["id"]:
        flash("No puedes editar esa parada.", "error")
        return redirect(url_for("recolector_dashboard"))

    registrar_movimiento_botes(db, "devolucion", 1, f"Recibido en parada #{parada['id']}")

    ruta_id = parada["ruta_id"]
    cliente_id = parada["cliente_id"]
    if cliente_id:
        u = db.execute("SELECT name FROM users WHERE id = ?", (cliente_id,)).fetchone()
        nombre = u["name"] if u else (parada["nombre_contacto"] or parada["direccion"])
        otras_solicitudes = db.execute(
            "SELECT id FROM solicitudes WHERE cliente_id = ?", (cliente_id,)
        ).fetchall()
        for s in otras_solicitudes:
            db.execute("DELETE FROM paradas WHERE solicitud_id = ?", (s["id"],))
            db.execute(
                "UPDATE paradas SET solicitud_extra_id = NULL, tipo_extra = NULL WHERE solicitud_extra_id = ?",
                (s["id"],),
            )
        db.execute("DELETE FROM nef_confirmaciones WHERE cliente_id = ?", (cliente_id,))
        db.execute("DELETE FROM solicitudes WHERE cliente_id = ?", (cliente_id,))
        db.execute("DELETE FROM notificaciones_admin WHERE cliente_id = ?", (cliente_id,))
        db.execute("DELETE FROM users WHERE id = ?", (cliente_id,))
    else:
        nombre = parada["nombre_contacto"] or parada["direccion"]
        db.execute("DELETE FROM paradas WHERE solicitud_id = ?", (parada["solicitud_id"],))
        db.execute(
            "UPDATE paradas SET solicitud_extra_id = NULL, tipo_extra = NULL WHERE solicitud_extra_id = ?",
            (parada["solicitud_id"],),
        )
        db.execute("DELETE FROM solicitudes WHERE id = ?", (parada["solicitud_id"],))
    promover_lista_espera(db)
    db.commit()
    flash(f"Bote recibido. '{nombre}' se eliminó permanentemente del sistema.", "success")
    return redirect(url_for("recolector_ver_ruta", ruta_id=ruta_id))


# ---------- NEF ----------

@app.route("/nef")
@login_required("nef")
def nef_dashboard(user):
    db = get_db()
    publicaciones_rows = db.execute(
        "SELECT * FROM nef_publicaciones ORDER BY created_at DESC"
    ).fetchall()
    publicaciones = []
    for p in publicaciones_rows:
        pub = dict(p)
        pub["confirmaciones"] = db.execute(
            "SELECT COUNT(*) AS n FROM nef_confirmaciones WHERE publicacion_id = ?", (p["id"],)
        ).fetchone()["n"]
        publicaciones.append(pub)
    return render_template("nef_dashboard.html", publicaciones=publicaciones)


@app.route("/nef/publicaciones/nueva", methods=["POST"])
@login_required("nef")
def nef_nueva_publicacion(user):
    tipo = request.form.get("tipo")
    if tipo not in ("informacion", "video", "evento", "webinar"):
        flash("Tipo de publicación inválido.", "error")
        return redirect(url_for("nef_dashboard"))
    titulo = request.form.get("titulo", "").strip()
    contenido = request.form.get("contenido", "").strip() or None
    fecha_evento = request.form.get("fecha_evento") or None
    hora_evento = request.form.get("hora_evento") or None
    lugar_evento = request.form.get("lugar_evento", "").strip() or None
    link_webinar = request.form.get("link_webinar", "").strip() or None
    lat = request.form.get("lat", "").strip()
    lon = request.form.get("lon", "").strip()
    try:
        lat = float(lat) if lat else None
        lon = float(lon) if lon else None
    except ValueError:
        lat = lon = None
    if not titulo:
        flash("Ponle un título a la publicación.", "error")
        return redirect(url_for("nef_dashboard", tab=tipo))
    if tipo == "webinar" and not link_webinar:
        flash("Pon la liga del webinar.", "error")
        return redirect(url_for("nef_dashboard", tab=tipo))

    video_archivo = None
    if tipo == "video":
        archivo = request.files.get("video_archivo")
        if not archivo or not archivo.filename:
            flash("Sube un video.", "error")
            return redirect(url_for("nef_dashboard", tab=tipo))
        extension = archivo.filename.rsplit(".", 1)[-1].lower() if "." in archivo.filename else ""
        if extension not in NEF_VIDEO_EXTENSIONES:
            flash("Formato de video no soportado. Usa mp4, mov, webm o m4v.", "error")
            return redirect(url_for("nef_dashboard", tab=tipo))
        os.makedirs(NEF_VIDEOS_DIR, exist_ok=True)
        video_archivo = f"{secrets.token_hex(8)}_{secure_filename(archivo.filename)}"
        archivo.save(os.path.join(NEF_VIDEOS_DIR, video_archivo))

    db = get_db()
    cur = db.execute(
        "INSERT INTO nef_publicaciones (tipo, titulo, contenido, fecha_evento, hora_evento, "
        "lugar_evento, lat, lon, link_webinar, video_archivo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tipo, titulo, contenido, fecha_evento, hora_evento, lugar_evento, lat, lon, link_webinar, video_archivo),
    )
    db.commit()
    flash("Publicación creada. Los pacientes suscritos la verán en su sesión, en la pestaña NEF.", "success")
    return redirect(url_for("nef_dashboard", tab=tipo))


@app.route("/nef/publicaciones/<int:publicacion_id>/eliminar", methods=["POST"])
@login_required("nef")
def nef_eliminar_publicacion(user, publicacion_id):
    db = get_db()
    pub = db.execute("SELECT * FROM nef_publicaciones WHERE id = ?", (publicacion_id,)).fetchone()
    if pub and pub["video_archivo"]:
        ruta_archivo = os.path.join(NEF_VIDEOS_DIR, pub["video_archivo"])
        if os.path.exists(ruta_archivo):
            os.remove(ruta_archivo)
    db.execute("DELETE FROM nef_confirmaciones WHERE publicacion_id = ?", (publicacion_id,))
    db.execute("DELETE FROM nef_publicaciones WHERE id = ?", (publicacion_id,))
    db.commit()
    flash("Publicación eliminada.", "success")
    tab = pub["tipo"] if pub else None
    return redirect(url_for("nef_dashboard", tab=tab))


@app.route("/nef/geocodificar")
@login_required("nef")
def nef_geocodificar(user):
    direccion = request.args.get("direccion", "").strip()
    codigo_postal = request.args.get("cp", "").strip()
    if not direccion:
        return jsonify({"error": "Escribe una dirección."}), 400
    resultados = geocodificar_direccion(direccion, codigo_postal=codigo_postal or None)
    if not resultados:
        return jsonify({"error": "No se encontró esa dirección."}), 404
    return jsonify({"resultados": resultados})


@app.route("/cliente/nef/<int:publicacion_id>/confirmar", methods=["POST"])
@login_required("cliente")
def cliente_confirmar_asistencia(user, publicacion_id):
    if not user["recibir_info_nef"]:
        flash("No tienes acceso a esa sección.", "error")
        return redirect(url_for("cliente_dashboard"))
    db = get_db()
    pub = db.execute(
        "SELECT * FROM nef_publicaciones WHERE id = ? AND tipo = 'evento'", (publicacion_id,)
    ).fetchone()
    if pub is None:
        flash("Ese evento ya no existe.", "error")
        return redirect(url_for("cliente_dashboard", tab="nef"))
    db.execute(
        "INSERT OR IGNORE INTO nef_confirmaciones (publicacion_id, cliente_id) VALUES (?, ?)",
        (publicacion_id, user["id"]),
    )
    db.commit()
    flash(f"Confirmaste tu asistencia a '{pub['titulo']}'.", "success")
    return redirect(url_for("cliente_dashboard", tab="nef"))


@app.route("/webhook/whatsapp", methods=["POST"])
def webhook_whatsapp():
    """Recibe los mensajes entrantes de WhatsApp que reenvía Twilio (sandbox o número real).
    Valida primero que la petición realmente venga de Twilio (firma X-Twilio-Signature) — si no,
    la rechaza sin procesarla, para que nadie pueda mandarle mensajes falsos a este endpoint
    público haciéndose pasar por WhatsApp. Hoy no hay ningún flujo que dependa de leer lo que
    contesta el paciente (la confirmación de cuenta y de contraseña se hacen dando clic en un
    enlace, no respondiendo texto) — así que solo registra el mensaje en consola y contesta un
    acuse genérico. Twilio espera una respuesta en TwiML (XML)."""
    firma_recibida = request.headers.get("X-Twilio-Signature", "")
    if not validar_firma_twilio(_url_publica_actual(), request.form.to_dict(), firma_recibida):
        return ("Firma inválida.", 403)

    remitente = request.form.get("From", "")
    cuerpo = request.form.get("Body", "")
    print(f"[webhook_whatsapp] Mensaje de {remitente}: {cuerpo!r}")

    respuesta_texto = "Gracias por tu mensaje. Por ahora este número no atiende respuestas — para dudas, contáctanos directamente."
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{respuesta_texto}</Message></Response>"
    )
    return app.response_class(twiml, mimetype="text/xml")


init_db()
threading.Thread(target=_hilo_avisos_programados, daemon=True).start()

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=debug_mode, threaded=True)

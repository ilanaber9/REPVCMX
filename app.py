import json
import os
import re
import secrets
import smtplib
import sqlite3
import subprocess
import threading
from datetime import date, datetime, timedelta
from datetime import time as dtime
from email.mime.text import MIMEText
from functools import wraps
from math import atan2, cos, radians, sin, sqrt
import urllib.request
from urllib.parse import urlencode

from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for, flash
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "database.db")
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
ENV_PATH = os.path.join(BASE_DIR, ".env")

if os.path.exists(ENV_PATH):
    with open(ENV_PATH) as f:
        for linea in f:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                clave, valor = linea.split("=", 1)
                os.environ.setdefault(clave.strip(), valor.strip())

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
    "Máquina Baxter", "Máquina Pisa", "Manual Baxter verde",
    "Manual Baxter amarilla", "Manual Pisa verde", "Manual Pisa amarilla",
]
PERSONAS_PRODUCTIVIDAD = ["Gabriela", "Paola", "Monserrat"]
ACTIVIDADES_PRODUCTIVIDAD = ["moler", "cortar", "secar", "envasar"]
ACTIVIDADES_PRODUCTIVIDAD_LABELS = {
    "moler": "Moler", "cortar": "Cortar", "secar": "Secar", "envasar": "Envasar",
}
ZONA_BOOTSTRAP_DEFAULT = "Zona 1"
DIAS_ESPERA_PRIMERA_RECOLECCION = 30  # tras entregar el bote, cuántos días esperar antes de que
# el paciente aparezca listo para programar su primera recolección real de material.
PERSONAS_VACACIONES = ["Lety", "Martin", "Gaby", "Paola", "Monserrat"]
DIAS_VACACIONES_DEFAULT = 12
DURACION_MAXIMA_RUTA_MIN = 7 * 60 + 30  # 7:30 hrs por ruta antes de dividirla en otra
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


def dividir_puntos_por_duracion(puntos, minutos_max=DURACION_MAXIMA_RUTA_MIN):
    """Agrupa puntos (en el orden dado) en tandas cuya duración estimada de ida y vuelta
    al depósito no exceda minutos_max. Si un solo punto ya la excede, queda solo en su tanda."""
    grupos = []
    grupo_actual = []
    for p in puntos:
        candidato = grupo_actual + [p]
        estimado = estimar_ruta(candidato)
        if estimado and estimado["minutos"] > minutos_max and grupo_actual:
            grupos.append(grupo_actual)
            grupo_actual = [p]
        else:
            grupo_actual = candidato
    if grupo_actual:
        grupos.append(grupo_actual)
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


def siguiente_numero_ruta(db):
    """Siguiente número consecutivo libre para nombrar una ruta como 'Ruta NN (...)', tomando
    el máximo usado tanto en zonas importadas como en nombres de rutas ya creadas."""
    maximo = 0
    filas = db.execute(
        "SELECT zona AS nombre FROM solicitudes WHERE zona IS NOT NULL UNION SELECT nombre FROM rutas"
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


def _notificar_paradas_programadas(parada_ids, host_url):
    """Manda a cada paciente un correo con la información de su recolección recién programada
    (fecha, horario estimado, recolector) y dos ligas para confirmar si podrá recibir la
    recolección ese día o no. Corre en un hilo aparte con su propia conexión a la base de
    datos, así que no bloquea la respuesta de quien creó la ruta."""
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
                "SELECT name, email FROM users WHERE id = ?", (parada["cliente_id"],)
            ).fetchone()
            if not paciente or not paciente["email"]:
                continue

            token = secrets.token_urlsafe(24)
            conn.execute("UPDATE paradas SET confirmacion_token = ? WHERE id = ?", (token, parada_id))
            conn.commit()

            horario = horario_estimado_parada(conn, parada_id)
            base = host_url.rstrip("/")
            link_si = f"{base}/parada/{token}/confirmar?respuesta=si"
            link_no = f"{base}/parada/{token}/confirmar?respuesta=no"

            cuerpo = (
                f"Hola {paciente['name']},\n\n"
                f"Ya programamos tu recolección para el {parada['fecha']}"
                + (f", entre {horario}" if horario else "")
                + ".\n"
                f"Recolector: {parada['recolector_nombre'] or 'por asignar'}\n"
                f"Dirección: {parada['direccion']}\n\n"
                "¿Vas a poder recibir la recolección ese día?\n\n"
                f"Sí puedo: {link_si}\n"
                f"No puedo: {link_no}\n"
            )
            enviar_email(paciente["email"], "Tu recolección ya está programada — RE-PVC", cuerpo)
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
            "SELECT name, email FROM users WHERE id = ?", (sol["cliente_id"],)
        ).fetchone()
        if not paciente or not paciente["email"]:
            return

        horario = horario_estimado_siguiente(conn, siguiente["id"])
        cuerpo = (
            f"Hola {paciente['name']},\n\n"
            "Prepárate, el recolector se encuentra en camino a tu domicilio"
            + (f", entre {horario}" if horario else "") + ".\n\n"
            f"Dirección registrada: {sol['direccion']}\n\n"
            "Ten tu material listo para cuando llegue."
        )
        enviar_email(paciente["email"], "¡Eres el siguiente! — RE-PVC", cuerpo)
    finally:
        conn.close()


def normalizar_telefono(raw):
    """Toma solo los dígitos del número que escribió el paciente y le antepone +52.
    Devuelve None si no escribió nada."""
    digitos = re.sub(r"\D", "", raw or "")
    if not digitos:
        return None
    return f"+52 {digitos}"


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
        return False
    msg = MIMEText(cuerpo)
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = destinatario
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(remitente, clave)
            server.sendmail(remitente, [destinatario], msg.as_string())
        return True
    except Exception:
        return False


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


def zona_mas_cercana(db, lat, lon):
    """Busca, entre los puntos que ya tienen zona asignada, cuál está más cerca de (lat, lon)
    y devuelve (zona, distancia_km), o None si no hay ningún punto con zona y coordenadas."""
    filas = db.execute(
        "SELECT zona, lat, lon FROM solicitudes WHERE zona IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL"
    ).fetchall()
    mejor = None
    for f in filas:
        d = haversine_km(lat, lon, f["lat"], f["lon"])
        if mejor is None or d < mejor[1]:
            mejor = (f["zona"], d)
    return mejor


LIMITE_MINUTOS_COBERTURA = 20


def fuera_de_cobertura(db, lat, lon):
    """True si no hay ningún punto ya cubierto (con zona asignada) a menos de
    LIMITE_MINUTOS_COBERTURA minutos de manejo real desde (lat, lon). Si todavía no hay ningún
    punto con zona en el sistema (arranque en frío, p. ej. justo después de vaciar la base de
    datos), se compara contra el depósito en su lugar — así la primera zona que se cree de forma
    automática sigue respetando un radio real de cobertura, en vez de aceptar cualquier lugar."""
    filas = db.execute(
        "SELECT lat, lon FROM solicitudes WHERE zona IS NOT NULL AND lat IS NOT NULL AND lon IS NOT NULL"
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
            target=_notificar_paradas_programadas, args=(parada_ids_nuevas, request.host_url), daemon=True
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
    correo_paciente = None
    if siguiente["cliente_id"]:
        u = db.execute("SELECT name, email FROM users WHERE id = ?", (siguiente["cliente_id"],)).fetchone()
        if u:
            nombre = u["name"]
            correo_paciente = u["email"]
    mensaje = f"'{nombre}' salió de la lista de espera y ya quedó activo"
    mensaje += f", asignado a {zona}." if zona else "."
    crear_notificacion_admin(db, siguiente["cliente_id"], mensaje)

    if correo_paciente:
        enviar_email(
            correo_paciente, "Ya te integramos a una ruta — RE-PVC",
            f"Hola {nombre},\n\n"
            "¡Buenas noticias! Ya se liberó un lugar y saliste de la lista de espera: "
            + (f"quedaste integrado a {zona}." if zona else "en breve te asignaremos una ruta.")
            + "\nTe avisaremos con la fecha y el horario aproximado en cuanto tu recolección quede programada.",
        )


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
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
            "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, ?)",
            ("Administrador", "admin@rutas.local", generate_password_hash("admin123", method="pbkdf2:sha256"), "admin"),
        )
        if os.path.exists(SEED_SOLICITUDES_PATH):
            with open(SEED_SOLICITUDES_PATH, encoding="utf-8") as f:
                puntos = json.load(f)
            for p in puntos:
                db.execute(
                    "INSERT INTO solicitudes (nombre_contacto, direccion, material, lat, lon, zona, estado) "
                    "VALUES (?, ?, ?, ?, ?, ?, 'pendiente')",
                    (p["nombre_contacto"], p["direccion"], p["material"], p["lat"], p["lon"], p["zona"]),
                )
            print(f"Se cargaron {len(puntos)} puntos de recolección desde seed_solicitudes.json.")
        db.commit()
        print("Base de datos creada. Login admin -> admin@rutas.local / admin123")
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
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        tipo = request.form.get("tipo") or None
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user is None:
            flash("Este correo no está registrado.", "error")
            return render_template("login.html", tipo=tipo, tipo_label=TIPO_LOGIN_LABELS.get(tipo))
        if not check_password_hash(user["password_hash"], password):
            flash("Correo o contraseña incorrectos.", "error")
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
        email = request.form.get("email", "").strip().lower()
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if user:
            token = secrets.token_urlsafe(32)
            expira = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            db.execute(
                "UPDATE users SET reset_token = ?, reset_token_expira = ? WHERE id = ?",
                (token, expira, user["id"]),
            )
            db.commit()
            link = url_for("restablecer_password", token=token, _external=True)
            enviar_email(
                email,
                "Recuperar contraseña — RE-PVC",
                f"Hola {user['name']},\n\n"
                "Recibimos una solicitud para restablecer tu contraseña en RE-PVC.\n"
                f"Entra a este enlace para poner una nueva (válido por 1 hora):\n{link}\n\n"
                "Si tú no pediste esto, ignora este correo.",
            )
        flash("Si ese correo está registrado, te enviamos un enlace para restablecer tu contraseña.", "success")
        return redirect(url_for("login"))
    return render_template("olvide_password.html")


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
    """Cuando el paciente confirma que NO va a poder recibir la recolección/entrega
    programada, saca esa parada de la lista pendiente del recolector para ese día (queda
    'ausente', igual que si el recolector hubiera tocado y no hubiera nadie) y regresa la(s)
    solicitud(es) a pendiente para que se puedan reprogramar en otra ruta. No toca kg ni
    movimientos de inventario porque no se llegó a recoger/entregar nada."""
    parada = db.execute("SELECT * FROM paradas WHERE id = ?", (parada_id,)).fetchone()
    if parada is None or parada["estado"] != "pendiente":
        return
    db.execute("UPDATE paradas SET estado = 'ausente' WHERE id = ?", (parada_id,))
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


@app.route("/parada/<token>/confirmar")
def parada_confirmar(token):
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
    db.execute("UPDATE paradas SET confirmado_paciente = ? WHERE id = ?", (respuesta, parada["id"]))
    if respuesta == "no":
        marcar_parada_ausente_por_rechazo(db, parada["id"])
    db.commit()
    return render_template("parada_confirmar.html", valido=True, respuesta=respuesta, parada=parada)


@app.route("/solicitud/<token>/existencia")
def solicitud_confirmar_existencia(token):
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
        marcar_parada_ausente_por_rechazo(db, parada_id)
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
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            flash("Ese correo ya está registrado.", "error")
            return render_template("registro.html")
        token = secrets.token_urlsafe(32)
        db.execute(
            "INSERT INTO users (name, email, password_hash, role, email_verificado, verificacion_token) "
            "VALUES (?, ?, ?, 'cliente', 0, ?)",
            (name, email, generate_password_hash(password, method="pbkdf2:sha256"), token),
        )
        db.commit()
        link = url_for("verificar_correo", token=token, _external=True)
        enviar_email(
            email, "Verifica tu correo — RE-PVC",
            f"Hola {name},\n\nGracias por registrarte en RE-PVC. Confirma tu correo entrando a este enlace:\n{link}\n\n"
            "Si tú no creaste esta cuenta, ignora este correo.",
        )
        flash("Cuenta creada. Revisa tu correo para verificarla antes de continuar.", "success")
        return redirect(url_for("login"))
    return render_template("registro.html")


@app.route("/verificar-correo/<token>")
def verificar_correo(token):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE verificacion_token = ?", (token,)).fetchone()
    if user is None:
        flash("Ese enlace de verificación ya no es válido.", "error")
        return redirect(url_for("login"))
    db.execute(
        "UPDATE users SET email_verificado = 1, verificacion_token = NULL WHERE id = ?", (user["id"],)
    )
    db.commit()
    flash("¡Correo verificado! Ya puedes continuar.", "success")
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
        link = url_for("verificar_correo", token=token, _external=True)
        enviar_email(
            user["email"], "Verifica tu correo — RE-PVC",
            f"Hola {user['name']},\n\nConfirma tu correo entrando a este enlace:\n{link}",
        )
        flash("Te reenviamos el correo de verificación.", "success")
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
        telefono = normalizar_telefono(request.form.get("telefono", ""))
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
            "INSERT INTO solicitudes (cliente_id, direccion, codigo_postal, telefono, material, lat, lon, zona, "
            "estado, fuera_cobertura) VALUES (?, ?, ?, ?, 'PVC', ?, ?, ?, ?, ?)",
            (user["id"], direccion, codigo_postal, telefono, lat, lon, zona, estado_inicial,
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
            enviar_email(
                user["email"], "Quedaste en lista de espera — RE-PVC",
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
            enviar_email(
                user["email"], "Registro recibido — RE-PVC",
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
    if user["recibir_info_nef"]:
        nef_publicaciones = db.execute(
            "SELECT * FROM nef_publicaciones ORDER BY created_at DESC"
        ).fetchall()
        nef_confirmados = {
            r["publicacion_id"] for r in db.execute(
                "SELECT publicacion_id FROM nef_confirmaciones WHERE cliente_id = ?", (user["id"],)
            ).fetchall()
        }
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

    return render_template(
        "cliente_dashboard.html", solicitudes=solicitudes,
        nef_publicaciones=nef_publicaciones, nef_confirmados=nef_confirmados,
        admin_videos=admin_videos,
        cajas_donadas=cajas_donadas, cajas_recibidas=cajas_recibidas,
        cajas_donadas_total=cajas_donadas_total, cajas_recibidas_total=cajas_recibidas_total,
    )


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
            enviar_email(
                user["email"], "Ya tenemos existencia — RE-PVC",
                f"Hola {user['name']},\n\nYa tenemos existencia de {cajas_texto} de {material}. "
                "Ya puedes pasar a recolectarlas a:\n"
                "Filiberto Gómez 279, Tlaxcopan, Tlalnepantla de Baz, Estado de México, CP 54030",
            )
        else:
            crear_notificacion_admin(
                db, user["id"],
                f"'{user['name']}' quiere pasar a recoger {cajas_texto} de {material} en RE-PVC "
                "— por ahora no hay existencia suficiente.",
            )
            enviar_email(
                user["email"], "Recibimos tu solicitud — RE-PVC",
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
            enviar_email(
                user["email"], "Ya tenemos existencia — RE-PVC",
                f"Hola {user['name']},\n\nYa tenemos existencia de {cajas_texto} de {material}. "
                "Tu entrega quedó programada, te avisaremos la fecha y el horario aproximado.",
            )
        else:
            crear_notificacion_admin(
                db, user["id"],
                f"'{user['name']}' solicita recibir {cajas_texto} de {material} — por ahora no hay "
                "existencia suficiente.",
            )
            enviar_email(
                user["email"], "Recibimos tu solicitud — RE-PVC",
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


# ---------- Admin ----------

@app.route("/admin")
@login_required("admin")
def admin_dashboard(user):
    db = get_db()
    solicitudes_clientes = db.execute(
        "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre FROM solicitudes s "
        "LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.estado = 'pendiente' AND s.cliente_id IS NOT NULL AND ("
        "  s.fecha_reinicio_espera IS NULL"
        f"  OR datetime(s.fecha_reinicio_espera, '+{DIAS_ESPERA_PRIMERA_RECOLECCION} days') <= datetime('now', 'localtime')"
        ") ORDER BY s.created_at"
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

    recolecciones_en_sitio = db.execute(
        "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre FROM solicitudes s "
        "LEFT JOIN users u ON u.id = s.cliente_id "
        "WHERE s.recoger_en_sitio = 1 AND s.confirmado_existencia = 0 ORDER BY s.created_at"
    ).fetchall()

    zonas = [
        row["zona"] for row in db.execute(
            "SELECT DISTINCT zona FROM solicitudes "
            "WHERE estado IN ('pendiente', 'pendiente_entrega') AND zona IS NOT NULL AND ("
            "  fecha_reinicio_espera IS NULL"
            f"  OR datetime(fecha_reinicio_espera, '+{DIAS_ESPERA_PRIMERA_RECOLECCION} days') <= datetime('now', 'localtime')"
            ") ORDER BY zona"
        ).fetchall()
    ]
    zona_actual = request.args.get("zona") or (zonas[0] if zonas else None)
    puntos_zona = []
    if zona_actual:
        puntos_zona = db.execute(
            "SELECT s.*, COALESCE(u.name, s.nombre_contacto) AS cliente_nombre FROM solicitudes s "
            "LEFT JOIN users u ON u.id = s.cliente_id "
            "WHERE s.estado IN ('pendiente', 'pendiente_entrega') AND s.zona = ? AND ("
            "  s.fecha_reinicio_espera IS NULL"
            f"  OR datetime(s.fecha_reinicio_espera, '+{DIAS_ESPERA_PRIMERA_RECOLECCION} days') <= datetime('now', 'localtime')"
            ") ORDER BY s.id",
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
    hoy = date.today().isoformat()
    rutas_activas = [r for r in rutas if r["estado"] != "completada"]
    rutas_finalizadas = [r for r in rutas if r["estado"] == "completada"]
    rutas_hoy = [r for r in rutas_activas if r["fecha"] == hoy]

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
        paciente["telefono_actual"] = ruta_sol["telefono"] if ruta_sol else None
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

    return render_template(
        "admin_dashboard.html",
        pendientes=solicitudes_clientes,
        pendientes_entrega=pendientes_entrega,
        recolecciones_en_sitio=recolecciones_en_sitio,
        zonas=zonas,
        zona_actual=zona_actual,
        puntos_zona=puntos_zona,
        rutas=rutas_activas,
        rutas_finalizadas=rutas_finalizadas,
        rutas_hoy=rutas_hoy,
        hoy=hoy,
        recolectores=recolectores,
        cuentas_nef=cuentas_nef,
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


@app.route("/admin/pacientes/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_paciente(user):
    nombre = request.form["nombre"].strip()
    direccion = request.form["direccion"].strip()
    codigo_postal = request.form.get("codigo_postal", "").strip() or None
    telefono = normalizar_telefono(request.form.get("telefono", ""))

    edad = request.form.get("edad", "").strip()
    tipo_maquina = request.form.get("tipo_maquina")
    marca = request.form.get("marca")
    frecuencia_semana = request.form.get("frecuencia_semana", "").strip()
    causa_enfermedad = request.form.get("causa_enfermedad")

    if tipo_maquina not in ("maquina", "manual") or marca not in ("baxter", "pisa"):
        flash("Selecciona el tipo y la marca.", "error")
        return redirect(url_for("admin_dashboard", tab="paciente"))
    if causa_enfermedad not in ("diabetes", "hipertension", "autoinmune", "desconocida"):
        flash("Selecciona la causa de la enfermedad renal.", "error")
        return redirect(url_for("admin_dashboard", tab="paciente"))
    try:
        edad = int(edad)
        frecuencia_semana = int(frecuencia_semana)
    except ValueError:
        flash("Edad y frecuencia deben ser números.", "error")
        return redirect(url_for("admin_dashboard", tab="paciente"))

    lat = request.form.get("lat", "").strip()
    lon = request.form.get("lon", "").strip()
    try:
        lat = float(lat) if lat else None
        lon = float(lon) if lon else None
    except ValueError:
        lat = lon = None

    db = get_db()

    if lat is not None and lon is not None and direccion_ya_registrada(db, lat, lon):
        flash("Esa dirección ya está registrada con otro paciente.", "error")
        return redirect(url_for("admin_dashboard", tab="paciente"))

    en_espera = contar_pacientes_activos(db) >= MAX_PACIENTES_ACTIVOS
    zona = None
    if not en_espera and lat is not None and lon is not None:
        cercana = zona_mas_cercana(db, lat, lon)
        zona = cercana[0] if cercana else ZONA_BOOTSTRAP_DEFAULT

    estado_inicial = "lista_espera" if en_espera else "pendiente_entrega"
    cur = db.execute(
        "INSERT INTO solicitudes (cliente_id, nombre_contacto, direccion, codigo_postal, telefono, "
        "edad, tipo_maquina, marca, frecuencia_semana, causa_enfermedad, material, lat, lon, zona, estado) "
        "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PVC', ?, ?, ?, ?)",
        (nombre, direccion, codigo_postal, telefono, edad, tipo_maquina, marca, frecuencia_semana,
         causa_enfermedad, lat, lon, zona, estado_inicial),
    )
    if zona:
        reequilibrar_rutas_zona(db, zona, cur.lastrowid)
        zona = db.execute("SELECT zona FROM solicitudes WHERE id = ?", (cur.lastrowid,)).fetchone()["zona"]
    db.commit()
    if en_espera:
        flash(
            f"Paciente '{nombre}' agregado, pero llegamos al cupo máximo de "
            f"{MAX_PACIENTES_ACTIVOS} pacientes activos: quedó en lista de espera.",
            "success",
        )
    elif zona:
        flash(f"Paciente '{nombre}' agregado a {zona}, pendiente de entrega de bote.", "success")
    else:
        flash(
            f"Paciente '{nombre}' agregado, pendiente de entrega de bote. "
            "No se pudo asignar a una ruta automáticamente (sin coordenadas cercanas a ninguna zona).",
            "success",
        )
    return redirect(url_for("admin_dashboard", tab="solicitudes"))


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


@app.route("/admin/solicitudes/<int:solicitud_id>/confirmar-existencia", methods=["POST"])
@login_required("admin")
def admin_confirmar_existencia(user, solicitud_id):
    db = get_db()
    sol = db.execute(
        "SELECT * FROM solicitudes WHERE id = ? AND recoger_en_sitio = 1", (solicitud_id,)
    ).fetchone()
    if sol is None:
        flash("Esa solicitud ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="solicitudes"))

    nombre = sol["nombre_contacto"]
    correo_paciente = None
    if sol["cliente_id"]:
        u = db.execute("SELECT name, email FROM users WHERE id = ?", (sol["cliente_id"],)).fetchone()
        if u:
            nombre = u["name"]
            correo_paciente = u["email"]

    db.execute("UPDATE solicitudes SET confirmado_existencia = 1 WHERE id = ?", (solicitud_id,))
    if sol["cantidad_cajas"]:
        registrar_movimiento_cajas(
            db, sol["material"], "entrega", -sol["cantidad_cajas"],
            f"Recolección en sitio — solicitud #{solicitud_id}",
        )
    db.commit()

    if correo_paciente:
        cantidad_texto = f" ({sol['cantidad_cajas']} cajas)" if sol["cantidad_cajas"] else ""
        enviar_email(
            correo_paciente, "Ya puedes pasar por tus cajas — RE-PVC",
            f"Hola {nombre},\n\n"
            f"Ya confirmamos que tenemos existencia de {sol['material']}{cantidad_texto}. "
            "Ya puedes pasar a recolectarlas a:\n"
            "Filiberto Gómez 279, Tlaxcopan, Tlalnepantla de Baz, Estado de México, CP 54030\n\n"
            "Te esperamos.",
        )
    flash(f"Existencia confirmada para '{nombre}'. Se le avisó que ya puede pasar a recolectar.", "success")
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
        "WHERE s.zona = ? AND s.estado IN ('pendiente', 'pendiente_entrega') AND ("
        "  s.fecha_reinicio_espera IS NULL"
        f"  OR datetime(s.fecha_reinicio_espera, '+{DIAS_ESPERA_PRIMERA_RECOLECCION} days') <= datetime('now', 'localtime')"
        ") ORDER BY s.id",
        (zona,),
    ).fetchall()
    puntos_json = json.dumps([dict(p) for p in puntos])
    estimado = estimar_ruta(puntos)
    return render_template(
        "admin_zona_mapa.html", zona=zona, puntos=puntos, puntos_json=puntos_json, estimado=estimado
    )


@app.route("/admin/rutas/nueva", methods=["POST"])
@login_required("admin")
def admin_nueva_ruta(user):
    nombre = request.form["nombre"].strip()
    fecha = request.form.get("fecha") or date.today().isoformat()
    hora_salida = request.form.get("hora_salida") or "08:00"
    recolector_id = request.form.get("recolector_id") or None
    solicitud_ids = request.form.getlist("solicitud_ids")

    if not recolector_id:
        flash("Debes asignar un recolector para poder programar la ruta.", "error")
        return redirect(url_for("admin_dashboard", tab="solicitudes"))

    db = get_db()
    zona = None
    if solicitud_ids:
        fila = db.execute(
            "SELECT zona FROM solicitudes WHERE id = ? AND zona IS NOT NULL", (solicitud_ids[0],)
        ).fetchone()
        zona = fila["zona"] if fila else None
    cur = db.execute(
        "INSERT INTO rutas (nombre, zona, fecha, hora_salida, recolector_id) VALUES (?, ?, ?, ?, ?)",
        (nombre, zona, fecha, hora_salida, recolector_id),
    )
    ruta_id = cur.lastrowid
    puntos_raw = [
        db.execute(
            "SELECT id, estado, lat, lon, cliente_id, direccion FROM solicitudes WHERE id = ?", (sid,)
        ).fetchone()
        for sid in solicitud_ids
    ]
    puntos_fusionados = fusionar_puntos_mismo_cliente([p for p in puntos_raw if p is not None])
    puntos, sobrantes_cajas = limitar_cajas_grupo(db, puntos_fusionados)
    parada_ids_nuevas = []
    for i, p in enumerate(puntos, start=1):
        cur_parada = db.execute(
            "INSERT INTO paradas (ruta_id, solicitud_id, solicitud_extra_id, tipo_extra, orden, tipo) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ruta_id, p["id"], p.get("extra_id"), p.get("tipo_extra"), i, p["tipo"]),
        )
        parada_ids_nuevas.append(cur_parada.lastrowid)
        db.execute("UPDATE solicitudes SET estado = 'programada' WHERE id = ?", (p["id"],))
        if p.get("extra_id"):
            db.execute("UPDATE solicitudes SET estado = 'programada' WHERE id = ?", (p["extra_id"],))
    db.commit()
    if parada_ids_nuevas:
        threading.Thread(
            target=_notificar_paradas_programadas, args=(parada_ids_nuevas, request.host_url), daemon=True
        ).start()
    mensaje = f"Ruta '{nombre}' creada con {len(puntos)} parada(s)."
    if sobrantes_cajas:
        mensaje += (
            f" {len(sobrantes_cajas)} solicitud(es) no se incluyeron por exceder el máximo de "
            f"{CAJAS_MAX_ENTREGA_RUTA} cajas de entrega o {CAJAS_MAX_RECEPCION_RUTA} de recepción por ruta; "
            "quedaron pendientes para otra ruta."
        )
    flash(mensaje, "success" if not sobrantes_cajas else "error")
    return redirect(url_for("admin_dashboard", tab="rutas"))


@app.route("/admin/rutas/<int:ruta_id>/eliminar", methods=["POST"])
@login_required("admin")
def admin_eliminar_ruta(user, ruta_id):
    db = get_db()
    ruta = db.execute("SELECT * FROM rutas WHERE id = ?", (ruta_id,)).fetchone()
    if ruta is None:
        flash("Esa ruta ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="rutas"))

    paradas_pendientes = db.execute(
        "SELECT * FROM paradas WHERE ruta_id = ? AND estado = 'pendiente'", (ruta_id,)
    ).fetchall()
    for p in paradas_pendientes:
        estado_previo = "pendiente_entrega" if p["tipo"] == "entrega" else "pendiente"
        db.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (estado_previo, p["solicitud_id"]))

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
    correo = None
    if sol["cliente_id"]:
        u = db.execute("SELECT name, email FROM users WHERE id = ?", (sol["cliente_id"],)).fetchone()
        if u:
            nombre = u["name"]
            correo = u["email"]
    db.commit()
    if correo:
        enviar_email(
            correo, "¡Ya tenemos ruta en tu zona! — RE-PVC",
            f"Hola {nombre},\n\n¡Buenas noticias! Ya tenemos ruta en tu zona"
            + (f" ({zona})" if zona else "") + " y quedaste integrado.\n"
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
        correo = None
        if sol["cliente_id"]:
            u = db.execute("SELECT name, email FROM users WHERE id = ?", (sol["cliente_id"],)).fetchone()
            if u:
                nombre = u["name"]
                correo = u["email"]
        if not correo:
            continue
        disponible -= necesita
        token = secrets.token_urlsafe(24)
        db.execute(
            "UPDATE solicitudes SET notificado_existencia = 1, token_existencia = ? WHERE id = ?",
            (token, sol["id"]),
        )
        cajas_texto = f"{necesita} caja(s)" if necesita else "cajas"
        link_si = url_for("solicitud_confirmar_existencia", token=token, respuesta="si", _external=True)
        link_cancelar = url_for(
            "solicitud_confirmar_existencia", token=token, respuesta="cancelar", _external=True
        )
        enviar_email(
            correo, "Ya hay existencia — RE-PVC",
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
    db = get_db()
    db.execute("DELETE FROM vacaciones_registros WHERE id = ?", (vacacion_id,))
    db.commit()
    flash("Registro de vacaciones eliminado.", "success")
    return redirect(url_for("admin_dashboard", tab="vacaciones"))


@app.route("/admin/vacaciones/saldo", methods=["POST"])
@login_required("admin")
def admin_actualizar_saldo_vacaciones(user):
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
        zonas_seleccionadas = request.form.getlist("zonas")
        fecha = request.form.get("fecha") or date.today().isoformat()
        hora_salida = request.form.get("hora_salida") or "08:00"
        rutas_creadas = 0
        paradas_creadas = 0
        zonas_omitidas = []
        solicitudes_cajas_omitidas = 0
        proximo_numero_ruta = siguiente_numero_ruta(db)
        parada_ids_nuevas = []
        for zona in zonas_seleccionadas:
            recolector_id = request.form.get(f"recolector_id__{zona}") or None
            if not recolector_id:
                zonas_omitidas.append(zona)
                continue
            puntos_raw = db.execute(
                "SELECT id, estado, lat, lon, cliente_id, direccion FROM solicitudes "
                "WHERE estado IN ('pendiente', 'pendiente_entrega') AND zona = ? AND ("
                "  fecha_reinicio_espera IS NULL"
                f"  OR datetime(fecha_reinicio_espera, '+{DIAS_ESPERA_PRIMERA_RECOLECCION} days') <= datetime('now', 'localtime')"
                ") ORDER BY id",
                (zona,),
            ).fetchall()
            if not puntos_raw:
                continue
            puntos = fusionar_puntos_mismo_cliente(puntos_raw)
            grupos_sin_filtrar = dividir_puntos_por_duracion(puntos)
            grupos = []
            for grupo_crudo in grupos_sin_filtrar:
                grupo_filtrado, sobrantes = limitar_cajas_grupo(db, grupo_crudo)
                solicitudes_cajas_omitidas += len(sobrantes)
                if grupo_filtrado:
                    grupos.append(grupo_filtrado)
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
                target=_notificar_paradas_programadas, args=(parada_ids_nuevas, request.host_url), daemon=True
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
        flash(mensaje, "success" if not zonas_omitidas and not solicitudes_cajas_omitidas else "error")
        return redirect(url_for("admin_dashboard"))

    zonas = db.execute(
        "SELECT zona, COUNT(*) AS n FROM solicitudes WHERE estado IN ('pendiente', 'pendiente_entrega') "
        "AND zona IS NOT NULL AND ("
        "  fecha_reinicio_espera IS NULL"
        f"  OR datetime(fecha_reinicio_espera, '+{DIAS_ESPERA_PRIMERA_RECOLECCION} days') <= datetime('now', 'localtime')"
        ") GROUP BY zona ORDER BY zona"
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
    return render_template(
        "admin_ruta.html", ruta=ruta, paradas=paradas, paradas_json=paradas_json, estimado=estimado,
        tiempo_real=tiempo_real, kg_total=kg_total,
    )


@app.route("/admin/usuarios/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_recolector(user):
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


@app.route("/admin/nef/nuevo", methods=["POST"])
@login_required("admin")
def admin_nuevo_nef(user):
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
    db = get_db()
    r = db.execute("SELECT * FROM users WHERE id = ? AND role = 'nef'", (user_id,)).fetchone()
    if r is None:
        flash("Esa cuenta de NEF ya no existe.", "error")
        return redirect(url_for("admin_dashboard", tab="recolectores"))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    flash(f"Cuenta de NEF '{r['name']}' eliminada.", "success")
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
    return render_template("recolector_dashboard.html", rutas=rutas)


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
        "s.cantidad_cajas, s.lat, s.lon, s.telefono, s.tipo_redistribucion, s.bote_a_devolver, "
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


@app.route("/recolector/paradas/<int:parada_id>/actualizar", methods=["POST"])
@login_required("recolector")
def recolector_actualizar_parada(user, parada_id):
    estado = request.form["estado"]
    notas = request.form.get("notas", "").strip()
    if estado not in ("completada", "incidencia", "ausente"):
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
    if parada["estado"] != "pendiente":
        flash("Esa parada ya quedó registrada y no se puede cambiar.", "error")
        return redirect(url_for("recolector_ver_ruta", ruta_id=parada["ruta_id"]))

    db.execute("UPDATE paradas SET estado = ?, notas = ? WHERE id = ?", (estado, notas, parada_id))

    es_entrega = parada["tipo"] == "entrega"

    kg_raw = request.form.get("kg", "").strip()
    kg_final = 0.0
    if estado == "completada" and kg_raw:
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

    cajas_raw = request.form.get("cajas", "").strip()
    cajas_final = None
    if estado == "completada" and cajas_raw:
        try:
            cajas_final = max(0, int(cajas_raw))
        except ValueError:
            cajas_final = None
    db.execute("UPDATE paradas SET cajas_reales = ? WHERE id = ?", (cajas_final, parada_id))

    # tipo_redistribucion NULL = la solicitud "propia" del paciente (su bote de bienvenida o su
    # recolección normal de PVC): al completarse cualquiera de las dos, vuelve a 'pendiente' con
    # el temporizador de espera reiniciado (DIAS_ESPERA_PRIMERA_RECOLECCION), porque en ambos
    # casos lo que sigue es una futura recolección de material. tipo_redistribucion 'material'/
    # 'donar' = una solicitud puntual de cajas (recibir o donar): al completarse ya no falta
    # nada más, así que se marca 'recolectada' (terminal) y desaparece de las listas de admin.
    botes_entregados = 0
    fecha_reinicio = None
    if estado == "completada":
        if parada["tipo_redistribucion"] is None:
            solicitud_estado = "pendiente"
            fecha_reinicio = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if es_entrega:
                botes_entregados += 1
        else:
            solicitud_estado = "recolectada"
    elif estado == "ausente":
        solicitud_estado = "pendiente_entrega" if es_entrega else "pendiente"
    else:
        solicitud_estado = "incidencia"
    if fecha_reinicio:
        db.execute(
            "UPDATE solicitudes SET estado = ?, fecha_reinicio_espera = ? WHERE id = ?",
            (solicitud_estado, fecha_reinicio, parada["solicitud_id"]),
        )
    else:
        db.execute("UPDATE solicitudes SET estado = ? WHERE id = ?", (solicitud_estado, parada["solicitud_id"]))

    if parada["solicitud_extra_id"]:
        es_entrega_extra = parada["tipo_extra"] == "entrega"
        fecha_reinicio_extra = None
        if estado == "completada":
            if parada["tipo_redistribucion_extra"] is None:
                solicitud_estado_extra = "pendiente"
                fecha_reinicio_extra = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if es_entrega_extra:
                    botes_entregados += 1
            else:
                solicitud_estado_extra = "recolectada"
        elif estado == "ausente":
            solicitud_estado_extra = "pendiente_entrega" if es_entrega_extra else "pendiente"
        else:
            solicitud_estado_extra = "incidencia"
        if fecha_reinicio_extra:
            db.execute(
                "UPDATE solicitudes SET estado = ?, fecha_reinicio_espera = ? WHERE id = ?",
                (solicitud_estado_extra, fecha_reinicio_extra, parada["solicitud_extra_id"]),
            )
        else:
            db.execute(
                "UPDATE solicitudes SET estado = ? WHERE id = ?",
                (solicitud_estado_extra, parada["solicitud_extra_id"]),
            )

    if botes_entregados:
        registrar_movimiento_botes(db, "entrega", botes_entregados, f"Parada #{parada['id']}")

    if estado == "completada":
        if parada["tipo_redistribucion"] and parada["cantidad_cajas"]:
            signo = 1 if parada["tipo_redistribucion"] == "donar" else -1
            registrar_movimiento_cajas(
                db, parada["material_cajas"], "donacion" if signo > 0 else "entrega",
                signo * parada["cantidad_cajas"], f"Parada #{parada['id']}",
            )
        if parada["tipo_redistribucion_extra"] and parada["cantidad_cajas_extra"]:
            signo = 1 if parada["tipo_redistribucion_extra"] == "donar" else -1
            registrar_movimiento_cajas(
                db, parada["material_cajas_extra"], "donacion" if signo > 0 else "entrega",
                signo * parada["cantidad_cajas_extra"], f"Parada #{parada['id']} (extra)",
            )

    total_paradas = db.execute(
        "SELECT COUNT(*) c FROM paradas WHERE ruta_id = ?", (parada["ruta_id"],)
    ).fetchone()["c"]
    paradas_pendientes = db.execute(
        "SELECT COUNT(*) c FROM paradas WHERE ruta_id = ? AND estado = 'pendiente'", (parada["ruta_id"],)
    ).fetchone()["c"]
    nuevo_estado_ruta = "completada" if paradas_pendientes == 0 else "en_curso"
    db.execute("UPDATE rutas SET estado = ? WHERE id = ?", (nuevo_estado_ruta, parada["ruta_id"]))

    db.commit()
    if paradas_pendientes > 0:
        threading.Thread(
            target=_notificar_siguiente_parada, args=(parada["ruta_id"],), daemon=True
        ).start()
    flash("Parada actualizada.", "success")
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

def _enviar_invitaciones_nef(publicacion_id):
    """Manda la invitación de un evento/webinar de NEF a todos los pacientes que pidieron
    recibir información de NEF. Corre en un hilo aparte (con su propia conexión a la base de
    datos) para no bloquear la respuesta al crear la publicación."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        pub = conn.execute("SELECT * FROM nef_publicaciones WHERE id = ?", (publicacion_id,)).fetchone()
        if pub is None:
            return
        pacientes = conn.execute(
            "SELECT name, email FROM users WHERE role = 'cliente' AND recibir_info_nef = 1"
        ).fetchall()
    finally:
        conn.close()

    detalles = f"Fecha: {pub['fecha_evento'] or 'por confirmar'}"
    if pub["hora_evento"]:
        detalles += f" a las {pub['hora_evento']}"
    if pub["tipo"] == "evento":
        asunto = f"Invitación: {pub['titulo']} — NEF"
        if pub["lugar_evento"]:
            detalles += f"\nLugar: {pub['lugar_evento']}"
        if pub["lat"] is not None and pub["lon"] is not None:
            detalles += (
                f"\nUbicación: https://www.google.com/maps/dir/?api=1&destination={pub['lat']},{pub['lon']}"
            )
    else:
        asunto = f"Invitación a webinar: {pub['titulo']} — NEF"
        if pub["link_webinar"]:
            detalles += f"\nLiga para ingresar: {pub['link_webinar']}"

    for p in pacientes:
        cuerpo = f"Hola {p['name']},\n\nNEF te invita a: {pub['titulo']}\n\n{detalles}\n\n"
        if pub["contenido"]:
            cuerpo += f"{pub['contenido']}\n\n"
        if pub["tipo"] == "evento":
            cuerpo += "Puedes confirmar tu asistencia desde tu sesión en RE-PVC, en la pestaña NEF."
        enviar_email(p["email"], asunto, cuerpo)


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
    publicacion_id = cur.lastrowid
    if tipo in ("evento", "webinar"):
        threading.Thread(target=_enviar_invitaciones_nef, args=(publicacion_id,), daemon=True).start()
        flash("Publicación creada. Se están enviando las invitaciones por correo a los pacientes suscritos.", "success")
    else:
        flash("Publicación creada.", "success")
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


init_db()

if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=debug_mode, threaded=True)

DROP TABLE IF EXISTS paradas;
DROP TABLE IF EXISTS rutas;
DROP TABLE IF EXISTS solicitudes;
DROP TABLE IF EXISTS notificaciones_admin;
DROP TABLE IF EXISTS nef_confirmaciones;
DROP TABLE IF EXISTS nef_publicaciones;
DROP TABLE IF EXISTS admin_videos;
DROP TABLE IF EXISTS pedidos_material;
DROP TABLE IF EXISTS inventario_botes;
DROP TABLE IF EXISTS inventario_cajas;
DROP TABLE IF EXISTS productividad;
DROP TABLE IF EXISTS vacaciones_registros;
DROP TABLE IF EXISTS vacaciones_saldo;
DROP TABLE IF EXISTS horas_extra;
DROP TABLE IF EXISTS users;

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

CREATE UNIQUE INDEX idx_users_telefono ON users(telefono);

CREATE TABLE nef_publicaciones (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tipo TEXT NOT NULL CHECK(tipo IN ('informacion','video','evento','webinar')),
  titulo TEXT NOT NULL,
  contenido TEXT,
  video_url TEXT,
  fecha_evento TEXT,
  hora_evento TEXT,
  lugar_evento TEXT,
  lat REAL,
  lon REAL,
  link_webinar TEXT,
  video_archivo TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE nef_confirmaciones (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  publicacion_id INTEGER NOT NULL REFERENCES nef_publicaciones(id),
  cliente_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT DEFAULT (datetime('now','localtime')),
  UNIQUE(publicacion_id, cliente_id)
);

CREATE TABLE admin_videos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  titulo TEXT NOT NULL,
  descripcion TEXT,
  archivo TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE pedidos_material (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material TEXT NOT NULL,
  cantidad REAL,
  unidad TEXT,
  proveedor TEXT,
  notas TEXT,
  estado TEXT NOT NULL DEFAULT 'pendiente' CHECK(estado IN ('pendiente','recibido')),
  fecha_pedido TEXT DEFAULT (datetime('now','localtime')),
  fecha_recibido TEXT
);

CREATE TABLE inventario_botes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tipo TEXT NOT NULL CHECK(tipo IN ('compra','entrega','devolucion')),
  cantidad INTEGER NOT NULL,
  notas TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE inventario_cajas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material TEXT NOT NULL,
  tipo TEXT NOT NULL CHECK(tipo IN ('donacion','entrega','ajuste')),
  cantidad INTEGER NOT NULL,
  notas TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE productividad (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fecha TEXT NOT NULL DEFAULT (date('now','localtime')),
  persona TEXT NOT NULL CHECK(persona IN ('Gabriela','Paola','Monserrat')),
  actividad TEXT NOT NULL CHECK(actividad IN ('moler','cortar','secar','envasar')),
  cantidad_kg REAL,
  notas TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE vacaciones_saldo (
  persona TEXT PRIMARY KEY CHECK(persona IN ('Lety','Martin','Gaby','Paola','Monserrat')),
  dias_totales INTEGER NOT NULL DEFAULT 12
);

CREATE TABLE vacaciones_registros (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  persona TEXT NOT NULL CHECK(persona IN ('Lety','Martin','Gaby','Paola','Monserrat')),
  fecha_inicio TEXT NOT NULL,
  fecha_fin TEXT NOT NULL,
  dias INTEGER NOT NULL,
  notas TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE horas_extra (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recolector_id INTEGER NOT NULL REFERENCES users(id),
  fecha TEXT NOT NULL DEFAULT (date('now','localtime')),
  hora_inicio TEXT NOT NULL,
  hora_salida TEXT NOT NULL,
  horas_trabajadas REAL NOT NULL,
  horas_extra REAL NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE notificaciones_admin (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cliente_id INTEGER REFERENCES users(id),
  mensaje TEXT NOT NULL,
  leida INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE solicitudes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cliente_id INTEGER REFERENCES users(id),
  nombre_contacto TEXT,
  direccion TEXT NOT NULL,
  codigo_postal TEXT,
  telefono TEXT,
  edad INTEGER,
  tipo_maquina TEXT CHECK(tipo_maquina IN ('maquina','manual')),
  marca TEXT CHECK(marca IN ('baxter','pisa')),
  frecuencia_semana INTEGER,
  causa_enfermedad TEXT CHECK(causa_enfermedad IN ('diabetes','hipertension','autoinmune','desconocida')),
  material TEXT NOT NULL,
  notas TEXT,
  cantidad_cajas INTEGER,
  tipo_redistribucion TEXT CHECK(tipo_redistribucion IN ('donar','material')),
  recoger_en_sitio INTEGER NOT NULL DEFAULT 0,
  confirmado_existencia INTEGER NOT NULL DEFAULT 0,
  notificado_existencia INTEGER NOT NULL DEFAULT 0,
  token_existencia TEXT,
  bote_a_devolver INTEGER NOT NULL DEFAULT 0,
  fuera_cobertura INTEGER NOT NULL DEFAULT 0,
  fecha_reinicio_espera TEXT,
  modalidad TEXT CHECK(modalidad IN ('compra','donacion')),
  lat REAL,
  lon REAL,
  zona TEXT,
  estado TEXT NOT NULL DEFAULT 'pendiente' CHECK(estado IN ('pendiente','pendiente_entrega','entregado','programada','recolectada','incidencia','cancelada','lista_espera')),
  created_at TEXT DEFAULT (datetime('now','localtime')),
  CHECK (cliente_id IS NOT NULL OR nombre_contacto IS NOT NULL)
);

CREATE TABLE rutas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  nombre TEXT NOT NULL,
  zona TEXT,
  fecha TEXT NOT NULL,
  hora_salida TEXT NOT NULL DEFAULT '08:00',
  hora_inicio_real TEXT,
  hora_fin_real TEXT,
  recolector_id INTEGER REFERENCES users(id),
  estado TEXT NOT NULL DEFAULT 'planificada' CHECK(estado IN ('planificada','en_curso','completada')),
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE movimientos_dinero (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tipo TEXT NOT NULL CHECK(tipo IN ('ingreso','egreso')),
  monto REAL NOT NULL,
  motivo TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE almacen_movimientos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tipo TEXT NOT NULL CHECK(tipo IN ('entrada','salida')),
  material TEXT NOT NULL,
  cantidad REAL NOT NULL,
  motivo TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE paradas (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ruta_id INTEGER NOT NULL REFERENCES rutas(id),
  solicitud_id INTEGER NOT NULL REFERENCES solicitudes(id),
  solicitud_extra_id INTEGER REFERENCES solicitudes(id),
  tipo_extra TEXT CHECK(tipo_extra IN ('recoleccion','entrega')),
  orden INTEGER NOT NULL,
  tipo TEXT NOT NULL DEFAULT 'recoleccion' CHECK(tipo IN ('recoleccion','entrega')),
  estado TEXT NOT NULL DEFAULT 'pendiente' CHECK(estado IN ('pendiente','completada','incidencia','ausente')),
  estado_extra TEXT NOT NULL DEFAULT 'pendiente' CHECK(estado_extra IN ('pendiente','completada','incidencia','ausente')),
  notas TEXT,
  kg_recolectados REAL,
  cajas_reales INTEGER,
  cajas_reales_extra INTEGER,
  confirmado_paciente TEXT,
  confirmacion_token TEXT
);

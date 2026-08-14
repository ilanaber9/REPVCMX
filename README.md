# Rutas de Recolección

App web multi-usuario (Flask + SQLite) para gestionar rutas de recolección de material reciclable.

## Roles

- **Administrador**: ve solicitudes pendientes, crea rutas y las asigna a un recolector, crea cuentas de recolector.
- **Recolector**: ve sus rutas asignadas, marca cada parada como recolectada o reporta incidencia.
- **Cliente**: se registra, solicita recolecciones y ve el estado de sus solicitudes.

Varios usuarios pueden usar la app al mismo tiempo desde distintos navegadores/dispositivos (cada quien con su sesión); los cambios se ven al recargar la página.

## Cómo correrla

```bash
cd "rutas-recoleccion"
./venv/bin/python app.py
```

Abre http://127.0.0.1:5050 (o http://TU_IP_LOCAL:5050 desde otro dispositivo en la misma red).

Admin de prueba ya creado: `admin@rutas.local` / `admin123`

## Mapa

El panel de admin, el detalle de cada ruta y la vista del recolector muestran un mapa (Leaflet + OpenStreetMap, gratis, sin API key) con los puntos de recolección y, dentro de una ruta, la línea que conecta las paradas en orden.

En la vista de ruta del recolector hay un botón **"Seguir mi ubicación"** que usa el GPS del navegador (`navigator.geolocation`) para mostrar su posición en vivo con un punto azul mientras recorre la ruta. Requiere que el navegador/celular tenga permiso de ubicación concedido y, en la práctica, HTTPS o `localhost` (en producción real se necesitaría servir la app con HTTPS para que el navegador permita geolocalización).

## Crear rutas masivamente por zona

En **Crear rutas por zona** (`/admin/rutas/masivas`) el admin puede marcar varias zonas importadas a la vez, asignar un recolector a cada una (o dejarlas sin asignar) y una fecha compartida, y crear todas esas rutas en un solo envío — en vez de repetir "seleccionar zona → crear ruta" una por una.

## Importar puntos desde un mapa (Leaflet/Google My Maps exportado)

Si tienes un archivo HTML con una variable `ROUTES_DATA` (nombre, dirección, lat/lon por parada, agrupados por ruta) puedes importarlo:

```bash
./venv/bin/python import_routes.py /ruta/al/archivo.html
```

Cada parada se crea como un punto pendiente sin cliente asignado, agrupado por zona (`Ruta N (X km)`). Desde el panel de admin, elige la zona en el desplegable "Puntos importados", selecciona los puntos y arma la ruta real (con fecha y recolector) igual que con las solicitudes de clientes.

## Pacientes nuevos y entrega de botes

Un paciente puede necesitar dos cosas distintas:

- **Ya tiene material para donar** → se agrega como punto pendiente de recolección normal.
- **Es nuevo / necesita un bote** → se agrega como **pendiente de entrega de bote**. Este punto se arma en una ruta igual que cualquier otro, pero cuando el recolector lo marca, el botón dice "Marcar bote entregado" (no "recolectado") y, al completarlo, el paciente vuelve a quedar disponible como punto normal de recolección para la próxima vez que su bote esté lleno.

El admin puede dar de alta pacientes nuevos manualmente desde **"Agregar nuevo paciente"** en el panel, y los propios pacientes (cuenta de cliente) pueden elegir la misma opción — "Tengo material para donar" vs. "Necesito que me entreguen un bote" — al crear su solicitud.

## Nadie en casa

En cada parada, además de "Marcar recolectado/entregado" e "Reportar incidencia", el recolector tiene el botón **"Nadie en casa"**. Al usarlo, esa parada queda marcada como `ausente` (para que quede el historial de la visita) y el punto vuelve automáticamente a la lista de pendientes — de recolección o de entrega de bote, según corresponda — listo para incluirse en una futura ruta.

## Datos

Todo se guarda en `database.db` (SQLite). Bórralo y reinicia la app para empezar de cero (se vuelve a crear con el admin de prueba).

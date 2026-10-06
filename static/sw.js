/* Service worker de RE-PVC (recolectores): guarda en el celular las pantallas de rutas y los
   archivos de la app para poder abrirlas sin señal. Lo que el recolector registre sin señal lo
   guarda static/offline.js y lo manda cuando regresa la conexión.
   (Se registra para todos los roles desde static/instalar.js; el guardado de pantallas es solo del recolector.)
   Sube VERSION si cambias algo aquí, para que los celulares tiren el contenido guardado de antes. */
const VERSION = 'v1';
const PAGINAS = 'repvc-paginas-' + VERSION;
const ESTATICOS = 'repvc-estaticos-' + VERSION;
const EXTERNOS = 'repvc-externos-' + VERSION;
const ESPERA_RED_MS = 5000; // con señal débil, a los 5 s se usa lo guardado

const PAGINA_SIN_CONEXION = `<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sin conexión</title>
<style>body{font-family:system-ui,sans-serif;margin:0;padding:32px 20px;background:#f4f6f5;color:#1c2420;text-align:center}
h1{font-size:1.3rem}p{color:#5a655f}button{background:#1f7a4d;color:#fff;border:0;border-radius:8px;padding:12px 24px;font-size:1rem}</style>
</head><body><h1>📴 Sin señal</h1>
<p>Esta pantalla todavía no se había guardado en tu celular. Ábrela una vez con internet (antes de salir
a la ruta) y después podrás usarla sin señal.</p>
<button onclick="location.reload()">Reintentar</button></body></html>`;

self.addEventListener('install', () => self.skipWaiting());

self.addEventListener('activate', (evento) => {
  evento.waitUntil((async () => {
    const vigentes = [PAGINAS, ESTATICOS, EXTERNOS];
    for (const nombre of await caches.keys()) {
      if (nombre.startsWith('repvc-') && !vigentes.includes(nombre)) await caches.delete(nombre);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('message', (evento) => {
  if (evento.data === 'limpiar') evento.waitUntil(caches.delete(PAGINAS));
});

function esHtmlValido(respuesta) {
  const tipo = respuesta.headers.get('content-type') || '';
  const aLogin = respuesta.redirected && new URL(respuesta.url).pathname.startsWith('/login');
  return respuesta.ok && tipo.includes('text/html') && !aLogin;
}

// Pantallas del recolector: primero la red; si no hay señal (o tarda demasiado) la última versión guardada.
async function paginaRecolector(peticion) {
  const cache = await caches.open(PAGINAS);
  const llave = new URL(peticion.url).pathname;
  const guardada = await cache.match(llave);
  const red = fetch(peticion).then(async (respuesta) => {
    if (esHtmlValido(respuesta)) await cache.put(llave, respuesta.clone());
    return respuesta;
  });
  if (!guardada) {
    try { return await red; } catch (e) {
      return new Response(PAGINA_SIN_CONEXION, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
    }
  }
  const limite = new Promise((resolver) => setTimeout(() => resolver(guardada), ESPERA_RED_MS));
  return Promise.race([red.catch(() => guardada), limite]);
}

// Archivos que casi no cambian: se sirve lo guardado y se refresca por detrás.
async function actualizarPorDetras(peticion, nombreCache) {
  const cache = await caches.open(nombreCache);
  const guardada = await cache.match(peticion);
  const red = fetch(peticion).then((respuesta) => {
    if (respuesta.ok || respuesta.type === 'opaque') cache.put(peticion, respuesta.clone());
    return respuesta;
  });
  if (guardada) { red.catch(() => {}); return guardada; }
  return red;
}

self.addEventListener('fetch', (evento) => {
  const peticion = evento.request;
  if (peticion.method !== 'GET') return;
  const url = new URL(peticion.url);

  if (url.origin === self.location.origin) {
    if (url.pathname === '/recolector' || url.pathname.startsWith('/recolector/')) {
      evento.respondWith(paginaRecolector(peticion));
    } else if (url.pathname.startsWith('/static/')) {
      evento.respondWith(actualizarPorDetras(peticion, ESTATICOS));
    }
    return;
  }
  // Leaflet (mapa) viene de cdnjs: se guarda para que la pantalla de la ruta abra completa sin señal.
  if (url.hostname === 'cdnjs.cloudflare.com') {
    evento.respondWith(actualizarPorDetras(peticion, EXTERNOS));
  }
});

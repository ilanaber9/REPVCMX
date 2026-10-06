/* Modo sin conexión para recolectores.
   1) Registra el service worker (static/sw.js), que guarda las pantallas de rutas para abrirlas sin señal.
   2) Los formularios marcados con data-offline (resultado de una parada, iniciar/finalizar ruta, bote
      recibido, horas extra) se mandan por fetch: si no hay señal se guardan en el celular (localStorage)
      y se envían solos, en orden, cuando regresa la conexión.
   Cada envío lleva la hora real en que se hizo (campo "momento") para que las horas de la ruta no se
   desfasen, y "diferido"/"ultimo" para que el servidor no mande avisos atrasados a los pacientes. */
(function () {
  'use strict';
  var CLAVE_COLA = 'repvc_cola_v1';
  var ESPERA_ENVIO_MS = 12000;
  var enviando = false;

  // ---------- almacenamiento de la cola ----------
  function leerCola() {
    try { return JSON.parse(localStorage.getItem(CLAVE_COLA)) || []; } catch (e) { return []; }
  }
  function guardarCola(cola) {
    try { localStorage.setItem(CLAVE_COLA, JSON.stringify(cola)); } catch (e) {}
  }

  // ---------- avisos en pantalla ----------
  var estilos = document.createElement('style');
  estilos.textContent =
    '#offline-barra{position:sticky;top:0;z-index:2000;display:none;padding:8px 14px;font-size:0.88rem;font-weight:600;' +
    'text-align:center;color:#fff}#offline-barra.sin-senal{background:#8a5a00}#offline-barra.pendientes{background:#1565C0}' +
    '#offline-barra button{margin-left:8px;padding:4px 10px;min-height:0;font-size:0.85rem;background:#fff;color:#1c2420;border:0;border-radius:6px}' +
    '#offline-aviso{position:fixed;left:12px;right:12px;bottom:16px;z-index:2100;background:#1c2420;color:#fff;border-radius:10px;' +
    'padding:12px 14px;font-size:0.92rem;display:none;box-shadow:0 6px 18px rgba(0,0,0,.3)}' +
    '.offline-guardado{background:#e8f0fe;border:1px solid #9bb7f0;color:#1a3d8f;border-radius:8px;padding:8px 10px;margin-top:8px;font-size:0.88rem}';
  document.head.appendChild(estilos);

  var barra = document.createElement('div');
  barra.id = 'offline-barra';
  var aviso = document.createElement('div');
  aviso.id = 'offline-aviso';
  function montar() {
    document.body.insertBefore(barra, document.body.firstChild);
    document.body.appendChild(aviso);
  }
  var temporizadorAviso = null;
  function avisar(texto) {
    aviso.textContent = texto;
    aviso.style.display = 'block';
    clearTimeout(temporizadorAviso);
    temporizadorAviso = setTimeout(function () { aviso.style.display = 'none'; }, 6000);
  }

  function pintarBarra() {
    var pendientes = leerCola().length;
    if (!navigator.onLine) {
      barra.className = 'sin-senal';
      barra.textContent = '📴 Sin señal — lo que registres se guarda en tu celular' +
        (pendientes ? ' (' + pendientes + ' por enviar)' : '');
      barra.style.display = 'block';
    } else if (pendientes) {
      barra.className = 'pendientes';
      barra.textContent = '⏳ ' + pendientes + ' cambio' + (pendientes === 1 ? '' : 's') + ' por enviar ';
      var boton = document.createElement('button');
      boton.type = 'button';
      boton.textContent = 'Enviar ahora';
      boton.addEventListener('click', sincronizar);
      barra.appendChild(boton);
      barra.style.display = 'block';
    } else {
      barra.style.display = 'none';
    }
  }

  // ---------- utilidades ----------
  function momentoCdmx() {
    // "YYYY-MM-DD HH:MM:SS" en hora de Ciudad de México, sin depender de la zona del celular
    return new Date().toLocaleString('sv-SE', { timeZone: 'America/Mexico_City' });
  }
  function rutaDe(accion) {
    try { return new URL(accion, location.href).pathname; } catch (e) { return accion; }
  }
  function etiquetaDe(formulario, boton) {
    var texto = boton && boton.textContent.trim() ? boton.textContent.trim() : 'Cambio';
    var detalles = [];
    var kg = formulario.querySelector('[name=kg]');
    var cajas = formulario.querySelector('[name=cajas]');
    if (kg && kg.value) detalles.push(kg.value + ' kg');
    if (cajas && cajas.value) detalles.push(cajas.value + ' cajas');
    return detalles.length ? texto + ' (' + detalles.join(', ') + ')' : texto;
  }
  function clavePareja(url, campos) {
    var parte = '';
    campos.forEach(function (c) { if (c[0] === 'parte') parte = c[1]; });
    return url + '|' + parte;
  }

  // ---------- envío ----------
  function enviarUna(item, ultimo) {
    var cuerpo = new URLSearchParams();
    item.campos.forEach(function (c) { cuerpo.append(c[0], c[1]); });
    cuerpo.append('momento', item.momento);
    cuerpo.append('diferido', '1');
    cuerpo.append('ultimo', ultimo ? '1' : '0');
    var control = new AbortController();
    var corte = setTimeout(function () { control.abort(); }, ESPERA_ENVIO_MS);
    return fetch(item.url, {
      method: 'POST', body: cuerpo, credentials: 'same-origin', signal: control.signal,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    }).then(function (r) { clearTimeout(corte); return r; }, function (e) { clearTimeout(corte); throw e; });
  }

  function mensajesDelServidor(html) {
    try {
      var doc = new DOMParser().parseFromString(html, 'text/html');
      return Array.prototype.map.call(doc.querySelectorAll('.flash'), function (n) { return n.textContent.trim(); }).filter(Boolean);
    } catch (e) { return []; }
  }

  function sincronizar() {
    if (enviando || !navigator.onLine) return;
    var cola = leerCola();
    if (!cola.length) return;
    enviando = true;
    var enviados = 0;
    var detener = false;
    var cadena = Promise.resolve();
    cola.forEach(function (item, i) {
      cadena = cadena.then(function () {
        if (detener) return;
        return enviarUna(item, i === cola.length - 1).then(function (r) {
          if (new URL(r.url).pathname.indexOf('/login') === 0) {
            detener = true;
            avisar('Tu sesión venció. Inicia sesión de nuevo para enviar tus cambios (siguen guardados en tu celular).');
            return;
          }
          if (r.status >= 500) { detener = true; return; }
          // se quita de la cola: se registró, o el servidor la rechazó (ej. esa parada ya estaba registrada)
          guardarCola(leerCola().filter(function (x) { return x.id !== item.id; }));
          enviados++;
        }, function () { detener = true; });
      });
    });
    cadena.then(function () {
      enviando = false;
      pintarBarra();
      if (enviados) {
        avisar('Se enviaron ' + enviados + ' cambio' + (enviados === 1 ? '' : 's') + ' guardado' + (enviados === 1 ? '' : 's') + ' sin señal.');
        setTimeout(function () { location.reload(); }, 1200);
      }
    });
  }

  function encolar(formulario, campos, boton) {
    var url = rutaDe(formulario.getAttribute('action'));
    var item = {
      id: Date.now() + '-' + Math.random().toString(36).slice(2, 8),
      url: url, campos: campos, momento: momentoCdmx(), etiqueta: etiquetaDe(formulario, boton)
    };
    var cola = leerCola();
    cola.push(item);
    guardarCola(cola);
    marcarGuardado(formulario, item.etiqueta);
    avisar('Guardado en tu celular. Se enviará solo cuando haya señal.');
    pintarBarra();
  }

  function marcarGuardado(formulario, etiqueta) {
    var nota = document.createElement('div');
    nota.className = 'offline-guardado';
    nota.textContent = '⏳ Guardado sin señal: ' + etiqueta + ' — pendiente de enviar';
    formulario.replaceWith(nota);
  }

  // Al abrir la pantalla (con o sin señal) se vuelve a marcar lo que ya está guardado en la cola.
  function marcarGuardadosEnPantalla() {
    var cola = leerCola();
    if (!cola.length) return;
    document.querySelectorAll('form[data-offline]').forEach(function (formulario) {
      var campos = [];
      new FormData(formulario).forEach(function (v, k) { campos.push([k, String(v)]); });
      var pareja = clavePareja(rutaDe(formulario.getAttribute('action')), campos);
      var coincide = cola.filter(function (it) { return clavePareja(it.url, it.campos) === pareja; })[0];
      if (coincide) marcarGuardado(formulario, coincide.etiqueta);
    });
  }

  // ---------- formularios con data-offline ----------
  document.addEventListener('submit', function (evento) {
    var formulario = evento.target;
    if (!formulario.matches || !formulario.matches('form[data-offline]')) return;
    evento.preventDefault();
    evento.stopImmediatePropagation(); // el "¿Seguro?" (onsubmit) se corre una sola vez, aquí abajo
    if (typeof formulario.onsubmit === 'function' && formulario.onsubmit.call(formulario, evento) === false) return;
    var boton = evento.submitter || null;
    var campos = [];
    new FormData(formulario).forEach(function (v, k) { if (typeof v === 'string') campos.push([k, v]); });
    if (boton && boton.name) campos.push([boton.name, boton.value]);
    if (!navigator.onLine) { encolar(formulario, campos, boton); return; }

    var cuerpo = new URLSearchParams();
    campos.forEach(function (c) { cuerpo.append(c[0], c[1]); });
    var control = new AbortController();
    var corte = setTimeout(function () { control.abort(); }, ESPERA_ENVIO_MS);
    var botones = formulario.querySelectorAll('button');
    botones.forEach(function (b) { b.disabled = true; });
    fetch(formulario.getAttribute('action'), {
      method: 'POST', body: cuerpo, credentials: 'same-origin', signal: control.signal,
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    }).then(function (r) {
      clearTimeout(corte);
      if (new URL(r.url).pathname.indexOf('/login') === 0) { location.href = r.url; return; }
      return r.text().then(function (html) {
        var mensajes = mensajesDelServidor(html);
        if (mensajes.length) avisar(mensajes.join(' · '));
        setTimeout(function () { location.reload(); }, mensajes.length ? 1400 : 0);
      });
    }, function () {
      clearTimeout(corte);
      botones.forEach(function (b) { b.disabled = false; });
      encolar(formulario, campos, boton);   // se cayó la señal justo ahora: se guarda para después
    });
  }, true);

  // ---------- al salir ----------
  document.addEventListener('click', function (evento) {
    var enlace = evento.target.closest ? evento.target.closest('a[href="/logout"]') : null;
    if (!enlace) return;
    var pendientes = leerCola().length;
    if (pendientes && !confirm('Tienes ' + pendientes + ' cambio(s) guardado(s) sin enviar. Si cierras sesión ahora se perderán. ¿Salir de todos modos?')) {
      evento.preventDefault();
      return;
    }
    guardarCola([]);
    if (window.caches) caches.keys().then(function (nombres) {
      nombres.filter(function (n) { return n.indexOf('repvc-paginas-') === 0; }).forEach(function (n) { caches.delete(n); });
    });
  });

  // ---------- guardar las rutas para usarlas sin señal ----------
  function guardarRutasParaSinSenal() {
    if (!navigator.onLine || !('serviceWorker' in navigator)) return;
    var enlaces = Array.prototype.map.call(document.querySelectorAll('a[data-guardar-sin-senal]'), function (a) {
      return a.getAttribute('href');
    }).filter(function (h, i, todos) { return /^\/recolector\/rutas\/\d+$/.test(h) && todos.indexOf(h) === i; });
    var indicador = document.getElementById('rutas-sin-senal');
    navigator.serviceWorker.ready.then(function () {
      return Promise.all(enlaces.map(function (h) { return fetch(h, { credentials: 'same-origin' }).then(function (r) { return r.ok; }, function () { return false; }); }));
    }).then(function (resultados) {
      if (!indicador) return;
      var guardadas = resultados.filter(Boolean).length;
      indicador.textContent = enlaces.length
        ? '✅ ' + guardadas + ' de ' + enlaces.length + ' ruta' + (enlaces.length === 1 ? '' : 's') + ' guardada' + (guardadas === 1 ? '' : 's') + ' para usar sin señal.'
        : '';
    });
  }

  // ---------- arranque ----------
  window.addEventListener('online', function () { pintarBarra(); sincronizar(); });
  window.addEventListener('offline', pintarBarra);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) sincronizar(); });
  setInterval(sincronizar, 30000);
  document.addEventListener('DOMContentLoaded', function () {
    montar();
    marcarGuardadosEnPantalla();
    pintarBarra();
    sincronizar();
    guardarRutasParaSinSenal();
  });
})();

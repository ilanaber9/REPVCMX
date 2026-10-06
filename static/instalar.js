/* Para todos los roles: registra el service worker (lo que permite instalar el sitio como app) y
   maneja el botón "Instalar app" del encabezado, que solo aparece cuando el navegador ofrece instalarla. */
(function () {
  'use strict';
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
  }
  var eventoInstalar = null;
  var yaInstalada = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;

  function mostrarBoton(visible) {
    var boton = document.getElementById('instalar-app');
    if (boton) boton.style.display = visible ? 'inline-block' : 'none';
  }
  // El botón se ve siempre (salvo que la app ya esté instalada y abierta como app). Si el navegador
  // ofrece instalar, se instala con un toque; si no, se muestran los pasos para hacerlo a mano.
  mostrarBoton(!yaInstalada);

  window.addEventListener('beforeinstallprompt', function (evento) {
    evento.preventDefault();
    eventoInstalar = evento;
  });
  window.addEventListener('appinstalled', function () { mostrarBoton(false); });

  document.addEventListener('click', function (evento) {
    if (!evento.target || evento.target.id !== 'instalar-app') return;
    if (eventoInstalar) {
      eventoInstalar.prompt();
      eventoInstalar = null;
      return;
    }
    var ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
    alert(ios
      ? 'Para instalar la app en iPhone: toca el botón Compartir (el cuadro con la flecha) en Safari y elige "Agregar a pantalla de inicio".'
      : 'Para instalar la app: abre el menú de tu navegador (los tres puntos ⋮) y elige "Instalar app" o "Agregar a pantalla de inicio". Si no aparece, abre esta página en Chrome.');
  });
})();

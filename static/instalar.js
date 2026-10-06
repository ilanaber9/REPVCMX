/* Para todos los roles: registra el service worker (lo que permite instalar el sitio como app) y
   maneja el botón "Instalar app" del encabezado, que solo aparece cuando el navegador ofrece instalarla. */
(function () {
  'use strict';
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(function () {});
  }
  var eventoInstalar = null;
  window.addEventListener('beforeinstallprompt', function (evento) {
    evento.preventDefault();
    eventoInstalar = evento;
    var boton = document.getElementById('instalar-app');
    if (boton) boton.style.display = 'inline-block';
  });
  window.addEventListener('appinstalled', function () {
    var boton = document.getElementById('instalar-app');
    if (boton) boton.style.display = 'none';
  });
  document.addEventListener('click', function (evento) {
    if (evento.target && evento.target.id === 'instalar-app' && eventoInstalar) {
      eventoInstalar.prompt();
      eventoInstalar = null;
      evento.target.style.display = 'none';
    }
  });
})();

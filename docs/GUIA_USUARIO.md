# Guía de usuario

## Qué hace la aplicación

Escritorio Remoto Bravo permite ver y controlar, desde un navegador, el escritorio del equipo donde se ejecuta el servidor. También transfiere archivos, muestra dispositivos conectados y ofrece clientes nativos para Windows y Linux.

## Puesta en marcha

1. En el equipo remoto, ejecuta `run.sh` (Linux) o `run.bat` (Windows).
2. Guarda las credenciales que aparecen en el primer arranque.
3. En otro equipo, abre `http://IP-DEL-SERVIDOR:5000`.
4. Inicia sesión y abre **Escritorio Web**.
5. Haz clic sobre la imagen antes de utilizar teclado y ratón.

El equipo remoto debe permanecer encendido y con sesión gráfica activa. En Linux usa X11/Xorg.

## Pestañas

- **Escritorio Web:** vídeo en tiempo real, ratón, teclado, envío de texto y pantalla completa.
- **Aplicaciones:** descarga los clientes nativos ya empaquetados cuando estén disponibles.
- **FTP:** mueve archivos entre el dispositivo cliente y el servidor. El navegador exige seleccionar manualmente las carpetas locales.
- **Dispositivos Conectados:** consulta navegadores/agentes, muestra información de red autorizada y permite chat privado.
- **Configuración:** cambia transmisión, credenciales, FTP, acceso directo, Cloudflare Tunnel y archivos del servidor.
- **Ayuda:** guía integrada, seguridad y soluciones a problemas frecuentes.

## Acceso desde Internet

Se recomienda una VPN o un túnel HTTPS. El ejemplo de Nginx está en `deploy/nginx-escritorio-remoto.conf`. La redirección de puertos del router solo debe usarse con HTTPS y contraseña robusta.

## Seguridad

- Cambia la contraseña inicial.
- Usa HTTPS/VPN para conexiones externas.
- No publiques `instance/`, `.env`, tokens de agente ni credenciales FTP.
- Limita `RDWEB_FILE_ROOT` a la carpeta estrictamente necesaria.
- Cierra sesión al terminar en dispositivos compartidos.

## Solución rápida de problemas

- **Pantalla negra:** confirma que existe una sesión gráfica activa y que Linux usa Xorg.
- **Sin control de entrada:** pulsa sobre el lienzo. UAC y el escritorio seguro de Windows no admiten control normal.
- **No conecta:** comprueba puerto 5000, firewall y soporte WebSocket en el proxy/túnel.
- **Va lento:** reduce FPS, calidad JPEG y ancho máximo.
- **Archivos bloqueados:** revisa permisos, espacio y carpeta raíz configurada.

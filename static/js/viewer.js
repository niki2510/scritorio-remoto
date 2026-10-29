/**
 * Cliente de visor/control del Escritorio Remoto.
 *
 * - Recibe frames JPEG por WebSocket y los dibuja en un <canvas>.
 * - Captura eventos de mouse/teclado sobre el canvas y los reenvía al
 *   servidor como coordenadas relativas (0.0 - 1.0), independientes de
 *   la resolución real de la pantalla remota y del escalado del navegador.
 */
(function () {
  const canvas = document.getElementById("screen");
  const ctx = canvas.getContext("2d");
  const statusEl = document.getElementById("status");
  const textInput = document.getElementById("text-input");
  const sendTextBtn = document.getElementById("send-text-btn");
  const fullscreenBtn = document.getElementById("fullscreen-btn");
  const toggleToolbarBtn = document.getElementById("toggle-toolbar-btn");
  const canvasWrapper = document.getElementById("canvas-wrapper");
  const remoteCursor = document.getElementById("remote-cursor");

  let remoteWidth = 1280;
  let remoteHeight = 720;
  const frameImage = new Image();
  let pendingFrame = null;

  function fitCanvasToWindow() {
    if (!canvas.width || !canvas.height || !canvasWrapper.clientWidth || !canvasWrapper.clientHeight) return;
    const scale = Math.min(
      canvasWrapper.clientWidth / canvas.width,
      canvasWrapper.clientHeight / canvas.height
    );
    canvas.style.width = Math.max(1, Math.floor(canvas.width * scale)) + "px";
    canvas.style.height = Math.max(1, Math.floor(canvas.height * scale)) + "px";
  }

  function setStatus(text, cls) {
    statusEl.textContent = text;
    statusEl.className = "status " + cls;
  }

  const socket = io({ transports: ["websocket", "polling"] });
  window.remoteSocket = socket;

  function deviceMetadata() {
    const ua = navigator.userAgent;
    const browser = ua.includes("Edg/") ? "Microsoft Edge" : ua.includes("Firefox/") ? "Firefox" :
      ua.includes("Chrome/") ? "Chrome" : ua.includes("Safari/") ? "Safari" : "Navegador web";
    const os = ua.includes("Windows") ? "Windows" : ua.includes("Android") ? "Android" :
      /iPhone|iPad/.test(ua) ? "iOS/iPadOS" : ua.includes("Mac OS") ? "macOS" :
      ua.includes("Linux") ? "Linux" : "Desconocido";
    return {
      name: localStorage.getItem("remoteChatUsername") || `${os} · ${browser}`,
      browser, os, platform: navigator.platform || "",
      language: navigator.language || "",
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "",
      local_ip: "No disponible por privacidad del navegador",
      wifi_name: "No disponible por privacidad del navegador",
      screen_resolution: `${screen.width} × ${screen.height}`,
      viewport: `${window.innerWidth} × ${window.innerHeight}`,
      user_agent: ua,
    };
  }
  // Datos de red ya averiguados, para no perderlos al reenviar los metadatos
  // (por ejemplo, al guardar el nombre de usuario).
  let networkMetadata = {};
  window.getDeviceMetadata = () => ({ ...deviceMetadata(), ...networkMetadata });

  async function detectNetworkMetadata() {
    const metadata = {};
    if (window.desktopRemote?.networkInfo) {
      try {
        const network = await window.desktopRemote.networkInfo();
        if (network.local_ip) metadata.local_ip = network.local_ip;
        metadata.wifi_name = network.wifi_name || "No conectado mediante Wi-Fi";
        return metadata;
      } catch (_error) {}
    }
    if (!window.RTCPeerConnection) return metadata;
    try {
      const peer = new RTCPeerConnection({ iceServers: [] });
      peer.createDataChannel("");
      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);
      await new Promise((resolve) => window.setTimeout(resolve, 500));
      const addresses = (peer.localDescription?.sdp || "").match(/(?:\d{1,3}\.){3}\d{1,3}/g) || [];
      const local = addresses.find((ip) => /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(ip));
      peer.close();
      if (local) metadata.local_ip = local;
    } catch (_error) {}
    return metadata;
  }

  socket.on("connect", () => {
    setStatus("Conectado", "status-connected");
    // Mantener join_viewer sin argumentos permite conectar mientras un
    // proceso servidor anterior todavía termina de reiniciarse.
    socket.emit("join_viewer");
    socket.emit("register_device", window.getDeviceMetadata());
    detectNetworkMetadata().then((metadata) => {
      networkMetadata = metadata;
      if (socket.connected) socket.emit("register_device", window.getDeviceMetadata());
    });
    canvas.focus();
  });

  socket.on("disconnect", () => {
    setStatus("Desconectado", "status-disconnected");
  });

  // Si el servidor se ha actualizado mientras esta pestaña estaba abierta,
  // recargamos para no seguir usando HTML/CSS/JS antiguos.
  socket.on("app_version", (data) => {
    if (window.APP_VERSION && data.version && data.version !== window.APP_VERSION) {
      window.location.reload();
    }
  });

  socket.on("connect_error", () => {
    setStatus("Error de conexión", "status-disconnected");
  });

  socket.on("screen_info", (data) => {
    remoteWidth = data.width;
    remoteHeight = data.height;
    canvas.width = remoteWidth;
    canvas.height = remoteHeight;
    fitCanvasToWindow();
  });

  socket.on("server_error", (data) => {
    console.error("Error del servidor:", data.message);
  });

  socket.on("frame", (data) => {
    // Evita encolar frames más rápido de lo que se pueden decodificar.
    pendingFrame = data;
    if (frameImage.complete) {
      renderNextFrame();
    }
  });

  function renderNextFrame() {
    if (!pendingFrame) return;
    const data = pendingFrame;
    pendingFrame = null;
    if (canvas.width !== data.width || canvas.height !== data.height) {
      canvas.width = data.width;
      canvas.height = data.height;
      fitCanvasToWindow();
    }
    frameImage.onload = () => {
      ctx.drawImage(frameImage, 0, 0, canvas.width, canvas.height);
      socket.emit("frame_ready");
      if (pendingFrame) renderNextFrame();
    };
    frameImage.onerror = () => {
      socket.emit("frame_ready");
      if (pendingFrame) renderNextFrame();
    };
    frameImage.src = "data:image/jpeg;base64," + data.image;
  }

  // ---------------- Mouse ----------------
  function relativeCoords(evt) {
    const rect = canvas.getBoundingClientRect();
    const x = (evt.clientX - rect.left) / rect.width;
    const y = (evt.clientY - rect.top) / rect.height;
    return {
      x: Math.min(1, Math.max(0, x)),
      y: Math.min(1, Math.max(0, y)),
    };
  }

  // Icono de cursor dibujado sobre el vídeo: flecha en reposo, mano al
  // hacer clic. Se posiciona en coordenadas de página (no relativas al
  // canvas) para que quede exactamente bajo el puntero real del usuario.
  if (remoteCursor) {
    remoteCursor.classList.add("mode-arrow");
  }

  function moveCursorIcon(evt) {
    if (!remoteCursor || !canvasWrapper) return;
    const wrapperRect = canvasWrapper.getBoundingClientRect();
    remoteCursor.style.left = (evt.clientX - wrapperRect.left) + "px";
    remoteCursor.style.top = (evt.clientY - wrapperRect.top) + "px";
    remoteCursor.classList.add("visible");
  }

  function setCursorMode(mode) {
    if (!remoteCursor) return;
    remoteCursor.classList.remove("mode-arrow", "mode-hand");
    remoteCursor.classList.add(mode);
  }

  let lastMoveSent = 0;
  canvas.addEventListener("mousemove", (evt) => {
    moveCursorIcon(evt);
    const now = performance.now();
    if (now - lastMoveSent < 20) return; // ~50 Hz máximo
    lastMoveSent = now;
    const { x, y } = relativeCoords(evt);
    socket.emit("mouse_move", { x, y });
  });

  canvas.addEventListener("mousedown", (evt) => {
    evt.preventDefault();
    canvas.focus();
    setCursorMode("mode-hand");
    moveCursorIcon(evt);
    const { x, y } = relativeCoords(evt);
    socket.emit("mouse_move", { x, y });
    socket.emit("mouse_button", { action: "down", button: evt.button });
  });

  canvas.addEventListener("mouseup", (evt) => {
    evt.preventDefault();
    setCursorMode("mode-arrow");
    socket.emit("mouse_button", { action: "up", button: evt.button });
  });

  canvas.addEventListener("mouseenter", (evt) => {
    moveCursorIcon(evt);
  });

  canvas.addEventListener("mouseleave", () => {
    setCursorMode("mode-arrow");
    if (remoteCursor) remoteCursor.classList.remove("visible");
  });

  canvas.addEventListener("contextmenu", (evt) => evt.preventDefault());

  canvas.addEventListener(
    "wheel",
    (evt) => {
      evt.preventDefault();
      socket.emit("mouse_scroll", { dx: evt.deltaX, dy: evt.deltaY });
    },
    { passive: false }
  );

  // ---------------- Teclado ----------------
  const remoteHeldKeys = new Map();

  function isTextKey(evt) {
    const altGraph = evt.getModifierState && evt.getModifierState("AltGraph");
    return evt.key.length === 1 && (!evt.ctrlKey && !evt.altKey && !evt.metaKey || altGraph);
  }

  canvas.addEventListener("keydown", (evt) => {
    evt.preventDefault();
    if (isTextKey(evt)) socket.emit("type_text", { text: evt.key });
    else {
      remoteHeldKeys.set(evt.code || evt.key, evt.key);
      socket.emit("key_event", { action: "down", key: evt.key });
    }
  });

  canvas.addEventListener("keyup", (evt) => {
    evt.preventDefault();
    const token = evt.code || evt.key;
    const remoteKey = remoteHeldKeys.get(token);
    if (remoteKey) {
      remoteHeldKeys.delete(token);
      socket.emit("key_event", { action: "up", key: remoteKey });
    }
  });

  // Si el canvas pierde el foco (Alt+Tab, clic fuera...), el navegador no
  // envía los "keyup" pendientes: soltamos los modificadores en remoto.
  canvas.addEventListener("blur", () => {
    const keys = new Set([...remoteHeldKeys.values(), "Shift", "Control", "Alt", "AltGraph", "Meta"]);
    remoteHeldKeys.clear();
    keys.forEach((key) => {
      socket.emit("key_event", { action: "up", key });
    });
  });

  // ---------------- Barra de herramientas ----------------
  sendTextBtn.addEventListener("click", () => {
    const text = textInput.value;
    if (text) {
      socket.emit("type_text", { text });
      textInput.value = "";
    }
  });

  textInput.addEventListener("keydown", (evt) => {
    evt.stopPropagation();
    if (evt.key === "Enter") sendTextBtn.click();
  });

  fullscreenBtn.addEventListener("click", () => {
    const wrapper = document.getElementById("canvas-wrapper");
    if (!document.fullscreenElement) {
      wrapper.requestFullscreen().catch(() => {});
    } else {
      document.exitFullscreen();
    }
  });

  toggleToolbarBtn.addEventListener("click", () => {
    const toolbar = toggleToolbarBtn.closest(".viewer-toolbar");
    const collapsed = toolbar.classList.toggle("collapsed");
    toggleToolbarBtn.textContent = collapsed ? "+" : "−";
    toggleToolbarBtn.title = collapsed ? "Mostrar controles" : "Ocultar controles";
    requestAnimationFrame(fitCanvasToWindow);
  });

  // Expuesto para que settings.js pueda devolver el foco al canvas al
  // volver a la pestaña "Visor" (si no, el teclado deja de reenviarse).
  window.focusRemoteCanvas = () => canvas.focus();
  window.fitRemoteCanvas = fitCanvasToWindow;
  window.addEventListener("resize", fitCanvasToWindow);

  canvas.focus();
})();

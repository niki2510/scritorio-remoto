(function () {
  const socket = window.remoteSocket;
  if (!socket) return;

  const listEl = document.getElementById("devices-list");
  const countEl = document.getElementById("devices-count");
  const emptyEl = document.getElementById("device-empty");
  const contentEl = document.getElementById("device-content");
  const nameEl = document.getElementById("device-name");
  const descriptionEl = document.getElementById("device-description");
  const networkStatusEl = document.getElementById("device-network-status");
  const infoEl = document.getElementById("device-info");
  const infoToggle = document.getElementById("device-info-toggle");
  const infoToggleWrap = document.getElementById("device-info-toggle-wrap");
  const chatTitleEl = document.getElementById("chat-title");
  const messagesEl = document.getElementById("chat-messages");
  const statusEl = document.getElementById("chat-status");
  const chatInput = document.getElementById("chat-input");
  const usernameInput = document.getElementById("device-username");
  const nameStatusEl = document.getElementById("device-name-status");
  let devices = [];
  const GROUP_ID = "__all__";
  let selectedId = GROUP_ID;
  const conversations = new Map();
  const visibleDeviceInfo = new Set();

  const labels = {
    ip: "IP pública", browser: "Navegador", os: "Sistema operativo",
    local_ip: "IP local", wifi_name: "Red Wi-Fi",
    hostname: "Nombre del equipo", system_user: "Usuario del sistema",
    network_interface: "Interfaz activa", netmask: "Máscara de red", gateway: "Puerta de enlace",
    device_uid: "Identificador persistente", agent: "Agente local",
    platform: "Plataforma", language: "Idioma", timezone: "Zona horaria",
    screen_resolution: "Resolución de pantalla", viewport: "Área del navegador",
    connected_at: "Conectado desde", user_agent: "Agente de usuario", id: "ID de conexión",
  };

  function formatValue(key, value) {
    if (key === "connected_at") return new Date(value).toLocaleString();
    return value || "No disponible";
  }

  function knownNetworkValue(value) {
    return value && !String(value).startsWith("No disponible") && value !== "No conectado mediante Wi-Fi";
  }

  function localSubnet(ip) {
    const parts = String(ip || "").split(".");
    return parts.length === 4 ? parts.slice(0, 3).join(".") : "";
  }

  function networkRelation(device) {
    const own = devices.find((item) => item.id === socket.id);
    if (!own || device.id === socket.id) return { text: "Este es tu dispositivo actual.", level: "ok" };
    const details = [];
    let level = "unknown";
    if (knownNetworkValue(own.wifi_name) && knownNetworkValue(device.wifi_name)) {
      if (own.wifi_name === device.wifi_name) {
        details.push(`Misma Wi-Fi: ${device.wifi_name}`);
        level = "ok";
      } else {
        details.push(`Wi-Fi diferente: ${device.wifi_name}`);
        level = "warning";
      }
    }
    if (own.ip && device.ip) {
      if (own.ip === device.ip) {
        details.push("Misma IP pública / salida a Internet");
        if (level === "unknown") level = "ok";
      } else {
        details.push(`IP pública diferente: ${device.ip}. Verifica la identidad del usuario`);
        level = "danger";
      }
    }
    const ownSubnet = localSubnet(own.local_ip);
    const deviceSubnet = localSubnet(device.local_ip);
    if (ownSubnet && deviceSubnet) {
      details.push(ownSubnet === deviceSubnet ? `Subred local aparente coincidente: ${deviceSubnet}.x` : `Subred local diferente: ${deviceSubnet}.x`);
    }
    return { text: details.join(" · ") || "No hay datos de red suficientes para comparar.", level };
  }

  function renderList() {
    countEl.textContent = `${devices.length} dispositivo${devices.length === 1 ? "" : "s"} conectado${devices.length === 1 ? "" : "s"}`;
    if (selectedId !== GROUP_ID && selectedId && !devices.some((device) => device.id === selectedId)) selectedId = null;
    if (!selectedId) selectedId = GROUP_ID;
    listEl.replaceChildren();
    const groupButton = document.createElement("button");
    groupButton.type = "button";
    groupButton.className = "device-item group-item" + (selectedId === GROUP_ID ? " active" : "");
    const groupTitle = document.createElement("strong");
    groupTitle.textContent = "👥 Todos";
    const groupSummary = document.createElement("span");
    groupSummary.textContent = "Chat de grupo con todos los dispositivos";
    groupButton.append(groupTitle, groupSummary);
    groupButton.addEventListener("click", () => { selectedId = GROUP_ID; renderList(); });
    listEl.appendChild(groupButton);
    devices.forEach((device) => {
      const row = document.createElement("div");
      row.className = "device-row" + (device.id === selectedId ? " active" : "");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "device-item" + (device.id === selectedId ? " active" : "");
      const title = document.createElement("strong");
      title.textContent = `● ${device.name || "Dispositivo"}${device.id === socket.id ? " (este dispositivo)" : ""}${device.agent === true || device.agent === "True" ? " · Agente" : ""}`;
      title.className = "online-dot";
      const summary = document.createElement("span");
      const relation = networkRelation(device);
      const relationLabel = relation.level === "danger" ? "⚠ Red pública diferente" :
        relation.text.includes("Misma Wi-Fi") ? "Misma Wi-Fi" :
        relation.text.includes("Misma IP pública") ? "Misma IP pública" :
        device.id === socket.id ? "Este dispositivo" : "Red sin verificar";
      summary.textContent = `${device.ip || "IP desconocida"} · ${relationLabel}`;
      summary.classList.add(`network-${relation.level}`);
      button.append(title, summary);
      button.addEventListener("click", () => { selectedId = device.id; renderList(); });
      const toggleLabel = document.createElement("label");
      toggleLabel.className = "device-row-toggle";
      toggleLabel.title = "Mostrar u ocultar información de este dispositivo";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = visibleDeviceInfo.has(device.id);
      const toggleText = document.createElement("span");
      toggleText.textContent = "Información";
      toggle.addEventListener("change", () => {
        if (toggle.checked) visibleDeviceInfo.add(device.id);
        else visibleDeviceInfo.delete(device.id);
        selectedId = device.id;
        renderList();
      });
      toggleLabel.append(toggle, toggleText);
      row.append(button, toggleLabel);
      listEl.appendChild(row);
    });
    renderDetail();
  }

  function renderMessages() {
    messagesEl.replaceChildren();
    (conversations.get(selectedId) || []).forEach((item) => {
      const bubble = document.createElement("div");
      bubble.className = "chat-message" + (item.sender_id === socket.id ? " mine" : "");
      bubble.textContent = item.message;
      const meta = document.createElement("small");
      meta.textContent = `${item.sender_id === socket.id ? "Tú" : item.sender_name} · ${new Date(item.sent_at).toLocaleTimeString()}`;
      bubble.appendChild(meta);
      messagesEl.appendChild(bubble);
    });
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function renderDetail() {
    if (selectedId === GROUP_ID) {
      emptyEl.hidden = true;
      contentEl.hidden = false;
      nameEl.textContent = "Todos los dispositivos";
      descriptionEl.textContent = "Este mensaje será visible para todos los dispositivos conectados. Usa la casilla Información junto a un dispositivo para consultar sus datos.";
      infoEl.hidden = true;
      infoToggleWrap.hidden = true;
      networkStatusEl.hidden = true;
      chatTitleEl.textContent = "Chat de grupo";
      renderMessages();
      return;
    }
    const device = devices.find((item) => item.id === selectedId);
    emptyEl.hidden = Boolean(device);
    contentEl.hidden = !device;
    if (!device) return;
    nameEl.textContent = device.name || "Dispositivo";
    descriptionEl.textContent = device.id === socket.id ? "Este dispositivo" : "Conversación privada entre dos dispositivos.";
    const relation = networkRelation(device);
    networkStatusEl.hidden = false;
    networkStatusEl.className = `network-status network-${relation.level}`;
    networkStatusEl.textContent = relation.text;
    infoToggleWrap.hidden = false;
    infoToggle.checked = visibleDeviceInfo.has(device.id);
    infoEl.hidden = !infoToggle.checked;
    chatTitleEl.textContent = "Chat privado";
    infoEl.replaceChildren();
    Object.keys(labels).forEach((key) => {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = labels[key];
      dd.textContent = formatValue(key, device[key]);
      infoEl.append(dt, dd);
    });
    renderMessages();
  }

  function refresh() { socket.emit("request_devices", {}); }
  window.refreshConnectedDevices = refresh;
  infoToggle.addEventListener("change", () => {
    if (!selectedId || selectedId === GROUP_ID) return;
    if (infoToggle.checked) visibleDeviceInfo.add(selectedId);
    else visibleDeviceInfo.delete(selectedId);
    infoEl.hidden = !infoToggle.checked;
    renderList();
  });
  usernameInput.value = localStorage.getItem("remoteChatUsername") || "";
  document.getElementById("device-name-save").addEventListener("click", () => {
    const name = usernameInput.value.trim();
    if (!name) {
      nameStatusEl.textContent = "Escribe un nombre de usuario.";
      return;
    }
    localStorage.setItem("remoteChatUsername", name);
    const metadata = window.getDeviceMetadata ? window.getDeviceMetadata() : { name };
    socket.emit("register_device", metadata);
    nameStatusEl.textContent = `Nombre guardado: ${name}`;
  });
  document.getElementById("devices-refresh").addEventListener("click", refresh);
  document.getElementById("chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const message = chatInput.value.trim();
    if (!selectedId || !message) return;
    socket.emit("chat_message", { target_id: selectedId, message });
    chatInput.value = "";
    statusEl.textContent = "";
  });

  socket.on("devices_updated", (data) => {
    devices = Array.isArray(data.devices) ? data.devices : [];
    window.connectedFtpAgents = devices.filter((device) => device.agent === true || device.agent === "True");
    window.dispatchEvent(new CustomEvent("connected-agents-updated", { detail: window.connectedFtpAgents }));
    renderList();
  });
  socket.on("chat_message", (message) => {
    const peerId = message.chat_type === "group" ? GROUP_ID :
      (message.sender_id === socket.id ? message.target_id : message.sender_id);
    if (!conversations.has(peerId)) conversations.set(peerId, []);
    conversations.get(peerId).push(message);
    if (peerId === selectedId) renderMessages();
  });
  socket.on("chat_error", (data) => { statusEl.textContent = data.message || "No se pudo enviar el mensaje."; });
  socket.on("connect", refresh);
  if (socket.connected) refresh();
  window.setTimeout(() => {
    if (!devices.length) countEl.textContent = "El servidor no ha respondido. Reinicia el servicio y pulsa Actualizar.";
  }, 5000);

  const agentSetup = document.getElementById("agent-setup");
  const agentCommand = document.getElementById("agent-command");
  const agentWindowsCommand = document.getElementById("agent-windows-command");
  agentSetup.addEventListener("toggle", async () => {
    if (!agentSetup.open || agentCommand.dataset.loaded) return;
    try {
      const response = await fetch("/api/device-agent/config");
      const data = await response.json();
      if (!data.ok) throw new Error(data.error);
      agentCommand.textContent = `curl -fsSL ${data.server_url}/api/device-agent/download/linux-installer | bash -s -- ${data.server_url} ${data.agent_token}`;
      const installerUrl = `${data.server_url}/api/device-agent/download/windows-installer`;
      agentWindowsCommand.textContent = `$p="$env:TEMP\\install-device-agent.ps1"; Invoke-WebRequest -UseBasicParsing '${installerUrl}' -OutFile $p; powershell -ExecutionPolicy Bypass -File $p -ServerUrl '${data.server_url}' -AgentToken '${data.agent_token}'`;
      agentCommand.dataset.loaded = "true";
    } catch (error) {
      agentCommand.textContent = error.message || "No se pudo cargar la configuración.";
      agentWindowsCommand.textContent = agentCommand.textContent;
    }
  });
})();

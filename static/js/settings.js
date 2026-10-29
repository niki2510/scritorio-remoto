/**
 * Lógica de la pestaña "Configuración": tabs, ajustes de streaming,
 * credenciales, generador de subdominios (Cloudflare Tunnel) y gestor de
 * archivos (subida/descarga tipo FTP a través de HTTP autenticado).
 */
(function () {
  const tabButtons = document.querySelectorAll(".tab-btn");
  const panels = {
    viewer: document.getElementById("tab-viewer"),
    windows: document.getElementById("tab-windows"),
    ftp: document.getElementById("tab-ftp"),
    devices: document.getElementById("tab-devices"),
    settings: document.getElementById("tab-settings"),
    help: document.getElementById("tab-help"),
  };

  tabButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabButtons.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      Object.values(panels).forEach((p) => p.classList.remove("active"));
      panels[btn.dataset.tab].classList.add("active");
      document.body.classList.toggle("viewer-immersive", btn.dataset.tab === "viewer");
      if (btn.dataset.tab === "settings") {
        loadSettings();
        loadFiles(currentFmPath);
      } else if (btn.dataset.tab === "windows") {
        loadFtpStatus();
      } else if (btn.dataset.tab === "ftp") {
        loadServerFiles();
        loadClientBrowser();
      } else if (btn.dataset.tab === "devices") {
        if (window.refreshConnectedDevices) window.refreshConnectedDevices();
      } else if (btn.dataset.tab === "viewer") {
        // Sin esto, el canvas pierde el foco al volver de Configuración
        // y el teclado deja de reenviarse al equipo remoto.
        if (window.focusRemoteCanvas) window.focusRemoteCanvas();
        if (window.fitRemoteCanvas) window.fitRemoteCanvas();
      }
    });
  });

  async function apiFetch(url, options) {
    const resp = await fetch(url, options);
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = { ok: false, error: "Respuesta no válida del servidor." };
    }
    return { status: resp.status, data };
  }

  // ---------------- Cliente local ↔ servidor remoto ----------------
  const ftpTransferLog = document.getElementById("ftp-transfer-log");
  const transferProgress = document.getElementById("transfer-progress");
  const transferProgressBar = document.getElementById("transfer-progress-bar");
  const transferProgressDetail = document.getElementById("transfer-progress-detail");
  const transferProgressPercent = document.getElementById("transfer-progress-percent");
  const transferActions = document.getElementById("transfer-actions");
  const transferPause = document.getElementById("transfer-pause");
  const transferResume = document.getElementById("transfer-resume");
  const transferStop = document.getElementById("transfer-stop");
  const transferRetry = document.getElementById("transfer-retry");
  const ftpAgentSelect = document.getElementById("ftp-agent-select");
  const remoteSocket = window.remoteSocket;
  let transferStartedAt = 0;
  let ftpProgressTimeout = null;
  let activeFtpJobId = "";
  let lastFtpRequest = null;
  let selectedFtpAgentUid = "";
  const FTP_PROGRESS_TIMEOUT_MS = 120000;

  function armFtpTimeout() {
    window.clearTimeout(ftpProgressTimeout);
    ftpProgressTimeout = window.setTimeout(() => {
      controlFtpDownload("stop");
      showFtpFailure("No se recibió progreso durante 2 minutos. Se detuvo la descarga; comprueba la conexión del agente.");
    }, FTP_PROGRESS_TIMEOUT_MS);
  }

  function showFtpFailure(message) {
    activeFtpJobId = "";
    transferPause.hidden = true;
    transferResume.hidden = true;
    transferStop.hidden = true;
    transferRetry.hidden = !lastFtpRequest;
    transferActions.hidden = !lastFtpRequest;
    endTransfer(message, "error");
  }

  function updateAgentOptions(agents = window.connectedFtpAgents || []) {
    const previous = ftpAgentSelect.value;
    const previousOption = ftpAgentSelect.selectedOptions[0];
    const previousLabel = previousOption ? previousOption.textContent.replace(/ · reconectando…$/, "") : "Agente FTP";
    const previousUid = selectedFtpAgentUid || (previousOption && previousOption.dataset.deviceUid) || "";
    ftpAgentSelect.replaceChildren(new Option("Selecciona agente FTP", ""));
    agents.forEach((agent) => {
      const option = new Option(
        `${agent.name || agent.hostname || "Agente"} · ${agent.os || "Sistema desconocido"} · ${agent.local_ip || "IP local desconocida"}`, agent.id);
      option.dataset.deviceUid = agent.device_uid || "";
      ftpAgentSelect.add(option);
    });
    if (navigator.userAgent.includes("Windows") && !agents.some((agent) => String(agent.os || "").startsWith("Windows"))) {
      const missingWindows = new Option("Windows · agente no conectado", "");
      missingWindows.disabled = true;
      ftpAgentSelect.add(missingWindows);
    }
    const current = agents.find((agent) => agent.id === previous)
      || (previousUid && agents.find((agent) => agent.device_uid === previousUid));
    if (current) {
      ftpAgentSelect.value = current.id;
      selectedFtpAgentUid = current.device_uid || previousUid;
      if (lastFtpRequest && (!lastFtpRequest.agent_uid || lastFtpRequest.agent_uid === selectedFtpAgentUid)) {
        lastFtpRequest.agent_id = current.id;
        lastFtpRequest.agent_uid = selectedFtpAgentUid;
      }
    } else if (previous) {
      const reconnecting = new Option(`${previousLabel} · reconectando…`, previous, true, true);
      reconnecting.dataset.deviceUid = previousUid;
      reconnecting.disabled = true;
      ftpAgentSelect.add(reconnecting);
    }
  }
  window.addEventListener("connected-agents-updated", (event) => updateAgentOptions(event.detail));
  updateAgentOptions();

  remoteSocket.on("agent_ftp_started", (data) => {
    activeFtpJobId = String(data.job_id || "");
    transferActions.hidden = false;
    transferPause.hidden = false;
    transferResume.hidden = true;
    transferStop.hidden = false;
    transferRetry.hidden = true;
    updateTransferProgress(0, Number(data.total_bytes || 0), "Conectando por FTP");
    armFtpTimeout();
  });
  remoteSocket.on("agent_ftp_progress", (data) => {
    updateTransferProgress(Number(data.loaded || 0), Number(data.total || 0), `FTP: ${data.current || "transfiriendo"}`);
    armFtpTimeout();
  });
  remoteSocket.on("agent_ftp_complete", (data) => {
    window.clearTimeout(ftpProgressTimeout);
    activeFtpJobId = "";
    transferActions.hidden = true;
    updateTransferProgress(Number(data.loaded || 0), Number(data.total || 0), "FTP completado");
    const skipped = Array.isArray(data.skipped) ? data.skipped : [];
    if (skipped.length) {
      endTransfer(`Descarga FTP guardada en ${data.destination}. Se omitieron ${skipped.length} enlace(s) simbólico(s) que Windows no pudo crear: ${skipped.slice(0, 5).join(", ")}${skipped.length > 5 ? "…" : ""}`, "error");
    } else {
      endTransfer(`Descarga FTP completa guardada en ${data.destination}.`);
    }
    loadServerFiles();
  });
  remoteSocket.on("agent_ftp_error", (data) => {
    window.clearTimeout(ftpProgressTimeout);
    showFtpFailure(data.message || "Falló la descarga FTP.");
  });
  remoteSocket.on("agent_ftp_stopped", () => {
    window.clearTimeout(ftpProgressTimeout);
    showFtpFailure("Descarga detenida.");
  });

  function controlFtpDownload(action) {
    if (!activeFtpJobId) return;
    remoteSocket.emit("request_agent_ftp_control", { job_id: activeFtpJobId, action });
  }

  transferPause.addEventListener("click", () => {
    controlFtpDownload("pause");
    transferPause.hidden = true;
    transferResume.hidden = false;
    transferProgressDetail.textContent = "Descarga pausada";
    window.clearTimeout(ftpProgressTimeout);
  });
  transferResume.addEventListener("click", () => {
    controlFtpDownload("resume");
    transferPause.hidden = false;
    transferResume.hidden = true;
    transferProgressDetail.textContent = "Reanudando descarga…";
    armFtpTimeout();
  });
  transferStop.addEventListener("click", () => {
    controlFtpDownload("stop");
    transferPause.hidden = true;
    transferResume.hidden = true;
    transferStop.disabled = true;
    transferProgressDetail.textContent = "Deteniendo descarga…";
  });
  transferRetry.addEventListener("click", () => {
    if (lastFtpRequest) startAgentFtpDownload(lastFtpRequest);
  });
  const clientFiles = [];
  let clientPath = "";
  let clientRootLabel = "Cliente";
  // Carpeta elegida con showDirectoryPicker (Chrome/Edge): permite guardar en ella.
  let clientDirHandle = null;
  const nativeClientFs = Boolean(window.desktopRemote && window.desktopRemote.listClientDirectory);
  let nativeClientParent = null;
  let nativeClientPath = "";
  let agentClientParent = null;
  let agentClientPath = "";
  // Carpeta del servidor que se abre al entrar en la pestaña (si existe).
  const DEFAULT_SERVER_PATH = "proyectos";
  let serverPath = DEFAULT_SERVER_PATH;
  const clientBody = document.getElementById("client-files-body");
  const clientEmpty = document.getElementById("client-files-empty");
  const serverBody = document.getElementById("ftp-server-body");
  const serverEmpty = document.getElementById("ftp-server-empty");
  const agentFolderDialog = document.getElementById("agent-folder-dialog");
  const agentFolderList = document.getElementById("agent-folder-list");
  const agentFolderInput = document.getElementById("agent-folder-path-input");
  const agentFolderSearch = document.getElementById("agent-folder-search");
  const agentFolderStatus = document.getElementById("agent-folder-status");
  const agentFolderUse = document.getElementById("agent-folder-use");
  let dialogAgentPath = "";
  let dialogAgentParent = null;
  let dialogAgentListing = null;
  let dialogAgentFolders = [];

  function renderAgentFiles(data) {
    const entries = Array.isArray(data.entries) ? data.entries : [];
    clientBody.innerHTML = "";
    document.getElementById("client-folder-path").textContent = data.directory || "Descargas del agente";
    clientEmpty.hidden = entries.length > 0;
    clientEmpty.textContent = "Carpeta vacía.";
    entries.forEach((entry) => {
      const row = document.createElement("tr");
      row.innerHTML = `<td>${entry.is_dir ? "📁" : "📄"}</td><td></td><td>${entry.is_dir ? "" : formatSize(entry.size)}</td><td>${entry.modified ? formatDate(entry.modified) : ""}</td><td></td>`;
      row.children[1].textContent = entry.name;
      clientBody.appendChild(row);
    });
  }

  function requestAgentDirectory(path = "", context = "main") {
    if (!ftpAgentSelect.value) return;
    if (context === "dialog") {
      agentFolderStatus.textContent = "Leyendo carpetas…";
      agentFolderStatus.className = "folder-dialog-status";
      agentFolderList.innerHTML = "";
    } else {
      clientBody.innerHTML = "";
      clientEmpty.hidden = false;
      clientEmpty.textContent = "Leyendo carpetas del agente…";
    }
    remoteSocket.emit("request_agent_directory", {
      agent_id: ftpAgentSelect.value,
      path,
      context,
    });
  }

  function renderAgentDirectoryMain(data) {
    agentClientPath = data.path || "";
    agentClientParent = data.parent;
    clientBody.innerHTML = "";
    document.getElementById("client-folder-path").textContent = agentClientPath || "Unidades del agente";
    const entries = Array.isArray(data.entries) ? data.entries : [];
    clientEmpty.hidden = entries.length > 0;
    clientEmpty.textContent = agentClientPath
      ? "Carpeta vacía. Esta ruta será el destino FTP."
      : "No se encontraron unidades disponibles.";
    entries.forEach((entry) => {
      const row = document.createElement("tr");
      row.innerHTML = `<td>${entry.is_dir ? "📁" : "📄"}</td><td></td><td>${entry.is_dir ? "" : formatSize(entry.size)}</td><td>${entry.modified ? formatDate(entry.modified) : ""}</td><td></td>`;
      row.children[1].textContent = entry.name;
      if (entry.is_dir) {
        row.children[1].className = "fm-link";
        row.addEventListener("dblclick", () => requestAgentDirectory(entry.path));
      }
      clientBody.appendChild(row);
    });
  }

  function renderFilteredAgentFolders() {
    agentFolderList.innerHTML = "";
    const query = agentFolderSearch.value.trim().toLocaleLowerCase();
    const folders = query
      ? dialogAgentFolders.filter((entry) => entry.name.toLocaleLowerCase().includes(query))
      : dialogAgentFolders;
    folders.forEach((entry) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "folder-dialog-item";
      button.title = entry.path;
      button.innerHTML = "<span>📁</span><span></span>";
      button.lastElementChild.textContent = entry.name;
      button.addEventListener("dblclick", () => {
        agentFolderSearch.value = "";
        requestAgentDirectory(entry.path, "dialog");
      });
      agentFolderList.appendChild(button);
    });
    if (query) {
      agentFolderStatus.textContent = folders.length
        ? `${folders.length} carpeta(s) encontrada(s) en esta ubicación.`
        : "No hay carpetas que coincidan con la búsqueda.";
    } else if (!folders.length && dialogAgentPath) {
      agentFolderStatus.textContent = "Esta carpeta no contiene subcarpetas; puedes seleccionarla.";
    }
  }

  function renderAgentDirectoryDialog(data) {
    dialogAgentPath = data.path || "";
    dialogAgentParent = data.parent;
    dialogAgentListing = data;
    dialogAgentFolders = (Array.isArray(data.entries) ? data.entries : []).filter((entry) => entry.is_dir);
    agentFolderInput.value = dialogAgentPath;
    agentFolderUse.disabled = !dialogAgentPath;
    agentFolderStatus.textContent = dialogAgentPath
      ? "Busca por nombre o haz doble clic en una carpeta para abrirla."
      : "Busca o selecciona una unidad para comenzar.";
    agentFolderStatus.className = "folder-dialog-status";
    renderFilteredAgentFolders();
  }

  remoteSocket.on("agent_directory_listing", (data) => {
    if (data.agent_id && data.agent_id !== ftpAgentSelect.value) return;
    if (data.error) {
      if (data.context === "dialog") {
        agentFolderStatus.textContent = data.error;
        agentFolderStatus.className = "folder-dialog-status error";
      } else {
        clientEmpty.hidden = false;
        clientEmpty.textContent = data.error;
      }
      return;
    }
    if (data.context === "dialog") renderAgentDirectoryDialog(data);
    else renderAgentDirectoryMain(data);
  });

  function closeAgentFolderDialog() {
    agentFolderDialog.hidden = true;
  }

  function openAgentFolderDialog() {
    if (!ftpAgentSelect.value) {
      endTransfer("Selecciona primero el agente Windows.", "error");
      return;
    }
    agentFolderDialog.hidden = false;
    agentFolderSearch.value = "";
    requestAgentDirectory(agentClientPath || "", "dialog");
    window.setTimeout(() => agentFolderSearch.focus(), 0);
  }

  document.getElementById("agent-folder-go").addEventListener("click", () => requestAgentDirectory(agentFolderInput.value.trim(), "dialog"));
  agentFolderInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter") requestAgentDirectory(agentFolderInput.value.trim(), "dialog");
  });
  document.getElementById("agent-folder-up-dialog").addEventListener("click", () => {
    if (dialogAgentParent !== null) requestAgentDirectory(dialogAgentParent, "dialog");
  });
  document.getElementById("agent-folder-close").addEventListener("click", closeAgentFolderDialog);
  document.getElementById("agent-folder-cancel").addEventListener("click", closeAgentFolderDialog);
  agentFolderSearch.addEventListener("input", renderFilteredAgentFolders);
  document.getElementById("agent-folder-search-clear").addEventListener("click", () => {
    agentFolderSearch.value = "";
    renderFilteredAgentFolders();
    agentFolderSearch.focus();
  });
  agentFolderUse.addEventListener("click", () => {
    if (!dialogAgentPath || !dialogAgentListing) return;
    agentClientPath = dialogAgentPath;
    agentClientParent = dialogAgentParent;
    closeAgentFolderDialog();
    logTransfer(`Carpeta de destino seleccionada: ${dialogAgentPath}`, "ok");
  });
  agentFolderDialog.addEventListener("click", (event) => {
    if (event.target === agentFolderDialog) closeAgentFolderDialog();
  });

  ftpAgentSelect.addEventListener("change", () => {
    selectedFtpAgentUid = ftpAgentSelect.selectedOptions[0]?.dataset.deviceUid || "";
    agentClientPath = "";
    agentClientParent = null;
    if (ftpAgentSelect.value) logTransfer("Agente FTP seleccionado. La carpeta y los archivos actuales se mantienen.");
  });

  document.getElementById("install-windows-agent").addEventListener("click", () => {
    logTransfer("Abre el archivo descargado instalar-agente-windows.cmd. Windows aparecerá automáticamente al conectarse.");
    window.location.href = "/api/device-agent/download/windows-configured-installer";
  });

  function logTransfer(text, kind = "") {
    ftpTransferLog.textContent = text;
    ftpTransferLog.className = "ftp-transfer-log" + (kind ? " " + kind : "");
  }

  function beginTransfer(text) {
    transferStartedAt = performance.now();
    transferProgress.hidden = false;
    transferProgressBar.value = 0;
    transferProgressPercent.textContent = "0%";
    transferProgressDetail.textContent = text;
    transferActions.hidden = true;
    transferStop.disabled = false;
    logTransfer(text);
  }

  function updateTransferProgress(loaded, total, text = "Transfiriendo") {
    const percent = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
    const seconds = Math.max(0.1, (performance.now() - transferStartedAt) / 1000);
    transferProgressBar.removeAttribute("value");
    if (total > 0) transferProgressBar.value = percent;
    transferProgressPercent.textContent = total > 0 ? `${percent}%` : "…";
    const speed = loaded / seconds;
    const remaining = total > loaded && speed > 0 ? Math.ceil((total - loaded) / speed) : 0;
    const eta = remaining > 0 ? ` · faltan ~${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}` : "";
    transferProgressDetail.textContent = `${text} · ${formatSize(loaded)}${total > 0 ? ` / ${formatSize(total)}` : ""} · ${formatSize(speed)}/s${eta}`;
  }

  function endTransfer(text, kind = "ok") {
    if (kind === "ok") {
      transferProgressBar.value = 100;
      transferProgressPercent.textContent = "100%";
    } else {
      transferProgressBar.value = transferProgressBar.value || 0;
      transferProgressPercent.textContent = "Error";
    }
    logTransfer(text, kind);
  }

  function startAgentFtpDownload(item) {
    const request = item.agent_id ? { ...item } : {
      agent_id: ftpAgentSelect.value,
      agent_uid: selectedFtpAgentUid,
      path: item.path,
      destination_directory: agentClientPath || "",
    };
    lastFtpRequest = request;
    beginTransfer(`Solicitando descarga FTP de ${request.path}…`);
    transferActions.hidden = false;
    transferPause.hidden = false;
    transferResume.hidden = true;
    transferStop.hidden = false;
    transferRetry.hidden = true;
    armFtpTimeout();
    remoteSocket.emit("request_agent_ftp_download", request);
  }

  function uploadWithProgress(form, totalBytes, label) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/files/upload");
      xhr.responseType = "json";
      xhr.upload.addEventListener("progress", (event) => {
        updateTransferProgress(event.loaded, event.lengthComputable ? event.total : totalBytes, label);
      });
      xhr.addEventListener("load", () => {
        const data = xhr.response || { ok: false, error: "Respuesta no válida del servidor." };
        if (xhr.status >= 200 && xhr.status < 300 && data.ok) resolve(data);
        else reject(new Error(data.error || `Error HTTP ${xhr.status}.`));
      });
      xhr.addEventListener("error", () => reject(new Error("Se perdió la conexión durante la subida.")));
      xhr.send(form);
    });
  }

  // Añade la columna de acciones (editar/eliminar) a una fila de archivo.
  function appendRowActions(row, isDir, onEdit, onDelete) {
    const cell = document.createElement("td");
    cell.className = "ftp-row-actions";
    if (!isDir) {
      const edit = document.createElement("button");
      edit.type = "button"; edit.title = "Editar"; edit.textContent = "✏️";
      edit.addEventListener("click", (event) => { event.stopPropagation(); onEdit(); });
      cell.appendChild(edit);
    }
    const del = document.createElement("button");
    del.type = "button"; del.title = "Eliminar"; del.textContent = "🗑️";
    del.addEventListener("click", (event) => { event.stopPropagation(); onDelete(); });
    cell.appendChild(del);
    row.appendChild(cell);
  }

  // Ejecuta una operación sobre archivos mostrando el resultado en la barra inferior.
  async function runFileAction(action, okText) {
    try {
      await action();
      if (okText) logTransfer(okText, "ok");
    } catch (error) {
      logTransfer(error.message || String(error), "error");
    }
  }

  const textEditor = document.getElementById("text-editor");
  const textEditorContent = document.getElementById("text-editor-content");
  const textEditorStatus = document.getElementById("text-editor-status");
  let textEditorSave = null;

  function openTextEditor(title, content, save) {
    document.getElementById("text-editor-title").textContent = title;
    textEditorContent.value = content;
    textEditorStatus.textContent = "";
    textEditorSave = save;
    textEditor.hidden = false;
    textEditorContent.focus();
  }

  function closeTextEditor() {
    textEditor.hidden = true;
    textEditorSave = null;
  }

  async function saveTextEditor() {
    if (!textEditorSave) return;
    textEditorStatus.textContent = "Guardando…";
    try {
      await textEditorSave(textEditorContent.value);
      closeTextEditor();
    } catch (error) {
      textEditorStatus.textContent = error.message || String(error);
    }
  }

  document.getElementById("text-editor-save").addEventListener("click", saveTextEditor);
  document.getElementById("text-editor-cancel").addEventListener("click", closeTextEditor);
  textEditor.addEventListener("keydown", (event) => {
    // Evita que el teclado llegue al escritorio remoto mientras se edita.
    event.stopPropagation();
    if (event.key === "Escape") closeTextEditor();
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      saveTextEditor();
    }
  });

  // Decodifica bytes como UTF-8 estricto: si no es texto, no se edita.
  function decodeText(bytes) {
    if (bytes.byteLength > 2 * 1024 * 1024) throw new Error("El archivo es demasiado grande para editarlo (máx. 2 MB).");
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    } catch (_error) {
      throw new Error("El archivo no es de texto (UTF-8) y no se puede editar.");
    }
  }

  function renderClientFiles() {
    clientBody.innerHTML = "";
    document.getElementById("client-folder-path").textContent = clientRootLabel + (clientPath ? "/" + clientPath : "");
    const prefix = clientPath ? clientPath.replace(/\/$/, "") + "/" : "";
    const visible = new Map();
    clientFiles.forEach((item, index) => {
      if (!item.path.startsWith(prefix)) return;
      const remainder = item.path.slice(prefix.length);
      if (!remainder) return;
      const slash = remainder.indexOf("/");
      const name = slash === -1 ? remainder : remainder.slice(0, slash);
      const key = (slash === -1 ? "file:" : "dir:") + name;
      if (!visible.has(key)) visible.set(key, { name, is_dir: slash !== -1, indexes: [] });
      visible.get(key).indexes.push(index);
    });
    clientEmpty.hidden = visible.size > 0;
    // El navegador no puede listar el disco del cliente por seguridad: solo ve
    // lo que el usuario elige. La app nativa sí puede recorrer directorios.
    clientEmpty.textContent = clientFiles.length
      ? "Carpeta vacía."
      : "📂 Pulsa aquí para elegir una carpeta de tu equipo (el navegador no permite explorarlo solo), "
        + "arrastra archivos aquí o usa la aplicación nativa (pestaña «Aplicación Windows») para navegar por el disco.";
    visible.forEach((entry) => {
      const item = clientFiles[entry.indexes[0]];
      const row = document.createElement("tr");
      row.draggable = true;
      row.innerHTML = `<td>${entry.is_dir ? "📁" : "📄"}</td><td></td><td>${entry.is_dir ? "" : formatSize(item.file.size)}</td><td>${entry.is_dir ? "" : formatDate(item.file.lastModified / 1000)}</td>`;
      row.children[1].textContent = entry.name;
      if (entry.is_dir) {
        row.children[1].className = "fm-link";
        row.children[1].addEventListener("dblclick", () => {
          clientPath = prefix + entry.name;
          renderClientFiles();
        });
      }
      appendRowActions(row, entry.is_dir,
        () => editHandleFile(prefix + entry.name),
        () => deleteHandleEntry(prefix + entry.name, entry.is_dir));
      row.addEventListener("dragstart", (event) => {
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("application/x-rdweb-client-file", JSON.stringify(entry.indexes));
      });
      clientBody.appendChild(row);
    });
  }

  function addClientFiles(files) {
    Array.from(files).forEach((file) => clientFiles.push({
      file,
      path: file.webkitRelativePath || file.name,
    }));
    renderClientFiles();
  }

  async function loadClientBrowser(path = nativeClientPath) {
    if (!nativeClientFs) {
      renderClientFiles();
      return;
    }
    clientBody.innerHTML = "";
    clientEmpty.hidden = false;
    clientEmpty.textContent = "Leyendo directorios del cliente…";
    try {
      const listing = await window.desktopRemote.listClientDirectory(path || "");
      nativeClientPath = listing.path || "";
      nativeClientParent = listing.parent;
      document.getElementById("client-folder-path").textContent = nativeClientPath || "Unidades de Windows";
      clientEmpty.hidden = listing.entries.length > 0;
      clientEmpty.textContent = "Carpeta vacía.";
      listing.entries.forEach((entry) => {
        const row = document.createElement("tr");
        row.draggable = true;
        row.innerHTML = `<td>${entry.is_dir ? "📁" : "📄"}</td><td></td><td>${entry.is_dir ? "" : formatSize(entry.size)}</td><td>${entry.modified ? formatDate(entry.modified) : ""}</td>`;
        row.children[1].textContent = entry.name;
        if (entry.is_dir) {
          row.children[1].className = "fm-link";
          row.children[1].addEventListener("dblclick", () => loadClientBrowser(entry.path));
        }
        // Las unidades de Windows (lista raíz) no se pueden borrar.
        if (nativeClientPath) {
          appendRowActions(row, entry.is_dir, () => editNativeFile(entry), () => deleteNativeEntry(entry));
        } else {
          row.appendChild(document.createElement("td"));
        }
        row.addEventListener("dragstart", (event) => {
          event.dataTransfer.effectAllowed = "copy";
          event.dataTransfer.setData("application/x-rdweb-native-file", JSON.stringify(entry));
        });
        clientBody.appendChild(row);
      });
    } catch (error) {
      clientEmpty.hidden = false;
      clientEmpty.textContent = error.message || "No se pudo leer el directorio del cliente.";
    }
  }

  async function collectNativeUpload(entry, prefix = "") {
    if (!entry.is_dir) {
      const bytes = await window.desktopRemote.readClientFile(entry.path);
      return [{ file: new File([bytes], entry.name), path: prefix + entry.name }];
    }
    const listing = await window.desktopRemote.listClientDirectory(entry.path);
    const result = [];
    for (const child of listing.entries) {
      result.push(...await collectNativeUpload(child, prefix + entry.name + "/"));
    }
    return result;
  }

  let clientPlatform = navigator.userAgent.includes("Windows")
    ? "Windows" : (navigator.userAgent.includes("Linux") ? "Linux" : "este dispositivo");
  document.getElementById("client-device-title").textContent = `💻 Cliente · ${clientPlatform}`;
  if (nativeClientFs) {
    window.desktopRemote.clientPlatform().then((platform) => {
      clientPlatform = platform === "win32" ? "Windows" : (platform === "linux" ? "Linux" : clientPlatform);
      document.getElementById("client-device-title").textContent = `💻 Cliente · ${clientPlatform}`;
    });
  }

  async function collectDroppedItems(dataTransfer) {
    const entries = Array.from(dataTransfer.items || [])
      .map((item) => item.webkitGetAsEntry ? item.webkitGetAsEntry() : null)
      .filter(Boolean);
    if (!entries.length) {
      return Array.from(dataTransfer.files).map((file) => ({ file, path: file.name }));
    }
    const collected = [];
    async function walk(entry, prefix = "") {
      if (entry.isFile) {
        const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
        collected.push({ file, path: prefix + entry.name });
        return;
      }
      if (entry.isDirectory) {
        const reader = entry.createReader();
        const children = [];
        while (true) {
          const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
          if (!batch.length) break;
          children.push(...batch);
        }
        for (const child of children) await walk(child, prefix + entry.name + "/");
      }
    }
    for (const entry of entries) await walk(entry);
    return collected;
  }

  async function collectDirectoryHandle(handle, prefix = "") {
    const result = [];
    for await (const entry of handle.values()) {
      if (entry.kind === "file") {
        result.push({ file: await entry.getFile(), path: prefix + entry.name });
      } else if (entry.kind === "directory") {
        // Marcador de carpeta (file: null) para que también se vean las vacías.
        result.push({ file: null, path: prefix + entry.name + "/" });
        result.push(...await collectDirectoryHandle(entry, prefix + entry.name + "/"));
      }
    }
    return result;
  }

  async function loadServerFiles(path = serverPath) {
    serverBody.innerHTML = "";
    serverEmpty.hidden = false;
    serverEmpty.textContent = "Cargando…";
    const { data } = await apiFetch(`/api/files?path=${encodeURIComponent(path || "")}`);
    if (!data.ok) {
      if (path === DEFAULT_SERVER_PATH) return loadServerFiles("");
      serverEmpty.textContent = data.error || "No se pudo abrir el servidor.";
      return;
    }
    serverPath = data.path || "";
    document.getElementById("ftp-server-path").textContent = "/" + serverPath;
    serverEmpty.hidden = data.entries.length > 0;
    serverEmpty.textContent = "Carpeta vacía. Arrastra aquí archivos del cliente.";
    data.entries.forEach((entry) => {
      const row = document.createElement("tr");
      const fullPath = serverPath ? serverPath + "/" + entry.name : entry.name;
      row.draggable = true;
      row.innerHTML = `<td>${entry.is_dir ? "📁" : "📄"}</td><td></td><td>${entry.is_dir ? "" : formatSize(entry.size)}</td><td>${entry.modified ? formatDate(entry.modified) : ""}</td>`;
      row.children[1].textContent = entry.name;
      if (entry.is_dir) {
        row.children[1].className = "fm-link";
        row.children[1].addEventListener("dblclick", () => loadServerFiles(fullPath));
      }
      appendRowActions(row, entry.is_dir, () => editServerFile(fullPath), () => deleteServerEntry(fullPath, entry.is_dir));
      row.addEventListener("dragstart", (event) => {
        event.dataTransfer.effectAllowed = "copy";
        event.dataTransfer.setData("application/x-rdweb-server-file", JSON.stringify({ path: fullPath, is_dir: entry.is_dir, size: entry.size }));
      });
      serverBody.appendChild(row);
    });
  }

  async function uploadClientItems(items) {
    items = items.filter((item) => item.file);
    if (!items.length) return;
    const form = new FormData();
    form.append("path", serverPath);
    items.forEach((item) => {
      form.append("files", item.file, item.file.name);
      form.append("relative_paths", item.path);
    });
    const totalBytes = items.reduce((sum, item) => sum + (item.file?.size || 0), 0);
    beginTransfer(`Subiendo ${items.length} archivo(s)…`);
    try {
      await uploadWithProgress(form, totalBytes, `Subiendo ${items.length} archivo(s)`);
      endTransfer("Transferencia al servidor completada.");
      loadServerFiles();
    } catch (error) {
      endTransfer(error.message || "No se pudo completar la subida.", "error");
    }
  }

  // Lista recursivamente los archivos de un elemento del servidor como
  // [{ serverPath, path }], con `path` relativo a la carpeta de destino.
  async function collectServerFiles(item, relative) {
    if (!item.is_dir) return [{ serverPath: item.path, path: relative, size: item.size || 0 }];
    const { data } = await apiFetch(`/api/files?path=${encodeURIComponent(item.path)}`);
    if (!data.ok) throw new Error(data.error || "No se pudo leer la carpeta del servidor.");
    const result = [];
    for (const entry of data.entries) {
      result.push(...await collectServerFiles(
        { path: item.path + "/" + entry.name, is_dir: entry.is_dir, size: entry.size }, relative + "/" + entry.name));
    }
    return result;
  }

  async function writeToClientHandle(relativePath, blob) {
    const parts = relativePath.split("/");
    let dir = clientDirHandle;
    for (const part of parts.slice(0, -1)) dir = await dir.getDirectoryHandle(part, { create: true });
    const fileHandle = await dir.getFileHandle(parts[parts.length - 1], { create: true });
    const writable = await fileHandle.createWritable();
    await writable.write(blob);
    await writable.close();
    const existing = clientFiles.findIndex((entry) => entry.path === relativePath);
    const record = { file: await fileHandle.getFile(), path: relativePath };
    if (existing === -1) clientFiles.push(record); else clientFiles[existing] = record;
  }

  // Copia un archivo o carpeta del servidor a la carpeta abierta en el panel cliente.
  async function downloadToClient(item) {
    if (nativeClientFs && !nativeClientPath) throw new Error("Abre primero una carpeta del cliente.");
    if (!nativeClientFs) {
      const permission = await clientDirHandle.requestPermission({ mode: "readwrite" });
      if (permission !== "granted") throw new Error("Permiso de escritura denegado.");
    }
    const files = await collectServerFiles(item, item.path.split("/").pop());
    const totalBytes = files.reduce((sum, file) => sum + (file.size || 0), 0);
    let transferredBytes = 0;
    beginTransfer(`Descargando ${files.length} archivo(s)…`);
    for (let i = 0; i < files.length; i++) {
      ftpTransferLog.textContent = `Descargando al cliente ${i + 1}/${files.length}: ${files[i].path}`;
      ftpTransferLog.className = "ftp-transfer-log";
      const resp = await fetch(`/api/files/download?path=${encodeURIComponent(files[i].serverPath)}`);
      if (!resp.ok) throw new Error(`No se pudo descargar ${files[i].serverPath}.`);
      const reader = resp.body?.getReader();
      const chunks = [];
      let fileLoaded = 0;
      if (reader) {
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          fileLoaded += value.byteLength;
          updateTransferProgress(transferredBytes + fileLoaded, totalBytes,
            `Archivo ${i + 1}/${files.length}: ${files[i].path}`);
        }
      }
      const blob = reader ? new Blob(chunks) : await resp.blob();
      if (!reader) fileLoaded = blob.size;
      if (nativeClientFs) {
        await window.desktopRemote.writeClientFile(nativeClientPath, files[i].path, new Uint8Array(await blob.arrayBuffer()));
      } else {
        await writeToClientHandle((clientPath ? clientPath + "/" : "") + files[i].path, blob);
      }
      transferredBytes += fileLoaded;
    }
    endTransfer(`${files.length} archivo(s) guardados en la carpeta del cliente.`);
    if (nativeClientFs) loadClientBrowser(); else renderClientFiles();
  }

  function downloadToBrowser(item) {
    const url = item.is_dir
      ? `/api/files/download_zip?path=${encodeURIComponent(item.path)}`
      : `/api/files/download?path=${encodeURIComponent(item.path)}`;
    const baseName = item.path.split("/").pop() || "carpeta";
    const downloadName = item.is_dir ? `${baseName}.zip` : baseName;
    beginTransfer(item.is_dir ? `Preparando ${downloadName}…` : `Descargando ${downloadName}…`);
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("GET", url);
      xhr.responseType = "blob";
      xhr.addEventListener("progress", (event) => {
        updateTransferProgress(event.loaded, event.lengthComputable ? event.total : 0, `Descargando ${downloadName}`);
      });
      xhr.addEventListener("load", () => {
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new Error(`No se pudo descargar ${downloadName}.`));
          return;
        }
        const objectUrl = URL.createObjectURL(xhr.response);
        const link = document.createElement("a");
        link.href = objectUrl;
        link.download = downloadName;
        link.click();
        window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
        endTransfer(`Descarga completada: ${downloadName}.`);
        resolve();
      });
      xhr.addEventListener("error", () => reject(new Error("Se perdió la conexión durante la descarga.")));
      xhr.send();
    });
  }

  // ---- Crear / editar / eliminar en el servidor ----
  async function editServerFile(fullPath) {
    await runFileAction(async () => {
      const { data } = await apiFetch(`/api/files/text?path=${encodeURIComponent(fullPath)}`);
      if (!data.ok) throw new Error(data.error);
      openTextEditor("🖥️ " + fullPath, data.content, async (content) => {
        const { data: saved } = await apiFetch("/api/files/text", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: fullPath, content }),
        });
        if (!saved.ok) throw new Error(saved.error);
        logTransfer(`Guardado en el servidor: ${fullPath}`, "ok");
        loadServerFiles();
      });
    });
  }

  async function deleteServerEntry(fullPath, isDir) {
    if (!confirm(`¿Eliminar ${isDir ? "la carpeta" : "el archivo"} «${fullPath}» del servidor?${isDir ? " Se borrará todo su contenido." : ""}`)) return;
    await runFileAction(async () => {
      const { data } = await apiFetch("/api/files/delete", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: fullPath }),
      });
      if (!data.ok) throw new Error(data.error);
      loadServerFiles();
    }, `Eliminado del servidor: ${fullPath}`);
  }

  async function createServerFile() {
    const name = prompt("Nombre del nuevo archivo:");
    if (!name) return;
    const fullPath = serverPath ? serverPath + "/" + name : name;
    await runFileAction(async () => {
      const { data } = await apiFetch("/api/files/text", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: fullPath, content: "", create: true }),
      });
      if (!data.ok) throw new Error(data.error);
      await loadServerFiles();
      editServerFile(fullPath);
    }, `Archivo creado en el servidor: ${fullPath}`);
  }

  // ---- Crear / editar / eliminar en el cliente ----
  const READONLY_CLIENT_ERROR = "Este navegador no permite modificar archivos de tu equipo. "
    + "Elige la carpeta con «Seleccionar carpeta» en Chrome/Edge o usa la aplicación nativa.";

  function joinNative(dir, name) {
    const sep = dir.includes("\\") ? "\\" : "/";
    return dir.endsWith(sep) ? dir + name : dir + sep + name;
  }

  async function editNativeFile(entry) {
    await runFileAction(async () => {
      const content = decodeText(await window.desktopRemote.readClientFile(entry.path));
      openTextEditor("💻 " + entry.path, content, async (text) => {
        await window.desktopRemote.saveClientFile(entry.path, new TextEncoder().encode(text), false);
        logTransfer(`Guardado en el cliente: ${entry.path}`, "ok");
        loadClientBrowser();
      });
    });
  }

  async function deleteNativeEntry(entry) {
    if (!confirm(`¿Eliminar ${entry.is_dir ? "la carpeta" : "el archivo"} «${entry.path}» de este equipo?${entry.is_dir ? " Se borrará todo su contenido." : ""}`)) return;
    await runFileAction(async () => {
      await window.desktopRemote.deleteClientPath(entry.path);
      loadClientBrowser();
    }, `Eliminado del cliente: ${entry.path}`);
  }

  async function handleDirFor(relativeDir, create = false) {
    let dir = clientDirHandle;
    for (const part of relativeDir.split("/").filter(Boolean)) dir = await dir.getDirectoryHandle(part, { create });
    return dir;
  }

  async function ensureClientWritable() {
    if (!clientDirHandle) throw new Error(READONLY_CLIENT_ERROR);
    const permission = await clientDirHandle.requestPermission({ mode: "readwrite" });
    if (permission !== "granted") throw new Error("Permiso de escritura denegado.");
  }

  async function editHandleFile(relativePath) {
    await runFileAction(async () => {
      await ensureClientWritable();
      const parts = relativePath.split("/");
      const fileHandle = await (await handleDirFor(parts.slice(0, -1).join("/"))).getFileHandle(parts[parts.length - 1]);
      const content = decodeText(await (await fileHandle.getFile()).arrayBuffer());
      openTextEditor("💻 " + clientRootLabel + "/" + relativePath, content, async (text) => {
        await writeToClientHandle(relativePath, new Blob([text]));
        logTransfer(`Guardado en el cliente: ${relativePath}`, "ok");
        renderClientFiles();
      });
    });
  }

  async function deleteHandleEntry(relativePath, isDir) {
    if (!clientDirHandle) {
      logTransfer(READONLY_CLIENT_ERROR, "error");
      return;
    }
    if (!confirm(`¿Eliminar ${isDir ? "la carpeta" : "el archivo"} «${relativePath}» de tu equipo?${isDir ? " Se borrará todo su contenido." : ""}`)) return;
    await runFileAction(async () => {
      await ensureClientWritable();
      const parts = relativePath.split("/");
      await (await handleDirFor(parts.slice(0, -1).join("/"))).removeEntry(parts[parts.length - 1], { recursive: true });
      for (let i = clientFiles.length - 1; i >= 0; i--) {
        const itemPath = clientFiles[i].path;
        if (itemPath === relativePath || itemPath.startsWith(relativePath + "/")) clientFiles.splice(i, 1);
      }
      renderClientFiles();
    }, `Eliminado del cliente: ${relativePath}`);
  }

  async function createClientEntry(isDir) {
    if (nativeClientFs ? !nativeClientPath : !clientDirHandle) {
      logTransfer(nativeClientFs ? "Abre primero una carpeta del cliente." : READONLY_CLIENT_ERROR, "error");
      return;
    }
    const name = prompt(isDir ? "Nombre de la carpeta:" : "Nombre del nuevo archivo:");
    if (!name) return;
    if (/[\\/]/.test(name) || name === "." || name === "..") {
      logTransfer("Nombre no válido.", "error");
      return;
    }
    await runFileAction(async () => {
      if (nativeClientFs) {
        const target = joinNative(nativeClientPath, name);
        if (isDir) await window.desktopRemote.makeClientDirectory(nativeClientPath, name);
        else await window.desktopRemote.saveClientFile(target, new Uint8Array(), true);
        await loadClientBrowser();
        if (!isDir) editNativeFile({ path: target, name, is_dir: false });
        return;
      }
      await ensureClientWritable();
      const relativePath = (clientPath ? clientPath + "/" : "") + name;
      if (clientFiles.some((item) => item.path === relativePath || item.path.startsWith(relativePath + "/"))) {
        throw new Error("Ya existe un archivo o carpeta con ese nombre.");
      }
      if (isDir) {
        await handleDirFor(relativePath, true);
        clientFiles.push({ file: null, path: relativePath + "/" });
        renderClientFiles();
      } else {
        await writeToClientHandle(relativePath, new Blob([""]));
        renderClientFiles();
        editHandleFile(relativePath);
      }
    }, `${isDir ? "Carpeta creada" : "Archivo creado"} en el cliente: ${name}`);
  }

  document.getElementById("client-new-file").addEventListener("click", () => createClientEntry(false));
  document.getElementById("client-new-folder").addEventListener("click", () => createClientEntry(true));
  document.getElementById("ftp-server-newfile").addEventListener("click", createServerFile);

  const clientDropZone = document.getElementById("client-drop-zone");
  const serverDropZone = document.getElementById("server-drop-zone");
  [clientDropZone, serverDropZone].forEach((zone) => {
    zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("drag-over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag-over"));
  });
  clientDropZone.addEventListener("drop", async (event) => {
    event.preventDefault(); clientDropZone.classList.remove("drag-over");
    const remote = event.dataTransfer.getData("application/x-rdweb-server-file");
    if (remote) {
      const item = JSON.parse(remote);
      if (!ftpAgentSelect.value) {
        endTransfer("Selecciona primero el agente local que recibirá la descarga FTP.", "error");
        return;
      }
      startAgentFtpDownload(item);
    } else {
      clientFiles.push(...await collectDroppedItems(event.dataTransfer));
      renderClientFiles();
    }
  });
  serverDropZone.addEventListener("drop", async (event) => {
    event.preventDefault(); serverDropZone.classList.remove("drag-over");
    const nativeItem = event.dataTransfer.getData("application/x-rdweb-native-file");
    if (nativeItem) {
      ftpTransferLog.textContent = "Leyendo archivos del cliente…";
      uploadClientItems(await collectNativeUpload(JSON.parse(nativeItem)));
      return;
    }
    const index = event.dataTransfer.getData("application/x-rdweb-client-file");
    if (index !== "") uploadClientItems(JSON.parse(index).map((itemIndex) => clientFiles[itemIndex]));
    else uploadClientItems(await collectDroppedItems(event.dataTransfer));
  });

  const filesInput = document.getElementById("client-files-input");
  const folderInput = document.getElementById("client-folder-input");
  document.getElementById("client-select-files").addEventListener("click", () => filesInput.click());
  document.getElementById("client-select-folder").addEventListener("click", async () => {
    if (window.showDirectoryPicker) {
      try {
        const handle = await window.showDirectoryPicker({ mode: "readwrite" });
        clientDirHandle = handle;
        clientRootLabel = handle.name;
        clientPath = "";
        clientFiles.length = 0;
        clientFiles.push(...await collectDirectoryHandle(handle));
        renderClientFiles();
        ftpTransferLog.textContent = `${clientFiles.filter((item) => item.file).length} archivo(s) cargados desde ${handle.name}.`;
        ftpTransferLog.className = "ftp-transfer-log ok";
      } catch (error) {
        if (error.name !== "AbortError") {
          ftpTransferLog.textContent = `No se pudo abrir la carpeta: ${error.message}`;
          ftpTransferLog.className = "ftp-transfer-log error";
        }
      }
    } else {
      folderInput.click();
    }
  });
  // En el navegador, pulsar el panel vacío abre directamente el selector de carpeta.
  clientEmpty.addEventListener("click", () => {
    if (!ftpAgentSelect.value && !nativeClientFs && !clientFiles.length) document.getElementById("client-select-folder").click();
  });
  document.getElementById("client-upload-all").addEventListener("click", () => uploadClientItems(clientFiles));
  filesInput.addEventListener("change", () => { addClientFiles(filesInput.files); filesInput.value = ""; });
  folderInput.addEventListener("change", () => {
    if (!folderInput.files.length) {
      ftpTransferLog.textContent = "No se seleccionó ninguna carpeta.";
      ftpTransferLog.className = "ftp-transfer-log error";
      return;
    }
    clientDirHandle = null;
    const firstPath = folderInput.files[0].webkitRelativePath || folderInput.files[0].name;
    clientRootLabel = firstPath.split("/")[0] || "Cliente";
    clientPath = "";
    clientFiles.length = 0;
    Array.from(folderInput.files).forEach((file) => {
      const parts = (file.webkitRelativePath || file.name).split("/");
      clientFiles.push({ file, path: parts.length > 1 ? parts.slice(1).join("/") : parts[0] });
    });
    renderClientFiles();
    ftpTransferLog.textContent = `${clientFiles.length} archivo(s) cargados desde ${clientRootLabel}.`;
    ftpTransferLog.className = "ftp-transfer-log ok";
    folderInput.value = "";
  });
  document.getElementById("client-folder-up").addEventListener("click", () => {
    if (nativeClientFs) {
      if (nativeClientParent !== null) loadClientBrowser(nativeClientParent);
    }
    else {
      clientPath = clientPath.split("/").slice(0, -1).join("/");
      renderClientFiles();
    }
  });
  document.getElementById("ftp-server-refresh").addEventListener("click", () => loadServerFiles());
  document.getElementById("ftp-server-up").addEventListener("click", () => {
    serverPath = serverPath.split("/").slice(0, -1).join("/"); loadServerFiles();
  });
  document.getElementById("ftp-server-mkdir").addEventListener("click", async () => {
    const name = prompt("Nombre de la carpeta:");
    if (!name) return;
    const { data } = await apiFetch("/api/files/mkdir", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path: serverPath, name }),
    });
    ftpTransferLog.textContent = data.ok ? "Carpeta creada." : data.error;
    ftpTransferLog.className = "ftp-transfer-log " + (data.ok ? "ok" : "error");
    if (data.ok) loadServerFiles();
  });

  // ---------------- Ajustes de transmisión ----------------
  const fpsInput = document.getElementById("set-fps");
  const qualityInput = document.getElementById("set-quality");
  const maxWidthInput = document.getElementById("set-maxwidth");
  const streamMsg = document.getElementById("stream-msg");

  document.getElementById("save-stream-btn").addEventListener("click", async () => {
    const { data } = await apiFetch("/api/settings/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        fps: Number(fpsInput.value),
        jpeg_quality: Number(qualityInput.value),
        max_width: Number(maxWidthInput.value),
      }),
    });
    streamMsg.textContent = data.ok ? "Ajustes guardados y aplicados." : data.error;
    streamMsg.className = "form-msg " + (data.ok ? "ok" : "error");
  });

  // ---------------- Credenciales ----------------
  const credUsername = document.getElementById("cred-username");
  const credMsg = document.getElementById("cred-msg");

  document.getElementById("save-credentials-btn").addEventListener("click", async () => {
    const currentPassword = document.getElementById("cred-current").value;
    const newPassword = document.getElementById("cred-new").value;
    const newPasswordConfirm = document.getElementById("cred-new-confirm").value;

    const { data } = await apiFetch("/api/settings/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_password: currentPassword,
        username: credUsername.value,
        new_password: newPassword,
        new_password_confirm: newPasswordConfirm,
      }),
    });
    credMsg.textContent = data.ok ? "Credenciales actualizadas correctamente." : data.error;
    credMsg.className = "form-msg " + (data.ok ? "ok" : "error");
    if (data.ok) {
      document.getElementById("cred-current").value = "";
      document.getElementById("cred-new").value = "";
      document.getElementById("cred-new-confirm").value = "";
    }
  });

  // ---------------- Acceso directo mediante router ----------------
  const directFields = {
    enabled: document.getElementById("direct-enabled"),
    protocol: document.getElementById("direct-protocol"),
    publicIp: document.getElementById("direct-public-ip"),
    publicPort: document.getElementById("direct-public-port"),
    localIp: document.getElementById("direct-local-ip"),
    localPort: document.getElementById("direct-local-port"),
  };
  const directRule = document.getElementById("direct-rule");
  const directMsg = document.getElementById("direct-access-msg");

  function updateDirectRule() {
    const publicIp = directFields.publicIp.value.trim() || "IP_PÚBLICA";
    const localIp = directFields.localIp.value.trim() || "IP_LOCAL";
    const publicPort = directFields.publicPort.value || "5000";
    const localPort = directFields.localPort.value || "5000";
    directRule.textContent = `Regla del router: TCP ${publicIp}:${publicPort} → ${localIp}:${localPort}\nURL: ${directFields.protocol.value}://${publicIp}:${publicPort}`;
    directRule.style.whiteSpace = "pre-line";
  }
  Object.values(directFields).forEach((field) => field.addEventListener("input", updateDirectRule));
  document.getElementById("save-direct-access-btn").addEventListener("click", async () => {
    directMsg.textContent = "Guardando…";
    directMsg.className = "form-msg";
    const { data } = await apiFetch("/api/settings/direct-access", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        direct_access_enabled: directFields.enabled.checked,
        direct_access_protocol: directFields.protocol.value,
        direct_public_ip: directFields.publicIp.value,
        direct_public_port: Number(directFields.publicPort.value),
        direct_local_ip: directFields.localIp.value,
        direct_local_port: Number(directFields.localPort.value),
      }),
    });
    directMsg.textContent = data.ok ? `Guardado. Configura en el router: ${data.rule}` : data.error;
    directMsg.className = "form-msg " + (data.ok ? "ok" : "error");
    if (data.ok) updateDirectRule();
  });

  // ---------------- Cloudflare: opciones avanzadas ----------------
  const cfFields = {
    tunnel: document.getElementById("cf-tunnel"),
    config: document.getElementById("cf-config"),
    binary: document.getElementById("cf-binary"),
    restart: document.getElementById("cf-restart-method"),
    service: document.getElementById("cf-service"),
  };
  const cfSettingsMsg = document.getElementById("cf-settings-msg");

  // ---------------- FTP híbrido ----------------
  const ftpMode = document.getElementById("ftp-mode");
  const ftpServerSettings = document.getElementById("ftp-server-settings");
  const ftpClientSettings = document.getElementById("ftp-client-settings");
  const ftpSettingsMsg = document.getElementById("ftp-settings-msg");
  const ftpFields = {
    serverEnabled: document.getElementById("ftp-server-enabled"),
    serverHost: document.getElementById("ftp-server-host"),
    serverPublicHost: document.getElementById("ftp-server-public-host"),
    serverPort: document.getElementById("ftp-server-port"),
    serverUsername: document.getElementById("ftp-server-username"),
    serverPassword: document.getElementById("ftp-server-password"),
    serverRoot: document.getElementById("ftp-server-root"),
    clientHost: document.getElementById("ftp-client-host"),
    clientPort: document.getElementById("ftp-client-port"),
    clientUsername: document.getElementById("ftp-client-username"),
    clientPassword: document.getElementById("ftp-client-password"),
    clientTls: document.getElementById("ftp-client-tls"),
    clientRemotePath: document.getElementById("ftp-client-remote-path"),
  };

  function showFtpMode() {
    const serverMode = ftpMode.value === "server";
    ftpServerSettings.hidden = !serverMode;
    ftpClientSettings.hidden = serverMode;
  }
  ftpMode.addEventListener("change", showFtpMode);

  document.getElementById("save-ftp-settings-btn").addEventListener("click", async () => {
    ftpSettingsMsg.textContent = "Aplicando configuración…";
    ftpSettingsMsg.className = "form-msg";
    const { data } = await apiFetch("/api/settings/ftp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ftp_mode: ftpMode.value,
        ftp_server_enabled: ftpFields.serverEnabled.checked,
        ftp_server_host: ftpFields.serverHost.value,
        ftp_server_public_host: ftpFields.serverPublicHost.value,
        ftp_server_port: Number(ftpFields.serverPort.value),
        ftp_server_username: ftpFields.serverUsername.value,
        ftp_server_password: ftpFields.serverPassword.value,
        ftp_server_root: ftpFields.serverRoot.value,
        ftp_client_host: ftpFields.clientHost.value,
        ftp_client_port: Number(ftpFields.clientPort.value),
        ftp_client_username: ftpFields.clientUsername.value,
        ftp_client_password: ftpFields.clientPassword.value,
        ftp_client_tls: ftpFields.clientTls.checked,
        ftp_client_remote_path: ftpFields.clientRemotePath.value,
      }),
    });
    ftpSettingsMsg.textContent = data.ok ? "Configuración FTP guardada y aplicada." : data.error;
    ftpSettingsMsg.className = "form-msg " + (data.ok ? "ok" : "error");
    if (data.ok) {
      ftpFields.serverPassword.value = "";
      ftpFields.clientPassword.value = "";
    }
  });

  async function loadFtpStatus() {
    const status = document.getElementById("ftp-download-status");
    const details = document.getElementById("ftp-server-details");
    const { data } = await apiFetch("/api/ftp/status");
    if (!data.ok) {
      status.textContent = data.error || "No se pudo consultar FTP.";
      status.className = "info-banner error";
      return;
    }
    if (data.mode === "client") {
      status.textContent = "Modo cliente: la descarga se obtendrá desde el servidor FTP configurado.";
      details.hidden = true;
    } else {
      status.textContent = data.server.running
        ? "Modo servidor: el paquete Windows está publicado por FTP."
        : "Modo servidor configurado, pero el servicio FTP no está activo.";
      details.textContent = `Dirección: ${data.ftp_url}\nUsuario: ${data.username}`;
      details.style.whiteSpace = "pre-line";
      details.hidden = false;
    }
  }

  document.getElementById("save-cf-settings-btn").addEventListener("click", async () => {
    const { data } = await apiFetch("/api/settings/cloudflare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        cloudflare_tunnel: cfFields.tunnel.value,
        cloudflare_config_path: cfFields.config.value,
        cloudflare_binary: cfFields.binary.value,
        cloudflare_restart_method: cfFields.restart.value,
        cloudflare_service_name: cfFields.service.value,
      }),
    });
    cfSettingsMsg.textContent = data.ok ? "Opciones guardadas." : data.error;
    cfSettingsMsg.className = "form-msg " + (data.ok ? "ok" : "error");
  });

  // ---------------- Cloudflare: generar subdominio ----------------
  const cfLog = document.getElementById("cf-log");

  document.getElementById("cf-generate-btn").addEventListener("click", async () => {
    const hostname = document.getElementById("cf-hostname").value.trim();
    if (!hostname) {
      cfLog.innerHTML = '<div class="cf-step error">Introduce un subdominio.</div>';
      return;
    }
    cfLog.innerHTML = '<div class="cf-step">Generando y configurando…</div>';
    const { data } = await apiFetch("/api/cloudflare/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hostname }),
    });

    const lines = (data.steps || []).map(
      (s) => `<div class="cf-step ${s.ok ? "ok" : "error"}">${s.ok ? "✅" : "❌"} ${s.step}: ${escapeHtml(s.message)}</div>`
    );
    if (data.ok && data.url) {
      lines.push(`<div class="cf-step ok">🎉 Listo: <a href="${data.url}" target="_blank">${data.url}</a></div>`);
    }
    cfLog.innerHTML = lines.join("");
  });

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  async function loadSettings() {
    const { data } = await apiFetch("/api/settings");
    if (!data.ok) return;
    const s = data.settings;
    fpsInput.value = s.fps;
    qualityInput.value = s.jpeg_quality;
    maxWidthInput.value = s.max_width;
    credUsername.value = s.username;
    directFields.enabled.checked = Boolean(s.direct_access_enabled);
    directFields.protocol.value = s.direct_access_protocol || "https";
    directFields.publicIp.value = s.direct_public_ip || "";
    directFields.publicPort.value = s.direct_public_port || 5000;
    directFields.localIp.value = s.direct_local_ip || "";
    directFields.localPort.value = s.direct_local_port || s.port || 5000;
    updateDirectRule();
    cfFields.tunnel.value = s.cloudflare_tunnel || "";
    cfFields.config.value = s.cloudflare_config_path || "";
    cfFields.binary.value = s.cloudflare_binary || "";
    cfFields.restart.value = s.cloudflare_restart_method || "manual";
    cfFields.service.value = s.cloudflare_service_name || "";
    ftpMode.value = "server";
    ftpFields.serverEnabled.checked = Boolean(s.ftp_server_enabled);
    ftpFields.serverHost.value = s.ftp_server_host || "0.0.0.0";
    ftpFields.serverPublicHost.value = s.ftp_server_public_host || "";
    ftpFields.serverPort.value = s.ftp_server_port || 2121;
    ftpFields.serverUsername.value = s.ftp_server_username || "remote";
    ftpFields.serverRoot.value = s.ftp_server_root || "";
    ftpFields.clientHost.value = s.ftp_client_host || "";
    ftpFields.clientPort.value = s.ftp_client_port || 21;
    ftpFields.clientUsername.value = s.ftp_client_username || "anonymous";
    ftpFields.clientTls.checked = Boolean(s.ftp_client_tls);
    ftpFields.clientRemotePath.value = s.ftp_client_remote_path || "escritorio-remoto-windows.zip";
    ftpFields.serverPassword.placeholder = s.ftp_server_password_configured ? "Configurada (vacío = conservar)" : "Obligatoria para activar";
    ftpFields.clientPassword.placeholder = s.ftp_client_password_configured ? "Configurada (vacío = conservar)" : "Vacía";
    showFtpMode();
  }

  // ---------------- Gestor de archivos ----------------
  let currentFmPath = "";
  const fmBreadcrumb = document.getElementById("fm-breadcrumb");
  const fmTableBody = document.getElementById("fm-table-body");
  const fmMsg = document.getElementById("fm-msg");

  function formatSize(bytes) {
    if (bytes < 1024) return bytes + " B";
    const units = ["KB", "MB", "GB", "TB"];
    let value = bytes;
    let i = -1;
    do {
      value /= 1024;
      i++;
    } while (value >= 1024 && i < units.length - 1);
    return value.toFixed(1) + " " + units[i];
  }

  function formatDate(epochSeconds) {
    return new Date(epochSeconds * 1000).toLocaleString();
  }

  async function loadFiles(path) {
    const { data } = await apiFetch("/api/files?path=" + encodeURIComponent(path || ""));
    if (!data.ok) {
      fmMsg.textContent = data.error || "No se pudo listar la carpeta.";
      fmMsg.className = "form-msg error";
      return;
    }
    fmMsg.textContent = "";
    currentFmPath = data.path || "";
    fmBreadcrumb.textContent = "/" + currentFmPath;

    fmTableBody.innerHTML = "";

    if (currentFmPath) {
      const upRow = document.createElement("tr");
      upRow.innerHTML = '<td>📁</td><td class="fm-link">..</td><td></td><td></td><td></td>';
      upRow.querySelector(".fm-link").addEventListener("click", () => {
        const parent = currentFmPath.split("/").slice(0, -1).join("/");
        loadFiles(parent);
      });
      fmTableBody.appendChild(upRow);
    }

    data.entries.forEach((entry) => {
      const row = document.createElement("tr");
      const icon = entry.is_dir ? "📁" : "📄";
      const childPath = currentFmPath ? currentFmPath + "/" + entry.name : entry.name;

      const nameCell = document.createElement("td");
      nameCell.textContent = entry.name;
      if (entry.is_dir) {
        nameCell.className = "fm-link";
        nameCell.addEventListener("click", () => loadFiles(childPath));
      }

      const actionsCell = document.createElement("td");
      if (!entry.is_dir) {
        const dlBtn = document.createElement("button");
        dlBtn.textContent = "Descargar";
        dlBtn.addEventListener("click", () => {
          window.location.href = "/api/files/download?path=" + encodeURIComponent(childPath);
        });
        actionsCell.appendChild(dlBtn);
      } else {
        const dlZipBtn = document.createElement("button");
        dlZipBtn.textContent = "Descargar .zip";
        dlZipBtn.addEventListener("click", () => {
          window.location.href = "/api/files/download_zip?path=" + encodeURIComponent(childPath);
        });
        actionsCell.appendChild(dlZipBtn);
      }
      const delBtn = document.createElement("button");
      delBtn.textContent = "Borrar";
      delBtn.className = "danger";
      delBtn.addEventListener("click", async () => {
        if (!confirm(`¿Borrar "${entry.name}"? Esta acción no se puede deshacer.`)) return;
        const { data: delData } = await apiFetch("/api/files/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: childPath }),
        });
        if (!delData.ok) {
          fmMsg.textContent = delData.error;
          fmMsg.className = "form-msg error";
        }
        loadFiles(currentFmPath);
      });
      actionsCell.appendChild(delBtn);

      row.appendChild(document.createElement("td")).textContent = icon;
      row.appendChild(nameCell);
      row.appendChild(document.createElement("td")).textContent = entry.is_dir ? "" : formatSize(entry.size);
      row.appendChild(document.createElement("td")).textContent = formatDate(entry.modified);
      row.appendChild(actionsCell);
      fmTableBody.appendChild(row);
    });
  }

  document.getElementById("fm-refresh-btn").addEventListener("click", () => loadFiles(currentFmPath));

  document.getElementById("fm-download-folder-btn").addEventListener("click", () => {
    window.location.href = "/api/files/download_zip?path=" + encodeURIComponent(currentFmPath);
  });

  document.getElementById("fm-mkdir-btn").addEventListener("click", async () => {
    const name = prompt("Nombre de la nueva carpeta:");
    if (!name) return;
    const { data } = await apiFetch("/api/files/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentFmPath, name }),
    });
    if (!data.ok) {
      fmMsg.textContent = data.error;
      fmMsg.className = "form-msg error";
    }
    loadFiles(currentFmPath);
  });

  const fmUploadInput = document.getElementById("fm-upload-input");
  document.getElementById("fm-upload-btn").addEventListener("click", () => fmUploadInput.click());
  fmUploadInput.addEventListener("change", async () => {
    if (!fmUploadInput.files.length) return;
    const formData = new FormData();
    formData.append("path", currentFmPath);
    Array.from(fmUploadInput.files).forEach((f) => formData.append("files", f));
    fmMsg.textContent = "Subiendo…";
    fmMsg.className = "form-msg";
    const { data } = await apiFetch("/api/files/upload", { method: "POST", body: formData });
    fmMsg.textContent = data.ok ? `Subido(s): ${data.saved.join(", ")}` : data.error;
    fmMsg.className = "form-msg " + (data.ok ? "ok" : "error");
    fmUploadInput.value = "";
    loadFiles(currentFmPath);
  });
})();

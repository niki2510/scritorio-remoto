const { app, BrowserWindow, Menu, ipcMain, shell } = require("electron");
const fs = require("fs");
const path = require("path");
const os = require("os");
const dgram = require("dgram");
const { execFile } = require("child_process");

const DEFAULT_SERVER = "http://localhost:5000";
let mainWindow;
let serverUrl = DEFAULT_SERVER;

function settingsPath() {
  return path.join(app.getPath("userData"), "settings.json");
}

function loadSavedServer() {
  try {
    return normalizeServerUrl(JSON.parse(fs.readFileSync(settingsPath(), "utf8")).serverUrl);
  } catch (_error) {
    return DEFAULT_SERVER;
  }
}

function saveServer() {
  fs.mkdirSync(path.dirname(settingsPath()), { recursive: true });
  fs.writeFileSync(settingsPath(), JSON.stringify({ serverUrl }, null, 2));
}

function trustedRenderer(event) {
  const rendererUrl = event.senderFrame.url;
  if (rendererUrl.startsWith("file:")) return true;
  try {
    return new URL(rendererUrl).origin === new URL(serverUrl).origin;
  } catch (_error) {
    return false;
  }
}

function commandOutput(command, args) {
  return new Promise((resolve) => execFile(command, args, { timeout: 2000 }, (error, stdout) => resolve(error ? "" : stdout)));
}

// IP del adaptador por el que sale el tráfico (ruta por defecto). Conectar un
// socket UDP no envía ningún paquete; solo hace que el sistema elija la ruta.
function defaultRouteIp() {
  return new Promise((resolve) => {
    const socket = dgram.createSocket("udp4");
    const finish = (ip) => { try { socket.close(); } catch (_error) {} resolve(ip); };
    socket.on("error", () => finish(""));
    socket.connect(53, "8.8.8.8", () => {
      try { finish(socket.address().address); } catch (_error) { finish(""); }
    });
  });
}

const VIRTUAL_ADAPTER = /vethernet|virtualbox|vmware|hyper-v|wsl|docker|^br-|^veth|^virbr|loopback|tailscale|zerotier/i;
const PRIVATE_IPV4 = /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/;

async function localIpAddress() {
  const candidates = Object.entries(os.networkInterfaces())
    .flatMap(([name, items]) => (items || []).map((item) => ({ name, ...item })))
    .filter((item) => item.family === "IPv4" && !item.internal && !item.address.startsWith("169.254."));
  const routed = await defaultRouteIp();
  if (routed && candidates.some((item) => item.address === routed)) return routed;
  const physical = candidates.filter((item) => !VIRTUAL_ADAPTER.test(item.name));
  return (physical.find((item) => PRIVATE_IPV4.test(item.address)) || physical[0] || candidates[0])?.address || "";
}

async function networkInfo() {
  const localIp = await localIpAddress();
  let wifiName = "";
  if (process.platform === "win32") {
    const output = await commandOutput("netsh", ["wlan", "show", "interfaces"]);
    wifiName = output.match(/^\s*SSID\s*:\s*(.+)$/mi)?.[1]?.trim() || "";
  } else if (process.platform === "linux") {
    const output = await commandOutput("nmcli", ["-t", "-f", "active,ssid", "dev", "wifi"]);
    wifiName = output.split("\n").find((line) => line.startsWith("yes:"))?.slice(4) || "";
  }
  return { local_ip: localIp, wifi_name: wifiName };
}

async function listClientDirectory(requestedPath) {
  if (!requestedPath) {
    if (process.platform === "win32") {
      const roots = [];
      for (let code = 65; code <= 90; code++) {
        const drive = String.fromCharCode(code) + ":\\";
        if (fs.existsSync(drive)) roots.push({ name: drive, path: drive, is_dir: true, size: 0, modified: 0 });
      }
      return { path: "", parent: null, entries: roots };
    }
    requestedPath = require("os").homedir();
  }
  const absolute = path.resolve(requestedPath);
  const directoryEntries = await fs.promises.readdir(absolute, { withFileTypes: true });
  const entries = [];
  for (const entry of directoryEntries) {
    try {
      const fullPath = path.join(absolute, entry.name);
      const stat = await fs.promises.stat(fullPath);
      entries.push({
        name: entry.name,
        path: fullPath,
        is_dir: entry.isDirectory(),
        size: entry.isDirectory() ? 0 : stat.size,
        modified: stat.mtimeMs / 1000,
      });
    } catch (_error) {}
  }
  entries.sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.name.localeCompare(b.name));
  // En la raíz de una unidad Windows se vuelve a la lista de unidades; en la
  // raíz "/" de Linux no hay nada por encima.
  const atRoot = path.dirname(absolute) === absolute;
  const parent = atRoot ? (process.platform === "win32" ? "" : null) : path.dirname(absolute);
  return { path: absolute, parent, entries };
}

function normalizeServerUrl(value) {
  let parsed;
  try {
    parsed = new URL(String(value || "").trim());
  } catch (_error) {
    throw new Error("Introduce una URL válida, por ejemplo https://remoto.example.com");
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    throw new Error("La dirección debe comenzar por http:// o https://");
  }
  parsed.pathname = "/";
  parsed.search = "";
  parsed.hash = "";
  return parsed.toString().replace(/\/$/, "");
}

function buildMenu() {
  return Menu.buildFromTemplate([
    {
      label: "Servidor",
      submenu: [
        { label: "Conectar", click: () => mainWindow.loadURL(serverUrl) },
        { label: "Cambiar servidor…", click: () => mainWindow.loadFile(path.join(__dirname, "setup.html")) },
        { type: "separator" },
        { role: "quit", label: "Salir" },
      ],
    },
    {
      label: "Ver",
      submenu: [
        { role: "reload", label: "Recargar" },
        { role: "togglefullscreen", label: "Pantalla completa" },
      ],
    },
    {
      label: "Ayuda",
      submenu: [
        { label: "Abrir en el navegador", click: () => shell.openExternal(serverUrl) },
      ],
    },
  ]);
}

function createWindow() {
  serverUrl = loadSavedServer();
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: "Escritorio Remoto Bravo",
    backgroundColor: "#0f1115",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.maximize();
  Menu.setApplicationMenu(buildMenu());
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://") || url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (url.startsWith("file:")) return;
    try {
      if (new URL(url).origin === new URL(serverUrl).origin) return;
    } catch (_error) {}
    event.preventDefault();
    if (url.startsWith("http://") || url.startsWith("https://")) shell.openExternal(url);
  });
  mainWindow.loadFile(path.join(__dirname, "setup.html"));
}

ipcMain.handle("server:get", () => serverUrl);
ipcMain.handle("server:connect", async (event, value) => {
  try {
    if (!event.senderFrame.url.startsWith("file:")) throw new Error("Operación no permitida.");
    serverUrl = normalizeServerUrl(value);
    saveServer();
    await mainWindow.loadURL(serverUrl);
    return { ok: true };
  } catch (error) {
    return { ok: false, error: error.message };
  }
});
ipcMain.handle("clientfs:platform", (event) => trustedRenderer(event) ? process.platform : null);
ipcMain.handle("network:info", (event) => {
  if (!trustedRenderer(event)) throw new Error("Operación no permitida.");
  return networkInfo();
});
ipcMain.handle("clientfs:list", async (event, requestedPath) => {
  if (!trustedRenderer(event)) throw new Error("Operación no permitida.");
  return listClientDirectory(requestedPath);
});
ipcMain.handle("clientfs:read", async (event, filePath) => {
  if (!trustedRenderer(event)) throw new Error("Operación no permitida.");
  const stat = await fs.promises.stat(filePath);
  if (!stat.isFile()) throw new Error("La ruta no es un archivo.");
  if (stat.size > 256 * 1024 * 1024) throw new Error("El archivo supera el límite de 256 MB del cliente.");
  return fs.promises.readFile(filePath);
});

ipcMain.handle("clientfs:write", async (event, baseDir, relativePath, bytes) => {
  if (!trustedRenderer(event)) throw new Error("Operación no permitida.");
  const base = path.resolve(baseDir);
  const target = path.resolve(base, relativePath);
  // Impide que una ruta con ".." escriba fuera de la carpeta elegida.
  const relative = path.relative(base, target);
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative)) throw new Error("Ruta de destino no válida.");
  await fs.promises.mkdir(path.dirname(target), { recursive: true });
  await fs.promises.writeFile(target, Buffer.from(bytes));
  return true;
});

function validEntryName(name) {
  if (!name || name === "." || name === ".." || /[\\/\0]/.test(name)) throw new Error("Nombre no válido.");
  return name;
}
ipcMain.handle("clientfs:save", async (event, filePath, bytes, create) => {
  if (!trustedRenderer(event)) throw new Error("Operación no permitida.");
  const target = path.resolve(filePath);
  validEntryName(path.basename(target));
  // "wx" falla si el archivo ya existe, para no pisar nada al crear.
  await fs.promises.writeFile(target, Buffer.from(bytes), { flag: create ? "wx" : "w" }).catch((error) => {
    throw new Error(error.code === "EEXIST" ? "Ya existe un archivo con ese nombre." : error.message);
  });
  return true;
});
ipcMain.handle("clientfs:mkdir", async (event, parentDir, name) => {
  if (!trustedRenderer(event)) throw new Error("Operación no permitida.");
  await fs.promises.mkdir(path.join(path.resolve(parentDir), validEntryName(name))).catch((error) => {
    throw new Error(error.code === "EEXIST" ? "Ya existe un archivo o carpeta con ese nombre." : error.message);
  });
  return true;
});
ipcMain.handle("clientfs:delete", async (event, targetPath) => {
  if (!trustedRenderer(event)) throw new Error("Operación no permitida.");
  const target = path.resolve(targetPath);
  if (path.dirname(target) === target) throw new Error("No se puede borrar la raíz de una unidad.");
  await fs.promises.rm(target, { recursive: true });
  return true;
});
app.whenReady().then(createWindow);
app.on("window-all-closed", () => app.quit());

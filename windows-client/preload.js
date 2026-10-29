const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktopRemote", {
  currentServer: () => ipcRenderer.invoke("server:get"),
  connect: (url) => ipcRenderer.invoke("server:connect", url),
  clientPlatform: () => ipcRenderer.invoke("clientfs:platform"),
  networkInfo: () => ipcRenderer.invoke("network:info"),
  listClientDirectory: (path) => ipcRenderer.invoke("clientfs:list", path),
  readClientFile: (path) => ipcRenderer.invoke("clientfs:read", path),
  saveClientFile: (filePath, bytes, create) => ipcRenderer.invoke("clientfs:save", filePath, bytes, create),
  makeClientDirectory: (parentDir, name) => ipcRenderer.invoke("clientfs:mkdir", parentDir, name),
  deleteClientPath: (targetPath) => ipcRenderer.invoke("clientfs:delete", targetPath),
  writeClientFile: (baseDir, relativePath, bytes) => ipcRenderer.invoke("clientfs:write", baseDir, relativePath, bytes),
});

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("marketTicker", {
  getQuotes: () => ipcRenderer.invoke("quotes:get"),
  close: () => ipcRenderer.invoke("window:close"),
  minimize: () => ipcRenderer.invoke("window:minimize"),
  toggleTop: () => ipcRenderer.invoke("window:toggle-top")
});

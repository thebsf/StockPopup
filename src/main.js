const { app, BrowserWindow, ipcMain, Menu } = require("electron");
const path = require("node:path");
const iconv = require("node:util").TextDecoder;

const REFRESH_INTERVAL_MS = 30_000;
const REQUEST_TIMEOUT_MS = 8_000;

let mainWindow;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 360,
    height: 560,
    minWidth: 320,
    minHeight: 430,
    show: false,
    frame: false,
    transparent: true,
    resizable: true,
    alwaysOnTop: true,
    skipTaskbar: false,
    title: "桌面实时行情",
    backgroundColor: "#00000000",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.loadFile(path.join(__dirname, "renderer", "index.html"));
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null);
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

ipcMain.handle("quotes:get", async () => getQuotes());
ipcMain.handle("window:close", () => mainWindow?.close());
ipcMain.handle("window:minimize", () => mainWindow?.minimize());
ipcMain.handle("window:toggle-top", () => {
  if (!mainWindow) return false;
  const next = !mainWindow.isAlwaysOnTop();
  mainWindow.setAlwaysOnTop(next);
  return next;
});

async function getQuotes() {
  const startedAt = Date.now();
  const results = await Promise.allSettled([
    fetchSinaQuotes(),
    fetchYahooQuote({
      id: "xau",
      name: "纽约金",
      symbol: "GC=F",
      unit: "USD/oz",
      decimals: 2
    }),
    fetchYahooQuote({
      id: "nq",
      name: "纳指期货",
      symbol: "NQ=F",
      unit: "USD",
      decimals: 2
    }),
    fetchYahooQuote({
      id: "oil",
      name: "纽约原油",
      symbol: "CL=F",
      unit: "USD/bbl",
      decimals: 2
    }),
    fetchYahooQuote({
      id: "usd",
      name: "美元汇率",
      symbol: "CNY=X",
      unit: "CNY/USD",
      decimals: 4
    }),
    fetchYahooQuote({
      id: "jpy",
      name: "日元汇率",
      symbol: "JPYCNY=X",
      unit: "CNY/JPY",
      decimals: 4
    })
  ]);

  return {
    refreshIntervalMs: REFRESH_INTERVAL_MS,
    fetchedAt: new Date().toISOString(),
    elapsedMs: Date.now() - startedAt,
    quotes: [
      ...quoteGroupOrErrors(results[0], [
        { id: "cnGold", name: "国内金价" },
        { id: "sh", name: "上证指数" },
        { id: "star", name: "科创50" }
      ]),
      quoteOrError(results[1], { id: "xau", name: "纽约金" }),
      quoteOrError(results[2], { id: "nq", name: "纳指期货" }),
      quoteOrError(results[3], { id: "oil", name: "纽约原油" }),
      quoteOrError(results[4], { id: "usd", name: "美元汇率" }),
      quoteOrError(results[5], { id: "jpy", name: "日元汇率" })
    ]
  };
}

async function fetchSinaQuotes() {
  const text = await fetchSinaText("https://hq.sinajs.cn/list=SGE_AUTD,sh000001,sh000688");
  const assignments = parseSinaAssignments(text);
  return [
    parseSinaSgeAutd(assignments.SGE_AUTD || []),
    parseSinaIndex({
      values: assignments.sh000001 || [],
      id: "sh",
      name: "上证指数",
      symbol: "sh000001"
    }),
    parseSinaIndex({
      values: assignments.sh000688 || [],
      id: "star",
      name: "科创50",
      symbol: "sh000688"
    })
  ];
}

function parseSinaGlobalFuture({ values, id, name, symbol, unit, decimals }) {
  if (values.length < 3) {
    throw new Error(`${name} 数据为空`);
  }

  const price = toNumber(values[1]);
  const previousClose = toNumber(values[8]);
  const change = Number.isFinite(price) && Number.isFinite(previousClose)
    ? price - previousClose
    : null;
  const changePercent = Number.isFinite(change) && Number.isFinite(previousClose) && previousClose !== 0
    ? (change / previousClose) * 100
    : null;

  if (!Number.isFinite(price)) {
    throw new Error(`${name} 价格字段异常`);
  }

  return normalizeQuote({
    id,
    name,
    source: "新浪财经",
    symbol,
    price,
    change,
    changePercent,
    unit,
    decimals,
    marketTime: compactDateTime(values[12], values[6])
  });
}

function parseSinaSgeAutd(values) {
  const symbol = "SGE_AUTD";
  if (values.length < 18) {
    throw new Error("国内金价数据为空");
  }

  const price = toNumber(values[3]);
  const previousClose = toNumber(values[9]);
  const changePercent = percentTextToNumber(values[17]);
  const change = Number.isFinite(price) && Number.isFinite(previousClose)
    ? price - previousClose
    : null;

  if (!Number.isFinite(price)) {
    throw new Error("国内金价价格字段异常");
  }

  return normalizeQuote({
    id: "cnGold",
    name: "国内金价",
    source: "新浪财经",
    symbol,
    price,
    change,
    changePercent,
    unit: "CNY/g",
    decimals: 2,
    marketTime: values[16] || null
  });
}

function parseSinaIndex({ values, id, name, symbol }) {
  if (values.length < 4) {
    throw new Error(`${name} 数据为空`);
  }

  const price = toNumber(values[3]);
  const previousClose = toNumber(values[2]);
  const change = Number.isFinite(price) && Number.isFinite(previousClose)
    ? price - previousClose
    : null;
  const changePercent = Number.isFinite(change) && Number.isFinite(previousClose) && previousClose !== 0
    ? (change / previousClose) * 100
    : null;

  if (!Number.isFinite(price)) {
    throw new Error(`${name} 价格字段异常`);
  }

  return normalizeQuote({
    id,
    name,
    source: "新浪财经",
    symbol,
    price,
    change,
    changePercent,
    unit: "点",
    decimals: 2,
    marketTime: compactDateTime(values[30], values[31])
  });
}

async function fetchYahooQuote({ id, name, symbol, unit, decimals }) {
  return fetchYahooChart({ id, name, symbol, unit, decimals });
}

async function fetchYahooChart({ id, name, symbol, unit, decimals }) {
  const url = new URL(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(symbol)}`);
  url.searchParams.set("range", "1d");
  url.searchParams.set("interval", "1m");
  url.searchParams.set("_", String(Date.now()));

  const response = await fetchWithTimeout(url, {
    headers: {
      "User-Agent": "Mozilla/5.0",
      "Cache-Control": "no-cache",
      "Pragma": "no-cache",
      "Accept": "application/json"
    }
  });

  if (!response.ok) {
    throw new Error(`${name} HTTP ${response.status}`);
  }

  const data = await response.json();
  const result = data?.chart?.result?.[0];
  const meta = result?.meta;
  if (!meta || !Number.isFinite(meta.regularMarketPrice)) {
    throw new Error(`${name} 数据为空`);
  }

  const previousClose = meta.chartPreviousClose ?? meta.previousClose;
  const change = Number.isFinite(previousClose)
    ? meta.regularMarketPrice - previousClose
    : null;
  const changePercent = Number.isFinite(previousClose) && previousClose !== 0
    ? (change / previousClose) * 100
    : null;

  return normalizeQuote({
    id,
    name,
    source: "Yahoo Finance",
    symbol,
    price: meta.regularMarketPrice,
    change,
    changePercent,
    unit,
    decimals,
    marketTime: meta.regularMarketTime
      ? new Date(meta.regularMarketTime * 1000).toISOString()
      : null
  });
}

async function fetchSinaText(url) {
  const requestUrl = url.startsWith("https://hq.sinajs.cn/list=")
    ? new URL(`https://hq.sinajs.cn/?rn=${Date.now()}&list=${url.split("=").pop()}`)
    : new URL(url);

  const response = await fetchWithTimeout(requestUrl, {
    headers: {
      "User-Agent": "Mozilla/5.0",
      "Cache-Control": "no-cache",
      "Pragma": "no-cache",
      "Referer": "https://finance.sina.com.cn/"
    }
  });

  if (!response.ok) {
    throw new Error(`新浪财经 HTTP ${response.status}`);
  }

  const buffer = await response.arrayBuffer();
  return new iconv("gbk").decode(buffer);
}

async function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, {
      ...options,
      signal: controller.signal
    });
  } finally {
    clearTimeout(timer);
  }
}

function parseSinaAssignments(text) {
  const assignments = {};
  for (const match of text.matchAll(/var\s+hq_str_([^=]+)="([^"]*)";?/g)) {
    assignments[match[1]] = match[2].split(",").map((item) => item.trim());
  }
  return assignments;
}

function quoteGroupOrErrors(result, fallbacks) {
  if (result.status === "fulfilled") {
    return result.value;
  }
  return fallbacks.map((fallback) => ({
    ...fallback,
    status: "error",
    error: result.reason?.message || "行情获取失败"
  }));
}

function quoteOrError(result, fallback) {
  if (result.status === "fulfilled") {
    return result.value;
  }
  return {
    ...fallback,
    status: "error",
    error: result.reason?.message || "行情获取失败"
  };
}

function normalizeQuote({
  id,
  name,
  source,
  symbol,
  price,
  change,
  changePercent,
  unit,
  decimals,
  marketTime
}) {
  return {
    id,
    name,
    source,
    symbol,
    status: "ok",
    price,
    change: Number.isFinite(change) ? change : null,
    changePercent: Number.isFinite(changePercent) ? changePercent : null,
    direction: getDirection(change, changePercent),
    unit,
    decimals,
    marketTime
  };
}

function getDirection(change, changePercent) {
  const value = Number.isFinite(change) ? change : changePercent;
  if (!Number.isFinite(value) || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

function toNumber(value) {
  if (value === undefined || value === null || value === "" || value === "--") {
    return NaN;
  }
  return Number(String(value).replace(/%/g, ""));
}

function percentTextToNumber(value) {
  const parsed = toNumber(value);
  return Number.isFinite(parsed) ? parsed : NaN;
}

function compactDateTime(date, time) {
  if (!date && !time) return null;
  if (!date) return time;
  if (!time) return date;
  return `${date} ${time}`;
}

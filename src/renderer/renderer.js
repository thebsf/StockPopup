const quoteList = document.querySelector("#quoteList");
const statusText = document.querySelector("#statusText");
const updatedAt = document.querySelector("#updatedAt");
const latency = document.querySelector("#latency");
const refreshButton = document.querySelector("#refreshButton");
const pinButton = document.querySelector("#pinButton");
const minimizeButton = document.querySelector("#minimizeButton");
const closeButton = document.querySelector("#closeButton");

let refreshTimer = null;
let isRefreshing = false;
const defaultRefreshIntervalMs = 30000;

const numberFormatter = new Intl.NumberFormat("zh-CN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2
});

refreshButton.addEventListener("click", () => refreshQuotes(true));
pinButton.addEventListener("click", async () => {
  const isPinned = await window.marketTicker.toggleTop();
  pinButton.classList.toggle("active", isPinned);
});
minimizeButton.addEventListener("click", () => window.marketTicker.minimize());
closeButton.addEventListener("click", () => window.marketTicker.close());

refreshQuotes();

async function refreshQuotes(manual = false) {
  if (isRefreshing) return;
  if (refreshTimer) {
    window.clearTimeout(refreshTimer);
    refreshTimer = null;
  }

  isRefreshing = true;
  refreshButton.classList.add("spinning");
  statusText.textContent = manual ? "正在手动刷新" : "正在连接行情源";

  try {
    const payload = await window.marketTicker.getQuotes();
    renderQuotes(payload.quotes || []);
    statusText.textContent = buildStatus(payload.quotes || []);
    updatedAt.textContent = `更新 ${formatClock(payload.fetchedAt)}`;
    latency.textContent = `${payload.elapsedMs} ms`;

    scheduleRefresh(payload.refreshIntervalMs);
  } catch (error) {
    statusText.textContent = "行情源连接失败";
    quoteList.innerHTML = `
      <article class="quote-row error-row">
        <div>
          <h2>无法获取行情</h2>
          <p>${escapeHtml(error.message || "未知错误")}</p>
        </div>
      </article>
    `;
    scheduleRefresh(defaultRefreshIntervalMs);
  } finally {
    refreshButton.classList.remove("spinning");
    isRefreshing = false;
  }
}

function scheduleRefresh(intervalMs = defaultRefreshIntervalMs) {
  refreshTimer = window.setTimeout(refreshQuotes, intervalMs || defaultRefreshIntervalMs);
}

function renderQuotes(quotes) {
  quoteList.innerHTML = quotes.map((quote) => {
    if (quote.status !== "ok") {
      return `
        <article class="quote-row error-row">
          <div class="quote-meta">
            <h2>${escapeHtml(quote.name)}</h2>
            <p>${escapeHtml(quote.error || "获取失败")}</p>
          </div>
          <strong class="quote-price">--</strong>
        </article>
      `;
    }

    const directionClass = quote.direction === "up"
      ? "up"
      : quote.direction === "down"
        ? "down"
        : "flat";

    return `
      <article class="quote-row ${directionClass}">
        <div class="quote-meta">
          <h2>${escapeHtml(quote.name)}</h2>
          <p>${escapeHtml(quote.symbol)} · ${escapeHtml(quote.unit)} · ${escapeHtml(quote.source)}</p>
        </div>
        <div class="quote-values">
          <strong class="quote-price">${formatNumber(quote.price, quote.decimals)}</strong>
          <span class="quote-change">${formatChange(quote.change, quote.changePercent)}</span>
        </div>
      </article>
    `;
  }).join("");
}

function buildStatus(quotes) {
  const failed = quotes.filter((quote) => quote.status !== "ok").length;
  if (failed === 0) return "全部行情已更新";
  if (failed === quotes.length) return "行情源全部失败";
  return `${quotes.length - failed}/${quotes.length} 个行情已更新`;
}

function formatNumber(value, decimals = 2) {
  if (!Number.isFinite(value)) return "--";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals
  }).format(value);
}

function formatChange(change, changePercent) {
  const parts = [];
  if (Number.isFinite(change)) {
    parts.push(`${change > 0 ? "+" : ""}${numberFormatter.format(change)}`);
  }
  if (Number.isFinite(changePercent)) {
    parts.push(`${changePercent > 0 ? "+" : ""}${changePercent.toFixed(2)}%`);
  }
  return parts.length ? parts.join(" / ") : "--";
}

function formatClock(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit"
  });
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

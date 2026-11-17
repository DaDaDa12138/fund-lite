/* Fund-ANA 本地只读前端：无框架、无构建，直接消费 /api/v1。 */
const API = "/api/v1";
const TOKEN_KEY = "fundana_api_token";
function apiToken() { return localStorage.getItem(TOKEN_KEY) || ""; }
const $main = document.getElementById("main");
const state = {
  view: "overview",
  board: "bond_stable",
  q: "",
  style: "",
  offset: 0,
  limit: 50,
  sort: "rank",
  order: "asc",
  fund: "",
  fundTab: "overview",
  fundCache: null,
  table: null,
  shadowWindow: 1095,
  periodGran: "month",
  periodMode: "total",
  navPage: 1,
  navSize: 100,
  navStart: "",
  navEnd: "",
  watchCodes: new Set(),
  compareCodes: [],
  lockup: "", lockBucket: "", lockOffset: 0, dedupe: false, decisionCache: null,
  metrics: {}, metricsLoaded: false,
  sortSpec: [{ key: "rank", order: "asc" }],
};

/* ── 工具 ───────────────────────────────────────────── */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtBytes(n) {
  if (n == null) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = Number(n), i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(i === 0 ? 0 : 2)} ${units[i]}`;
}
const fmtInt = (n) => (n == null ? "—" : Number(n).toLocaleString("zh-CN"));
const fmtNum = (n, d = 2) => (n == null || Number.isNaN(Number(n)) ? "—" : Number(n).toFixed(d));
const fmtPct = (n, d = 2) => (n == null ? "—" : `${(Number(n) * 100).toFixed(d)}%`);
const fmtPctPoint = (n, d = 2) => (n == null ? "—" : `${Number(n).toFixed(d)}%`);
/* 策略指标封顶：达到 ±cap 时标星，避免把截断值当成真实值（见 docs/formula-contract.md）。 */
function fmtCap(n, cap, d = 2) {
  if (n == null) return "—";
  const hit = Math.abs(Number(n)) >= cap - 1e-9;
  return `<span title="${hit ? `策略值封顶 ±${cap}` : ""}">${Number(n).toFixed(d)}${hit ? "*" : ""}</span>`;
}


/* 涨跌语义：中国习惯红涨绿跌，正值加 + 号。 */
const trendCls = (v) => (v == null || Number.isNaN(Number(v)) ? "" : (Number(v) > 0 ? "up" : (Number(v) < 0 ? "down" : "")));
const signPctDec = (v, d = 2) => (v == null ? "—" : (Number(v) > 0 ? "+" : "") + (Number(v) * 100).toFixed(d) + "%");
const signPctPoint = (v, d = 2) => (v == null ? "—" : (Number(v) > 0 ? "+" : "") + Number(v).toFixed(d) + "%");
const signNum = (v, d = 2) => (v == null ? "—" : (Number(v) > 0 ? "+" : "") + Number(v).toFixed(d));

/* 轻提示：收藏/取消收藏等操作结果，2.4 秒后自动消失。 */
function toast(msg, tone) {
  let box = document.getElementById("toast-box");
  if (!box) {
    box = document.createElement("div");
    box.id = "toast-box";
    box.style.cssText = "position:fixed;right:18px;bottom:18px;z-index:60;display:flex;flex-direction:column;gap:8px;align-items:flex-end";
    document.body.appendChild(box);
  }
  const el = document.createElement("div");
  el.className = "notice " + (tone || "info");
  el.style.cssText = "box-shadow:0 10px 24px rgba(16,24,40,.18);max-width:320px;background:var(--panel)";
  el.textContent = msg;
  box.appendChild(el);
  setTimeout(function () { el.remove(); }, 2600);
}

/* 自选代码集合：一次拉全量，供榜单/详情页星标着色。 */
async function loadWatchCodes(force) {
  if (state.watchCodes.size && !force) return state.watchCodes;
  try {
    const res = await api("/watchlist");
    state.watchCodes = new Set(((res.data || {}).items || []).map(function (x) { return x.fcode; }));
  } catch (e) { /* 未发布/接口异常时不影响浏览 */ }
  return state.watchCodes;
}
function starBtn(code) {
  const on = state.watchCodes.has(code);
  return '<button class="star' + (on ? " on" : "") + '" title="' + (on ? "已收藏，点击取消" : "加入自选") +
    '" aria-label="收藏" onclick="event.stopPropagation();toggleWatch(\'' + esc(code) + '\',\'' +
    esc(state.view) + '\',this)">' + (on ? "★" : "☆") + "</button>";
}

/* 收藏开关：已收藏则取消，未收藏则加入；成功给绿色提示，不再报“已经在收藏里”。 */
async function toggleWatch(code, source, btn) {
  const fcode = String(code || "").trim();
  if (!fcode) { toast("请先输入基金代码", "warn"); return; }
  await loadWatchCodes();
  const on = state.watchCodes.has(fcode);
  try {
    if (on) {
      await api("/watchlist/" + encodeURIComponent(fcode), { method: "DELETE" });
      state.watchCodes.delete(fcode);
      if (btn) { btn.textContent = "☆"; btn.classList.remove("on"); btn.title = "加入自选"; }
      toast("已取消收藏 " + fcode);
    } else {
      await post("/watchlist", { fcode: fcode, source: source || "manual", note: null });
      state.watchCodes.add(fcode);
      if (btn) { btn.textContent = "★"; btn.classList.add("on"); btn.title = "已收藏，点击取消"; }
      toast("收藏成功：" + fcode, "success");
    }
    if (state.view === "watchlist") await loadWatchlist();
  } catch (err) {
    toast("操作失败：" + err.message, "warn");
  }
}

/* 顶栏搜索：输入防抖 180ms，回车/点击跳转详情。 */
function initSearch() {
  const input = document.getElementById("global-search");
  const box = document.getElementById("search-results");
  if (!input || !box) return;
  let timer = null;
  let items = [];
  let idx = -1;
  function close() { box.hidden = true; box.innerHTML = ""; idx = -1; }
  function paint() {
    if (!items.length) { box.innerHTML = '<div class="empty">没有匹配的基金</div>'; box.hidden = false; return; }
    box.innerHTML = items.map(function (it, i) {
      return '<a href="#/fund/' + esc(it.fcode) + '" data-i="' + i + '" class="' + (i === idx ? "active" : "") + '">' +
        '<span class="code">' + esc(it.fcode) + "</span><span>" + esc(it.name) + "</span>" +
        '<span class="muted small" style="margin-left:auto">' + esc(it.style_label || "") + "</span></a>";
    }).join("");
    box.hidden = false;
  }
  async function run(q) {
    if (!q) { close(); return; }
    try {
      const res = await api("/search?q=" + encodeURIComponent(q) + "&limit=8");
      items = res.data || [];
      idx = -1;
      paint();
    } catch (e) { close(); }
  }
  input.addEventListener("input", function () {
    clearTimeout(timer);
    const q = input.value.trim();
    timer = setTimeout(function () { run(q); }, 180);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { close(); input.blur(); return; }
    if (!items.length) return;
    if (e.key === "ArrowDown") { idx = Math.min(items.length - 1, idx + 1); paint(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { idx = Math.max(0, idx - 1); paint(); e.preventDefault(); }
    else if (e.key === "Enter") {
      const pick = items[idx >= 0 ? idx : 0];
      if (pick) { location.hash = "#/fund/" + pick.fcode; close(); input.blur(); }
    }
  });
  box.addEventListener("click", function (e) {
    const a = e.target.closest("a");
    if (a) { close(); input.value = ""; }
  });
  document.addEventListener("click", function (e) {
    if (e.target !== input && !box.contains(e.target)) close();
  });
}

/* 中文标签层：来自 /api/v1/meta/labels，前端不出现内部枚举名。 */
const LABELS = { styles: [], confidences: [], boards: [], layers: [], scale_sources: [],
  return_types: [], user_fits: [], bond_classes: [], buyable: [], run_modes: [],
  fetch_statuses: [], profiles: [], sources: [] };
const NAV_SOURCE_LABELS = { em_pzd: "天天基金 · 基金档案", dj_nav: "蛋卷 · 历史净值",
  em_lsjz: "天天基金 · 历史净值" };
let labelsPromise = null;
function ensureLabels() {
  if (!labelsPromise) {
    labelsPromise = api("/meta/labels").then(function (res) {
      Object.keys(LABELS).forEach(function (k) {
        if (res.data && Array.isArray(res.data[k])) LABELS[k] = res.data[k];
      });
      return res.data;
    }).catch(function () { return null; });
  }
  return labelsPromise;
}
var metricsPromise = null;
function ensureMetrics() {
  if (state.metricsLoaded) return Promise.resolve(state.metrics);
  if (!metricsPromise) {
    metricsPromise = api("/meta/metrics").then(function (r) {
      var map = {};
      ((r.data || {}).metrics || []).forEach(function (m) { map[m.key] = m; });
      state.metrics = map;
      state.metricsLoaded = true;
      return map;
    }).catch(function () { metricsPromise = null; return {}; });
  }
  return metricsPromise;
}

function metricMeta(key) { return (state.metrics || {})[key] || null; }

var UNIT_CN = { pct: "百分点（%）", dec: "小数比率（0.02 = 2%）", num: "数值", billion: "亿元",
  count: "个数", days: "天", score: "分" };

function infoBtn(key) {
  var m = metricMeta(key);
  if (!m) return "";
  var tip = m.desc + "（" + m.direction_label + "）";
  return '<button class="info" title="' + esc(tip) +
    '" onclick="event.stopPropagation();showMetric(\'' + key + '\')">i</button>';
}

function showMetric(key) {
  var m = metricMeta(key);
  if (!m) return;
  var el = document.createElement("div");
  el.className = "modal";
  el.addEventListener("click", function () { el.remove(); });
  el.innerHTML = '<div class="modal-card">' +
    '<div class="modal-head"><b>' + esc(m.label) + '</b><span class="muted small">' +
      esc(key) + '</span></div>' +
    '<div class="kv"><div><span>方向</span><b>' + esc(m.direction_label) + '</b></div>' +
    '<div><span>单位</span><b>' + esc(UNIT_CN[m.unit] || m.unit) + '</b></div></div>' +
    '<p>' + esc(m.desc) + '</p>' +
    '<p class="muted small">点任意处关闭；指标口径见 docs/formula-contract.md。</p></div>';
  el.querySelector(".modal-card").addEventListener("click", function (e) { e.stopPropagation(); });
  document.body.appendChild(el);
}

function labelOf(group, code, fallback) {
  if (code == null || code === "") return fallback || "—";
  const hit = (LABELS[group] || []).find(function (x) { return x.code === code; });
  return hit ? hit.label : (fallback || code);
}

/* 统一请求：15 秒超时 + 一次自动重试（只读接口，重试无副作用）。 */
async function api(path, opts, retried) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 15000);
  const init = Object.assign({}, opts || {}, { signal: ctrl.signal });
  const headers = Object.assign({}, (opts && opts.headers) || {});
  const token = apiToken();
  if (token) headers.Authorization = "Bearer " + token;
  init.headers = headers;
  let res;
  try {
    res = await fetch(API + path, init);
  } catch (e) {
    clearTimeout(timer);
    if (!retried) return api(path, opts, true);
    throw new Error(e.name === "AbortError" ? "请求超时（15 秒），请稍后重试" : "网络错误：" + e.message);
  }
  clearTimeout(timer);
  const body = await res.json().catch(() => ({ ok: false, error: { message: "响应不是 JSON" } }));
  if (!body.ok) {
    if (res.status === 401 && !retried) {
      const entered = window.prompt("此服务已启用访问令牌，请输入 API Token（仅保存在本机浏览器）");
      if (entered) {
        localStorage.setItem(TOKEN_KEY, entered.trim());
        return api(path, opts, true);
      }
    }
    if (res.status >= 500 && !retried) return api(path, opts, true);
    const err = new Error((body.error && body.error.message) || `HTTP ${res.status}`);
    err.code = body.error && body.error.code;
    err.status = res.status;
    throw err;
  }
  return body;
}

/* 全局错误提示：未捕获异常不再只留在控制台。 */
function showBanner(msg) {
  let el = document.getElementById("global-error");
  if (!el) {
    el = document.createElement("div");
    el.id = "global-error";
    el.setAttribute("role", "alert");
    el.className = "notice warn";
    el.style.cssText = "position:sticky;top:0;z-index:20;margin:0 0 12px";
    document.body.insertBefore(el, document.body.firstChild);
  }
  el.textContent = "出错了：" + msg + "（点击关闭）";
  el.onclick = () => el.remove();
}
window.addEventListener("error", (e) => showBanner(e.message || "脚本错误"));
window.addEventListener("unhandledrejection", (e) =>
  showBanner((e.reason && e.reason.message) || String(e.reason || "未处理的异步错误")));

function post(path, payload) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function notice(msg, tone = "info") {
  return `<div class="notice" style="${tone === "warn" ? "color:var(--warn)" : ""}">${msg}</div>`;
}

/* ── 前瞻验证（§15）：cohort / 成熟观测 / 证据等级 ───────── */
var LEVEL_CN = {
  0: "无有效数据源",
  1: "已建 cohort，尚无成熟观测",
  2: "有成熟观测，样本或月份不足",
  3: "12 个月 × 100 样本且超额为正",
  4: "费后净收益已验证",
};

async function renderForward() {
  $main.innerHTML = "<h1>前瞻验证</h1>" +
    "<p class='sub'>每次正式发布冻结一批 cohort（正式入选 + 同风格对照 + 风险匹配对照），" +
    "只取<strong>决策日之后</strong>的净值计算未来收益，避免未来函数。观察期未走完一律记 " +
    "not_ready，<strong>绝不写 0</strong>；清盘/停牌导致缺口超限记 unverifiable，不剔除样本。</p>" +
    "<div class='grid cols-4' id='fwd-cards'></div><div id='fwd-body'><p class='loading'>加载中…</p></div>";
  await loadForward();
}

async function loadForward() {
  var body = document.getElementById("fwd-body");
  if (!body) return;
  var res;
  try {
    res = await api("/forward/summary");
  } catch (e) {
    body.innerHTML = notice("读取失败：" + e.message, "warn");
    return;
  }
  var d = res.data || {};
  var cohorts = d.cohort_list || [];
  var rows = d.rows || [];
  var matured = d.matured_formal_observations || 0;
  var notReady = 0;
  rows.forEach(function (r) { notReady += (r.status_counts && r.status_counts.not_ready) || 0; });

  var cards = document.getElementById("fwd-cards");
  if (cards) {
    var nextTxt = d.next_maturity_date
      ? (d.next_maturity_days === 0 ? "今天到期" : "还有 " + fmtInt(d.next_maturity_days) + " 天")
      : "没有待到期观察期";
    cards.innerHTML = stat("证据等级", d.evidence_label || "—", LEVEL_CN[d.evidence_level] || "") +
      stat("cohort 数", fmtInt(d.cohorts), "每次正式发布建一个") +
      stat("成熟正式观测", fmtInt(matured), "净值越过观察终点才算") +
      stat("最近到期", d.next_maturity_date || "—",
        nextTxt + " · 未到期 " + fmtInt(notReady) + " 条");
  }

  var ctrs = cohorts.map(function (c) {
    var m = c.members || {};
    var o = c.observations || {};
    var ost = Object.keys(o).map(function (k) { return k + " " + fmtInt(o[k]); }).join(" · ") || "—";
    return "<tr><td class='mono small' title='" + esc(c.cohort_id) + "'>" +
      esc(String(c.cohort_id).slice(0, 17)) + "…</td>" +
      "<td class='mono small dt'>" + esc(c.business_date) + "</td>" +
      "<td><span class='badge'>" + esc(c.source) + "</span></td>" +
      "<td class='num'>" + fmtInt(m.formal || 0) + "</td>" +
      "<td class='num'>" + fmtInt(m.control_style || 0) + "</td>" +
      "<td class='num'>" + fmtInt(m.control_risk || 0) + "</td>" +
      "<td class='num'>" + esc((c.horizons || []).join(" / ") || "—") + "</td>" +
      "<td class='small'>" + esc(ost) + "</td></tr>";
  }).join("");

  var htrs = "";
  rows.forEach(function (r) {
    var f = r.formal || {};
    var s = r.control_style || {};
    var k = r.control_risk || {};
    var sc = r.status_counts || {};
    htrs += "<tr><td class='mono small dt'>" + esc(r.business_date) + "</td>" +
      "<td class='num'>" + fmtInt(r.horizon_days) + "</td>" +
      "<td class='num'>" + (f.n ? fmtNum(f.median, 2) : "—") + "</td>" +
      "<td class='num'>" + (s.n ? fmtNum(s.median, 2) : "—") + "</td>" +
      "<td class='num'>" + (k.n ? fmtNum(k.median, 2) : "—") + "</td>" +
      "<td class='num'>" + (r.excess_median_pct == null ? "—" : signNum(r.excess_median_pct, 2)) + "</td>" +
      "<td class='num'>" + (r.excess_win_rate_pct == null ? "—" : fmtNum(r.excess_win_rate_pct, 1) + "%") + "</td>" +
      "<td class='small'>成熟 " + fmtInt(sc.matured || 0) + " · 未到期 " +
        fmtInt(sc.not_ready || 0) + " · 不可核 " + fmtInt(sc.unverifiable || 0) + "</td></tr>";
  });

  var pendingRows = rows.filter(function (r) {
    return r.status_counts && r.status_counts.not_ready && r.days_to_maturity != null;
  }).sort(function (a, b) {
    return (a.days_to_maturity - b.days_to_maturity) || (a.horizon_days - b.horizon_days);
  });
  var ptr = pendingRows.map(function (r) {
    return "<tr><td class='mono small dt'>" + esc(r.business_date) + "</td>" +
      "<td class='num'>" + fmtInt(r.horizon_days) + " 天</td>" +
      "<td class='mono small dt'>" + esc(r.maturity_date || "—") + "</td>" +
      "<td class='num'>" + (r.days_to_maturity == null ? "—" : fmtInt(r.days_to_maturity)) + "</td>" +
      "<td class='num'>" + (r.progress_pct == null ? "—" : fmtNum(r.progress_pct, 1) + "%") + "</td>" +
      "<td class='num'>" + fmtInt((r.status_counts || {}).not_ready || 0) + "</td></tr>";
  }).join("");

  body.innerHTML = "<h2>cohort 一览</h2>" +
    "<div class='table-wrap'><table><thead><tr><th>cohort</th><th>决策日</th><th>来源</th>" +
    "<th class='num'>正式</th><th class='num'>同风格对照</th><th class='num'>风险对照</th>" +
    "<th class='num'>观察期(天)</th><th>观测状态</th></tr></thead><tbody>" +
    (ctrs || "<tr><td colspan='8' class='muted'>还没有 cohort（下次发布时自动建立）</td></tr>") +
    "</tbody></table></div>" +
    "<h2>观察期进度</h2>" +
    "<div class='table-wrap'><table><thead><tr><th>决策日</th><th class='num'>观察期</th>" +
    "<th>到期日</th><th class='num'>剩余天数</th><th class='num'>进度</th>" +
    "<th class='num'>未到期</th></tr></thead><tbody>" +
    (ptr || "<tr><td colspan='6' class='muted'>没有待到期观察期</td></tr>") +
    "</tbody></table></div>" +
    "<h2>按观察期对比</h2>" +
    (matured ? "<div class='table-wrap'><table><thead><tr><th>决策日</th><th class='num'>观察期</th>" +
      "<th class='num'>正式中位</th><th class='num'>同风格中位</th><th class='num'>风险对照中位</th>" +
      "<th class='num'>超额(pp)</th><th class='num'>超额胜率</th><th>观测</th></tr></thead><tbody>" +
      htrs + "</tbody></table></div>"
      : notice("目前没有任何观察期走完：最早 cohort 的 30 天观察期需要等到决策日之后满 30 天，" +
        "因此这里只显示 not_ready。这是设计行为——未成熟样本不参与任何结论。", "info")) +
    "<div class='card'><div class='card-head'><h3>证据等级怎么算</h3></div>" +
    "<div class='kv'>" +
    "<div><span>LEVEL_0</span><b>没有任何有效数据源</b></div>" +
    "<div><span>LEVEL_1</span><b>已建立 cohort，但尚无成熟观测（当前）</b></div>" +
    "<div><span>LEVEL_2</span><b>有成熟观测，但月份或样本数不足</b></div>" +
    "<div><span>LEVEL_3</span><b>≥12 个不同决策月份、≥100 个独立样本、且超额中位为正</b></div>" +
    "<div><span>LEVEL_4</span><b>费后净收益验证通过（本阶段不宣称）</b></div>" +
    "</div><p class='muted small'>" + esc(d.note || "") + "</p></div>";
}

/* ── 数据质量看板（A5）─────────────────────────────── */
var qualityDays = 7;

async function renderQuality() {
  $main.innerHTML = "<h1>数据质量</h1>" +
    "<p class='sub'>源成功率、字段缺失率、跨源不一致、净值新鲜度与发布门禁历史。" +
    "全部来自 fetch_log / nav_cross_check / publication_gate 已落库事实，不在页面重算。</p>" +
    "<div class='toolbar'><span class='muted small'>时间窗</span>" +
    "<button class='btn' onclick='setQualityDays(7)'>7 天</button>" +
    "<button class='btn' onclick='setQualityDays(30)'>30 天</button>" +
    "<button class='btn' onclick='setQualityDays(90)'>90 天</button>" +
    "<span id='q-window' class='muted small'></span></div>" +
    "<div class='grid cols-4' id='q-cards'></div><div id='q-body'><p class='loading'>加载中…</p></div>";
  await loadQuality();
}

function setQualityDays(n) {
  qualityDays = n;
  loadQuality();
}

async function loadQuality() {
  var body = document.getElementById("q-body");
  if (!body) return;
  var res;
  try {
    res = await api("/quality?days=" + qualityDays);
  } catch (e) {
    body.innerHTML = notice("读取失败：" + e.message, "warn");
    return;
  }
  var d = res.data || {};
  var nav = d.nav || {};
  var cross = d.cross_check || [];
  var gates = d.gates || [];
  var lastGate = gates.length ? gates[0] : null;
  var win = document.getElementById("q-window");
  if (win) win.textContent = "自 " + String(d.since || "").replace("T", " ").slice(0, 16);

  var cards = document.getElementById("q-cards");
  if (cards) {
    cards.innerHTML = stat("净值行数", fmtInt(nav.rows), "覆盖 " + fmtInt(nav.funds) + " 只基金") +
      stat("净值有效日", nav.effective_date || nav.last_date || "—",
        "覆盖 " + fmtNum(nav.coverage_pct, 1) + "%" +
        (nav.partial_latest ? " · 最新 " + esc(nav.max_date || "—") + " 仅 " + fmtNum(nav.max_coverage_pct, 1) + "%" : "")) +
      stat("净值年龄", fmtInt(nav.nav_age_days) + " 天",
        "业务日 " + (nav.business_date || "—") + " · 上限 " + fmtInt(nav.nav_age_limit_days) + " 天") +
      stat("最近门禁", lastGate ? lastGate.status : "—",
        lastGate ? ("阻断 " + fmtInt(lastGate.blocks) + " · 警告 " + fmtInt(lastGate.warnings)) : "无记录");
  }

  var srows = (d.sources || []).map(function (s) {
    var cls = s.success_rate_pct >= 99 ? "good" : (s.success_rate_pct >= 90 ? "" : "warn");
    return "<tr><td class='mono small'>" + esc(s.source_id) + "</td>" +
      "<td class='num'>" + fmtInt(s.calls) + "</td>" +
      "<td class='num'>" + fmtInt(s.business_ok) + "</td>" +
      "<td class='num'>" + fmtInt(s.business_failed) + "</td>" +
      "<td class='num'><span class='badge " + cls + "'>" + fmtNum(s.success_rate_pct, 2) + "%</span></td>" +
      "<td class='num'>" + fmtNum(s.avg_ms, 0) + "</td>" +
      "<td class='small dt'>" + esc(String(s.last_at || "").replace("T", " ").slice(0, 16)) + "</td></tr>";
  }).join("");

  var nrows = (d.field_nulls || []).map(function (f) {
    return "<tr><td class='mono small'>" + esc(f.field) + "</td>" +
      "<td class='num'>" + fmtInt(f.null_count) + "</td>" +
      "<td class='num'>" + fmtNum(f.null_ratio_pct, 2) + "%</td></tr>";
  }).join("");

  var crows = cross.map(function (c) {
    var cls = (c.mismatch || 0) > 0 ? "warn" : "good";
    return "<tr><td class='mono small dt'>" + esc(c.business_date) + "</td>" +
      "<td class='num'>" + fmtInt(c.n) + "</td>" +
      "<td class='num'><span class='badge " + cls + "'>" + fmtInt(c.mismatch) + "</span></td>" +
      "<td class='num'>" + fmtNum(c.mismatch_ratio_pct, 2) + "%</td>" +
      "<td class='num'>" + fmtInt(c.date_lag) + "</td>" +
      "<td class='num'>" + fmtInt(c.missing) + "</td></tr>";
  }).join("");

  var grows = gates.map(function (g) {
    var cls = g.status === "pass" ? "good" : (g.status === "blocked" ? "warn" : "");
    return "<tr><td class='mono small'>" + esc(String(g.publication_id).slice(0, 12)) + "…</td>" +
      "<td class='mono small'>" + esc(String(g.run_id).slice(0, 17)) + "…</td>" +
      "<td><span class='badge " + cls + "'>" + esc(g.status) + "</span></td>" +
      "<td class='num'>" + fmtInt(g.blocks) + "</td>" +
      "<td class='num'>" + fmtInt(g.warnings) + "</td>" +
      "<td class='small dt'>" + esc(String(g.created_at || "").replace("T", " ").slice(0, 16)) + "</td></tr>";
  }).join("");

  var cov = d.coverage_flags || {};
  var svRes = null;
  try {
    svRes = await api("/survivorship?lookback_days=30&limit=20");
  } catch (e) {
    svRes = null;
  }
  var sv = svRes && svRes.data;
  var svRows = ((sv && sv.candidates) || []).map(function (c) {
    return "<tr><td class='mono small'>" + esc(c.fcode) + "</td>" +
      "<td>" + esc(c.name || "—") + "</td>" +
      "<td class='small dt'>" + esc(c.last_seen || "—") + "</td>" +
      "<td class='num'>" + fmtInt(c.days_missing) + "</td>" +
      "<td class='small dt'>" + esc(c.nav_last_date || "—") + "</td>" +
      "<td class='num'>" + (c.nav_stale_days == null ? "—" : fmtInt(c.nav_stale_days)) + "</td></tr>";
  }).join("");
  var svHtml = "";
  if (sv) {
    svHtml = "<h2>幸存者偏差诊断（公开目录快照）</h2>" +
      "<p class='sub'>只比较最新目录与最近 " + fmtInt(sv.lookback_days) + " 天历史快照；" +
      "目录消失候选不等于清盘/合并，不进入筛选宇宙，也不改变榜单。</p>" +
      "<div class='kv'>" +
      "<div><span>快照覆盖</span><b>" + fmtInt(sv.snapshots) + " 天</b></div>" +
      "<div><span>区间</span><b>" + esc(sv.first_date || "—") + " 至 " + esc(sv.latest_date || "—") + "</b></div>" +
      "<div><span>消失候选</span><b>" + fmtInt(sv.bond_missing_count) + " 只（债券）</b></div>" +
      "<div><span>生命周期样本</span><b>" + fmtInt(sv.lifecycle_funds) + " 只</b></div>" +
      "</div>" +
      "<div class='table-wrap'><table><thead><tr><th>代码</th><th>名称</th>" +
      "<th>最后出现在目录</th><th class='num'>缺失天数</th><th>最后净值日</th>" +
      "<th class='num'>净值停更天数</th></tr></thead><tbody>" +
      (svRows || "<tr><td colspan='6' class='muted'>当前没有目录消失候选</td></tr>") +
      "</tbody></table></div>" +
      "<p class='muted small'>" + esc(sv.note || "") + "</p>";
  }
  body.innerHTML =
    "<h2>数据源成功率</h2><div class='table-wrap'><table><thead><tr><th>源</th>" +
    "<th class='num'>调用</th><th class='num'>业务成功</th><th class='num'>业务失败</th>" +
    "<th class='num'>成功率</th><th class='num'>平均耗时(ms)</th><th>最近调用</th></tr></thead><tbody>" +
    (srows || "<tr><td colspan='7' class='muted'>窗口内没有调用记录</td></tr>") + "</tbody></table></div>" +
    "<h2>关键字段缺失率</h2><div class='table-wrap'><table><thead><tr><th>字段</th>" +
    "<th class='num'>NULL 数</th><th class='num'>占比</th></tr></thead><tbody>" +
    (nrows || "<tr><td colspan='3' class='muted'>—</td></tr>") + "</tbody></table></div>" +
    "<h2>跨源核对（按业务日）</h2><div class='table-wrap'><table><thead><tr><th>业务日</th>" +
    "<th class='num'>核对行</th><th class='num'>不一致</th><th class='num'>不一致率</th>" +
    "<th class='num'>日期滞后</th><th class='num'>缺失</th></tr></thead><tbody>" +
    (crows || "<tr><td colspan='6' class='muted'>还没有核对记录</td></tr>") + "</tbody></table></div>" +
    "<h2>发布门禁历史</h2><div class='table-wrap'><table><thead><tr><th>发布</th><th>run</th>" +
    "<th>结论</th><th class='num'>阻断</th><th class='num'>警告</th><th>时间</th></tr></thead><tbody>" +
    (grows || "<tr><td colspan='6' class='muted'>还没有门禁记录</td></tr>") + "</tbody></table></div>" +
    "<h2>覆盖标记</h2><div class='kv'>" +
    "<div><span>净值缺口</span><b>" + fmtInt(cov.gap || 0) + " 只</b></div>" +
    "<div><span>净值陈旧</span><b>" + fmtInt(cov.stale || 0) + " 只</b></div>" +
    "<div><span>历史过短</span><b>" + fmtInt(cov.short_history || 0) + " 只</b></div>" +
    "</div>" + svHtml;
}


/* ── API 健康页 ─────────────────────────────────────── */
var apiHealthDays = 7;

function apiHealthBadge(health) {
  var map = {
    healthy: ["good", "健康"],
    degraded: ["warn", "降级"],
    down: ["bad", "异常"],
    unknown: ["", "未验证"],
    unregistered: ["bad", "未注册"],
  };
  var hit = map[health] || ["", health || "—"];
  return "<span class='badge " + hit[0] + "'>" + esc(hit[1]) + "</span>";
}

function apiFreshBadge(freshness) {
  var map = { fresh: ["good", "新鲜"], stale: ["warn", "过期"], unknown: ["", "无成功记录"] };
  var hit = map[freshness] || ["", freshness || "—"];
  return "<span class='badge " + hit[0] + "'>" + esc(hit[1]) + "</span>";
}

function apiAge(hours) {
  if (hours == null) return "—";
  if (hours < 48) return fmtNum(hours, 1) + " h";
  return fmtNum(hours / 24, 1) + " d";
}

async function renderApiHealth() {
  $main.innerHTML = "<h1>API 健康</h1>" +
    "<p class='sub'>当前项目用到的全部数据源接口、真实请求成功率、p50/p95 延迟、慢请求占比与新鲜度。" +
    "全部来自 <code>config/source_registry.yaml</code> 与 <code>fetch_log</code> 已落库事实，页面不额外发请求。</p>" +
    "<div class='toolbar'><span class='muted small'>时间窗</span>" +
    "<button class='btn' onclick='setApiHealthDays(1)'>1 天</button>" +
    "<button class='btn' onclick='setApiHealthDays(7)'>7 天</button>" +
    "<button class='btn' onclick='setApiHealthDays(30)'>30 天</button>" +
    "<button class='btn' onclick='loadApiHealth()'>刷新</button>" +
    "<span id='ah-window' class='muted small'></span></div>" +
    "<div class='grid cols-4' id='ah-cards'></div>" +
    "<div id='ah-body'><p class='loading'>加载中…</p></div>";
  await loadApiHealth();
}

function setApiHealthDays(n) {
  apiHealthDays = n;
  loadApiHealth();
}

async function loadApiHealth() {
  var body = document.getElementById("ah-body");
  if (!body) return;
  var res;
  try {
    res = await api("/api-health?days=" + apiHealthDays);
  } catch (e) {
    body.innerHTML = notice("读取失败：" + e.message, "warn");
    return;
  }
  var d = res.data || {};
  var sum = d.summary || {};
  var reg = d.registry || {};
  var sources = d.sources || [];
  var win = document.getElementById("ah-window");
  if (win) win.textContent = "自 " + String(d.since || "").replace("T", " ").slice(0, 16) +
    " · 注册表 " + (reg.version || "—");

  var cards = document.getElementById("ah-cards");
  if (cards) {
    cards.innerHTML =
      stat("接口总数", fmtInt(reg.count),
        "注册表 " + (reg.version || "—") + (reg.unregistered ? " · 未注册 " + fmtInt(reg.unregistered) : "")) +
      stat("健康 / 降级", fmtInt(sum.healthy) + " / " + fmtInt(sum.degraded),
        "异常 " + fmtInt(sum.down) + " · 未验证 " + fmtInt(sum.unknown) + " · 未注册 " + fmtInt(sum.unregistered),
        sum.down ? "warn" : "") +
      stat("窗口调用", fmtInt(sum.calls),
        "业务成功 " + fmtInt(sum.business_ok) + " · 慢请求 " + fmtNum(sum.slow_rate_pct, 2) + "%") +
      stat("总成功率", fmtNum(sum.success_rate_pct, 2) + "%",
        "p95 " + (sum.p95_ms == null ? "—" : fmtNum(sum.p95_ms, 0) + " ms") +
        " · 平均 " + (sum.avg_ms == null ? "—" : fmtNum(sum.avg_ms, 0) + " ms"));
  }

  var rows = sources.map(function (s) {
    var srCls = s.success_rate_pct >= 99 ? "good" : (s.success_rate_pct >= 90 ? "warn" : "bad");
    var slowCls = s.slow_rate_pct > 1 ? "warn" : "";
    return "<tr>" +
      "<td><div><b>" + esc(s.label) + "</b></div><div class='mono small muted'>" + esc(s.source_id) + "</div></td>" +
      "<td class='small'>" + esc(s.role_label) + "</td>" +
      "<td class='mono small endpoint' title='" + esc(s.url) + "'>" + esc(s.url || "—") + "</td>" +
      "<td class='num'>" + fmtInt(s.calls) + "</td>" +
      "<td class='num'><span class='badge " + srCls + "'>" + fmtNum(s.success_rate_pct, 2) + "%</span></td>" +
      "<td class='num'>" + (s.p50_ms == null ? "—" : fmtNum(s.p50_ms, 0)) + "</td>" +
      "<td class='num' title='预算 " + fmtInt(s.latency_budget_ms) + " ms'>" +
        (s.p95_ms == null ? "—" : fmtNum(s.p95_ms, 0)) + "</td>" +
      "<td class='num'><span class='" + slowCls + "'>" + fmtNum(s.slow_rate_pct, 2) + "%</span></td>" +
      "<td class='small dt'>" + esc(String(s.last_success_at || "—").replace("T", " ").slice(0, 16)) + "</td>" +
      "<td>" + apiFreshBadge(s.freshness) + "</td>" +
      "<td class='num'>" + (s.health === "unknown" || s.health === "unregistered" ? "—" : fmtNum(s.health_score, 1)) + "</td>" +
      "<td title='" + esc(s.status_note || "") + "'>" + apiHealthBadge(s.health) + "</td>" +
      "</tr>";
  }).join("");

  var problems = sources.filter(function (s) { return s.health !== "healthy"; });
  var problemCards = problems.map(function (s) {
    var b = s.score_breakdown || {};
    return "<div class='card tight'><div class='card-head'><h3>" + esc(s.label) +
      " <span class='mono small muted'>" + esc(s.source_id) + "</span></h3>" + apiHealthBadge(s.health) + "</div>" +
      "<dl class='kv'>" +
      "<dt>地址</dt><dd class='mono small endpoint'>" + esc(s.url || "—") + "</dd>" +
      "<dt>状态说明</dt><dd>" + esc(s.status_note || "—") + "</dd>" +
      "<dt>健康度拆解</dt><dd>成功 " + fmtNum(b.success, 2) + " · 延迟 " + fmtNum(b.latency, 1) +
        " · 新鲜 " + fmtNum(b.freshness, 1) + "</dd>" +
      "<dt>最近错误</dt><dd class='small'>" + esc(s.last_error || "—") + "</dd>" +
      "<dt>连续失败</dt><dd>" + fmtInt(s.consecutive_failures) + "</dd>" +
      "</dl></div>";
  }).join("");

  body.innerHTML =
    "<h2>接口清单</h2><div class='table-wrap'><table><thead><tr>" +
    "<th>接口</th><th>角色</th><th>主接口地址</th><th class='num'>调用</th><th class='num'>成功率</th>" +
    "<th class='num'>p50 ms</th><th class='num'>p95 ms</th><th class='num'>慢请求</th>" +
    "<th>最近成功</th><th>新鲜度</th><th class='num'>健康度</th><th>状态</th>" +
    "</tr></thead><tbody>" +
    (rows || "<tr><td colspan='12' class='muted'>注册表为空</td></tr>") +
    "</tbody></table></div>" +
    "<p class='muted small'>p95 为窗口内真实请求的 95 分位；慢请求 = 超过该源 domain 延迟预算的调用。" +
    "未注册源单独标出，表示 fetch_log 里出现但 source_registry.yaml 没有声明。</p>" +
    "<h2>异常与降级详情</h2>" +
    (problemCards ? "<div class='grid cols-2'>" + problemCards + "</div>" :
      "<div class='card tight'><p class='muted'>当前窗口内所有已注册接口均为健康。</p></div>") +
    "<h2>健康度口径</h2><div class='card'>" +
    "<dl class='kv'>" +
    "<dt>健康度</dt><dd>60% 成功率 + 25% 延迟分 + 15% 新鲜度分</dd>" +
    "<dt>延迟分</dt><dd>p95 ≤ 预算得 100；达到预算 2 倍得 0，中间线性</dd>" +
    "<dt>新鲜度</dt><dd>最近一次业务成功距现在 ≤ TTL 为新鲜；超过 TTL 即降级</dd>" +
    "<dt>状态</dt><dd>成功率 &lt;99% 或 p95 &gt; 预算 2 倍或超过 TTL → 降级；成功率 &lt;90% 或健康度 &lt;60 → 异常；无调用 → 未验证</dd>" +
    "</dl><p class='muted small'>口径刻意保守：HTTP 200 但业务解析失败不算成功；没有调用不写 0ms，也不伪装成健康。</p></div>";
}

/* ── 产品告警 ───────────────────────────────────────── */
var alertsBoard = "";
var ALERT_BOARD_LABEL = {
  bond_long: "长期核心（观察）",
  bond_core10: "收益精选 10",
  bond_core: "核心精选 50",
  bond_stable: "稳健榜",
  bond_enhanced: "增强榜",
  bond_rookie: "新锐榜",
  bond_watch: "观察榜",
  bond_locked: "持有期受限",
};
var ALERT_CODE_LABEL = {
  nav_missing: "净值缺失",
  nav_stale: "净值停更",
  manager_change: "更换经理",
  scale_small: "规模过小",
  scale_shrink: "规模缩水",
  drawdown: "回撤超限",
  style_drift: "风格漂移",
};
var ALERT_SEV = { high: ["bad", "高"], medium: ["warn", "中"], low: ["", "低"] };

function alertBadge(sev) {
  var hit = ALERT_SEV[sev] || ["", sev || "—"];
  return "<span class='badge " + hit[0] + "'>" + esc(hit[1]) + "</span>";
}

async function renderAlerts() {
  $main.innerHTML = "<h1>产品告警</h1>" +
    "<p class='sub'>对当前已发布榜单做事件扫描：净值停更、近 90 天换帅、规模过小/缩水、回撤超限、风格漂移。" +
    "告警只用于复核，不改变入选结果，也不构成买卖建议。</p>" +
    "<div class='toolbar'><span class='muted small'>榜单</span>" +
    "<button class='btn' onclick='setAlertsBoard(\"\")'>全部</button>" +
    "<button class='btn' onclick='setAlertsBoard(\"bond_core10\")'>收益精选 10</button>" +
    "<button class='btn' onclick='setAlertsBoard(\"bond_core\")'>核心精选 50</button>" +
    "<button class='btn' onclick='setAlertsBoard(\"bond_stable\")'>稳健榜</button>" +
    "<button class='btn' onclick='setAlertsBoard(\"bond_enhanced\")'>增强榜</button>" +
    "<button class='btn' onclick='loadAlerts()'>刷新</button>" +
    "<span id='al-window' class='muted small'></span></div>" +
    "<div class='grid cols-4' id='al-cards'></div>" +
    "<div id='al-body'><p class='loading'>加载中…</p></div>";
  await loadAlerts();
}

function setAlertsBoard(board) {
  alertsBoard = board;
  loadAlerts();
}

async function loadAlerts() {
  var body = document.getElementById("al-body");
  if (!body) return;
  var qs = alertsBoard ? "?board=" + encodeURIComponent(alertsBoard) : "";
  var res;
  try {
    res = await api("/alerts" + qs);
  } catch (e) {
    body.innerHTML = notice("读取失败：" + e.message, "warn");
    return;
  }
  var d = res.data || {};
  var sum = d.summary || {};
  var sev = sum.by_severity || {};
  var byCode = sum.by_code || {};
  var win = document.getElementById("al-window");
  if (win) win.textContent = "generation " + (d.generation || "—") + " · as_of " + (d.as_of || "—");

  var cards = document.getElementById("al-cards");
  if (cards) {
    cards.innerHTML =
      stat("扫描基金", fmtInt(d.funds), "已发布榜单去重后") +
      stat("告警总数", fmtInt(sum.alerts), "按基金/事件逐条列出") +
      stat("高优先级", fmtInt(sev.high), "净值停更 / 规模 <2 亿 / 无净值", sev.high ? "warn" : "") +
      stat("中优先级", fmtInt(sev.medium), "换帅 / 规模同比 <-30% / 回撤超限", sev.medium ? "warn" : "");
  }

  var codeRows = Object.keys(byCode).sort(function (a, b) { return byCode[b] - byCode[a]; })
    .map(function (k) {
      return "<tr><td>" + esc(ALERT_CODE_LABEL[k] || k) + "</td><td class='mono small'>" + esc(k) +
        "</td><td class='num'>" + fmtInt(byCode[k]) + "</td></tr>";
    }).join("");

  var rows = (d.alerts || []).map(function (a) {
    var boards = (a.boards || []).map(function (b) {
      return "<span class='badge'>" + esc(ALERT_BOARD_LABEL[b] || b) + "</span>";
    }).join(" ");
    return "<tr>" +
      "<td>" + alertBadge(a.severity) + "</td>" +
      "<td><div><b><a href='#/fund/" + esc(a.fcode) + "'>" + esc(a.name || a.fcode) + "</a></b></div>" +
        "<div class='mono small muted'>" + esc(a.fcode) + "</div></td>" +
      "<td class='small'>" + boards + "</td>" +
      "<td class='small'>" + esc(ALERT_CODE_LABEL[a.code] || a.code) + "</td>" +
      "<td class='small'>" + esc(a.message || "—") + "</td>" +
      "</tr>";
  }).join("");

  body.innerHTML =
    "<h2>告警明细</h2><div class='table-wrap'><table><thead><tr>" +
    "<th>级别</th><th>基金</th><th>所在榜单</th><th>类型</th><th>说明</th>" +
    "</tr></thead><tbody>" +
    (rows || "<tr><td colspan='5' class='muted'>当前榜单没有触发告警。</td></tr>") +
    "</tbody></table></div>" +
    "<div class='grid cols-2'>" +
    "<div><h2>按类型</h2><div class='table-wrap'><table><thead><tr><th>类型</th><th>代码</th><th class='num'>条数</th></tr></thead><tbody>" +
    (codeRows || "<tr><td colspan='3' class='muted'>无</td></tr>") + "</tbody></table></div></div>" +
    "<div><h2>口径</h2><div class='card tight'><dl class='kv'>" +
    "<dt>净值停更</dt><dd>最新净值距 as-of 超过 7 天；近一年有效观测不足 20 个记“净值缺失”</dd>" +
    "<dt>更换经理</dt><dd>任职起始日在近 90 天内（F10 任职表，可能有披露滞后）</dd>" +
    "<dt>规模</dt><dd>family 汇总总规模 <2 亿记高优先级；同比 <-30% 记中优先级（季报，滞后 45 天）</dd>" +
    "<dt>回撤</dt><dd>近一年最大回撤：收益精选/核心 2.5%、稳健 2.0%、增强 5.0%</dd>" +
    "<dt>风格漂移</dt><dd>相对上一份已提交发布的风格标签变化，低优先级</dd>" +
    "</dl><p class='muted small'>告警不写回榜单；首份发布或跨契约时风格漂移为空。</p></div></div></div>";
}

/* ── 路由 ───────────────────────────────────────────── */
const VIEWS = {
  overview: renderOverview,
  rankings: renderRankings,
  compare: renderCompare,
  locked: renderLocked,
  fund: renderFund,
  database: renderDatabase,
  watchlist: renderWatchlist,
  runs: renderRuns,
  diagnostics: renderDiagnostics,
  research: renderResearch,
  report: renderReport,
  tracker: renderTracker,
  forward: renderForward,
  quality: renderQuality,
  "api-health": renderApiHealth,
  alerts: renderAlerts,
};

async function route() {
  await ensureLabels();
  await ensureMetrics();
  await loadWatchCodes();
  const hash = location.hash.replace(/^#\/?/, "") || "overview";
  const [pathPart, queryPart] = hash.split("?");
  if (queryPart) {
    const params = new URLSearchParams(queryPart);
    if (params.get("board")) state.board = params.get("board");
    if (params.get("style")) state.style = params.get("style");
    if (params.get("q")) state.q = params.get("q");
    if (params.get("codes")) state.compareCodes = params.get("codes").split(",").filter(Boolean).slice(0, 3);
    if (params.get("tab")) state.fundTabParam = params.get("tab");
    if (params.get("dedupe")) state.dedupe = params.get("dedupe") !== "0";
    if (params.get("sort")) {
      var keys = params.get("sort").split(",").filter(Boolean).slice(0, 3);
      var orders = (params.get("order") || "").split(",");
      state.sortSpec = keys.map(function (k, i) {
        return { key: k, order: (orders[i] || "asc").toLowerCase() === "desc" ? "desc" : "asc" };
      });
      state.sort = state.sortSpec[0].key;
      state.order = state.sortSpec[0].order;
    } else if (pathPart.split("/")[0] === "rankings") {
      loadSortPrefs();   /* 地址栏没带排序时，用这个榜上次的排序 */
    }
  }
  const [view, arg] = pathPart.split("/");
  state.view = VIEWS[view] ? view : "overview";
  if (state.view === "fund" && arg) {
    state.fund = arg;
    state.fundTab = (FUND_TABS.some(function (t) { return t[0] === state.fundTabParam; }) ? state.fundTabParam : "overview");
    state.fundTabParam = null;
  }
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.view === state.view));
  $main.innerHTML = '<p class="loading">加载中…</p>';
  window.scrollTo(0, 0);
  VIEWS[state.view]().catch((err) => {
    $main.innerHTML = '<h1>出错了</h1><p class="err">' + esc(err.message) + '</p>';
  });
}

/* ── 概览 ───────────────────────────────────────────── */
async function renderOverview() {
  const [ovRes, stRes, pub, mkRes] = await Promise.all([
    api("/overview"),
    api("/stats/storage"),
    api("/publications/current").catch(() => ({ data: null })),
    api("/market/summary").catch(() => ({ data: null })),
  ]);
  const ov = ovRes.data;
  const nav = ov.nav || {};
  const fresh = ov.freshness || {};
  const metricAsOf = (fresh.metric || {}).metric_as_of;
  var dateText = esc(nav.effective_date || nav.last_date || "—");
  if (nav.coverage_pct != null) dateText += "（覆盖 " + fmtNum(nav.coverage_pct, 1) + "%）";
  if (nav.partial_latest) {
    dateText += " · 最新 " + esc(nav.max_date || "—") + " 仅 " + fmtNum(nav.max_coverage_pct, 1) + "%";
  }
  const st = stRes.data;
  const mk = (mkRes && mkRes.data) || null;
  const boards = ov.boards || {};
  const layers = (ov.layers || []).slice().sort((a, b) => b.count - a.count);
  const reasons = ov.reasons || [];
  const styles = (ov.styles || []).slice().sort((a, b) => b.count - a.count);
  const styleMax = Math.max(1, ...styles.map((x) => x.count));
  const top = await api("/rankings?board=bond_stable&limit=10").catch(() => ({ data: [] }));
  const gen = pub.data ? "generation " + (pub.data.generation == null ? "?" : pub.data.generation) : "未发布";
  const dist = (mk && mk.distribution) || [];
  const distMax = Math.max(1, ...dist.map((d) => d.count));
  const mkStyles = ((mk && mk.styles) || []).slice().sort((a, b) => b.n - a.n);
  $main.innerHTML =
    '<div class="hero"><div class="hero-main">' +
      '<div class="hero-kicker">债券型基金研究台</div>' +
      '<h1>全市场债券基金，先看横截面再看个体</h1>' +
      '<p class="sub">候选池 ' + fmtInt(ov.funds) + ' 只 · 数据截止 ' + dateText +
      (metricAsOf ? ' · 指标日 ' + esc(metricAsOf) : '') +
      ' · ' + esc(ov.screen_run ? ov.screen_run.run_id : "无筛选运行") + ' · ' + esc(gen) + '</p>' +
      '<div class="hero-stats">' +
        '<div><b>' + fmtInt(ov.funds) + '</b><span>债券型基金</span></div>' +
        '<div><b>' + fmtInt(ov.nav.rows) + '</b><span>净值观测</span></div>' +
        '<div><b>' + fmtInt(ov.buyable.confirmed_yes || 0) + '</b><span>当日可申购</span></div>' +
        '<div><b>' + fmtInt((boards.bond_stable || 0) + (boards.bond_enhanced || 0)) + '</b><span>正式入选</span></div>' +
      '</div>' +
    '</div></div>' +
    (mk ? '<h2>近一年横截面</h2>' +
    '<div class="grid cols-4">' +
      stat("中位年化", signPctDec(mk.annual_median), "样本 " + fmtInt(mk.n_metric) + " 只 · 均值 " + signPctDec(mk.annual_mean), trendCls(mk.annual_median)) +
      stat("正收益占比", fmtPct(mk.positive_share, 1), "四分位 " + signPctDec(mk.annual_p25) + " ~ " + signPctDec(mk.annual_p75)) +
      stat("中位夏普", fmtNum(mk.sharpe_median), "无风险利率 1.64% · 策略口径") +
      stat("中位最大回撤", fmtPct(mk.mdd_median), "近一年窗口") +
    '</div>' +
    '<div class="split">' +
      '<div><h2>年化收益分布</h2><div class="card">' +
        dist.map(function (d) {
          var down = String(d.label).indexOf("<") === 0 || String(d.label).indexOf("-2") === 0;
          return '<div class="bar-row"><span>' + esc(d.label) + '</span>' +
            '<span class="bar-track"><span class="bar ' + (down ? "down" : "up") +
            '" style="display:block;width:' + ((d.count / distMax) * 100).toFixed(1) + '%"></span></span>' +
            '<span class="num">' + fmtInt(d.count) + '</span></div>';
        }).join("") +
      '</div></div>' +
      '<div><h2>各风格表现（近一年）</h2><div class="card"><div class="table-wrap" style="max-height:320px">' +
        '<table><thead><tr><th>风格</th><th class="num">样本</th><th class="num">中位年化</th>' +
        '<th class="num">正收益占比</th><th class="num">中位回撤</th><th class="num">中位夏普</th></tr></thead><tbody>' +
        mkStyles.map(function (x) {
          return '<tr><td>' + esc(x.style_label) + '</td><td class="num">' + fmtInt(x.n) + '</td>' +
            '<td class="num ' + trendCls(x.annual_median) + '">' + signPctDec(x.annual_median) + '</td>' +
            '<td class="num">' + fmtPct(x.positive_share, 1) + '</td>' +
            '<td class="num">' + fmtPct(x.mdd_median) + '</td>' +
            '<td class="num">' + fmtNum(x.sharpe_median) + '</td></tr>';
        }).join("") +
        '</tbody></table></div></div></div>' +
    '</div>' : "") +
    '<h2>筛选漏斗（最近一次运行）</h2>' +
    '<div class="card"><div class="table-wrap" style="max-height:none">' +
      '<table><thead><tr><th>层</th><th class="num">数量</th><th class="num">占比</th><th>进度</th></tr></thead><tbody>' +
      layers.map(function (l) {
        return '<tr><td><b>' + esc(l.label) + '</b>' +
          (l.detail ? '<div class="layer-detail">' + esc(l.detail) + '</div>' : '') + '</td>' +
          '<td class="num">' + fmtInt(l.count) + '</td>' +
          '<td class="num">' + fmtPct(l.count / (ov.funds || 1), 1) + '</td>' +
          '<td><span class="bar-track" style="width:180px"><span class="bar" style="display:block;width:' +
          ((l.count / (ov.funds || 1)) * 100).toFixed(1) + '%"></span></span></td></tr>';
      }).join("") +
      '</tbody></table></div></div>' +
    '<div class="split" style="margin-top:18px">' +
      '<div><h2>风格分布</h2><div class="card">' +
        styles.map(function (x) {
          return '<div class="bar-row"><span>' + esc(x.label) + '</span>' +
            '<span class="bar-track"><span class="bar" style="display:block;width:' +
            ((x.count / styleMax) * 100).toFixed(1) + '%"></span></span>' +
            '<span class="num">' + fmtInt(x.count) + '</span></div>';
        }).join("") +
      '</div></div>' +
      '<div><h2>主要淘汰原因</h2><div class="card"><div class="table-wrap" style="max-height:320px">' +
        '<table><thead><tr><th>原因</th><th class="num">次数</th></tr></thead><tbody>' +
        reasons.map(function (r) {
          return '<tr><td>' + esc(r.label) + '</td><td class="num">' + fmtInt(r.count) + '</td></tr>';
        }).join("") +
      '</tbody></table></div></div></div>' +
    '</div>' +
    '<h2>稳健榜前 10</h2>' + rankTable(top.data || []) +
    '<p class="muted small">数据口径：复投总收益按日增长率链式复利，年化以 365.25 天为基准；' +
    '夏普 / 索提诺 / 卡玛为本项目策略口径（standard_metric_claim=false）。</p>';
}

function stat(label, value, hint, tone) {
  return '<div class="card stat"><div class="label">' + esc(label) + '</div>' +
    '<div class="value ' + (tone || "") + '">' + value + '</div>' +
    '<div class="hint">' + (hint || "") + '</div></div>';
}

function sortSpecIndex(key) {
  var spec = state.sortSpec || [];
  for (var i = 0; i < spec.length; i++) if (spec[i].key === key) return i;
  return -1;
}

function sortQuery() {
  var spec = (state.sortSpec && state.sortSpec.length)
    ? state.sortSpec : [{ key: state.sort, order: state.order }];
  return {
    sort: spec.map(function (s) { return s.key; }).join(","),
    order: spec.map(function (s) { return s.order; }).join(","),
  };
}

function setSortSpec(spec) {
  if (!spec || !spec.length) spec = [{ key: "rank", order: "asc" }];
  state.sortSpec = spec.slice(0, 3);
  state.sort = state.sortSpec[0].key;
  state.order = state.sortSpec[0].order;
  state.offset = 0;
  saveSortPrefs();
  syncSortHash();
  if (state.view === "locked") { state.lockOffset = 0; loadLocked(); return; }
  loadRankings();
}

function saveSortPrefs() {
  try {
    localStorage.setItem("fundana.sort." + state.board, JSON.stringify(state.sortSpec));
  } catch (e) { /* 隐私模式下 localStorage 不可用，忽略 */ }
}

function loadSortPrefs() {
  try {
    var raw = localStorage.getItem("fundana.sort." + state.board);
    if (!raw) { state.sortSpec = [{ key: "rank", order: "asc" }]; return; }
    var spec = JSON.parse(raw);
    if (spec && spec.length) {
      state.sortSpec = spec.slice(0, 3);
      state.sort = spec[0].key;
      state.order = spec[0].order;
    }
  } catch (e) { state.sortSpec = [{ key: "rank", order: "asc" }]; }
}

function syncSortHash() {
  if (state.view !== "rankings") return;
  try {
    var q = sortQuery();
    history.replaceState(null, "", "#/rankings?board=" + state.board +
      "&sort=" + encodeURIComponent(q.sort) + "&order=" + encodeURIComponent(q.order));
  } catch (e) { /* 忽略 */ }
}

function sortTh(key, label, cls) {
  var m = metricMeta(key);
  var tip = m ? m.desc + "（" + m.direction_label + "）" : "点击按此列排序";
  var sortable = state.view === "rankings" || state.view === "locked";
  var idx = sortSpecIndex(key);
  var on = idx >= 0;
  var spec = state.sortSpec || [];
  var arrow = on ? (spec[idx].order === "desc" ? " ↓" : " ↑") : "";
  /* 多列排序时标出优先级：1 是第一排序键，2 是第二… */
  var badge = on && spec.length > 1 ? "<sup>" + (idx + 1) + "</sup>" : "";
  var cls2 = cls == null ? "num" : cls;
  var hint = "点击排序；按住 Shift 点击可追加为第二/第三排序键";
  if (!sortable) {
    return '<th class="' + cls2 + '" title="' + esc(tip) + '">' + label + infoBtn(key) + "</th>";
  }
  return '<th class="' + cls2 + ' sortable' + (on ? " sorted" : "") + '" title="' + esc(tip) +
    '" onclick="setSort(\'' + key + '\', event)">' + label + arrow + badge + infoBtn(key) + "</th>";
}
function setSort(key, ev) {
  var spec = (state.sortSpec || []).slice();
  var idx = -1;
  for (var i = 0; i < spec.length; i++) if (spec[i].key === key) { idx = i; break; }
  var additive = !!(ev && ev.shiftKey);
  if (additive) {
    if (idx >= 0) {
      spec[idx].order = spec[idx].order === "desc" ? "asc" : "desc";
    } else {
      if (spec.length >= 3) spec.pop();
      spec.push({ key: key, order: "desc" });
    }
  } else if (idx === 0) {
    spec = [{ key: key, order: spec[0].order === "desc" ? "asc" : "desc" }];
  } else {
    spec = [{ key: key, order: key === "rank" ? "asc" : "desc" }];
  }
  setSortSpec(spec);
}
function cmpBox(code) {
  var on = state.compareCodes.indexOf(code) >= 0;
  return '<td class="num"><input type="checkbox" class="cmp" ' + (on ? "checked" : "") +
    ' onclick="event.stopPropagation();toggleCompare(\'' + esc(code) + '\')" title="加入对比（最多 3 只）"></td>';
}

function toggleCompare(code) {
  var i = state.compareCodes.indexOf(code);
  if (i >= 0) state.compareCodes.splice(i, 1);
  else {
    if (state.compareCodes.length >= 3) { toast("最多同时对比 3 只", "warn"); return; }
    state.compareCodes.push(code);
  }
  renderCompareBar();
  if (state.view === "rankings") loadRankings();
}

function renderCompareBar() {
  var el = document.getElementById("cmp-bar");
  if (!el) return;
  var n = state.compareCodes.length;
  el.innerHTML = "已选 <b>" + n + "</b>/3" + (n ? " · " + esc(state.compareCodes.join("、")) : "") +
    (n >= 2 ? ' <button class="primary small" onclick="goCompare()">去对比</button>' : "");
}

function goCompare() { location.hash = "#/compare?codes=" + state.compareCodes.join(","); }

var METRIC_FLAG_LABELS = { amortized_cost: "摊余成本/定开", zero_risk: "零风险净值",
  sparse_nav: "稀疏净值", stale_nav: "过期净值" };

function metricFlagText(flags) {
  return (flags || []).map(function (f) { return METRIC_FLAG_LABELS[f] || f; }).join("、");
}

var RISK_FLAG_LABELS = { top10: "前十大集中", top1: "单一债券集中",
  credit_issuer: "单一发行人集中", credit_issuer_top3: "前三大发行人集中",
  convertible: "转债暴露", equity: "股票暴露", "2022_q4": "2022Q4 压力",
  "2020_h1": "2020H1 压力", "2016_q4": "2016Q4 压力",
  worst60_5y: "近5年最差60日", worst60_all: "历史最差60日", holdings: "缺持仓" };

function riskTitle(r) {
  var parts = (r.risk_flags || []).map(function (f) { return RISK_FLAG_LABELS[f] || f; });
  var risk = r.risk || {};
  var h = risk.holdings || {};
  var s = risk.stress || {};
  if (h.available) {
    if (h.top10_pct != null) parts.push("前十大 " + h.top10_pct + "%");
    if (h.credit_issuer_top3_pct != null) parts.push("信用前三 " + h.credit_issuer_top3_pct + "%");
    if (h.convertible_pct) parts.push("转债 " + h.convertible_pct + "%");
  }
  if (s.available) {
    if (s["2022_q4"] && s["2022_q4"].pct != null) parts.push("2022Q4 " + s["2022_q4"].pct + "%");
    if (s.worst60_5y && s.worst60_5y.pct != null) parts.push("近5年最差60日 " + s.worst60_5y.pct + "%");
  }
  return parts.join("；") || "无风险旗标";
}

function riskBadge(r) {
  var s = r.risk_status;
  var title = riskTitle(r);
  if (s === "block" || r.risk_blocked) {
    return '<td class="small"><span class="badge bad" title="' + esc("已因独立风险门退出正式榜；" + title) + '">拦截</span></td>';
  }
  if (s === "watch") {
    return '<td class="small"><span class="badge warn" title="' + esc(title || "有观察项") + '">关注</span></td>';
  }
  if (s === "unknown") {
    return '<td class="small"><span class="badge warn" title="' + esc("持仓数据不足，不能证明集中度安全；" + title) + '">未知</span></td>';
  }
  if (s === "ok") return '<td class="small"><span class="badge good" title="' + esc(title) + '">通过</span></td>';
  return '<td class="small"><span class="muted">—</span></td>';
}

function rankTable(rows, start, showRank) {
  if (!rows.length) return notice("暂无数据");
  var annual = state.periodMode === "annual";
  var isLong = state.board === "bond_long";
  /* 序号 = 当前视图里的行序（跨分页连续，永远是 1、2、3…）；
     名次 = 本轮官方名次，同家族去重或换排序后会真实跳号，所以只在可能不一致时单列一列。 */
  var from = start || 0;
  var rankHead = showRank
    ? '<th class="num" title="本轮官方名次：同家族去重后保留真实跳号">名次</th>' : '';
  function cell(total, ann) {
    var v = annual ? (ann == null ? null : ann * 100) : total;
    var txt = annual ? (ann == null ? "—" : signPctDec(ann)) : signPctPoint(total);
    return '<td class="num ' + trendCls(v) + '">' + txt + '</td>';
  }
  var head = '<thead><tr><th class="num" title="当前视图行序">#</th>' + rankHead +
    '<th class="num" title="加入对比">比</th><th></th>' +
    sortTh("code", "代码", "") + sortTh("name", "名称", "") +
    sortTh("nav_date", "净值日期", "") +
    '<th class="num" title="365天指标截止日；早于有效净值日说明该基金指标还没跟上">指标日</th>' +
    sortTh("day_pct", "日涨跌") +
    sortTh("formal_score", "正式分") + sortTh("rank_score", "排序分") +
    sortTh("long_score", "长期分") + sortTh("style", "风格", "") +
    sortTh("ret_1w", "近1周") + sortTh("ret_1m", "近1月") + sortTh("ret_3m", "近3月") +
    sortTh("ret_6m", "近6月") + sortTh("annual_1y", "近1年") +
    sortTh("sharpe", "夏普") + sortTh("mdd", "1年回撤") +
    sortTh("mdd_3y", "3年回撤") +
    '<th class="num" title="3年指标实际覆盖的窗口/3年；低于 90% 不冒充完整 3 年">3年覆盖</th>' +
    '<th class="num" title="5年指标实际覆盖的窗口/5年；低于 90% 不冒充完整 5 年">5年覆盖</th>' +
    (isLong ? '<th class="num" title="5 年年化；为空表示 5 年窗口跨得够长但没有足够点数算出收益，不能当 5 年证据">5年年化</th>' : "") +
    '<th title="净值曲线是否可与同类按同一把尺子排序">可比性</th>' +
    '<th title="独立风险门：持仓集中度与压力测试；watch/unknown 只展示，block 已退出正式榜">风险</th>' +
    sortTh("worst90", "最差90日") +
    sortTh("scale", "家族规模(亿)") + sortTh("share_scale", "份额规模(亿)") +
    '<th class="num" title="现任经理在本基金的最短任职天数；缺失表示没有任职记录">经理最短</th>' +
    sortTh("fee", "成本% 1/3/5年") +
    sortTh("lockup_days", "持有期") +
    sortTh("confidence", "数据质量") + '</tr></thead>';
  var body = rows.map(function (r, i) {
    return '<tr class="clickable" onclick="location.hash=\'#/fund/' + esc(r.fcode) + '\'">' +
      '<td class="num">' + (from + i + 1) + '</td>' +
      (showRank ? '<td class="num">' + (r.rank == null ? "—" : r.rank) + '</td>' : '') +
      cmpBox(r.fcode) +
      '<td>' + starBtn(r.fcode) + '</td>' +
      '<td class="mono">' + esc(r.fcode) + '</td><td class="nm">' + esc(r.name) +
        (r.family_members > 1 ? ' <span class="badge mini" title="同一家族本轮入榜的份额数">同家族 ' +
          r.family_members + ' 只</span>' : '') + '</td>' +
      '<td class="mono small dt" title="' + esc(r.nav_date || "") + '">' + esc((r.nav_date || "").slice(5) || "—") + '</td>' +
      '<td class="mono small dt' + (r.metric_stale ? " down" : "") + '" title="' +
        esc(r.metric_stale ? "指标截止日早于本轮有效净值日" : "365天指标截止日") + '">' +
        esc((r.metric_end_date || "").slice(5) || "—") +
        (r.metric_stale ? ' <span class="badge warn">旧</span>' : '') + '</td>' +
      '<td class="num ' + trendCls(r.day_pct) + '">' + signPctPoint(r.day_pct) + '</td>' +
      '<td class="num">' + fmtNum(r.formal_score) + '</td>' +
      '<td class="num">' + fmtNum(r.rank_score) + '</td>' +
      '<td class="num">' + fmtNum(r.long_score) + '</td>' +
      '<td><span class="badge">' + esc(r.style_label || labelOf("styles", r.style_code)) + '</span></td>' +
      cell(r.ret_1w_pct, r.annual_1w_dec) + cell(r.ret_1m_pct, r.annual_1m_dec) +
      cell(r.ret_3m_pct, r.annual_3m_dec) + cell(r.ret_6m_pct, r.annual_6m_dec) +
      cell(r.ret_1y_pct, r.annual_1y) +
      '<td class="num">' + fmtCap(r.sharpe_1y, 5) + '</td>' +
      '<td class="num">' + fmtPct(r.mdd_1y) + '</td>' +
      '<td class="num">' + fmtPct(r.mdd_3y) + '</td>' +
      '<td class="num">' + (r.coverage_ratio_3y == null ? "—" :
        (r.is_full_3y ? "" : '<span class="muted">') + Math.round(r.coverage_ratio_3y * 100) + "%" +
        (r.is_full_3y ? "" : "</span>")) + '</td>' +
      '<td class="num">' + (r.coverage_ratio_5y == null ? "—" :
        (r.is_full_5y ? "" : '<span class="muted">') + Math.round(r.coverage_ratio_5y * 100) + "%" +
        (r.is_full_5y ? "" : "</span>")) + '</td>' +
      (isLong ? '<td class="num">' + (r.annual_5y == null ? "—" : signPctDec(r.annual_5y)) + '</td>' : "") +
      '<td class="small">' + (r.metric_comparable === false
        ? '<span class="badge warn" title="' + esc(metricFlagText(r.metric_flags)) + '">不可比</span>'
        : '<span class="muted">—</span>') + '</td>' +
      riskBadge(r) +
      '<td class="num ' + trendCls(r.worst_rolling90d) + '">' + signPctPoint(r.worst_rolling90d) + '</td>' +
      '<td class="num">' + fmtNum(r.asset_scale_billion) + '</td>' +
      '<td class="num">' + fmtNum(r.share_scale_billion) + '</td>' +
      '<td class="num">' + (r.manager_tenure_min_days == null ? "—" : fmtInt(r.manager_tenure_min_days)) + '</td>' +
      '<td class="num" title="持有1年 ' + fmtNum(r.cost_1y_pct) + '%；3年 ' + fmtNum(r.cost_3y_pct) +
        '%；5年 ' + fmtNum(r.cost_5y_pct) + '%；满1年赎回费 ' + fmtNum(r.redemption_1y_pct) + '%">' +
        fmtNum(r.cost_1y_pct) + ' / ' + fmtNum(r.cost_3y_pct) + ' / ' + fmtNum(r.cost_5y_pct) + '</td>' +
      '<td class="small">' + esc(r.lockup_days == null ? "无锁定" : (r.lockup_label + " " + r.lockup_days + " 天")) + '</td>' +
      '<td class="num">' + fmtNum(r.confidence, 1) + '</td></tr>';
  }).join("");
  return '<div class="table-wrap"><table>' + head + '<tbody>' + body + '</tbody></table></div>' +
    '<p class="muted small">点表头可排序；# 是当前视图的行序' +
    (showRank ? '，名次是本轮官方排名（同家族去重后会跳号）' : '') +
    '；回撤与最差 90 日是该区间内的最大跌幅，绿字表示下跌；' +
    (isLong ? '长期核心（观察）按长期分排序，5 年覆盖 ≥60% 且 5 年年化非空；它是影子榜，不是买入建议；' : '') +
    '3/5 年覆盖 &lt;90% 时以灰色显示，表示该行对应窗口不是完整窗口；份额规模是当前份额，不是家族汇总；' +
    '“不可比”指摊余成本定开、零风险净值、稀疏或过期净值，这类曲线不参与分位排序；' +
    '“风险”来自独立风险门，拦截表示已退出正式榜，关注/未知只提示、不改变收益分。</p>';
}

/* ── 榜单 ───────────────────────────────────────────── */
/* ── 跟踪榜（长期累积，规格书 §23.2） ───────────────── */
async function renderTracker() {
  $main.innerHTML =
    '<h1>跟踪榜</h1><p class="sub">以<strong>交易日</strong>为单位累积：一个交易日只记一份快照，' +
    '同一天重跑采集、重算指标后重新发布，以当天最后一次发布为准，不会把同一天算成多次。' +
    '新增 / 退出 / 回流按相邻两个交易日比对。频率 = 出现次数 ÷ 已记交易日数；' +
    '目前只积累了 1 个交易日，所以首批基金的出现次数都是 1、频率都是 100%——' +
    '它会随着每个交易日发布而变得有区分度。</p>' +
    '<div class="grid cols-4" id="trk-cards"></div>' +
    '<div class="toolbar"><span id="trk-meta" class="muted small"></span>' +
    '<label class="small"><input type="checkbox" id="trk-exited" onchange="loadTracker()"> 含已退出</label></div>' +
    '<div id="trk-body"><p class="loading">加载中…</p></div>';
  await loadTracker();
}

function trkChange(delta) {
  if (delta == null) return '<span class="muted">首次</span>';
  var v = Number(delta);
  if (Math.abs(v) < 1e-9) return '<span class="muted">持平</span>';
  var cls = v > 0 ? "up" : "down";
  return '<span class="' + cls + '">' + (v > 0 ? "+" : "") + v.toFixed(2) + '</span>';
}

async function loadTracker() {
  const body = document.getElementById("trk-body");
  if (!body) return;
  const chk = document.getElementById("trk-exited");
  const params = new URLSearchParams({ limit: "200" });
  if (chk && chk.checked) params.set("include_exited", "1");
  try {
    const res = await api("/tracker?" + params.toString());
    const d = res.data || {};
    const s = d.summary || {};
    const cards = document.getElementById("trk-cards");
    if (cards) {
      cards.innerHTML = stat("已记交易日", fmtInt(s.snapshots_total), "一个交易日一份，同日重发不叠加") +
        stat("在榜", fmtInt(s.active), "最新交易日的快照内") +
        stat("已退出", fmtInt(s.exited), "曾入榜、最新交易日不在榜") +
        stat("出现≥3次", fmtInt(s.appearances_ge_3), "跨交易日累积");
    }
    const meta = document.getElementById("trk-meta");
    if (meta) {
      meta.textContent = "共 " + fmtInt(d.total || 0) + " 只 · 最新交易日 " +
        ((d.latest_snapshot && d.latest_snapshot.business_date) || "—") +
        " · 已记交易日 " + fmtInt(d.snapshots_total || 0);
    }
    const rows = d.rows || [];
    if (!rows.length) { body.innerHTML = notice("暂无跟踪记录"); return; }
    var head = '<thead><tr><th class="num">当前</th><th>代码</th><th>名称</th><th>榜</th>' +
      '<th class="num">出现</th><th class="num">频率</th><th class="num">连续</th>' +
      '<th>首次入榜</th><th>最近在榜</th><th class="num">正式分</th>' +
      '<th class="num">较上次</th><th class="num">日涨跌</th><th>状态</th></tr></thead>';
    var bodyRows = rows.map(function (r) {
      return '<tr class="clickable" onclick="location.hash=\'#/fund/' + esc(r.fcode) + '\'">' +
        '<td class="num">' + (r.last_rank == null ? "—" : r.last_rank) + '</td>' +
        '<td class="mono">' + esc(r.fcode) + '</td>' +
        '<td class="nm">' + esc(r.name || "") + '</td>' +
        '<td><span class="badge">' + esc(r.board_label || r.last_board || "—") + '</span></td>' +
        '<td class="num">' + fmtInt(r.appearances) + '</td>' +
        '<td class="num">' + (r.frequency_pct == null ? "—" : fmtNum(r.frequency_pct, 1) + "%") + '</td>' +
        '<td class="num">' + fmtInt(r.streak) + '</td>' +
        '<td class="small">' + esc(r.first_seen_date || "") + '</td>' +
        '<td class="small">' + esc(r.last_seen_date || "") + '</td>' +
        '<td class="num">' + fmtNum(r.last_formal_score) + '</td>' +
        '<td class="num">' + trkChange(r.score_delta) + '</td>' +
        '<td class="num ' + trendCls(r.day_pct) + '">' + signPctPoint(r.day_pct) + '</td>' +
        '<td>' + esc(r.status_label || "") + '</td></tr>';
    }).join("");
    body.innerHTML = '<div class="table-wrap"><table>' + head + '<tbody>' + bodyRows +
      '</tbody></table></div>' +
      '<p class="muted small">「出现」= 在榜的交易日数；「连续」= 连续在榜的交易日数；' +
      '「较上次」= 本交易日正式分 − 上一交易日正式分（同日重发以最后一次为准，只算一天）。' +
      '跟踪榜只记正式榜（稳健 / 增强），持有期受限榜单列，不参与累积。</p>';
  } catch (err) {
    body.innerHTML = '<p class="err">' + esc(err.message) + '</p>';
  }
}

async function renderRankings() {
  const boards = [
    ["bond_stable", "稳健榜"], ["bond_enhanced", "增强榜"], ["bond_long", "长期核心（观察）"],
    ["bond_core", "核心精选（50）"],
    ["bond_core10", "收益精选（10）"], ["bond_rookie", "新锐榜（1–3 年）"],
    ["bond_watch", "观察榜"], ["bond_locked", "持有期受限"],
    ["bond_enhanced_watch", "增强观察池（研究）"],
  ];
  const styleOpts = (LABELS.styles || []).filter((s) => s.code !== "unknown");
  const sorts = [
    ["rank:asc", "排名"], ["formal_score:desc", "正式分"], ["annual_1y:desc", "1 年年化"],
    ["sharpe:desc", "夏普比率"], ["mdd:asc", "1 年回撤（小到大）"], ["scale:desc", "家族规模"],
    ["fee:asc", "费率（低到高）"], ["ret_1w:desc", "近 1 周"], ["ret_1m:desc", "近 1 月"],
    ["ret_3m:desc", "近 3 月"], ["ret_6m:desc", "近 6 月"],
  ];
  const cur = state.sort + ":" + state.order;
  $main.innerHTML =
    '<h1>榜单</h1><p class="sub">按排序分（综合分 85% + 近 6 月收益分位 15%）排序；正式分是次级键；数据质量分只展示、不参与排序；同一基金的不同份额已按家族折叠。' +
    '长期核心（观察）是影子榜：要求 5 年覆盖 ≥60% 且 5 年年化非空，按长期分（0.6×长期正式分 + 0.4×长期核心分）排序，不替换稳健榜。' +
    '核心精选榜从纯债/短债/信用/利率/指数五类中按排序分精选 50 只，不含增强债与可转债；会与稳健/增强榜重叠，不是另一套标准。' +
    '周期列可切换「区间收益 / 年化」——短周期年化是外推值，只用于横向比较，不代表未来。</p>' +
    '<div class="toolbar">' +
      boards.map(function (b) {
        return '<button class="' + (state.board === b[0] ? "primary" : "") +
          '" onclick="setBoard(\'' + b[0] + '\')">' + b[1] + '</button>';
      }).join("") +
      '<span class="sep"></span>' +
      '<input id="q" type="search" placeholder="代码或名称" value="' + esc(state.q) + '" style="width:170px">' +
      '<select id="style"><option value="">全部风格</option>' +
        styleOpts.map(function (s) {
          return '<option value="' + esc(s.code) + '"' + (state.style === s.code ? " selected" : "") +
            '>' + esc(s.label) + '</option>';
        }).join("") +
      '</select>' +
      '<select id="sort" onchange="setSortKey(this.value)">' +
        sorts.map(function (o) {
          return '<option value="' + o[0] + '"' + (o[0] === cur ? " selected" : "") + '>' +
            esc(o[1]) + '</option>';
        }).join("") +
      '</select>' +
      '<button class="primary" onclick="applyFilters()">筛选</button>' +
    '</div>' +
    '<div class="toolbar">' +
      '<span class="muted">显示</span>' +
      '<button class="' + (state.periodMode === "total" ? "primary" : "ghost") + '" onclick="setPeriodMode(\'total\')">区间收益</button>' +
      '<button class="' + (state.periodMode === "annual" ? "primary" : "ghost") + '" onclick="setPeriodMode(\'annual\')">年化</button>' +
      '<span class="sep"></span>' +
      '<label class="chk"><input type="checkbox" id="dedupe"' + (state.dedupe ? " checked" : "") +
        ' onchange="setDedupe(this.checked)"> 同家族只显示一只</label>' +
      '<span class="sep"></span>' +
      (state.board === "bond_enhanced_watch" ? "" :
        '<button class="ghost" onclick="exportRankingsCsv()" title="导出当前筛选与排序下的全部行（最多 5000 行）">导出 CSV</button>') +
      '<span class="sep"></span>' +
      '<span class="muted" id="rank-meta"></span>' +
      '<span class="sep"></span>' +
      '<span class="muted" id="cmp-bar"></span>' +
    '</div>' +
    '<div id="rank-body"><p class="loading">加载中…</p></div>' +
    '<div class="toolbar" style="margin-top:12px">' +
      '<button class="ghost" onclick="pageRank(-1)">上一页</button>' +
      '<button class="ghost" onclick="pageRank(1)">下一页</button>' +
    '</div>';
  renderCompareBar();
  await loadRankings();
}

function setSortKey(v) {
  var parts = String(v).split(":");
  setSortSpec([{ key: parts[0], order: parts[1] || "desc" }]);
}

async function loadRankings() {
  const body = document.getElementById("rank-body");
  if (!body) return;
  if (state.board === "bond_enhanced_watch") return loadEnhancedWatch(body);
  const sq = sortQuery();
  const params = new URLSearchParams({
    board: state.board, limit: state.limit, offset: state.offset,
    sort: sq.sort, order: sq.order,
  });
  if (state.q) params.set("q", state.q);
  if (state.style) params.set("style", state.style);
  if (state.dedupe) params.set("dedupe", "family");
  try {
    const res = await api("/rankings?" + params.toString());
    const meta = res.meta || {};
    const el = document.getElementById("rank-meta");
    if (el) {
      var bits = [];
      if (meta.nav_as_of) bits.push("净值 " + meta.nav_as_of);
      if (meta.metric_as_of) bits.push("指标 " + meta.metric_as_of);
      if (meta.freshness_status && meta.freshness_status !== "ok") bits.push("新鲜度 " + meta.freshness_status);
      if ((meta.freshness_warnings || []).length) bits.push(meta.freshness_warnings.length + " 项提示");
      el.textContent = (meta.total
        ? "共 " + fmtInt(meta.total) + " 条 · 第 " + (state.offset + 1) + "–" +
          (state.offset + (res.data || []).length) + " 条"
        : "共 0 条") + (bits.length ? " · " + bits.join(" · ") : "");
    }
    /* 只在"按名次排"且未去重时，序号与官方名次完全一致，不必多占一列。 */
    body.innerHTML = rankTable(res.data || [], state.offset,
      state.sort !== "rank" || !!state.dedupe);
    if (state.board === "bond_enhanced" && !(res.data || []).length) {
      body.innerHTML += await enhancedExplainer();
    }
  } catch (err) {
    body.innerHTML = '<p class="err">' + esc(err.message) + '</p>';
  }
}

/* 增强债观察池：只读 diagnostics/enhanced_near_miss.csv，research_only，不进正式榜 */
async function loadEnhancedWatch(body) {
  const metaEl = document.getElementById("rank-meta");
  try {
    const res = await api("/research/enhanced_watch?limit=36");
    const meta = res.meta || {};
    if (metaEl) metaEl.textContent = "研究池显示 " + fmtInt((res.data || []).length) + " / " +
      fmtInt(meta.total_near_miss || 0) + " 只（非正式入选）";
    const rows = res.data || [];
    if (!rows.length) {
      body.innerHTML = notice("观察池为空：没有距门槛 1 项以内的增强风格基金。", "warn") + await enhancedExplainer();
      return;
    }
    body.innerHTML =
      "<div class='notice warn' style='margin-bottom:10px'>" +
      "<b>这不是正式榜单。</b>增强债正式准入 7 项硬比较（年化 / Sharpe / Calmar / MDD / 日胜率 / 费率 / 规模）" +
      "在 1 年窗口下 0 只全过（ADR-017 不静默调阈值）。下表只列出距门槛 1 项以内的基金，供研究跟踪；" +
      "序号不代表推荐顺序。</div>" +
      "<div class='table-wrap'><table><thead><tr>" +
      ["代码", "名称", "风格", "通过", "差在哪", "1年年化", "Sharpe", "Calmar", "1年回撤", "日胜率"]
        .map((h) => "<th>" + h + "</th>").join("") +
      "</tr></thead><tbody>" +
      rows.map(function (r) {
        return "<tr><td class='mono'>" + esc(r.fcode) + "</td><td>" + esc(r.name) + "</td>" +
          "<td><span class='badge'>" + esc(labelOf("styles", r.style_code)) + "</span></td>" +
          "<td class='num'>" + esc(r.checks_passed) + "/" + esc(r.checks_total) + "</td>" +
          "<td>" + esc(checkCn(r.failed)) + "</td>" +
          "<td class='num'>" + fmtNum(r.annual_1y_pct) + "%</td>" +
          "<td class='num'>" + fmtNum(r.sharpe) + "</td>" +
          "<td class='num'>" + fmtNum(r.calmar) + "</td>" +
          "<td class='num'>" + fmtNum(Number(r.mdd) * 100) + "%</td>" +
          "<td class='num'>" + fmtNum(r.positive_pct, 1) + "%</td></tr>";
      }).join("") +
      "</tbody></table></div>";
  } catch (err) {
    body.innerHTML = "<p class='err'>" + esc(err.message) + "</p>";
  }
}

function setBoard(board) {
  state.board = board;
  state.offset = 0;
  loadSortPrefs();          /* 每个榜记住自己的排序 */
  renderRankings();
}
function setPeriodMode(mode) {
  state.periodMode = mode;
  if (state.view === "locked") { loadLocked(); return; }
  renderRankings();
}
function setDedupe(on) { state.dedupe = !!on; state.offset = 0; loadRankings(); }
function applyFilters() {
  state.q = (document.getElementById("q") || {}).value || "";
  state.style = (document.getElementById("style") || {}).value || "";
  state.offset = 0;
  loadRankings();
}
function pageRank(dir) {
  state.offset = Math.max(0, state.offset + dir * state.limit);
  loadRankings();
}

/* ── 基金详情 ───────────────────────────────────────── */
async function renderFund() {
  if (state.fund) {
    /* 已有代码时不再显示查询框（顶部全局搜索随时可换），把版面留给基金本身。 */
    $main.innerHTML = '<div id="fund-body"></div>';
    await loadFund(state.fund);
    return;
  }
  $main.innerHTML =
    '<h1>基金详情</h1><p class="sub">输入 6 位基金代码，或用顶部搜索框；数据来自本地融合层，未取到即为 NULL（不填 0）。</p>' +
    '<div class="toolbar">' +
    '<input id="fcode" type="text" placeholder="如 000032" style="width:160px">' +
    '<button class="primary" onclick="openFund()">查询</button>' +
    '<span class="muted">或在榜单里点一行</span></div>' +
    '<div id="fund-body">' + notice("先输入基金代码，或在榜单里点一只基金。") + "</div>";
}

function openFund() {
  const code = ((document.getElementById("fcode") || {}).value || "").trim();
  if (!code) return;
  state.fund = code;
  location.hash = "#/fund/" + code;
  loadFund(code);
}

async function loadFund(code) {
  const body = document.getElementById("fund-body");
  if (!body) return;
  state.navPage = 1;
  body.innerHTML = '<p class="loading">加载中…</p>';
  try {
    const res = await api("/funds/" + encodeURIComponent(code));
    state.fundCache = res.data;
    try {
      const dv = await api("/funds/" + encodeURIComponent(code) + "/decision");
      state.decisionCache = dv.data;
    } catch (e) {
      state.decisionCache = null;   /* 没有决策记录（例如目录里新出现、还没参与筛选） */
    }
    body.innerHTML = fundHero(res.data) + fundTabs();
    renderFundTab(state.fundTab || "overview");
  } catch (err) {
    body.innerHTML = '<p class="err">' + esc(err.message) + '</p>';
  }
}

function fundHero(d) {
  const f = d.fund || {}, p = d.profile || {}, L = d.labels || {}, n = d.latest_nav || {};
  const m1 = (d.metrics || []).filter(function (m) { return m.period_days === 365; })[0] || {};
  const pick = d.pick
    ? '<span class="badge good">' + esc(L.board || "入选") + " #" + d.pick.rank + "</span>"
    : '<span class="badge">未入选</span>';
  const buyCls = (d.buyable || {}).buyable_state === "confirmed_yes" ? "good" : "warn";
  return '<div class="hero"><div class="hero-main">' +
    '<div class="hero-kicker">' + esc(f.type_text || "债券型") + " · " + esc(f.company_name || "—") + "</div>" +
    '<h1>' + esc(f.name) + ' <span class="mono muted" style="font-size:15px">' + esc(f.fcode) + "</span></h1>" +
    '<div class="pill-row" style="margin:8px 0 2px">' +
      '<span class="badge accent">' + esc(L.style || "—") + "</span>" +
      '<span class="badge">' + esc(L.confidence || "—") + "</span>" +
      '<span class="badge ' + buyCls + '">' + esc(L.buyable || "—") + "</span>" +
      pick +
      starBtn(f.fcode) +
    "</div>" +
    '<div class="hero-stats">' +
      "<div><b>" + (n.unit_nav == null ? "—" : Number(n.unit_nav).toFixed(4)) + "</b><span>最新净值 " +
        esc(n.nav_date || "—") + (n.growth_pct == null ? "" : ' · <i class="' + trendCls(n.growth_pct) + '">' + signPctPoint(n.growth_pct) + "</i>") + "</span></div>" +
      '<div><b class="' + trendCls(m1.annual_dec) + '">' + signPctDec(m1.annual_dec) + "</b><span>近一年年化</span></div>" +
      "<div><b>" + fmtPct(m1.mdd_dec) + "</b><span>近一年最大回撤</span></div>" +
      "<div><b>" + fmtNum(p.asset_scale_billion) + "</b><span>家族规模（亿元）</span></div>" +
    "</div></div></div>";
}

const FUND_TABS = [["overview", "概览"], ["decision", "决策"], ["performance", "业绩"],
  ["nav", "净值明细"], ["risk", "风险"], ["factors", "因子"], ["holdings", "持仓"],
  ["fees", "费用"], ["managers", "经理"], ["profile", "档案"]];

function fundTabs() {
  return '<div class="tabs">' + FUND_TABS.map(function (t) {
    return '<button class="tab' + (state.fundTab === t[0] ? " active" : "") + '" data-tab="' + t[0] +
      '" onclick="setFundTab(this.dataset.tab)">' + t[1] + "</button>";
  }).join("") + '</div><div id="fund-tab-body" class="tab-panel"></div>';
}

function setFundTab(name) {
  state.fundTab = name;
  /* 深链：地址栏同步 ?tab=，刷新或分享后仍停在同一页签（replaceState 不触发 hashchange）。 */
  if (state.fund) {
    try { history.replaceState(null, "", "#/fund/" + state.fund + "?tab=" + name); } catch (e) { /* 忽略 */ }
  }
  document.querySelectorAll(".tabs .tab").forEach(function (b) {
    b.classList.toggle("active", b.getAttribute("data-tab") === name);
  });
  renderFundTab(name);
}

function renderFundTab(name) {
  const d = state.fundCache;
  const box = document.getElementById("fund-tab-body");
  if (!d || !box) return;
  const code = (d.fund || {}).fcode || state.fund;
  if (name === "overview") {
    box.innerHTML =
      '<div class="grid cols-2"><div>' + overviewMetrics(d.metrics) + "</div>" +
      '<div id="peer-box"><p class="loading">加载同类分位…</p></div></div>' +
      navCard("nav-chart") + decisionBlock(d);
    loadPeer(code);
    loadNavChart(code, "nav-chart");
  } else if (name === "decision") {
    box.innerHTML = rankHistoryCard() + decisionCard(state.decisionCache);
    loadRankHistory(code);
  } else if (name === "performance") {
    box.innerHTML = stageTable(d.metrics) + annualBarsCard() + periodCard();
    loadAnnualBars(code);
    loadPeriods(code, state.periodGran);
  } else if (name === "nav") {
    state.navBounds = d.nav_bounds || null;
    box.innerHTML = navCard("nav-chart-nav") + navRowsCard();
    loadNavChart(code, "nav-chart-nav");
    loadNavRows(code);
  } else if (name === "risk") {
    box.innerHTML = riskTable(d.metrics) + tailCard(d.tail) + drawdownCard();
    loadDrawdown(code);
  } else if (name === "factors") {
    box.innerHTML = factorsBlock(d.factors);
  } else if (name === "holdings") {
    box.innerHTML = holdingsBlock(d.holdings, d.holding_summary, d.holding_history);
  } else if (name === "fees") {
    box.innerHTML = costCard(code) + feeScheduleTable(d.fee_schedule);
    setTimeout(function () { calcCost(code); }, 0);
  } else if (name === "managers") {
    box.innerHTML = managersBlock(d.managers, d.manager_tenure) || notice("暂无经理数据。");
  } else if (name === "profile") {
    box.innerHTML = profileBlock(d);
  }
}

const PERIOD_LABELS = { 7: "近 1 周", 30: "近 1 月", 90: "近 3 月", 180: "近半年", 365: "近 1 年",
  730: "近 2 年", 1095: "近 3 年", 1460: "近 4 年", 1825: "近 5 年" };

function overviewMetrics(metrics) {
  const rows = (metrics || []).map(function (m) {
    return "<tr><td>" + esc(PERIOD_LABELS[m.period_days] || m.period_days + " 天") + "</td>" +
      '<td class="num ' + trendCls(m.annual_dec) + '">' + signPctDec(m.annual_dec) + "</td>" +
      '<td class="num">' + fmtPct(m.mdd_dec) + "</td>" +
      '<td class="num">' + fmtCap(m.sharpe, 5) + "</td>" +
      '<td class="num">' + (m.n_obs == null ? "—" : m.n_obs) + "</td></tr>";
  }).join("");
  return '<div class="card"><h2 style="margin-top:0">九周期风险收益</h2>' +
    '<div class="table-wrap" style="max-height:none"><table><thead><tr><th>窗口</th>' +
    sortTh("annual_1y", "年化") + sortTh("mdd", "最大回撤") + sortTh("sharpe", "夏普") +
    sortTh("n_obs", "观测") +
    "</tr></thead><tbody>" + rows + "</tbody></table></div>" +
    '<p class="code-hint">年化按 365.25 天口径；夏普用 rf=1.64%、为本项目策略口径；未达最小观测数的窗口显示 —。</p></div>';
}

function stageTable(metrics) {
  const rows = (metrics || []).map(function (m) {
    return "<tr><td>" + esc(PERIOD_LABELS[m.period_days] || m.period_days + " 天") + "</td>" +
      '<td class="num ' + trendCls(m.total_return_pct) + '">' + signPctPoint(m.total_return_pct) + "</td>" +
      '<td class="num ' + trendCls(m.annual_dec) + '">' + signPctDec(m.annual_dec) + "</td>" +
      '<td class="num">' + fmtPct(m.vol_dec) + "</td>" +
      '<td class="num">' + fmtPct(m.mdd_dec) + "</td>" +
      '<td class="num">' + fmtPctPoint(m.positive_ratio_pct, 1) + "</td>" +
      '<td class="num">' + fmtPctPoint(m.non_negative_ratio_pct, 1) + "</td></tr>";
  }).join("");
  return '<div class="card"><h2 style="margin-top:0">阶段涨幅</h2>' +
    '<div class="table-wrap" style="max-height:none"><table><thead><tr><th>区间</th>' +
    sortTh("ret_1y", "累计收益") + sortTh("annual_1y", "年化") + sortTh("vol", "波动率") +
    sortTh("mdd", "最大回撤") + sortTh("positive", "日胜率") +
    sortTh("non_negative", "非负日占比") +
    "</tr></thead><tbody>" + rows +
    "</tbody></table></div>" +
    '<p class="code-hint">累计收益按日增长率链式复利（含分红再投），不是单位净值首末差。</p></div>';
}

function annualBarsCard() {
  return '<h2>年度收益</h2><div class="card" id="annual-bars"><p class="loading">加载中…</p></div>';
}

/* 九周期风险收益全表：累计/年化/波动/回撤/夏普/索提诺/卡玛/日胜率/观测。 */
function riskTable(metrics) {
  const rows = (metrics || []).map(function (m) {
    const obs = (m.n_obs == null ? "—" : fmtInt(m.n_obs)) +
      (m.min_points_used == null ? "" : ' <span class="muted">/ ' + fmtInt(m.min_points_used) + "</span>");
    return "<tr><td>" + esc(PERIOD_LABELS[m.period_days] || m.period_days + " 天") + "</td>" +
      '<td class="num ' + trendCls(m.total_return_pct) + '">' + signPctPoint(m.total_return_pct) + "</td>" +
      '<td class="num ' + trendCls(m.annual_dec) + '">' + signPctDec(m.annual_dec) + "</td>" +
      '<td class="num">' + fmtPct(m.vol_dec) + "</td>" +
      '<td class="num">' + fmtPct(m.mdd_dec) + "</td>" +
      '<td class="num">' + fmtCap(m.sharpe, 5) + "</td>" +
      '<td class="num">' + fmtCap(m.sortino, 10) + "</td>" +
      '<td class="num">' + fmtCap(m.calmar_dec, 20) + "</td>" +
      '<td class="num">' + fmtPctPoint(m.positive_ratio_pct, 1) + "</td>" +
      '<td class="num">' + fmtPctPoint(m.non_negative_ratio_pct, 1) + "</td>" +
      '<td class="num">' + obs + "</td></tr>";
  }).join("");
  return '<div class="card"><h2 style="margin-top:0">九周期风险收益</h2>' +
    '<div class="table-wrap" style="max-height:none"><table><thead><tr><th>窗口</th>' +
    sortTh("ret_1y", "累计收益") + sortTh("annual_1y", "年化") + sortTh("vol", "波动率") +
    sortTh("mdd", "最大回撤") + sortTh("sharpe", "夏普") + sortTh("sortino", "索提诺") +
    sortTh("calmar", "卡玛") + sortTh("positive", "日胜率") +
    sortTh("non_negative", "非负日占比") + sortTh("n_obs", "观测") +
    "</tr></thead><tbody>" + rows + "</tbody></table></div>" +
    '<p class="code-hint">带 * 的夏普/索提诺/卡玛是策略口径封顶值（±5 / ±10 / ±20）；' +
    "观测列斜杠后是该窗口的最小点数门槛（按序列自身节奏折算）。</p></div>";
}

function drawdownCard() {
  return '<h2>回撤曲线（近 3 年）</h2><div class="card" id="dd-chart"><p class="loading">加载中…</p></div>';
}

function decisionBlock(d) {
  const L = d.labels || {}, dec = d.decision || {};
  const reasons = (L.reasons && L.reasons.length) ? L.reasons : [];
  const pick = d.pick;
  return '<div class="card"><h2 style="margin-top:0">筛选结论</h2>' +
    '<dl class="kv">' +
    "<dt>结论</dt><dd>" + (pick
      ? '<span class="badge good">' + esc(L.board || "入选") + " #" + pick.rank + "</span> 正式分 " + fmtNum(pick.formal_score) +
        " · 数据质量 " + fmtNum(pick.confidence, 1)
      : '<span class="badge">未进入正式榜</span>') + "</dd>" +
    "<dt>最远通过层</dt><dd>" + esc(L.layer || "—") + (dec.passed ? " · 通过" : "") + "</dd>" +
    "<dt>风格判定</dt><dd>" + esc(L.style || "—") + " · " + esc(L.confidence || "—") + "</dd>" +
    "<dt>规模口径</dt><dd>" + esc(L.scale_source || "—") + "</dd>" +
    "</dl>" +
    (reasons.length ? '<div class="pill-row" style="margin-top:10px">' + reasons.map(function (r) {
      return '<span class="badge warn" title="' + esc(r.code) + '">' + esc(r.label) + "</span>";
    }).join("") + "</div>" : "") +
    '<p class="code-hint">阈值快照与逐项得分在发布产物 decisions/&lt;代码&gt;.json 中；本页只显示结论。</p></div>';
}

function profileBlock(d) {
  const f = d.fund || {}, p = d.profile || {}, L = d.labels || {}, b = d.buyable || {}, nb = d.nav_bounds || {};
  return '<div class="card"><h2 style="margin-top:0">基金档案</h2><dl class="kv">' +
    "<dt>基金全称</dt><dd>" + esc(p.full_name || f.name || "—") + "</dd>" +
    "<dt>基金代码</dt><dd class=\"mono\">" + esc(f.fcode) + "</dd>" +
    "<dt>类型</dt><dd>" + esc(f.type_text || "—") + "</dd>" +
    "<dt>基金公司</dt><dd>" + esc(f.company_name || "—") + "</dd>" +
    "<dt>成立日</dt><dd class=\"mono\">" + esc(f.estab_date || "—") + "</dd>" +
    "<dt>家族规模</dt><dd>" + fmtNum(p.asset_scale_billion) + " 亿元 <span class=\"muted\">（" +
      esc(L.scale_source || "—") + (p.share_class_scale_billion != null ? " · 本份额 " + fmtNum(p.share_class_scale_billion) + " 亿" : "") + "）</span></dd>" +
    "<dt>持续计提费</dt><dd>" + fmtNum(p.recurring_fee_pct) + " % <span class=\"muted\">管理 " +
      fmtNum(p.mgr_fee_pct) + " + 托管 " + fmtNum(p.trust_fee_pct) + " + 销售 " + fmtNum(p.sales_fee_pct) + "</span></dd>" +
    "<dt>申赎费（最高档）</dt><dd>" + fmtNum(p.max_sub_fee_pct) + " % / " + fmtNum(p.max_red_fee_pct) + " %</dd>" +
    "<dt>业绩基准</dt><dd>" + esc(p.benchmark_text || "—") + "</dd>" +
    "<dt>跟踪标的</dt><dd>" + esc(p.tracking_text || "—") + "</dd>" +
    "<dt>申购状态</dt><dd>" + esc(b.purchase_status || "—") + " · " + esc(L.buyable || "—") +
      (b.next_open_date ? '<span class="muted">（下一开放日 ' + esc(b.next_open_date) + "）</span>" : "") + "</dd>" +
    "<dt>赎回状态</dt><dd>" + esc(b.redemption_status || "—") + "</dd>" +
    "<dt>最低申购</dt><dd>" + (b.min_purchase == null ? "—" : fmtInt(b.min_purchase) + " 元") + "</dd>" +
    "<dt>净值区间</dt><dd class=\"mono\">" + esc(nb.first_date || "—") + " → " + esc(nb.last_date || "—") +
      ' <span class="muted">' + fmtInt(nb.n_obs) + " 个净值点</span></dd>" +
    "</dl>" +
    '<p class="code-hint">规模、费率、基准来自基金基本概况（jbgk）；申购状态来自当日全市场申赎快照；' +
    "净值来自基金档案接口（pingzhongdata）全历史。</p></div>";
}

function managersBlock(rows, tenures) {
  if ((!rows || !rows.length) && (!tenures || !tenures.length)) return "";
  var html = "";
  if (rows && rows.length) {
    html += '<h2>现任经理（' + rows.length + '）</h2>' +
      '<div class="table-wrap" style="max-height:none"><table><thead><tr><th>姓名</th><th>角色</th>' +
      '<th class="num">职业年限</th><th class="num">任期收益</th><th class="num">东财评分</th><th class="num">星级</th></tr></thead><tbody>' +
      rows.map(function (m) {
        return '<tr><td>' + esc(m.mgr_name || m.mgr_id) + '</td>' +
          '<td>' + (m.is_lead ? '<span class="badge accent">主理</span>' : '<span class="badge">共同</span>') + '</td>' +
          '<td class="num">' + (m.career_days == null ? "—" : (m.career_days / 365).toFixed(1) + " 年") + '</td>' +
          '<td class="num">' + fmtNum(m.tenure_return_pct) + '%</td>' +
          '<td class="num">' + fmtNum(m.power_avr, 1) + '</td>' +
          '<td class="num">' + (m.star == null ? "—" : m.star) + '</td></tr>';
      }).join("") + '</tbody></table></div>';
  }
  if (tenures && tenures.length) {
    html += '<h2 style="margin-top:18px">历任经理（' + tenures.length + '）</h2>' +
      '<div class="table-wrap" style="max-height:none"><table><thead><tr><th>姓名</th>' +
      '<th>起始</th><th>截止</th><th class="num">任职天数</th><th>状态</th></tr></thead><tbody>' +
      tenures.map(function (t) {
        return '<tr><td>' + esc(t.mgr_name || t.mgr_id) + '</td>' +
          '<td class="mono">' + esc(t.start_date || "—") + '</td>' +
          '<td class="mono">' + (t.end_date ? esc(t.end_date) : "至今") + '</td>' +
          '<td class="num">' + (t.days == null ? "—" : fmtInt(t.days)) + '</td>' +
          '<td>' + (t.is_current ? '<span class="badge accent">在任</span>' : '<span class="badge">已离任</span>') + '</td></tr>';
      }).join("") + '</tbody></table></div>';
  }
  html += '<p class="code-hint">职业年限取自东财 workTime（证券从业年限，不等于本基金任职年限）；东财评分是 0–100 综合分，<b>不是 MGOLD 10 分制</b>，两者不互相映射；历任经理来自 jjjl，任职天数按 as-of 重算，缺失一律显示 —。</p>';
  return html;
}

function factorsBlock(f) {
  const head = '<h2>风格因子暴露</h2>';
  if (!f) {
    return head + notice("这只基金还没有因子暴露（可能是指数日线尚未采集，或样本不足 60 个共同交易日）。先跑 python3 -m fundana collect --domain index，再跑 python3 -m fundana factors。");
  }
  const betas = f.betas || {};
  const pct = f.percentiles || {};
  const labels = f.labels || {};
  const notes = f.notes || {};
  const keys = ["duration", "credit", "convertible"];
  const hasBeta = keys.some(function (k) { return betas[k] != null; });
  const win = f.window || {};
  const summary = '<p class="sub">截至 ' + esc(f.as_of || "—") + ' · 窗口 ' +
    esc(win.start_date || "—") + " ~ " + esc(win.end_date || "—") + " · 样本 " +
    fmtInt(win.n_obs) + " 个交易日 · R² " +
    (win.r2 == null ? "—" : Number(win.r2).toFixed(3)) + " · 同批 " + fmtInt(f.peer_n) + " 只</p>";
  if (!hasBeta) {
    return head + summary + notice("这只基金对三因子的解释力不足（R² < 5%），beta 不可信，已留空不展示。", "warn");
  }
  const cards = keys.map(function (k) {
    const v = betas[k];
    const p = pct[k];
    const hint = (p == null ? "" : "同批分位 " + Math.round(p * 100) + "% · ") + (notes[k] || "");
    return stat(labels[k] || k, v == null ? "—" : Number(v).toFixed(2), hint);
  }).join("");
  return head + summary +
    '<div class="grid cols-3">' + cards + "</div>" +
    '<p class="code-hint">beta 是标准化暴露（基金与因子都按标准差缩放，≈相关系数，-1..1）：' +
    "久期看国债、信用看企债剔除国债后的残差、转债看中证转债；三因子已正交化，避免国债与信用共线。" +
    "R² 是模型对这只基金日收益的解释比例；R² < 5% 时不展示 beta。因子是同期暴露，不是收益预测。</p>";
}

function holdingsBlock(rows, summary, history) {
  const head = '<h2>债券持仓（最近报告期）</h2>';
  const donutHtml = donut(summary);
  const historyHtml = holdingHistoryBlock(history);
  if (!rows || !rows.length) {
    return head + (donutHtml || notice("这只基金没有采集到债券持仓明细（可能未披露或不在采集范围）。")) + historyHtml;
  }
  return head + (donutHtml ? '<div class="card">' + donutHtml + "</div>" : "") +
    '<div class="table-wrap"><table><thead><tr>' +
    "<th>债券代码</th><th>名称</th><th>类别</th><th class=\"num\">占净值</th>" +
    '<th class="num">市值(万元)</th><th>报告期</th></tr></thead><tbody>' +
    rows.map(function (h) {
      return '<tr><td class="mono">' + esc(h.bond_code) + "</td><td>" + esc(h.bond_name) + "</td>" +
        '<td><span class="badge">' + esc(labelOf("bond_classes", h.bond_class)) + "</span></td>" +
        '<td class="num">' + fmtNum(h.pct_nav) + "%</td>" +
        '<td class="num">' + fmtInt(h.market_value_wan) + "</td>" +
        '<td class="mono">' + esc(h.report_period) + "</td></tr>";
    }).join("") + "</tbody></table></div>" +
    '<p class="code-hint">只显示占净值前 15 只；类别按债券代码前缀与名称判定（可转债 / 利率债 / 信用债），用于风格证据，不是官方分类。</p>' +
    historyHtml;
}

function holdingHistoryBlock(history) {
  if (!history || !history.length) return "";
  const classes = ["convertible", "rate", "cd", "credit", "other"];
  const head = '<h2 style="margin-top:22px">券种结构变化（最近 ' + history.length + ' 个报告期）</h2>';
  const rows = history.map(function (h) {
    const s = h.shares || {};
    const top = (h.top || []).map(function (x) {
      return esc(x.bond_name || x.bond_code) + " " + fmtNum(x.pct_nav) + "%";
    }).join(" · ");
    return "<tr><td class=\"mono\">" + esc(h.period) + "</td>" +
      classes.map(function (c) {
        return '<td class="num">' + (s[c] == null ? "—" : fmtNum(s[c] * 100) + "%") + "</td>";
      }).join("") +
      '<td class="num">' + fmtNum(h.total_pct) + "%</td><td>" + top + "</td></tr>";
  }).join("");
  return head + '<div class="table-wrap"><table><thead><tr><th>报告期</th>' +
    classes.map(function (c) { return "<th class=\"num\">" + esc(labelOf("bond_classes", c)) + "</th>"; }).join("") +
    '<th class="num">前五合计</th><th>前三大重仓</th></tr></thead><tbody>' + rows +
    "</tbody></table></div>" +
    '<p class="code-hint">按季报前五大债券市值计算占比；数据只覆盖已采集季度，缺失季度不补零。</p>';
}

function donut(summary) {
  const items = (summary || []).filter(function (x) { return x.pct_nav > 0; });
  if (!items.length) return "";
  const total = items.reduce(function (a, b) { return a + b.pct_nav; }, 0) || 1;
  const colors = ["#1d4ed8", "#0e9f6e", "#d92b2b", "#b54708", "#7c3aed", "#0891b2"];
  const R = 54, CIRC = 2 * Math.PI * R;
  let acc = 0;
  const arcs = items.map(function (x, i) {
    const frac = x.pct_nav / total;
    const seg = '<circle r="' + R + '" cx="70" cy="70" fill="none" stroke="' + colors[i % colors.length] +
      '" stroke-width="20" stroke-dasharray="' + (frac * CIRC).toFixed(2) + " " + CIRC.toFixed(2) +
      '" stroke-dashoffset="' + (-acc * CIRC).toFixed(2) +
      '" transform="rotate(-90 70 70)"></circle>';
    acc += frac;
    return seg;
  }).join("");
  const legend = items.map(function (x, i) {
    return '<div class="bar-row"><span><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:' +
      colors[i % colors.length] + ';margin-right:6px"></i>' + esc(x.label) + "</span>" +
      '<span class="num">' + fmtNum(x.pct_nav) + "% · " + fmtInt(x.count) + " 只</span></div>";
  }).join("");
  return '<div class="split"><div><svg viewBox="0 0 140 140" width="180" height="180" role="img" aria-label="持仓类别占比">' +
    arcs + '<text x="70" y="66" text-anchor="middle" class="axis">债券持仓</text>' +
    '<text x="70" y="84" text-anchor="middle" style="font-size:13px;font-weight:600">' + fmtNum(total) + "%</text></svg></div>" +
    "<div>" + legend + '<p class="code-hint">占净值合计只统计已披露的债券明细，其余资产（现金、其他）不在其中。</p></div></div>';
}

function feeScheduleTable(schedule) {
  const sc = schedule || {};
  const meta = sc.meta || {};
  const purchase = sc.purchase || [];
  const redemption = sc.redemption || [];
  if (!purchase.length && !redemption.length) {
    return '<h2>费率阶梯</h2>' + notice("这只基金还没有采集到费率档位（jjfl 页面缺失或未在采集范围）。");
  }
  function amountRange(t) {
    const lo = t.amount_lo_cny, hi = t.amount_hi_cny;
    if (lo == null && hi == null) return "—";
    if (hi == null) return "≥ " + fmtInt(lo) + " 元";
    if (!lo) return "< " + fmtInt(hi) + " 元";
    return fmtInt(lo) + " – " + fmtInt(hi) + " 元";
  }
  function daysRange(t) {
    const lo = t.days_lo, hi = t.days_hi;
    if (lo == null && hi == null) return "—";
    if (hi == null) return "≥ " + fmtInt(lo) + " 天";
    if (lo == null) return "< " + fmtInt(hi) + " 天";
    return fmtInt(lo) + " – " + fmtInt(hi) + " 天";
  }
  function rate(t) {
    if (t.fixed_cny != null && t.rate_pct == null) return "每笔 " + fmtInt(t.fixed_cny) + " 元";
    return fmtNum(t.rate_pct) + " %" + (t.discount_pct != null ? "（折后 " + fmtNum(t.discount_pct) + " %）" : "");
  }
  return '<h2>费率阶梯</h2><div class="split">' +
    '<div class="card"><h3 style="margin-top:0">申购费率</h3><div class="table-wrap" style="max-height:none">' +
    '<table><thead><tr><th>金额档</th><th class="num">费率</th></tr></thead><tbody>' +
    purchase.map(function (t) {
      return "<tr><td>" + amountRange(t) + '</td><td class="num">' + rate(t) + "</td></tr>";
    }).join("") + "</tbody></table></div></div>" +
    '<div class="card"><h3 style="margin-top:0">赎回费率</h3><div class="table-wrap" style="max-height:none">' +
    '<table><thead><tr><th>持有天数</th><th class="num">费率</th></tr></thead><tbody>' +
    redemption.map(function (t) {
      return "<tr><td>" + daysRange(t) + '</td><td class="num">' + rate(t) + "</td></tr>";
    }).join("") + "</tbody></table></div></div></div>" +
    '<p class="code-hint">运作费：管理 ' + fmtNum(meta.mgr_fee_pct) + "% + 托管 " + fmtNum(meta.trust_fee_pct) +
    "% + 销售服务 " + fmtNum(meta.sales_fee_pct) + "%。申购费按外扣、赎回费按内扣；持有期内已计提的费用体现在净值里，不重复扣。</p>";
}

async function loadPeer(code) {
  const box = document.getElementById("peer-box");
  if (!box) return;
  try {
    const res = await api("/funds/" + encodeURIComponent(code) + "/peer");
    const d = res.data || {};
    const items = d.items || [];
    if (!items.length) { box.innerHTML = '<div class="card">' + notice("同类样本不足，暂不做分位比较。") + "</div>"; return; }
    box.innerHTML = '<div class="card"><h2 style="margin-top:0">同类分位 · ' + esc(d.style_label || "") + "</h2>" +
      '<p class="sub" style="margin:0 0 10px">同风格 ' + fmtInt(d.n_peers) + " 只，按近一年指标比较。</p>" +
      items.map(function (it) {
        const pct = it.percentile == null ? null : it.percentile * 100;
        let val = "—";
        if (it.value != null) {
          if (it.unit === "pct") val = fmtPct(it.value);
          else if (it.unit === "pctpoint") val = fmtNum(it.value, 2) + "%";
          else val = fmtNum(it.value);
        }
        const tone = pct == null ? "" : (pct >= 50 ? "good" : "warn");
        return '<div class="bar-row"><span>' + esc(it.label) + '</span>' +
          '<span class="bar-track"><span class="bar" style="display:block;width:' +
          (pct == null ? 0 : pct).toFixed(1) + '%"></span></span>' +
          '<span class="num">' + val + "</span>" +
          '<span class="badge ' + tone + '">' + (pct == null ? "样本不足" : "好于 " + pct.toFixed(1) + "% 同类") + "</span></div>";
      }).join("") +
      '<p class="code-hint">分位来自同风格近一年指标的横截面；样本少于 ' + fmtInt(res.meta ? res.meta.min_peers : 20) + " 只时不做分位。</p></div>";
  } catch (e) {
    box.innerHTML = '<div class="card"><p class="err">' + esc(e.message) + "</p></div>";
  }
}

async function loadDrawdown(code) {
  const el = document.getElementById("dd-chart");
  if (!el) return;
  try {
    const res = await api("/funds/" + encodeURIComponent(code) + "/drawdown?days=1095");
    el.innerHTML = ddSvg(res.data || {});
  } catch (e) {
    el.innerHTML = '<p class="err">' + esc(e.message) + "</p>";
  }
}

function ddSvg(d) {
  const pts = d.points || [];
  if (pts.length < 2) return '<p class="empty">暂无回撤数据</p>';
  const w = 900, h = 200, padL = 48, padR = 12, padT = 12, padB = 24;
  const ys = pts.map(function (p) { return p.dd_pct; });
  const lo = Math.min.apply(null, ys.concat([0]));
  const hi = 0;
  const span = Math.max(0.1, hi - lo);
  function X(i) { return padL + i * (w - padL - padR) / (pts.length - 1); }
  function Y(v) { return padT + (hi - v) * (h - padT - padB) / span; }
  const line = pts.map(function (p, i) {
    return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(p.dd_pct).toFixed(1);
  }).join(" ");
  const area = line + " L " + X(pts.length - 1).toFixed(1) + " " + Y(0).toFixed(1) +
    " L " + X(0).toFixed(1) + " " + Y(0).toFixed(1) + " Z";
  const ticks = [0, 0.5, 1].map(function (f) {
    const v = hi + (lo - hi) * f;
    return '<text x="' + (padL - 6) + '" y="' + (Y(v) + 4).toFixed(1) +
      '" text-anchor="end" class="axis">' + v.toFixed(2) + "%</text>";
  }).join("");
  const xl = [0, 0.5, 1].map(function (f) {
    const i = Math.min(pts.length - 1, Math.round(f * (pts.length - 1)));
    const anchor = f === 0 ? "start" : (f === 1 ? "end" : "middle");
    return '<text x="' + X(i).toFixed(1) + '" y="' + (h - 6) +
      '" text-anchor="' + anchor + '" class="axis">' + esc(pts[i].date) + "</text>";
  }).join("");
  return '<div class="chart-head"><span class="down">最大回撤 ' + fmtPctPoint(d.max_dd_pct, 2) + "</span>" +
    '<span class="muted">最低点 ' + esc(d.max_dd_date || "—") + " · 当前 " +
    fmtPctPoint(d.current_dd_pct, 2) + " · " + fmtInt(pts.length) + " 个观测</span></div>" +
    '<svg viewBox="0 0 ' + w + " " + h + '" role="img" aria-label="回撤曲线">' +
    '<path d="' + area + '" class="area down-area"></path><path d="' + line + '" class="line down"></path>' +
    ticks + xl + "</svg>";
}

async function loadAnnualBars(code) {
  const el = document.getElementById("annual-bars");
  if (!el) return;
  try {
    const res = await api("/funds/" + encodeURIComponent(code) + "/periods?granularity=year&limit=12");
    const rows = ((res.data || {}).rows || []).slice().reverse();
    if (!rows.length) { el.innerHTML = notice("没有可聚合的年度数据"); return; }
    const maxAbs = Math.max.apply(null, rows.map(function (r) { return Math.abs(r.chain_return_pct || 0); }).concat([1]));
    el.innerHTML = rows.map(function (r) {
      const v = r.chain_return_pct || 0;
      const width = Math.max(1.5, Math.abs(v) / maxAbs * 100);
      return '<div class="bar-row"><span>' + esc(r.period_label) +
        (r.partial ? ' <span class="badge warn">未结束</span>' : "") + "</span>" +
        '<span class="bar-track"><span class="bar ' + (v >= 0 ? "up" : "down") +
        '" style="display:block;width:' + width.toFixed(1) + '%"></span></span>' +
        '<span class="num ' + trendCls(v) + '">' + signPctPoint(v) + "</span></div>";
    }).join("") + '<p class="code-hint">按日增长率链式复利，含分红再投；与单位净值涨跌的差额来自分红。</p>';
  } catch (e) {
    el.innerHTML = '<p class="err">' + esc(e.message) + "</p>";
  }
}

function safeJson(text, fallback) {
  try { return JSON.parse(text); } catch { return fallback; }
}

function fmtDays(v) { return v == null ? '—' : fmtInt(v) + ' 天'; }

function navCard(id) {
  return '<h2>净值曲线（近 3 年）</h2><div class="card" id="' + (id || "nav-chart") +
    '"><p class="loading">加载中…</p></div>';
}

function navSvg(d) {
  const pts = d.points || [];
  if (pts.length < 2) return '<p class="empty">暂无净值数据</p>';
  const w = 900, h = 230, padL = 56, padR = 12, padT = 10, padB = 26;
  const ys = pts.map(function (p) { return p.unit_nav; });
  let lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
  if (hi - lo < 1e-6) { hi = lo + 0.001; lo -= 0.001; }
  const span = hi - lo; lo -= span * 0.06; hi += span * 0.06;
  function X(i) { return padL + i * (w - padL - padR) / (pts.length - 1); }
  function Y(v) { return padT + (hi - v) * (h - padT - padB) / (hi - lo); }
  const line = pts.map(function (p, i) {
    return (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(p.unit_nav).toFixed(1);
  }).join(" ");
  const area = line + " L " + X(pts.length - 1).toFixed(1) + " " + (h - padB) +
    " L " + X(0).toFixed(1) + " " + (h - padB) + " Z";
  const gl = [0, 0.5, 1].map(function (f) {
    const i = Math.min(pts.length - 1, Math.round(f * (pts.length - 1)));
    const anchor = f === 0 ? "start" : (f === 1 ? "end" : "middle");
    return '<text x="' + X(i).toFixed(1) + '" y="' + (h - 8) +
      '" text-anchor="' + anchor + '" class="axis">' + esc(pts[i].nav_date) + "</text>";
  }).join("");
  const yl = [0, 0.5, 1].map(function (f) {
    const v = lo + (hi - lo) * f;
    return '<text x="' + (padL - 8) + '" y="' + (Y(v) + 4).toFixed(1) +
      '" text-anchor="end" class="axis">' + v.toFixed(4) + "</text>";
  }).join("");
  const first = pts[0], last = pts[pts.length - 1];
  const chg = (last.unit_nav / first.unit_nav - 1) * 100;
  const cls = chg >= 0 ? "up" : "down";
  return '<div class="chart-head"><span class="' + cls + '">区间 ' +
    (chg >= 0 ? "+" : "") + chg.toFixed(2) + '%</span><span class="muted">' +
    esc(first.nav_date) + " → " + esc(last.nav_date) + " · " + fmtInt(d.n_total) + " 个净值点" +
    (d.n_points < d.n_total ? "（抽样 " + d.n_points + "）" : "") + "</span></div>" +
    '<svg viewBox="0 0 ' + w + " " + h + '" role="img" aria-label="净值曲线">' +
    '<path d="' + area + '" class="area"></path><path d="' + line + '" class="line"></path>' +
    gl + yl + "</svg>";
}

async function loadNavChart(code, id) {
  const el = document.getElementById(id || "nav-chart");
  if (!el) return;
  try {
    const res = await api("/funds/" + code + "/nav?days=1095");
    el.innerHTML = navSvg(res.data || {});
  } catch (e) {
    el.innerHTML = '<p class="err">' + esc(e.message) + "</p>";
  }
}

function periodCard() {
  const tabs = [["week", "每周"], ["month", "每月"], ["year", "每年"]];
  return '<h2>周 / 月 / 年净值涨跌</h2><div class="card">' +
    '<div class="toolbar">' + tabs.map(function (t) {
      return '<button class="' + (state.periodGran === t[0] ? "primary" : "") + '" data-gran="' + t[0] +
        '" onclick="setPeriodGran(this.dataset.gran)">' + t[1] + '</button>';
    }).join("") + '<span class="muted" id="period-meta"></span></div>' +
    '<div id="period-table"><p class="loading">加载中…</p></div>' +
    '<p class="code-hint">“复投收益”按每日净值增长率链式复利，含分红再投；“净值涨跌”只比较首末单位净值，除息日会低估，两者差额即分红影响。期内最大回撤用该周期内的单位净值峰值法。标注“未结束”的周期只含已披露交易日。</p></div>';
}

async function setPeriodGran(gran) {
  state.periodGran = gran;
  document.querySelectorAll("[data-gran]").forEach(function (el) {
    el.classList.toggle("primary", el.getAttribute("data-gran") === gran);
  });
  await loadPeriods(state.fund, gran);
}

async function loadPeriods(code, gran) {
  const box = document.getElementById("period-table");
  if (!box) return;
  box.innerHTML = '<p class="loading">加载中…</p>';
  try {
    const res = await api("/funds/" + encodeURIComponent(code) + "/periods?granularity=" + gran + "&limit=200");
    const d = res.data || {}, rows = d.rows || [];
    const meta = document.getElementById("period-meta");
    if (meta) meta.textContent = rows.length
      ? "共 " + fmtInt(d.total_periods) + " 个周期 · 显示最近 " + rows.length + " 个" : "";
    if (!rows.length) { box.innerHTML = notice("没有可聚合的净值数据"); return; }
    box.innerHTML = '<div class="table-wrap" style="max-height:430px"><table><thead><tr>' +
      '<th>周期</th><th class="num">复投收益</th><th class="num">净值涨跌</th><th class="num">期内最大回撤</th>' +
      '<th class="num">观测</th><th>区间</th></tr></thead><tbody>' +
      rows.map(function (r) {
        const cCls = (r.chain_return_pct || 0) >= 0 ? "up" : "down";
        const nCls = (r.nav_return_pct || 0) >= 0 ? "up" : "down";
        return '<tr><td>' + esc(r.period_label) +
            (r.partial ? ' <span class="badge warn" title="' + esc(r.partial_reason || "") + '">未结束</span>' : "") + '</td>' +
          '<td class="num ' + cCls + '">' + fmtPctPoint(r.chain_return_pct, 2) + '</td>' +
          '<td class="num ' + nCls + '">' + fmtPctPoint(r.nav_return_pct, 2) + '</td>' +
          '<td class="num">' + fmtPctPoint(r.mdd_pct, 2) + '</td>' +
          '<td class="num">' + fmtInt(r.n_obs) + '</td>' +
          '<td class="mono muted">' + esc(r.first_date) + " → " + esc(r.last_date) + '</td></tr>';
      }).join("") + '</tbody></table></div>';
  } catch (e) {
    box.innerHTML = '<p class="err">' + esc(e.message) + '</p>';
  }
}

function navRowsCard() {
  return '<h2>逐日净值明细</h2><div class="card">' +
    '<div class="toolbar">' +
    '<label>起 <input id="nav-start" type="date" style="width:150px" value="' + esc(state.navStart) + '"></label>' +
    '<label>止 <input id="nav-end" type="date" style="width:150px" value="' + esc(state.navEnd) + '"></label>' +
    '<label>每页 <select id="nav-size">' + [50, 100, 200, 500].map(function (n) {
      return '<option value="' + n + '"' + (state.navSize === n ? " selected" : "") + '>' + n + '</option>';
    }).join("") + '</select></label>' +
    '<button class="primary" onclick="applyNavRange()">查询</button>' +
    '<button class="ghost" onclick="setNavPreset(365)">近 1 年</button>' +
    '<button class="ghost" onclick="setNavPreset(1095)">近 3 年</button>' +
    '<button class="ghost" onclick="setNavPreset(0)">全部</button>' +
    '<button class="ghost" onclick="pageNavRows(-1)">上一页</button>' +
    '<button class="ghost" onclick="pageNavRows(1)">下一页</button>' +
    '<span class="muted" id="nav-meta"></span></div>' +
    '<div id="nav-rows"><p class="loading">加载中…</p></div>' +
    '<p class="code-hint">全历史净值按日期倒序分页；日期留空即全区间。日涨跌为当天披露的净值增长率（百分点），除息日与单位净值变化不一致属正常。</p></div>';
}

function applyNavRange() {
  state.navStart = (document.getElementById("nav-start") || {}).value || "";
  state.navEnd = (document.getElementById("nav-end") || {}).value || "";
  const size = parseInt((document.getElementById("nav-size") || {}).value || "100", 10);
  state.navSize = isNaN(size) ? 100 : size;
  state.navPage = 1;
  loadNavRows(state.fund);
}

function setNavPreset(days) {
  const end = (state.navBounds || {}).last_date || "";
  if (!days || !end) {
    state.navStart = "";
    state.navEnd = "";
  } else {
    const d = new Date(end + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() - days);
    state.navStart = d.toISOString().slice(0, 10);
    state.navEnd = end;
  }
  const s = document.getElementById("nav-start");
  if (s) s.value = state.navStart;
  const e = document.getElementById("nav-end");
  if (e) e.value = state.navEnd;
  state.navPage = 1;
  loadNavRows(state.fund);
}

function pageNavRows(dir) {
  const next = state.navPage + dir;
  if (next < 1) return;
  state.navPage = next;
  loadNavRows(state.fund);
}

async function loadNavRows(code) {
  const box = document.getElementById("nav-rows");
  if (!box) return;
  box.innerHTML = '<p class="loading">加载中…</p>';
  const params = new URLSearchParams({ page: state.navPage, page_size: state.navSize });
  if (state.navStart) params.set("start", state.navStart);
  if (state.navEnd) params.set("end", state.navEnd);
  try {
    const res = await api("/funds/" + encodeURIComponent(code) + "/nav/rows?" + params.toString());
    const d = res.data || {}, rows = d.rows || [];
    const meta = document.getElementById("nav-meta");
    if (meta) meta.textContent = d.total
      ? "第 " + fmtInt((d.page - 1) * d.page_size + 1) + "–" + fmtInt((d.page - 1) * d.page_size + rows.length) +
        " 条 / 共 " + fmtInt(d.total) + " 条 · 第 " + d.page + "/" + fmtInt(d.pages) + " 页"
      : "无数据";
    if (!rows.length) { box.innerHTML = notice("该区间没有净值记录"); return; }
    box.innerHTML = '<div class="table-wrap" style="max-height:460px"><table><thead><tr>' +
      '<th>净值日期</th><th class="num">单位净值</th><th class="num">累计净值</th><th class="num">日涨跌</th><th>来源</th></tr></thead><tbody>' +
      rows.map(function (r) {
        const cls = r.growth_pct == null ? "" : (r.growth_pct >= 0 ? "up" : "down");
        return '<tr><td class="mono">' + esc(r.nav_date) + '</td>' +
          '<td class="num">' + fmtNum(r.unit_nav, 4) + '</td>' +
          '<td class="num">' + fmtNum(r.acc_nav, 4) + '</td>' +
          '<td class="num ' + cls + '">' + (r.growth_pct == null ? "—" : fmtPctPoint(r.growth_pct, 2)) + '</td>' +
          '<td class="muted">' + esc(NAV_SOURCE_LABELS[r.nav_source] || r.nav_source || "—") + '</td></tr>';
      }).join("") + '</tbody></table></div>';
  } catch (e) {
    box.innerHTML = '<p class="err">' + esc(e.message) + '</p>';
  }
}

function tailCard(t) {
  if (!t) return '<h2>三年尾部风险</h2>' + notice('该基金没有可用的三年尾部指标（历史不足或净值缺口超过阈值）。');
  var comp = t.completeness == null ? '—' : Math.round(t.completeness * 8) + '/8';
  var rows = [
    ['三年最大回撤', fmtPct(t.mdd_dec)],
    ['最差滚动 90 日', fmtPctPoint(t.worst_rolling90d_pct) + (t.worst_rolling90d_start ? ' <span class="muted">' + esc(t.worst_rolling90d_start) + ' → ' + esc(t.worst_rolling90d_end) + '</span>' : '')],
    ['最差滚动 180 日', fmtPctPoint(t.worst_rolling180d_pct)],
    ['CVaR 5%', fmtPct(t.cvar5_dec)],
    ['最差单日', fmtPctPoint(t.worst_day_pct) + (t.worst_day_date ? ' <span class="muted">' + esc(t.worst_day_date) + '</span>' : '')],
    ['最差单周', fmtPctPoint(t.worst_week_pct)],
    ['自然月正收益比例', fmtPctPoint(t.positive_month_ratio_pct) + (t.months_total ? ' <span class="muted">' + fmtInt(t.months_positive) + '/' + fmtInt(t.months_total) + ' 个月</span>' : '')],
    ['最长水下观测', (t.max_underwater_obs == null ? '—' : fmtInt(t.max_underwater_obs) + ' 个观测') + (t.ongoing_drawdown ? ' <span class="badge warn">仍在水下</span>' : '')],
    ['完整度', comp + ' <span class="muted">（非空组件/8，' + fmtInt(t.n_obs) + ' 个净值点）</span>']
  ];
  return '<h2>三年尾部风险（' + fmtInt(t.window_days) + ' 天窗口）</h2><div class="card"><dl class="kv">' +
    rows.map(function (kv) { return '<dt>' + kv[0] + '</dt><dd>' + kv[1] + '</dd>'; }).join('') +
    '</dl><p class="code-hint">尾部指标是 V2 影子口径，不参与正式分与榜单排序；completeness 表示 8 个组件中可计算的比例，不足时对应字段为 — 而不是 0。</p></div>';
}

function costCard(code) {
  return '<h2>费用与到手收益（情景）</h2><div class="card">' +
    '<div class="toolbar"><label>投入金额（元）<input id="cost-amount" type="number" value="10000" min="1" step="1000" style="width:140px"></label>' +
    '<label>持有天数<input id="cost-days" type="number" value="30" min="0" step="1" style="width:100px"></label>' +
    '<button class="primary" onclick="calcCost(\'' + code + '\')">计算</button></div>' +
    '<div id="cost-result"><p class="code-hint">申购费按外扣、赎回费按内扣；持有期内管理费/托管费已体现在净值里，不重复扣。结果是情景估算，不是真实成交价。</p></div></div>';
}

async function calcCost(code) {
  var box = document.getElementById('cost-result');
  if (!box) return;
  var amount = (document.getElementById('cost-amount') || {}).value || '10000';
  var days = (document.getElementById('cost-days') || {}).value || '30';
  box.innerHTML = '<p class="loading">计算中…</p>';
  try {
    var res = await api('/funds/' + encodeURIComponent(code) + '/cost?amount=' + encodeURIComponent(amount) + '&holding_days=' + encodeURIComponent(days));
    var d = res.data, p = d.purchase || {}, r = d.redemption || {};
    var pTxt = p.rate_pct == null ? (p.fixed_cny == null ? '—' : '固定 ' + fmtInt(p.fixed_cny) + ' 元') : fmtNum(p.rate_pct, 2) + '%';
    var rTxt = r.rate_pct == null ? '—' : fmtNum(r.rate_pct, 2) + '%';
    var rl = d.reason_labels || [];
    var reason = rl.length ? '<div class="pill-row">' + rl.map(function (x) {
      return '<span class="badge warn" title="' + esc(x.code) + '">' + esc(x.label) + '</span>';
    }).join('') + '</div>' : '';
    box.innerHTML = '<div class="grid cols-3">' +
      stat('原费率情景净收益', fmtPctPoint(d.net_return_pct, 4), '口径：' + esc(labelOf('return_types', d.return_type))) +
      stat('优惠费率情景净收益', fmtPctPoint(d.net_return_discount_pct, 4), '适配：' + esc(labelOf('user_fits', d.user_fit))) +
      stat('申购 / 赎回档', pTxt + ' / ' + rTxt, '持有 ' + fmtInt(d.holding_days) + ' 天 · 区间净值回报' +
        (d.nav_return_window_days == null ? '' : '（近 ' + fmtInt(d.nav_return_window_days) + ' 天）') +
        ' ' + fmtPctPoint(d.nav_return_pct)) +
      '</div>' + reason +
      '<p class="code-hint">' + (d.assumptions || []).map(function (x) { return esc(x); }).join('；') + '</p>';
  } catch (err) {
    box.innerHTML = '<p class="err">' + esc(err.message) + '</p>';
  }
}

/* ── 决策快照：这只基金差在哪一项 ─────────────────────── */
function checkVal(v, unit) {
  if (v == null) return "—";
  if (unit === "pct") return fmtNum(v, 2) + "%";
  if (unit === "dec") return fmtPct(v);
  if (unit === "billion") return fmtNum(v, 2) + " 亿";
  if (unit === "count") return fmtInt(v);
  return fmtNum(v, 2);
}

function checkMargin(v, unit) {
  if (v == null) return "—";
  var txt = checkVal(Math.abs(v), unit);
  if (v === 0) return "刚好达标";
  return (v > 0 ? "余量 " : "差 ") + txt;
}

function windowCn(w) {
  if (w === "1y_3y" || w === "1y+3y") return "1 年 + 3 年各半";
  if (w === "1y_only") return "仅 1 年（3 年历史不足）";
  if (w === "3y_only") return "仅 3 年";
  return "无可用窗口";
}

function runLabel(id) {
  var m = /^(\d{8})T(\d{6})-([a-z]+)/.exec(id || "");
  if (!m) return id || "—";
  return m[1].slice(4, 6) + "-" + m[1].slice(6, 8) + " " + m[2].slice(0, 2) + ":" +
    m[2].slice(2, 4) + " · " + labelOf("run_modes", m[3], m[3]);
}

function deltaText(v, unit) {
  if (v == null) return "—";
  var sign = v > 0 ? "+" : "";
  if (unit === "pct") return sign + v.toFixed(3) + " pp";
  if (unit === "billion") return sign + v.toFixed(2) + " 亿";
  if (unit === "count" || unit === "score") return sign + v.toFixed(2);
  return sign + v.toFixed(4);
}

function decisionCompare(d) {
  var hist = d.history || [];
  var prev = d.previous || null;
  if (hist.length < 2 && !prev) return "";
  var opts = hist.map(function (h) {
    var sel = prev && prev.run_id === h.run_id ? " selected" : "";
    var score = h.formal_score == null ? "未入榜" : "正式分 " + fmtNum(h.formal_score, 2);
    return '<option value="' + esc(h.run_id) + '"' + sel + ">" +
      esc(runLabel(h.run_id)) + " · " + esc(score) + "</option>";
  }).join("");
  var scoreLine = "";
  if (prev) {
    var cur = d.formal_score;
    scoreLine = '<p class="muted small">对比轮次 ' + esc(runLabel(prev.run_id)) +
      "：正式分 " + (prev.formal_score == null ? "未入榜" : fmtNum(prev.formal_score, 2)) +
      " → 本轮 " + (cur == null ? "未入榜" : fmtNum(cur, 2)) +
      (prev.score_delta == null ? "" : "（" + (prev.score_delta > 0 ? "+" : "") +
        prev.score_delta.toFixed(2) + "）") + "</p>";
  }
  var changes = (prev && prev.changes) || [];
  var table = changes.length
    ? '<div class="table-wrap"><table><thead><tr><th>分组</th><th>项目</th>' +
      '<th class="num">上轮门槛</th><th class="num">本轮门槛</th><th class="num">门槛变化</th>' +
      '<th class="num">我的数值</th><th>结果</th></tr></thead><tbody>' +
      changes.map(function (c) {
        var flip = c.prev_passed === c.passed ? "" : (c.passed ? "（转为通过）" : "（转为未通过）");
        return "<tr><td>" + esc(c.group_title) + "</td><td>" + esc(c.label) + "</td>" +
          '<td class="num">' + checkVal(c.prev_threshold, c.unit) + "</td>" +
          '<td class="num">' + checkVal(c.threshold, c.unit) + "</td>" +
          '<td class="num">' + deltaText(c.threshold_delta, c.unit) + "</td>" +
          '<td class="num">' + checkVal(c.value, c.unit) + "</td>" +
          '<td><span class="badge ' + (c.passed ? "good" : "warn") + '">' +
            (c.passed ? "通过" : "未通过") + esc(flip) + "</span></td></tr>";
      }).join("") + "</tbody></table></div>"
    : '<p class="muted small">与所选轮次相比，门槛值与自身指标都没有变化（同一套门槛、同一份净值）。</p>';
  return '<div class="card"><div class="card-head"><h3>门槛变化对比</h3>' +
    '<label class="chk">对比轮次 <select onchange="setDecisionPrev(this.value)">' +
    opts + "</select></label></div>" + scoreLine + table +
    '<p class="muted small">门槛随母体与风格分位变化而移动；同一只基金的排名变化，' +
    "可能是自身指标变了，也可能只是门槛线移动了。</p></div>";
}

async function setDecisionPrev(runId) {
  var code = state.fund;
  if (!code) return;
  try {
    var res = await api("/funds/" + code + "/decision?prev=" + encodeURIComponent(runId));
    state.decisionCache = res.data;
    renderFundTab("decision");
  } catch (e) {
    toast("切换对比轮次失败：" + (e.message || e), "warn");
  }
}
function rankHistoryCard() {
  return '<div class="card" id="rank-history"><p class="loading">加载排名走势…</p></div>';
}

function rankHistorySvg(h) {
  var items = (h.items || []);
  if (!items.length) return '<p class="muted small">这只基金还没有可比较的筛选轮次。</p>';
  var W = 900, H = 220, padL = 46, padR = 52, padT = 18, padB = 26;
  var iw = W - padL - padR, ih = H - padT - padB;
  var n = items.length;
  function x(i) { return n === 1 ? padL + iw / 2 : padL + (iw * i) / (n - 1); }
  var rankPts = [], scorePts = [], ranks = [], scores = [];
  items.forEach(function (it, i) {
    var sc = (it.score != null ? it.score : it.formal_score);
    if (it.rank != null) { ranks.push(it.rank); rankPts.push([x(i), 0, it]); }
    if (sc != null) { scores.push(sc); scorePts.push([x(i), 0, it, sc]); }
  });
  var hasRank = rankPts.length > 0, hasScore = scorePts.length > 0;
  if (!hasRank && !hasScore) {
    return '<p class="muted small">这 ' + fmtInt(n) + ' 轮既没有入榜记录，也没有留下可比的正式分。</p>';
  }
  var maxRank = hasRank ? Math.max.apply(null, ranks) : 1;
  var minRank = hasRank ? Math.min.apply(null, ranks) : 1;
  var loR = Math.max(1, minRank - 1), hiR = maxRank + Math.max(1, Math.round(maxRank * 0.08));
  function yRank(v) { return padT + ih * (v - loR) / Math.max(1, hiR - loR); }
  var loS = hasScore ? Math.min.apply(null, scores) : 0;
  var hiS = hasScore ? Math.max.apply(null, scores) : 1;
  if (hiS - loS < 0.5) { var mid = (hiS + loS) / 2; loS = mid - 0.25; hiS = mid + 0.25; }
  function yScore(v) { return padT + ih * (1 - (v - loS) / (hiS - loS)); }

  var grid = "";
  [0, 0.5, 1].forEach(function (t) {
    var y = padT + ih * t;
    grid += '<line class="grid-line" x1="' + padL + '" y1="' + y.toFixed(1) + '" x2="' +
      (W - padR) + '" y2="' + y.toFixed(1) + '"></line>';
    grid += '<text class="axis" x="' + (padL - 8) + '" y="' + (y + 4).toFixed(1) +
      '" text-anchor="end">' + (hasRank ? Math.round(loR + (hiR - loR) * t) : "\u2014") + '</text>';
    grid += '<text class="axis" x="' + (W - padR + 8) + '" y="' + (y + 4).toFixed(1) +
      '">' + (hasScore ? (hiS - (hiS - loS) * t).toFixed(1) : "\u2014") + '</text>';
  });

  rankPts.forEach(function (p) { p[1] = yRank(p[2].rank); });
  scorePts.forEach(function (p) { p[1] = yScore(p[3]); });
  function path(pts) {
    return pts.map(function (p, k) { return (k ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); }).join(" ");
  }
  function tip(it, sc) {
    var parts = [esc(String(it.created_at || "").slice(0, 16).replace("T", " "))];
    parts.push(it.rank == null ? "未入榜" : "排名 " + it.rank);
    if (sc != null) parts.push((it.score_source === "decision" ? "正式分(决策快照) " : "正式分 ") + sc.toFixed(2));
    if (it.composite != null) parts.push("综合分 " + it.composite.toFixed(2));
    if (it.layer_label) parts.push(it.layer_label);
    return parts.join(" \u00b7 ");
  }
  var dots = "";
  if (hasRank) {
    dots = rankPts.map(function (p) {
      return '<circle class="dot" cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) +
        '" r="3"><title>' + tip(p[2], p[2].score) + "</title></circle>";
    }).join("");
  } else {
    dots = scorePts.map(function (p) {
      return '<circle class="dot score" cx="' + p[0].toFixed(1) + '" cy="' + p[1].toFixed(1) +
        '" r="2.5"><title>' + tip(p[2], p[3]) + "</title></circle>";
    }).join("");
  }
  var xLabels = "";
  [0, Math.floor((n - 1) / 2), n - 1].forEach(function (i, k) {
    if (i < 0) return;
    var d = String(items[i].created_at || "");
    var anchor = k === 0 ? "start" : k === 2 ? "end" : "middle";
    xLabels += '<text class="axis" x="' + x(i).toFixed(1) + '" y="' + (H - 6) +
      '" text-anchor="' + anchor + '">' + esc(d.slice(5, 16).replace("T", " ")) + "</text>";
  });

  var legend = hasRank
    ? '<text class="axis" x="' + padL + '" y="' + (padT - 5) + '">排名（越小越好）</text>'
    : '<text class="axis" x="' + padL + '" y="' + (padT - 5) + '">未入过榜</text>';
  legend += '<text class="axis" x="' + (W - padR) + '" y="' + (padT - 5) +
    '" text-anchor="end">正式分</text>';
  return '<svg viewBox="0 0 ' + W + " " + H + '" width="100%" height="' + H +
    '" role="img" aria-label="排名与正式分走势">' + grid +
    (hasRank ? '<path class="line" d="' + path(rankPts) + '"></path>' : "") +
    (hasScore ? '<path class="line score" d="' + path(scorePts) + '"></path>' : "") +
    dots + xLabels + legend + "</svg>";
}

async function loadRankHistory(code) {
  var host = document.getElementById("rank-history");
  if (!host) return;
  try {
    var res = await api("/funds/" + code + "/rank-history");
    var h = res.data || {};
    var items = h.items || [];
    if (!items.length) { host.innerHTML = '<div class="card-head"><h3>排名走势</h3></div>' +
      '<p class="muted small">还没有可比较的筛选轮次。</p>'; return; }
    var first = items[0], last = items[items.length - 1];
    var delta = h.score_delta;
    host.innerHTML = '<div class="card-head"><h3>排名走势</h3>' +
      '<span class="muted small">共 ' + fmtInt(h.runs) + " 轮 · " +
      (h.best_rank == null ? "从未入榜" : "最好第 " + fmtInt(h.best_rank) + " 名") +
      (h.latest_rank == null ? "" : " · 最新第 " + fmtInt(h.latest_rank) + " 名") +
      "</span></div>" +
      rankHistorySvg(h) +
      '<p class="muted small">' + (h.best_rank == null
        ? "这只基金还没进过正式榜，曲线是它每轮的正式分（右轴）；左轴留空。"
        : "左轴是榜单排名（越靠上越好），右轴是正式分（虚线）。") +
      "悬停圆点看具体轮次。首次 " + esc(String(first.created_at || "").slice(0, 16).replace("T", " ")) +
      " → 最新 " + esc(String(last.created_at || "").slice(0, 16).replace("T", " ")) +
      (delta == null ? "" : "，正式分变化 " + (delta > 0 ? "+" : "") + delta.toFixed(2)) +
      "。排名变化既可能来自自身指标，也可能来自门槛线移动（见下方对比）。</p>";
  } catch (e) {
    host.innerHTML = '<p class="err">排名走势加载失败：' + esc(e.message) + "</p>";
  }
}

function counterfactualCard(d) {
  var cf = d && d.counterfactual;
  if (!cf) return "";
  var rows = [];
  (cf.groups || []).forEach(function (g) {
    (g.items || []).forEach(function (i) {
      if (i.passed === false) rows.push({ group: g.title, item: i });
    });
  });
  var head = '<div class="card"><div class="card-head"><h3>反事实：按上一轮门槛</h3>' +
    '<span class="muted small">对比 ' + esc(runLabel(cf.run_id)) + "</span></div>" +
    '<div class="pill-row">' +
      '<span class="badge ' + (cf.would_pass ? "good" : "warn") + '">' +
        (cf.would_pass ? "按上轮门槛可过" : "按上轮门槛仍不过") + "</span>" +
      '<span class="badge">通过 ' + fmtInt(cf.n_passed) + " / " + fmtInt(cf.n_items) + " 项</span>" +
      (cf.rank_estimate ? '<span class="badge accent">估算第 ' +
        fmtInt(cf.rank_estimate.position) + " / " + fmtInt(cf.rank_estimate.sample_n) + " 名</span>" : "") +
    "</div>" +
    (cf.rank_estimate ? '<p class="muted small" style="margin:10px 0 0">' +
      esc(cf.rank_estimate.note) + "（" + esc(cf.rank_estimate.board_label) + "）。</p>" : "") +
    '<p class="muted small" style="margin:6px 0 0">' + esc(cf.note) + "。</p>";
  var table = rows.length
    ? '<div class="table-wrap"><table><thead><tr><th>分组</th><th>项目</th>' +
      '<th class="num">上轮门槛</th><th class="num">我的数值</th><th class="num">差距</th>' +
      "</tr></thead><tbody>" +
      rows.map(function (r) {
        var i = r.item;
        return "<tr><td>" + esc(r.group) + "</td><td>" + esc(i.label) + "</td>" +
          '<td class="num">' + (i.op === "max" ? "≤ " : "≥ ") + checkVal(i.threshold, i.unit) + "</td>" +
          '<td class="num">' + checkVal(i.value, i.unit) + "</td>" +
          '<td class="num margin-bad">' + checkMargin(i.margin, i.unit) + "</td></tr>";
      }).join("") + "</tbody></table></div>"
    : '<p class="muted small">上一轮每一项门槛按现在的数值都还能过——排名变化来自门槛线移动或其他基金变化。</p>';
  return head + table + "</div>";
}

function exportRankingsCsv() {
  if (state.board === "bond_enhanced_watch") return;
  var sq = sortQuery();
  var params = new URLSearchParams({ board: state.board, fmt: "csv", sort: sq.sort, order: sq.order });
  if (state.q) params.set("q", state.q);
  if (state.style) params.set("style", state.style);
  if (state.dedupe) params.set("dedupe", "family");
  window.location = "/api/v1/rankings?" + params.toString();
}

function exportLockedCsv() {
  var params = new URLSearchParams({ fmt: "csv" });
  if (state.lockup) params.set("lockup", state.lockup);
  if (state.lockBucket) params.set("bucket", state.lockBucket);
  if (state.sort) params.set("sort", state.sort);
  if (state.order) params.set("order", state.order);
  window.location = "/api/v1/locked?" + params.toString();
}

/* ── 决策卡：缓释与跨源校验（Batch A）─────────────────── */
var RELIEF_TYPE_CN = {
  scale: "规模略低于门槛",
  annual_1y: "1 年年化略低于门槛",
  positive: "日胜率略低于门槛",
};
var CROSS_FIELD_CN = {
  unit_nav: "单位净值",
  nav_date: "净值日期",
  return_1y: "近 1 年总回报",
  growth_pct: "日增长率",
};
var CROSS_STATUS_CN = {
  pass: "一致",
  mismatch: "不一致",
  missing: "双方缺失",
  missing_a: "主源缺失",
  missing_b: "对照源缺失",
  date_lag: "日期滞后",
  soft_lag: "日期软滞后",
};

function reliefCard(d) {
  var r = d && d.relief;
  if (!r) return "";
  var det = r.detail || {};
  var allowed = r.allowed === true;
  var failed = (det.failed || []).join("、") || "—";
  var bits = [];
  bits.push('<div><span>失败项</span><b>' + esc(failed) + "</b></div>");
  if (det.value != null && det.threshold != null) {
    bits.push("<div><span>实际 / 门槛</span><b>" + fmtNum(det.value, 4) + " / " +
      fmtNum(det.threshold, 4) + "</b></div>");
  }
  if (det.gap != null) bits.push("<div><span>差距</span><b>" + fmtNum(det.gap, 4) + "</b></div>");
  if (det.composite != null) {
    bits.push("<div><span>综合分</span><b>" + fmtNum(det.composite, 2) + "</b></div>");
  }
  
  var head = '<div class="card"><div class="card-head"><h3>单一原因缓释</h3>' +
    '<span class="badge ' + (allowed ? "good" : "warn") + '">' +
    (allowed ? "已放行" : "未放行") + "</span></div>" +
    '<p class="muted small">只有“恰好一项硬比较失败”且命中单一原因规则才允许缓释；' +
    "两项及以上失败一律不放行；放行只留痕，不改数据质量分。</p>" +
    '<div class="kv">' + bits.join("") + "</div>";
  if (det.blocked) {
    head += '<p class="muted small">未放行原因：' + esc(det.blocked) + "</p>";
  }
  return head + "</div>";
}

function crossCard(d) {
  var rows = (d && d.cross_check) || [];
  if (!rows.length) return "";
  var trs = rows.map(function (r) {
    var st = CROSS_STATUS_CN[r.status] || r.status;
    var cls = r.status === "pass" ? "good" : (r.status === "mismatch" ? "warn" : "");
    var det = null;
    try { det = r.detail_json ? JSON.parse(r.detail_json) : null; } catch (e) { det = null; }
    var isDate = r.field === "nav_date";
    var da = (det && det.status_snapshot_date) || "";
    var db = (det && det.nav_daily_date) || "";
    // 主源只给月-日、对照源给完整日期：月日相同时统一显示 MM-DD，避免看起来像不一致
    if (isDate && da && db && db.slice(5) === da.slice(-5)) { da = da.slice(-5); db = db.slice(-5); }
    var a = isDate ? (da || "—") : (r.value_a == null ? "—" : fmtNum(r.value_a, 4));
    var b = isDate ? (db || "—") : (r.value_b == null ? "—" : fmtNum(r.value_b, 4));
    var delta = r.delta_abs == null ? "—" : fmtNum(r.delta_abs, 4);
    return "<tr><td>" + esc(CROSS_FIELD_CN[r.field] || r.field) + "</td>" +
      "<td class='small'>" + esc(r.source_a) + "</td>" +
      "<td class='small'>" + esc(r.source_b) + "</td>" +
      "<td class='num'>" + a + "</td><td class='num'>" + b + "</td>" +
      "<td class='num'>" + (isDate ? "—" : delta) + "</td>" +
      "<td><span class='badge " + cls + "'>" + esc(st) + "</span></td></tr>";
  }).join("");
  return '<div class="card"><div class="card-head"><h3>跨源校验</h3>' +
    '<span class="muted small">不一致时双值保留，不静默择优</span></div>' +
    '<div class="table-wrap"><table><thead><tr><th>项目</th><th>主源</th><th>对照源</th>' +
    '<th class="num">主源值</th><th class="num">对照值</th><th class="num">绝对差</th>' +
    "<th>结论</th></tr></thead><tbody>" + trs + "</tbody></table></div></div>";
}

function explainCard(d) {
  var why = d.why_selected || [], caveat = d.caveat || [];
  if (!why.length && !caveat.length) return "";
  function list(items) {
    if (!items.length) return '<p class="muted small">本轮没有生成这一侧的条目。</p>';
    return '<ul style="margin:6px 0 0 18px;line-height:1.65">' +
      items.map(function (x) { return "<li>" + esc(x.text) + "</li>"; }).join("") + "</ul>";
  }
  return '<div class="card"><div class="card-head"><h3>为什么选它 / 它不适合谁</h3>' +
    '<span class="muted small">由规则命中与风险证据生成，不是营销文案</span></div>' +
    '<div class="grid cols-2"><div><h4 style="margin:0 0 4px">为什么选它</h4>' + list(why) +
    '</div><div><h4 style="margin:0 0 4px">它不适合谁</h4>' + list(caveat) + "</div></div></div>";
}

function shareClassCard(d) {
  var s = d.share_class_compare;
  if (!s || !(s.members || []).length) return "";
  var rec = s.recommended || {};
  var rows = s.members.map(function (m) {
    var c = m.costs || {};
    function cost(h) { var x = c[String(h)] || {}; return fmtNum(x.total_pct); }
    return "<tr><td class='mono'>" + esc(m.fcode) + "</td><td>" + esc(m.class_letter || "—") + "</td>" +
      "<td class='num'>" + cost(365) + " / " + cost(1095) + " / " + cost(1825) + "</td>" +
      "<td class='num'>" + fmtNum(m.redemption_1y_pct) + "</td>" +
      "<td>" + (m.fcode === d.fcode ? '<span class="badge">当前</span> ' : "") +
      (rec.fcode === m.fcode ? '<span class="badge accent">3年成本最低</span>' : "") + "</td></tr>";
  }).join("");
  var note = s.note ? '<p class="muted small">' + esc(s.note) + "</p>" : "";
  if (s.break_even_years != null) {
    note += '<p class="muted small">当前份额与推荐份额约 ' + fmtNum(s.break_even_years, 2) +
      " 年持平；短于这个期限，当前份额可能更便宜。</p>";
  }
  if (s.advantage_3y_pct != null && s.advantage_3y_pct > 0) {
    note += '<p class="muted small">按 3 年持有，推荐份额比当前份额少 ' + fmtNum(s.advantage_3y_pct, 3) +
      " 个百分点成本。</p>";
  }
  return '<div class="card"><div class="card-head"><h3>同家族份额成本（A/C/D）</h3>' +
    '<span class="muted small">只给建议，不自动换榜；赎回费未计入</span></div>' +
    '<div class="table-wrap"><table><thead><tr><th>代码</th><th>类别</th>' +
    '<th class="num">1/3/5 年成本%</th><th class="num">满1年赎回费%</th><th>标记</th></tr></thead><tbody>' +
    rows + "</tbody></table></div>" + note + "</div>";
}

function decisionCard(d) {
  if (!d) return notice("这只基金还没有决策记录（可能本轮未被评分，或库中还没有它的筛选结果）。", "warn");
  var head = '<div class="card"><div class="card-head"><h3>决策快照</h3>' +
    '<span class="muted small">门槛值取自本轮落库快照，不在页面重算</span></div>' +
    '<div class="pill-row">' +
      (d.board ? '<span class="badge accent">' + esc(d.board_label || d.board) + " #" + d.rank + "</span>"
               : '<span class="badge">未入榜</span>') +
      '<span class="badge">' + esc(d.layer_label || d.layer_reached) + "</span>" +
      '<span class="badge ' + (d.passed ? "good" : "warn") + '">' +
        (d.passed ? "通过门槛" : "未通过") + "</span>" +
      (d.confidence == null ? "" : '<span class="badge">数据质量 ' + fmtNum(d.confidence, 1) + "</span>") +
    "</div>";
  if (d.scores && d.scores.length) {
    head += '<div class="kv" style="margin-top:10px">' + d.scores.map(function (x) {
      return "<div><span>" + esc(x.label) + "</span><b>" + fmtNum(x.value, 2) + "</b></div>";
    }).join("") + "</div>";
  }
  if (d.note) head += '<p class="muted small" style="margin:10px 0 0">' + esc(d.note) + "</p>";
  head += "</div>";

  var body = (d.groups || []).map(function (g) {
    var meta = g.meta || {};
    var metaBits = [];
    if (meta.threshold_source_label) metaBits.push("口径：" + meta.threshold_source_label);
    if (meta.peers) metaBits.push("同风格样本 " + fmtInt(meta.peers) + " 只");
    if (meta.annual_window) metaBits.push("年化窗口：" + windowCn(meta.annual_window));
    if (meta.fallback) metaBits.push("样本不足，已回退全局分位");
    if (meta.min_dimensions) metaBits.push("需通过 " + meta.min_dimensions + " 项，实际 " +
      (meta.dimensions_passed == null ? "—" : meta.dimensions_passed) + " 项");
    var rows = g.items.map(function (i) {
      var pct = 0;
      if (i.threshold) pct = Math.max(0, Math.min(150, (i.value / i.threshold) * 100));
      /* 增强债的年化是 1 年 + 3 年各半，和概览页的"近一年年化"不是同一个数，标签里必须写清楚 */
      var label = (i.key === "annual_1y" && i.window_label)
        ? "年化（" + i.window_label + "）"
        : i.label + (i.window_label ? "（" + i.window_label + "）" : "");
      var tip = (i.key === "annual_1y" && i.window)
        ? ' title="增强债年化按 1 年与 3 年各半加权，避免只看某一年"'
        : "";
      /* 封顶值（夏普 5 / 索提诺 10 / 卡玛 20）加星号：数字是策略口径的上限，不是真实极值 */
      var capped = (i.key === "sharpe" && i.value >= 5) || (i.key === "sortino" && i.value >= 10) ||
        (i.key === "calmar" && i.value >= 20);
      var valTxt = checkVal(i.value, i.unit) + (capped ? "*" : "");
      var marginCls = i.margin == null ? "" : (i.passed ? "margin-ok" : "margin-bad");
      return "<tr" + tip + "><td>" + esc(label) + "</td>" +
        '<td class="num"' + (capped ? ' title="已触及策略口径上限，真实值更高"' : "") + ">" + valTxt + "</td>" +
        '<td class="num">' + (i.op === "max" ? "≤ " : "≥ ") + checkVal(i.threshold, i.unit) + "</td>" +
        '<td class="num ' + marginCls + '">' + checkMargin(i.margin, i.unit) + "</td>" +
        '<td><span class="badge ' + (i.passed ? "good" : "warn") + '">' + (i.passed ? "通过" : "未通过") +
          "</span></td>" +
        '<td class="bar-cell"><span class="bar ' + (i.passed ? "" : "bad") +
          '" style="width:' + Math.round(Math.min(100, pct)) + '%"></span></td></tr>';
    }).join("");
    var table = '<div class="table-wrap"><table><thead><tr><th>项目</th><th class="num">实际</th>' +
      '<th class="num">门槛</th><th class="num">余量</th><th>结果</th><th>达标度</th></tr></thead>' +
      "<tbody>" + rows + "</tbody></table></div>";
    var title = '<div class="card-head"><h3>' + esc(g.title) + "</h3>" +
      '<span class="muted small">通过 ' + g.passed_count + " / " + g.total_count + " 项</span></div>" +
      (metaBits.length ? '<p class="muted small">' + esc(metaBits.join(" · ")) + "</p>" : "");
    if (g.archive) {
      return '<details class="card"><summary>' + esc(g.title) + " · 未参与判定（展开查看）</summary>" +
        table + "</details>";
    }
    return '<div class="card">' + title + table + "</div>";
  }).join("");

  var compare = decisionCompare(d);
  var reasons = (d.reason_codes || []).length
    ? '<div class="card"><div class="card-head"><h3>判定原因码</h3>' +
      '<span class="muted small">原始码在悬停提示里</span></div><div class="pill-row">' +
      d.reason_codes.map(function (r) {
        return '<span class="badge warn" title="' + esc(r.code) + '">' + esc(r.label) + "</span>";
      }).join("") + "</div></div>"
    : "";
  return head + explainCard(d) + compare + counterfactualCard(d) + shareClassCard(d) +
    reliefCard(d) + crossCard(d) + body + reasons;
}

/* ── 持有期受限榜 ───────────────────────────────────── */
function lockBadge(r) {
  if (r.open_state === "upcoming") return "距开放 " + fmtInt(r.days_until_open) + " 天";
  if (r.open_state === "observed_past") {
    var d = String(r.next_open_date || "");
    return "开放期 " + (d ? d.slice(5) + " 起" : "已开始");
  }
  return "未披露下次开放日";
}

function lockedTable(rows) {
  if (!rows.length) return notice("这个筛选条件下没有基金。");
  var annual = state.periodMode === "annual";
  function cell(total, ann) {
    var v = annual ? (ann == null ? null : ann * 100) : total;
    var txt = annual ? (ann == null ? "—" : signPctDec(ann)) : signPctPoint(total);
    return '<td class="num ' + trendCls(v) + '">' + txt + "</td>";
  }
  var head = '<thead><tr><th class="num">#</th>' + sortTh("code", "代码", "") +
    sortTh("name", "名称", "") + sortTh("style", "风格", "") +
    sortTh("lockup", "受限类型", "") + sortTh("lockup", "持有期") + "<th>开放状态</th>" +
    sortTh("ret_1w", "近1周") + sortTh("ret_1m", "近1月") +
    sortTh("ret_3m", "近3月") + sortTh("ret_6m", "近6月") +
    sortTh("annual_1y", "年化") + sortTh("sharpe", "夏普") +
    sortTh("mdd", "回撤") + sortTh("positive", "胜率") +
    sortTh("scale", "规模(亿)") + sortTh("fee", "费率%") + "</tr></thead>";
  var body = rows.map(function (r, i) {
    var tone = r.open_state === "upcoming" ? "good" : r.open_state === "observed_past" ? "warn" : "";
    return '<tr class="clickable" onclick="location.hash=\'#/fund/' + esc(r.fcode) + '\'">' +
      '<td class="num">' + (i + 1) + "</td>" +
      '<td class="mono">' + esc(r.fcode) + "</td>" +
      '<td class="nm">' + esc(r.name) + "</td>" +
      '<td><span class="badge">' + esc(r.style_label || "—") + "</span></td>" +
      "<td>" + esc(r.lockup_label || "—") + "</td>" +
      '<td class="num">' + fmtDays(r.effective_days) + "</td>" +
      '<td><span class="badge ' + tone + '" title="' + esc(r.hint || "") + '">' +
        esc(lockBadge(r)) + "</span></td>" +
      cell(r.ret_1w_pct, r.annual_1w_dec) + cell(r.ret_1m_pct, r.annual_1m_dec) +
      cell(r.ret_3m_pct, r.annual_3m_dec) + cell(r.ret_6m_pct, r.annual_6m_dec) +
      '<td class="num ' + trendCls(r.annual_1y_pct) + '">' +
        signPctDec(r.annual_1y_pct == null ? null : r.annual_1y_pct / 100) + "</td>" +
      '<td class="num">' + fmtCap(r.sharpe_1y, 5) + "</td>" +
      '<td class="num">' + fmtPct(r.mdd_1y_pct == null ? null : r.mdd_1y_pct / 100) + "</td>" +
      '<td class="num">' + fmtNum(r.positive_1y_pct, 1) + "%</td>" +
      '<td class="num">' + fmtNum(r.scale_billion) + "</td>" +
      '<td class="num">' + fmtNum(r.recurring_fee_pct) + "</td></tr>";
  }).join("");
  /* 17 列宽表用固定列宽：不指定时浏览器会把中文名称列压到一字宽，行高暴涨 */
  var cols = '<colgroup><col style="width:40px"><col style="width:70px"><col style="width:170px">' +
    '<col style="width:92px"><col style="width:84px"><col style="width:62px"><col style="width:106px">' +
    '<col style="width:68px"><col style="width:68px"><col style="width:68px"><col style="width:68px">' +
    '<col style="width:76px"><col style="width:56px"><col style="width:70px"><col style="width:64px">' +
    '<col style="width:82px"><col style="width:56px"></colgroup>';
  return '<div class="table-wrap"><table class="fixed dense">' + cols + head + "<tbody>" + body + "</tbody></table></div>" +
    '<p class="muted small">点表头可排序；短周期年化是外推值（7 天的年化会被放大成三位数），请结合区间收益一起看。</p>';
}
async function renderLocked() {
  $main.innerHTML = '<h1>持有期受限</h1>' +
    '<p class="sub">这些基金的基础资格与规模都过线，但<b>买入后有最短持有期</b>（或定期开放、封闭期），' +
    "90 天内不能赎回，因此不进稳健榜与增强榜，单列在这里备查。持有期是产品条款，不是风险指标。</p>" +
    '<div id="locked-summary"><p class="loading">加载中…</p></div>' +
    '<div class="toolbar">' +
      '<select id="lockup" onchange="setLockup(this.value)">' +
        '<option value="">全部受限类型</option><option value="holding">持有期</option>' +
        '<option value="periodic_open">定期开放</option></select>' +
      '<span class="sep"></span><span class="muted">持有期档位</span>' +
      '<button class="ghost" data-bucket="" onclick="setLockBucket(this.dataset.bucket)">全部</button>' +
      '<button class="ghost" data-bucket="91-180" onclick="setLockBucket(this.dataset.bucket)">91–180 天</button>' +
      '<button class="ghost" data-bucket="181-365" onclick="setLockBucket(this.dataset.bucket)">181–365 天</button>' +
      '<button class="ghost" data-bucket="366-730" onclick="setLockBucket(this.dataset.bucket)">366–730 天</button>' +
      '<button class="ghost" data-bucket="731+" onclick="setLockBucket(this.dataset.bucket)">731 天以上</button>' +
      '<span class="sep"></span><span class="muted">收益口径</span>' +
      '<button class="ghost" onclick="setPeriodMode(\'total\')">区间收益</button>' +
      '<button class="ghost" onclick="setPeriodMode(\'annual\')">年化</button>' +
      '<span class="sep"></span>' +
      '<button class="ghost" onclick="exportLockedCsv()" title="导出当前筛选与排序下的全部行">导出 CSV</button>' +
    "</div>" +
    '<div id="locked-body"><p class="loading">加载中…</p></div>';
  await loadLocked();
}

async function loadLocked() {
  var body = document.getElementById("locked-body");
  if (!body) return;
  var params = new URLSearchParams({ limit: 200, offset: state.lockOffset || 0 });
  if (state.lockup) params.set("lockup", state.lockup);
  if (state.lockBucket) params.set("bucket", state.lockBucket);
  if (state.sort) params.set("sort", state.sort);
  if (state.order) params.set("order", state.order);
  try {
    var res = await api("/locked?" + params.toString());
    var d = res.data || {};
    var sm = d.summary || {};
    var buckets = (sm.by_bucket || []).map(function (b) {
      return '<div><b>' + fmtInt(b.count) + "</b><span>" + esc(b.label) + "</span></div>";
    }).join("");
    var sum = document.getElementById("locked-summary");
    if (sum) {
      function pick(code) {
        return ((sm.by_lockup || []).filter(function (x) { return x.code === code; })[0] || {}).count;
      }
      sum.innerHTML = '<div class="grid cols-4">' +
        '<div class="card stat"><div class="label">受限基金</div><div class="value">' +
          fmtInt(d.total) + '</div><div class="hint">数据日 ' + esc(sm.as_of || "—") + "</div></div>" +
        '<div class="card stat"><div class="label">定期开放</div><div class="value">' +
          fmtInt(pick("periodic_open")) + '</div><div class="hint">只在开放期可申赎</div></div>' +
        '<div class="card stat"><div class="label">最短持有期</div><div class="value">' +
          fmtInt(pick("holding")) + '</div><div class="hint">买入后锁定期</div></div>' +
        '<div class="card stat"><div class="label">持有期中位数</div><div class="value">' +
          fmtDays(sm.median_days) + '</div><div class="hint">超过 90 天即单列</div></div>' +
      "</div>" +
      '<div class="card"><div class="card-head"><h3>持有期档位分布</h3></div>' +
        '<div class="hero-stats" style="grid-template-columns:repeat(4,1fr)">' + buckets + "</div>" +
        '<p class="muted small">' + esc(sm.note || "") + "</p></div>";
    }
    var from = (d.offset || 0) + 1;
    var to = (d.offset || 0) + (d.rows || []).length;
    body.innerHTML = lockedTable(d.rows || []) +
      '<div class="toolbar" style="margin-top:10px">' +
        '<button class="ghost" onclick="pageLocked(-1)">上一页</button>' +
        '<button class="ghost" onclick="pageLocked(1)">下一页</button>' +
        '<span class="muted" id="locked-meta">第 ' + from + "–" + to + " 条 / 共 " + fmtInt(d.total) + " 条</span>" +
      "</div>" +
      '<p class="muted small">共 ' + fmtInt(d.total) + " 只" +
      (d.total > (d.rows || []).length ? "，本页 " + (d.rows || []).length + " 只" : "") +
      "。定期开放的“开放状态”来自当日申赎快照的开放日字段：若该日期已过去，说明是本次开放期的起始日，" +
      "上游没有给下一次开放日，页面不会把它当成未来解锁日。</p>";
  } catch (err) {
    body.innerHTML = '<p class="err">' + esc(err.message) + "</p>";
  }
}

function setLockup(v) { state.lockup = v || ""; state.lockOffset = 0; loadLocked(); }
function pageLocked(dir) {
  state.lockOffset = Math.max(0, (state.lockOffset || 0) + dir * 200);
  loadLocked();
}
function setLockBucket(v) {
  state.lockBucket = v || "";
  state.lockOffset = 0;
  document.querySelectorAll('[data-bucket]').forEach(function (b) {
    b.className = (b.dataset.bucket === state.lockBucket) ? "primary" : "ghost";
  });
  loadLocked();
}

/* ── 数据库与存储 ───────────────────────────────────── */
async function renderDatabase() {
  const stRes = await api("/stats/storage");
  const st = stRes.data, d = st.disk || {};
  const usedPct = d.host_total_bytes
    ? 100 * (d.host_total_bytes - d.host_free_bytes) / d.host_total_bytes : null;
  const parts = [
    ["数据库文件", st.db.bytes, "含 WAL " + fmtBytes(st.db.wal_bytes)],
    ["原始响应 var/raw", d.raw_bytes, fmtInt(d.raw_files) + " 个文件（可离线重放）"],
    ["发布产物 reports", d.reports_bytes, fmtInt(d.reports_files) + " 个文件"],
    ["冻结快照 var/freeze", d.freeze_bytes, fmtInt(d.freeze_files) + " 个文件"],
  ];
  let rows = "";
  for (let i = 0; i < parts.length; i += 1) {
    rows += '<tr><td>' + esc(parts[i][0]) + '</td><td class="num">' + fmtBytes(parts[i][1]) +
      '</td><td class="muted">' + esc(parts[i][2]) + '</td></tr>';
  }
  $main.innerHTML =
    '<h1>数据库与存储</h1>' +
    '<p class="sub">只回答两件事：数据库多大、整个项目占了多少硬盘。逐表行数用 ' +
    '<span class="mono">python3 -m fundana stats</span> 查看。</p>' +
    '<div class="grid cols-3">' +
    stat("数据库文件", fmtBytes(st.db.bytes), "SQLite · " + fmtBytes(st.db.wal_bytes) + " WAL") +
    stat("项目总占用", fmtBytes(d.project_bytes), fmtInt(d.project_files) + " 个文件") +
    stat("所在磁盘可用", fmtBytes(d.host_free_bytes),
         usedPct == null ? "—" : "已用 " + usedPct.toFixed(1) + "% / 共 " + fmtBytes(d.host_total_bytes)) +
    '</div><h2>占用构成</h2><div class="table-wrap"><table>' +
    '<thead><tr><th>部分</th><th class="num">占用</th><th>说明</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table></div>' +
    '<p class="code-hint">数据库：<span class="mono">' + esc(st.db.path) + '</span> · 项目目录：' +
    '<span class="mono">' + esc(d.project_dir || "—") + '</span></p>';
}

/* ── 对比 ───────────────────────────────────────────── */
const CMP_COLORS = ["#1d4ed8", "#d92b2b", "#0e9f6e"];

async function renderCompare() {
  const codes = state.compareCodes.slice(0, 3);
  $main.innerHTML =
    '<h1>基金对比</h1><p class="sub">最多 3 只；净值曲线归一化到区间起点 = 100，跨风格也能直观比较波动。</p>' +
    '<div class="toolbar">' +
    '<input id="cmp-codes" type="text" placeholder="代码，用逗号分隔，如 000032,000003" value="' +
      esc(codes.join(",")) + '" style="width:340px">' +
    '<button class="primary" onclick="applyCompare()">对比</button>' +
    '<span class="muted">也可以先在榜单勾选再点「去对比」</span></div>' +
    '<div id="cmp-body">' +
      (codes.length >= 2 ? '<p class="loading">加载中…</p>' : notice("至少需要 2 只基金。")) + "</div>";
  if (codes.length >= 2) await loadCompare(codes);
}

function applyCompare() {
  const raw = ((document.getElementById("cmp-codes") || {}).value || "");
  const codes = raw.split(/[,，\s]+/).filter(Boolean).slice(0, 3);
  if (codes.length < 2) { toast("至少输入 2 个基金代码", "warn"); return; }
  state.compareCodes = codes;
  location.hash = "#/compare?codes=" + codes.join(",");
  loadCompare(codes);
}

async function loadCompare(codes) {
  const body = document.getElementById("cmp-body");
  if (!body) return;
  try {
    const res = await api("/compare?codes=" + encodeURIComponent(codes.join(",")));
    const d = res.data || {};
    const items = d.items || [];
    if (!items.length) { body.innerHTML = notice("没有取到这些基金的数据"); return; }
    const rows = [
      ["风格", function (f) { return esc(f.style_label || "—"); }],
      ["基金公司", function (f) { return esc(f.company_name || "—"); }],
      ["成立日", function (f) { return '<span class="mono">' + esc(f.estab_date || "—") + "</span>"; }],
      ["家族规模（亿元）", function (f) { return fmtNum(f.scale_billion); }],
      ["持续计提费（%）", function (f) { return fmtNum(f.recurring_fee_pct); }],
      ["近一年年化", function (f) { return '<span class="' + trendCls(f.metrics && f.metrics.annual_dec) + '">' +
        signPctDec(f.metrics && f.metrics.annual_dec) + "</span>"; }],
      ["近一年累计", function (f) { return '<span class="' + trendCls(f.metrics && f.metrics.total_return_pct) + '">' +
        signPctPoint(f.metrics && f.metrics.total_return_pct) + "</span>"; }],
      ["夏普比率", function (f) { return fmtNum(f.metrics && f.metrics.sharpe); }],
      ["索提诺", function (f) { return fmtNum(f.metrics && f.metrics.sortino); }],
      ["卡玛比率", function (f) { return fmtNum(f.metrics && f.metrics.calmar_dec); }],
      ["近一年最大回撤", function (f) { return fmtPct(f.metrics && f.metrics.mdd_dec); }],
      ["波动率", function (f) { return fmtPct(f.metrics && f.metrics.vol_dec); }],
      ["日胜率", function (f) { return fmtPctPoint(f.metrics && f.metrics.positive_ratio_pct, 1); }],
      ["近三年最大回撤", function (f) { return fmtPct(f.mdd_3y); }],
      ["正式榜", function (f) { return f.rank ? esc(f.board_label || "入选") + " #" + f.rank : '<span class="muted">未入选</span>'; }],
    ];
    const head = '<tr><th>指标</th>' + items.map(function (f, i) {
      return '<th><span style="color:' + CMP_COLORS[i % 3] + '">●</span> ' + esc(f.name) +
        '<div class="mono muted small">' + esc(f.fcode) + "</div></th>";
    }).join("") + "</tr>";
    var CMP_KEYS = { "近一年年化": "annual_1y", "近一年累计": "ret_1y", "夏普比率": "sharpe",
      "索提诺": "sortino", "卡玛比率": "calmar", "近一年最大回撤": "mdd_1y", "波动率": "vol",
      "日胜率": "positive", "近三年最大回撤": "mdd_3y" };
    const bodyRows = rows.map(function (r) {
      return "<tr><td>" + r[0] + infoBtn(CMP_KEYS[r[0]]) + "</td>" + items.map(function (f) {
        return '<td class="num">' + r[1](f) + "</td>";
      }).join("") + "</tr>";
    }).join("");
    body.innerHTML =
      '<div class="table-wrap"><table>' + head + "<tbody>" + bodyRows + "</tbody></table></div>" +
      '<h2>归一化净值</h2><div class="card">' + cmpChart(items) + "</div>" +
      '<h2>年度收益</h2><div class="card">' + cmpAnnual(items) + "</div>" +
      '<p class="code-hint">区间 ' + esc(d.start || "—") + " → " + esc(d.end || "—") +
      "（" + fmtInt(d.days) + " 天）。年化为 365.25 天口径，收益按日增长率链式复利。</p>";
  } catch (e) {
    body.innerHTML = '<p class="err">' + esc(e.message) + "</p>";
  }
}

function cmpChart(items) {
  const series = items.filter(function (f) { return (f.nav || []).length >= 2; });
  if (!series.length) return '<p class="empty">暂无净值数据</p>';
  const w = 900, h = 260, padL = 48, padR = 12, padT = 12, padB = 26;
  let lo = Infinity, hi = -Infinity, n = 0;
  series.forEach(function (f) {
    f.nav.forEach(function (p) { lo = Math.min(lo, p.value); hi = Math.max(hi, p.value); });
    n = Math.max(n, f.nav.length);
  });
  if (!isFinite(lo) || !isFinite(hi)) return '<p class="empty">暂无净值数据</p>';
  if (hi - lo < 1e-6) { hi += 0.5; lo -= 0.5; }
  const span = hi - lo; lo -= span * 0.05; hi += span * 0.05;
  function Y(v) { return padT + (hi - v) * (h - padT - padB) / (hi - lo); }
  const paths = series.map(function (f, i) {
    const m = f.nav.length;
    const d = f.nav.map(function (p, k) {
      const x = padL + k * (w - padL - padR) / Math.max(1, m - 1);
      return (k ? "L" : "M") + x.toFixed(1) + " " + Y(p.value).toFixed(1);
    }).join(" ");
    return '<path d="' + d + '" fill="none" stroke="' + CMP_COLORS[i % 3] + '" stroke-width="1.8"></path>';
  }).join("");
  const ticks = [0, 0.5, 1].map(function (f) {
    const v = lo + (hi - lo) * f;
    return '<text x="' + (padL - 6) + '" y="' + (Y(v) + 4).toFixed(1) +
      '" text-anchor="end" class="axis">' + v.toFixed(1) + "</text>";
  }).join("");
  const base = series[0].nav;
  const xl = [0, 0.5, 1].map(function (f) {
    const i = Math.min(base.length - 1, Math.round(f * (base.length - 1)));
    const anchor = f === 0 ? "start" : (f === 1 ? "end" : "middle");
    return '<text x="' + (padL + i * (w - padL - padR) / Math.max(1, base.length - 1)).toFixed(1) +
      '" y="' + (h - 6) + '" text-anchor="' + anchor + '" class="axis">' + esc(base[i].date) + "</text>";
  }).join("");
  const legend = series.map(function (f, i) {
    const last = f.nav[f.nav.length - 1].value;
    return '<span style="margin-right:14px;color:' + CMP_COLORS[i % 3] + '">● ' + esc(f.name) +
      " " + signNum(last - 100, 2) + "%</span>";
  }).join("");
  return '<div class="chart-head">' + legend + "</div>" +
    '<svg viewBox="0 0 ' + w + " " + h + '" role="img" aria-label="归一化净值对比">' +
    paths + ticks + xl + "</svg>";
}

function cmpAnnual(items) {
  const years = [];
  items.forEach(function (f) {
    (f.annual || []).forEach(function (a) {
      if (years.indexOf(a.period) < 0) years.push(a.period);
    });
  });
  years.sort();
  if (!years.length) return '<p class="empty">暂无年度数据</p>';
  const head = '<tr><th>年度</th>' + items.map(function (f) {
    return "<th>" + esc(f.name) + "</th>";
  }).join("") + "</tr>";
  const rows = years.map(function (y) {
    return "<tr><td>" + esc(y) + " 年</td>" + items.map(function (f) {
      const hit = (f.annual || []).filter(function (a) { return a.period === y; })[0];
      if (!hit) return '<td class="num muted">—</td>';
      return '<td class="num ' + trendCls(hit.chain_return_pct) + '">' + signPctPoint(hit.chain_return_pct) +
        (hit.partial ? ' <span class="badge warn">未结束</span>' : "") + "</td>";
    }).join("") + "</tr>";
  }).join("");
  return '<div class="table-wrap" style="max-height:none"><table>' + head + "<tbody>" + rows + "</tbody></table></div>";
}

/* ── 收藏 ───────────────────────────────────────────── */
async function renderWatchlist() {
  $main.innerHTML =
    '<h1>收藏</h1><p class="sub">长期观察用：只存代码与备注，指标每次取最新一次采集，不存过期快照。</p>' +
    '<div class="toolbar">' +
    '<input id="watch-code" type="text" placeholder="输入 6 位基金代码" style="width:190px">' +
    '<input id="watch-note" type="text" placeholder="备注（可选）" style="width:220px">' +
    '<button class="primary" onclick="addWatchInput()">加入收藏</button>' +
    '<span class="muted" id="watch-meta"></span></div>' +
    '<div id="watch-body"><p class="loading">加载中…</p></div>';
  await loadWatchlist();
}

async function loadWatchlist() {
  const body = document.getElementById("watch-body");
  if (!body) return;
  try {
    const res = await api("/watchlist");
    const rows = res.data.items || [];
    const meta = document.getElementById("watch-meta");
    if (meta) meta.textContent = rows.length ? "共 " + fmtInt(rows.length) + " 只" : "";
    if (!rows.length) {
      body.innerHTML = notice("还没有收藏。可以在上面的输入框填代码，或在榜单/基金详情点 ☆ 加入。");
      return;
    }
    let tr = "";
    rows.forEach(function (r) {
      tr += '<tr class="clickable" onclick="location.hash=\'#/fund/' + esc(r.fcode) + '\'">' +
        '<td class="mono">' + esc(r.fcode) + '</td><td>' + esc(r.name || "—") + '</td>' +
        '<td><span class="badge">' + esc(r.style_label || labelOf("styles", r.style_code)) + '</span></td>' +
        '<td class="mono">' + esc((r.added_at || "").replace("T", " ").slice(0, 16)) + '</td>' +
        '<td class="num">' + fmtPct(r.annual_1y_dec) + '</td>' +
        '<td class="num">' + fmtCap(r.sharpe_1y, 5) + '</td>' +
        '<td class="num">' + fmtPct(r.mdd_1y_dec) + '</td>' +
        '<td class="num">' + fmtPctPoint(r.positive_1y_pct, 1) + '</td>' +
        '<td class="num">' + fmtNum(r.asset_scale_billion) + '</td>' +
        '<td>' + esc(r.buyable_label || labelOf("buyable", r.buyable_state)) + '</td>' +
        '<td><button class="ghost" onclick="event.stopPropagation();toggleWatch(\'' +
          esc(r.fcode) + '\',\'watchlist\',null)">移除</button></td></tr>';
    });
    body.innerHTML = '<div class="table-wrap"><table><thead><tr><th>代码</th><th>名称</th>' +
      '<th>风格</th><th>加入时间</th>' + sortTh("annual_1y", "1年年化") +
      sortTh("sharpe", "夏普") + sortTh("mdd", "1年回撤") + sortTh("positive", "日胜率") +
      sortTh("scale", "家族规模(亿)") +
      '<th>当日申购</th><th>操作</th></tr></thead><tbody>' + tr + '</tbody></table></div>';
  } catch (err) {
    body.innerHTML = '<p class="err">' + esc(err.message) + '</p>';
  }
}

async function addWatchInput() {
  const code = ((document.getElementById("watch-code") || {}).value || "").trim();
  const note = ((document.getElementById("watch-note") || {}).value || "").trim();
  if (!code) { showBanner("请先输入基金代码"); return; }
  await addWatch(code, "manual", null, note);
}

/* 加入收藏：统一走 toggleWatch，已收藏时再点即取消，不再弹“已经在收藏里”的错误。 */
async function addWatch(code, source, btn, note) {
  const fcode = String(code || "").trim();
  if (!fcode) { toast("请先输入基金代码", "warn"); return; }
  await loadWatchCodes();
  if (state.watchCodes.has(fcode)) {
    toast("这只基金已经在收藏里：" + fcode);
    return;
  }
  try {
    await post("/watchlist", { fcode: fcode, source: source || "manual", note: note || null });
    state.watchCodes.add(fcode);
    if (btn) { btn.textContent = "★"; btn.classList.add("on"); }
    const box = document.getElementById("watch-code");
    if (box) box.value = "";
    toast("收藏成功：" + fcode, "success");
    if (state.view === "watchlist") await loadWatchlist();
  } catch (err) {
    toast("收藏失败：" + err.message, "warn");
  }
}

async function removeWatch(code) {
  try {
    await api("/watchlist/" + encodeURIComponent(code), { method: "DELETE" });
    await loadWatchlist();
  } catch (err) {
    showBanner(err.message);
  }
}

/* ── 运行状态 ───────────────────────────────────────── */
async function renderRuns() {
  const [runs, ops] = await Promise.all([api("/runs?limit=30"), api("/operations/status")]);
  const prov = ops.data.provenance || {};
  const provOk = prov.status === "ok";
  $main.innerHTML = `
    <h1>运行状态</h1><p class="sub">采集与筛选运行记录；抓取状态按源统计。</p>
    <h2>数据来源与真实性</h2>
    <div class="card">
      <p><span class="badge ${provOk ? "good" : "warn"}">${provOk ? "溯源通过" : esc(prov.status || "待核对")}</span>
      ${fmtInt(prov.tables || 0)} 张源表 · 共 ${fmtInt(prov.total_rows || 0)} 行；
      最近门禁核对 ${fmtInt(prov.checked_rows || 0)} 行${prov.nav_since ? "（NAV 取 " + esc(prov.nav_since) + " 起近 90 天，其余源表全量）" : ""}。</p>
      <p class="muted">raw_sha256 缺失 ${fmtInt(prov.missing_raw || 0)} · 格式错 ${fmtInt(prov.bad_format || 0)} · 未注册来源 ${fmtInt(prov.unregistered || 0)}。
      基金原始数据只来自公开 API；缺失值显示「—」，不填充 0，不生成模拟值；ETF 只作风格参考。</p>
    </div>
    <h2>运行历史</h2>
    <div class="table-wrap"><table><thead><tr><th>run_id</th><th>模式</th><th>状态</th><th>开始</th><th>结束</th></tr></thead>
    <tbody>${runs.data.map((r) => `<tr><td class="mono">${esc(r.run_id)}</td><td>${esc(r.mode_label || r.mode)}</td>
      <td><span class="badge ${r.status === "ok" ? "good" : r.status === "running" ? "accent" : "warn"}">${esc(r.status_label || r.status)}</span></td>
      <td class="mono">${esc(r.started_at)}</td><td class="mono">${esc(r.finished_at || "—")}</td></tr>`).join("")}
    </tbody></table></div>
    <h2>抓取状态</h2>
    <div class="split">
      <div class="card"><div class="table-wrap" style="max-height:360px"><table>
        <thead><tr><th>状态</th><th class="num">次数</th></tr></thead><tbody>
        ${(ops.data.fetch_status_labels || []).map((r) => `<tr><td title="${esc(r.code)}">${esc(r.label)}</td><td class="num">${fmtInt(r.count)}</td></tr>`).join("")}
      </tbody></table></div></div>
      <div class="card"><div class="table-wrap" style="max-height:360px"><table>
        <thead><tr><th>来源</th><th>状态</th><th class="num">次数</th></tr></thead><tbody>
        ${(ops.data.by_source || []).map((r) => `<tr><td title="${esc(r.source_id)}">${esc(r.source_label || r.source_id)}</td><td>${esc(r.status_label || r.status)}</td>
          <td class="num">${fmtInt(r.c)}</td></tr>`).join("")}
      </tbody></table></div></div>
    </div>
  `;
}

/* ── 发布报告 ───────────────────────────────────────── */
const FILE_NOTE = {
  "summary.md": "报告摘要（中文，发布时生成）",
  "validated.json": "发布清单（逐文件 SHA256 + 大小）",
  "publication_descriptor.json": "发布描述（run / 契约 / 计数）",
  "all_formal_candidates.csv": "全部正式候选（过线但未入榜也在内）",
  "bond_stable.csv": "纯债稳健榜",
  "bond_enhanced.csv": "增强债榜",
  "bond_core.csv": "核心精选榜（正式候选按排序分取 50 只）",
  "bond_core10.csv": "收益精选榜（跨风格再集中到 10 只，不含增强债与可转债，控制风格集中与相关性）",
  "bond_long.csv": "长期核心观察榜（5 年覆盖 ≥60% 且 5 年年化非空，按长期分排序）",
  "bond_locked.csv": "持有期受限榜",
  "bond_watch.csv": "观察榜（质量达标但当期买不到）",
  "bond_rookie.csv": "新锐榜（历史 1–3 年）",
};
function fileNote(path) {
  if (FILE_NOTE[path]) return FILE_NOTE[path];
  if (path.indexOf("diagnostics/") === 0) return "诊断表：" + (DIAG_CN[path.split("/").pop()] || "明细");
  if (path.indexOf("decisions/") === 0) return "单只决策（阈值/得分/理由）";
  if (path.indexOf("freeze/") === 0) return "冻结输入（可离线重放）";
  if (path.indexOf("audit/") === 0) return "审计：原始请求索引";
  return "";
}

async function renderReport() {
  const [pub, summary] = await Promise.all([
    api("/publications/current").catch((e) => ({ data: null, error: e })),
    api("/publications/summary").catch((e) => ({ data: null, error: e })),
  ]);
  if (!pub.data) {
    $main.innerHTML = `<h1>发布报告</h1>${notice("当前没有可用的发布：" + esc(pub.error.message), "warn")}`;
    return;
  }
  const files = pub.data.files || [];
  $main.innerHTML = `
    <h1>发布报告</h1><p class="sub">generation ${pub.data.generation} · ${esc(pub.data.publication_id || "")}</p>
    <div class="grid cols-3">
      ${stat("文件数", fmtInt(files.length), "validated.json 记录 SHA256")}
      ${stat("发布时刻", esc((pub.data.committed_at || pub.data.generated_at || "").slice(0, 19)), esc(pub.data.run_id || ""))}
      ${stat("总字节", fmtBytes(files.reduce((a, f) => a + (f.size || 0), 0)), "")}
    </div>
    <h2>summary.md</h2>
    <div class="card"><pre class="md">${esc((summary.data || {}).summary_md || "（无 summary.md）")}</pre></div>
    <h2>文件清单（前 60）</h2>
    <div class="table-wrap"><table><thead><tr><th>路径</th><th>说明</th><th class="num">字节</th><th>sha256</th></tr></thead>
    <tbody>${files.slice(0, 60).map((f) => `<tr>
      <td class="mono"><a href="/api/v1/publications/files/${encodeURI(f.path)}" download>${esc(f.path)}</a></td>
      <td>${esc(fileNote(f.path))}</td><td class="num">${fmtInt(f.size)}</td>
      <td class="mono muted">${esc((f.sha256 || "").slice(0, 16))}…</td></tr>`).join("")}
    </tbody></table></div>
  `;
}

/* 启动放在文件末尾：renderResearch 依赖下面用 const 声明的 SHADOW_WINDOWS，
   在这里调用会命中暂时性死区（TDZ）。 */
/* ── 诊断 ───────────────────────────────────────────── */
/* ── 研究：增强债影子榜 + 观察池（全部 research_only）──────── */
/* 7 项增强准入的检查项中文名：把 near-miss/shadow 的 failed 列翻成中文。 */
const CHECK_CN = { annual_1y: "1 年年化", sharpe: "夏普比率", sortino: "索提诺", calmar: "卡玛比率",
  positive: "日胜率", mdd: "最大回撤", vol: "波动率", fee: "持续计提费",
  scale: "规模", formal_score: "正式分" };
function checkCn(text) {
  if (!text) return "—";
  return String(text).split(",").map(function (x) {
    const k = x.trim();
    return CHECK_CN[k] || k;
  }).join("、");
}
const SHADOW_WINDOWS = [365, 730, 1095, 1825];
/* 风格中文名统一走 /meta/labels，避免前后端两套映射。 */
const styleCn = (code) => labelOf("styles", code);

async function renderResearch() {
  const win = state.shadowWindow || 1095;
  let opts = "";
  SHADOW_WINDOWS.forEach(function (w) {
    opts += "<option value='" + w + "'" + (w === win ? " selected" : "") + ">" +
      w + " 天 ≈ " + (w / 365).toFixed(1) + " 年</option>";
  });
  $main.innerHTML =
    "<h1>研究</h1>" +
    "<p class='sub'>research_only：只换窗口复算或挑出距门槛最近者，<b>不改阈值、不写库、不进正式发布</b>。</p>" +
    "<div class='card'><div class='toolbar' style='margin:0'>" +
    "<label for='shadow-win'>增强债影子榜窗口</label>" +
    "<select id='shadow-win'>" + opts + "</select>" +
    "<button class='primary' id='shadow-reload'>重新载入</button></div>" +
    "<p class='code-hint'>正式增强榜固定 1 年窗口；这里用同一套 7 项阈值换窗口复算，" +
    "回答“换 3 年会不会有人通过”，不改变正式结论。</p></div>" +
    "<div id='shadow-body'><p class='loading'>加载中…</p></div>" +
    "<h2>增强观察池</h2><div id='watch-body'><p class='loading'>加载中…</p></div>" +
    "<h2>v9/v10 影子前瞻</h2><div id='shadow-forward-body'><p class='loading'>加载中…</p></div>";
  const sel = document.getElementById("shadow-win");
  sel.addEventListener("change", function () {
    state.shadowWindow = Number(sel.value);
    loadShadow();
  });
  document.getElementById("shadow-reload").addEventListener("click", loadShadow);
  await Promise.all([loadShadow(), loadWatchPool(), loadShadowForward()]);
}

async function loadShadow() {
  const body = document.getElementById("shadow-body");
  if (!body) return;
  const win = state.shadowWindow || 1095;
  body.innerHTML = "<p class='loading'>加载中…</p>";
  try {
    const res = await api("/research/enhanced_shadow?window=" + win + "&limit=80");
    const rows = res.data || [];
    const meta = res.meta || {};
    const by = meta.by_style || {};
    let byRows = "";
    Object.keys(by).forEach(function (k) {
      byRows += "<tr><td>" + esc(styleCn(k)) + "</td>" +
        "<td class='num'>" + fmtInt(by[k].entered) + "</td>" +
        "<td class='num'>" + fmtInt(by[k].full_pass) + "</td>" +
        "<td class='num'>" + fmtInt(by[k].one_short) + "</td></tr>";
    });
    let trs = "";
    rows.forEach(function (r) {
      const full = Number(r.checks_passed) === Number(r.checks_total);
      trs += "<tr>" +
        "<td class='mono'>" + esc(r.fcode) + "</td><td>" + esc(r.name) + "</td>" +
        "<td><span class='badge'>" + esc(styleCn(r.style_code)) + "</span></td>" +
        "<td class='num'>" + (full ? "<span class='badge good'>" + esc(r.checks_passed) + "/" + esc(r.checks_total) + "</span>"
          : esc(r.checks_passed) + "/" + esc(r.checks_total)) + "</td>" +
        "<td>" + esc(checkCn(r.failed)) + "</td>" +
        "<td class='num'>" + fmtNum(r.annual_pct) + "%</td>" +
        "<td class='num'>" + fmtNum(r.sharpe) + "</td>" +
        "<td class='num'>" + fmtNum(r.calmar) + "</td>" +
        "<td class='num'>" + fmtNum(r.mdd_pct) + "%</td>" +
        "<td class='num'>" + fmtNum(r.positive_pct, 1) + "%</td>" +
        "<td class='num'>" + fmtNum(r.fee_pct) + "%</td>" +
        "<td class='num'>" + fmtNum(r.scale_billion) + " 亿</td></tr>";
    });
    body.innerHTML =
      "<div class='grid cols-3'>" +
      stat(win + " 天窗口进入复算", fmtInt(meta.entered), "增强风格（一级/二级/转债）") +
      stat("7 项全过", fmtInt(meta.full_pass), "研究参考，非正式入选") +
      stat("只差 1 项", fmtInt(meta.one_short), "最接近门槛") +
      "</div>" +
      "<div class='notice' style='margin:12px 0'>同一套准入阈值、不同窗口；" +
      "正式榜仍按 1 年窗口发布，本表不参与排序与配额。</div>" +
      (byRows ? "<div class='table-wrap' style='max-height:none;margin-bottom:14px'><table><thead><tr>" +
        "<th>风格</th><th class='num'>进入</th><th class='num'>全过</th><th class='num'>差 1 项</th>" +
        "</tr></thead><tbody>" + byRows + "</tbody></table></div>" : "") +
      "<div class='table-wrap'><table><thead><tr>" +
      ["代码", "名称", "风格", "通过", "差在哪", "年化", "Sharpe", "Calmar", "回撤", "日胜率", "费率", "家族规模"]
        .map(function (h) { return "<th>" + h + "</th>"; }).join("") +
      "</tr></thead><tbody>" + (trs || "<tr><td colspan='12' class='muted'>该窗口下没有增强风格基金</td></tr>") +
      "</tbody></table></div>" +
      "<p class='code-hint'>生成时间 " + esc(meta.generated_at || "—") +
      " · 数据源 reports/analysis/enhanced_shadow_" + win + "d.csv · 复算命令 <code>python3 -m fundana shadow --window " + win + "</code></p>";
  } catch (err) {
    body.innerHTML = notice("尚未生成 " + win + " 天影子榜：" + esc(err.message) +
      "<br>请先运行 <code>python3 -m fundana shadow --window " + win + "</code>", "warn");
  }
}

async function loadShadowForward() {
  const body = document.getElementById("shadow-forward-body");
  if (!body) return;
  try {
    const res = await api("/shadow-forward/summary");
    const d = res.data || {};
    if (!d.cohorts) {
      body.innerHTML = notice("尚无 v9/v10 影子 cohort。daily 发布后会冻结；先运行 " +
        "<code>python3 -m fundana shadow-forward --run-id &lt;run_id&gt;</code>。", "warn");
      return;
    }
    const latest = (d.cohort_list || [])[0] || {};
    const rows = (d.rows || []).filter(function (r) { return r.cohort_id === latest.cohort_id; });
    let trs = "";
    rows.forEach(function (r) {
      const sc = r.status_counts || {};
      const diff = (r.v9_median_pct == null || r.v10_median_pct == null) ? null
        : r.v9_median_pct - r.v10_median_pct;
      trs += "<tr><td>" + esc(r.board === "core10" ? "核心10" : "核心50") + "</td>" +
        "<td>" + esc(r.strategy) + "</td><td class='num'>" + fmtInt(r.horizon_days) + " 天</td>" +
        "<td class='num'>" + fmtInt((r.status_counts || {}).matured || 0) + "</td>" +
        "<td class='num'>" + fmtInt(sc.not_ready || 0) + "</td>" +
        "<td class='num'>" + fmtNum(r.v10_median_pct) + "%</td>" +
        "<td class='num'>" + fmtNum(r.v9_median_pct) + "%</td>" +
        "<td class='num'>" + (diff == null ? "—" : signPctPoint(diff)) + "</td>" +
        "<td class='num'>" + (r.v9_win_rate_vs_v10_median_pct == null ? "—"
          : fmtNum(r.v9_win_rate_vs_v10_median_pct, 1) + "%") + "</td></tr>";
    });
    body.innerHTML =
      "<div class='grid cols-4'>" +
      stat("证据等级", esc(d.evidence_label || "LEVEL_0"), "research_only，不构成切换依据") +
      stat("cohort", fmtInt(d.cohorts), "每次发布冻结一批") +
      stat("成熟观测", fmtInt(d.matured_observations), "未到期记 not_ready，不写 0") +
      stat("决策月", fmtInt(d.distinct_decision_months), "LEVEL_3 需 ≥12 个月") +
      "</div>" +
      "<div class='table-wrap' style='margin-top:12px'><table><thead><tr>" +
      "<th>榜单</th><th>策略</th><th class='num'>周期</th><th class='num'>成熟</th><th class='num'>未到期</th>" +
      "<th class='num'>v10 中位</th><th class='num'>v9 中位</th><th class='num'>v9−v10</th><th class='num'>v9 胜率</th>" +
      "</tr></thead><tbody>" + (trs || "<tr><td colspan='9' class='muted'>该 cohort 尚无观测</td></tr>") +
      "</tbody></table></div>" +
      "<p class='code-hint'>" + esc(d.note || "") + "</p>";
  } catch (err) {
    body.innerHTML = notice("影子前瞻暂不可用：" + esc(err.message), "warn");
  }
}

async function loadWatchPool() {
  const body = document.getElementById("watch-body");
  if (!body) return;
  try {
    const res = await api("/research/enhanced_watch?limit=108");
    const rows = res.data || [];
    const meta = res.meta || {};
    if (!rows.length) {
      body.innerHTML = notice("观察池为空：没有距门槛 1 项以内的增强风格基金。", "warn");
      return;
    }
    body.innerHTML =
      "<div class='notice warn' style='margin-bottom:10px'><b>这不是正式榜单。</b>" +
      "增强债 1 年窗口 7 项硬比较 0 只全过（ADR-017 不静默调阈值）；本表列出距门槛 1 项以内的 " +
      fmtInt(meta.total_near_miss || rows.length) + " 只，仅供研究跟踪，序号不代表推荐。</div>" +
      "<div class='table-wrap'><table><thead><tr>" +
      ["代码", "名称", "风格", "通过", "差在哪", "1年年化", "Sharpe", "Calmar", "1年回撤", "日胜率"]
        .map(function (h) { return "<th>" + h + "</th>"; }).join("") +
      "</tr></thead><tbody>" +
      rows.map(function (r) {
        return "<tr><td class='mono'>" + esc(r.fcode) + "</td><td>" + esc(r.name) + "</td>" +
          "<td><span class='badge'>" + esc(styleCn(r.style_code)) + "</span></td>" +
          "<td class='num'>" + esc(r.checks_passed) + "/" + esc(r.checks_total) + "</td>" +
          "<td>" + esc(checkCn(r.failed)) + "</td>" +
          "<td class='num'>" + fmtNum(r.annual_1y_pct) + "%</td>" +
          "<td class='num'>" + fmtNum(r.sharpe) + "</td>" +
          "<td class='num'>" + fmtNum(r.calmar) + "</td>" +
          "<td class='num'>" + fmtNum(Number(r.mdd) * 100) + "%</td>" +
          "<td class='num'>" + fmtNum(r.positive_pct, 1) + "%</td></tr>";
      }).join("") +
      "</tbody></table></div>";
  } catch (err) {
    body.innerHTML = notice("观察池不可用：" + esc(err.message), "warn");
  }
}

/* 诊断 CSV 与常见列的中文名（文件名/列名仍保留在 title 中便于排障）。 */
const DIAG_CN = {
  "bond_stable_by_family.csv": "同家族入榜（家族去重视图）",
  "conflicts.csv": "多源冲突明细",
  "coverage.csv": "净值覆盖与缺口",
  "enhanced_near_miss.csv": "增强债差几项（研究用途）",
  "family_duplicates.csv": "同家族重复入榜",
  "nulls.csv": "缺失字段统计",
  "style_evidence.csv": "风格判定证据",
  "tail_coverage.csv": "三年尾部指标覆盖",
  "fee_coverage.csv": "费用阶梯覆盖",
};
const COL_CN = {
  fcode: "代码", name: "名称", style_code: "风格", confidence: "判定置信度",
  nav_points: "净值点数", nav_first_date: "净值起始", nav_last_date: "净值最新",
  gap_max_days: "最大间隔(天)", freshness_days: "新鲜度(天)", flags_json: "覆盖标记",
  family_id: "家族", company: "基金公司", members: "份额数", codes: "份额代码",
  best_rank: "最好名次", best_score: "最高正式分", representative: "代表份额",
  checks_passed: "通过项", checks_total: "总项", failed: "未通过项",
  annual_1y_pct: "1年年化(%)", sharpe: "夏普比率", calmar: "卡玛比率",
  mdd: "最大回撤(小数)", positive_pct: "日胜率(%)", fee_pct: "费率(%)",
  scale_billion: "规模(亿)", manager_career_min_days: "经理最短职业(天)",
  reason_codes: "未通过原因", layer_reached: "最远层", passed: "是否通过",
  with_holdings: "有持仓证据", n: "数量", full: "完整数", avg_completeness: "平均完整度",
  avg_mdd_3y: "3年平均回撤", purchase_tiers: "申购档位", redemption_tiers: "赎回档位",
  has_meta: "有费率档案", mgr_fee_pct: "管理费(%)", trust_fee_pct: "托管费(%)",
  sales_fee_pct: "销售服务费(%)", kind: "费用类型", tier_no: "档位",
  amount_lo_cny: "金额下限", amount_hi_cny: "金额上限", days_lo: "天数下限",
  days_hi: "天数上限", rate_pct: "费率(%)", discount_pct: "折后费率(%)",
  fixed_cny: "固定费(元)", bond_class: "债券类别", holding_total_pct: "披露债券占净值(%)",
  convertible_pct: "可转债占比(%)", classifier_version: "判定规则版本",
  source: "数据来源", business_date: "业务日期", record_period: "报告期",
};

async function renderDiagnostics() {
  const listed = await api("/diagnostics").catch(function (e) { return { data: null, error: e }; });
  if (!listed.data) {
    $main.innerHTML = "<h1>诊断</h1>" + notice("当前没有可用的发布：" + esc(listed.error.message), "warn");
    return;
  }
  const files = listed.data;
  let cards = "";
  files.forEach(function (f) {
    cards += "<div class='card diag-card' data-name='" + esc(f.name) + "' style='cursor:pointer'>" +
      "<div style='font-weight:600'>" + esc(DIAG_CN[f.name] || f.name) + "</div>" +
      "<div class='mono muted' style='margin-top:4px'>" + esc(f.name) + "</div>" +
      "<div class='muted' style='margin-top:4px'>" + fmtBytes(f.bytes) + "</div></div>";
  });
  $main.innerHTML = "<h1>诊断</h1>" +
    "<p class='sub'>当前发布内的诊断 CSV；点击卡片预览前 500 行。这些表回答“为什么某只基金没过”。</p>" +
    "<div class='grid cols-3'>" + cards + "</div><div id='diag-body'></div>";
  document.querySelectorAll(".diag-card").forEach(function (el) {
    el.addEventListener("click", function () { previewDiagnostic(el.getAttribute("data-name")); });
  });
}

async function previewDiagnostic(name) {
  const body = document.getElementById("diag-body");
  if (!body) return;
  body.innerHTML = "<p class='loading'>加载中…</p>";
  try {
    const res = await api("/diagnostics/" + encodeURIComponent(name));
    const d = res.data;
    const cols = d.columns || [];
    const rows = d.rows || [];
    let head = "";
    cols.forEach(function (c) { head += "<th title='" + esc(c) + "'>" + esc(COL_CN[c] || c) + "</th>"; });
    let trs = "";
    rows.forEach(function (r) {
      let tds = "";
      cols.forEach(function (c) {
        let v = r[c] == null ? "" : String(r[c]);
        if (v.length > 60) v = v.slice(0, 60) + "…";
        const cls = (c === "fcode" || c === "code") ? " class='mono'" : "";
        tds += "<td" + cls + ">" + esc(v) + "</td>";
      });
      trs += "<tr>" + tds + "</tr>";
    });
    body.innerHTML = "<h2>" + esc(name) + " <span class='muted'>(" + fmtInt(d.total) + " 行" +
      (d.truncated ? "，预览前 " + rows.length : "") + ")</span></h2>" +
      "<div class='table-wrap'><table><thead><tr>" + head + "</tr></thead><tbody>" + trs + "</tbody></table></div>";
  } catch (err) {
    body.innerHTML = "<p class='err'>" + esc(err.message) + "</p>";
  }
}

/* 增强榜为空：读 diagnostics/enhanced_near_miss.csv，按风格汇总 + 展示最接近的 10 只 */
async function enhancedExplainer() {
  let rows = [];
  try {
    rows = (await api("/diagnostics/enhanced_near_miss.csv?limit=2000")).data.rows || [];
  } catch (e) {
    return notice("增强榜为空；诊断文件暂不可读。", "warn");
  }
  const annFloor = { bond_primary_enhanced: 4.5, bond_secondary_enhanced: 5.5, bond_convertible: 4.0 };
  function median(a) {
    if (!a.length) return null;
    const s = a.slice().sort(function (x, y) { return x - y; });
    const m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  const by = {};
  rows.forEach(function (r) { (by[r.style_code] = by[r.style_code] || []).push(r); });
  let summary = "";
  Object.keys(by).forEach(function (s) {
    const rs = by[s];
    const near = rs.filter(function (r) { return Number(r.checks_passed) >= 6; }).length;
    const ann = median(rs.map(function (r) { return Number(r.annual_1y_pct); }).filter(function (x) { return !Number.isNaN(x); }));
    const pos = median(rs.map(function (r) { return Number(r.positive_pct); }).filter(function (x) { return !Number.isNaN(x); }));
    summary += "<tr><td>" + esc(labelOf("styles", s)) + "</td><td class='num'>" + rs.length + "</td><td class='num'>" + near + "</td>" +
      "<td class='num'>" + fmtNum(ann) + "% / " + fmtNum(annFloor[s]) + "%</td>" +
      "<td class='num'>" + fmtNum(pos, 1) + "% / 55%</td></tr>";
  });
  let top = "";
  rows.filter(function (r) { return Number(r.checks_passed) >= 6; })
    .sort(function (a, b) { return Number(b.checks_passed) - Number(a.checks_passed) || Number(b.annual_1y_pct) - Number(a.annual_1y_pct); })
    .slice(0, 10)
    .forEach(function (r) {
      top += "<tr><td class='mono'>" + esc(r.fcode) + "</td><td>" + esc(r.name) + "</td>" +
        "<td><span class='badge'>" + esc(labelOf("styles", r.style_code)) + "</span></td>" +
        "<td class='num'>" + r.checks_passed + "/" + r.checks_total + "</td>" +
        "<td>" + esc(checkCn(r.failed)) + "</td>" +
        "<td class='num'>" + fmtNum(Number(r.annual_1y_pct)) + "%</td>" +
        "<td class='num'>" + fmtNum(Number(r.positive_pct), 1) + "%</td></tr>";
    });
  return "<div class='card' style='margin-top:18px'>" +
    "<h2 style='margin-top:0'>增强榜为什么是 0 只</h2>" +
    "<p>正式口径是 7 项硬比较（年化 / Sharpe / Calmar / MDD / 日胜率 / 费率 / 规模），本轮 " + fmtInt(rows.length) +
    " 只增强风格基金全部至少差 1 项。下表按“差几项”分组，可对照 <a href='#/diagnostics'>诊断</a> 里的 " +
    "<span class='mono'>enhanced_near_miss.csv</span>。</p>" +
    "<div class='table-wrap' style='max-height:none'><table><thead><tr><th>风格</th><th class='num'>进入 L6E</th>" +
    "<th class='num'>只差 1 项</th><th class='num'>年化中位 / 门槛</th><th class='num'>日胜率中位 / 门槛</th></tr></thead>" +
    "<tbody>" + summary + "</tbody></table></div>" +
    "<p class='muted' style='margin-top:10px'>根因：这套门槛按“1 年窗口”设计；2026 年债市 1 年收益中枢约 2–4%，" +
    "而一级/二级门槛是 4.5%/5.5%。含权益仓位的产品日胜率天然在 50–52%，达不到 55%。" +
    "转债档还有额外问题：管理费+托管费普遍 0.7–1.0%，而门槛是 0.70%。</p>" +
    "<h2>最接近的 10 只（研究用途，非正式入选）</h2>" +
    "<div class='table-wrap' style='max-height:none'><table><thead><tr><th>代码</th><th>名称</th><th>风格</th>" +
    "<th class='num'>通过</th><th>差在哪</th><th class='num'>1年年化</th><th class='num'>日胜率</th></tr></thead><tbody>" +
    (top || "<tr><td colspan='7' class='muted'>无 6/7 通过的基金</td></tr>") + "</tbody></table></div></div>";
}

/* ── 启动（必须在所有 const 声明之后）────────────────── */
window.addEventListener("hashchange", route);
initSearch();
api("/healthz").then((r) => {
  const el = document.getElementById("foot-ver");
  if (el) el.textContent = `v${r.data.code_version} · ${r.data.tables} 表`;
}).catch(() => {});
route();
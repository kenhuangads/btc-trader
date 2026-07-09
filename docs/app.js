/* BTC 波段訊號儀表板（純前端，資料由 GitHub Actions 每日產出的 JSON 提供） */
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtR = r => r == null ? "—" : (r > 0 ? "+" : "") + Number(r).toFixed(2) + "R";
const pct = (x, d = 0) => x == null ? "—" : (x * 100).toFixed(d) + "%";
const DIR = { LONG: "做多", SHORT: "做空", FLAT: "觀望" };
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

let LATEST = null, REVIEW = null, OPT = null, MODE = "all";

/* ---------------- 啟動 ---------------- */
async function boot() {
  try {
    const bust = "?t=" + Math.floor(Date.now() / 60000);
    const [l, r, o] = await Promise.all([
      fetch("data/latest.json" + bust, { cache: "no-store" }).then(x => x.json()),
      fetch("data/review.json" + bust, { cache: "no-store" }).then(x => x.json()),
      fetch("data/optimizer.json" + bust, { cache: "no-store" }).then(x => x.json()),
    ]);
    LATEST = l; REVIEW = r; OPT = o;
  } catch (e) {
    $("#hero").innerHTML = `<div class="skeleton">資料載入失敗，請下拉重新整理<br><small>${esc(e.message)}</small></div>`;
    return;
  }
  renderHeader(); renderToday(); renderReview(); renderSystem();
  livePriceLoop(); setInterval(tickCountdown, 1000);
  window.addEventListener("resize", debounce(() => { renderChart(); renderEquity(); renderTouch(); }, 200));
}

function debounce(fn, ms) { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }

/* ---------------- Header ---------------- */
function renderHeader() {
  const age = Date.now() - LATEST.generated_at;
  $("#data-time").textContent = `資料：${LATEST.generated_taipei}（台北）· 每日 08:07 後自動更新`;
  if (LATEST.stale || age > 30 * 3600e3) $("#stale-badge").hidden = false;
  $("#live-price").textContent = fmt(LATEST.price.close);
}

/* ---------------- 今日 ---------------- */
function renderToday() {
  const S = LATEST.signal, plan = S.plan;
  // 總經事件
  const evs = (S.macro?.events || []).filter(e => e.hours_until >= 0);
  $("#macro-strip").innerHTML = evs.length
    ? `<div class="card" style="padding:10px 14px"><span class="badge badge-amber">⚠ 重大事件</span>
       <span style="font-size:13px;margin-left:6px">${evs.map(e => `${esc(e.name)} ${e.date}（${Math.round(e.hours_until)}h 後）`).join("、")}
       <span class="hint">事件前 48h 降槓桿、24h 內不開新倉</span></span></div>` : "";

  // Hero
  const cls = S.direction === "LONG" ? "long" : S.direction === "SHORT" ? "short" : "flat";
  let notes = "";
  if (S.gates?.length) notes += S.gates.map(g => `<li class="gate">${esc(g)}</li>`).join("");
  if (S.direction === "FLAT" && S.watch?.length) notes += S.watch.map(w => `<li>${esc(w)}</li>`).join("");
  if (S.position_note) notes += `<li class="gate">${esc(S.position_note)}</li>`;
  $("#hero").className = `card hero ${cls}`;
  $("#hero").innerHTML = `
    <div class="hero-top">
      <div>
        <div class="dir-badge ${cls}">${DIR[S.direction]}</div>
        <div style="font-size:12px;color:var(--muted)">綜合分數 <b>${S.score > 0 ? "+" : ""}${S.score}</b> ·
          因子同向率 ${pct(S.agree)}</div>
      </div>
      <div class="hero-date">訊號日 ${LATEST.signal_date}<br>收盤 ${fmt(LATEST.price.close)}
        <span class="${LATEST.price.chg_1d >= 0 ? "up" : "down"}">${LATEST.price.chg_1d > 0 ? "+" : ""}${LATEST.price.chg_1d}%</span></div>
    </div>
    <div class="conf-row">
      <span style="font-size:12px;color:var(--muted)">信心</span>
      <div class="meter"><i style="width:${S.confidence}%"></i></div>
      <span class="conf-num">${S.confidence}</span>
    </div>
    ${notes ? `<ul class="hero-notes">${notes}</ul>` : ""}`;

  // 交易計畫
  if (plan) {
    const sgn = plan.direction === "LONG" ? 1 : -1;
    const rows = plan.entries.map(e => `
      <tr><td><b>第 ${e.i + 1} 檔</b><div class="prob-txt">${esc(e.tag)}</div></td>
        <td class="num"><b>${fmt(e.price)}</b></td>
        <td class="num">${Math.round(e.w * 100)}%</td>
        <td><div class="prob-bar"><i style="width:${(e.prob || 0) * 100}%"></i></div>
            <div class="prob-txt">${e.prob != null ? pct(e.prob) : "—"} 成交率</div></td></tr>`).join("");
    const tps = plan.tps.map(t => `
      <div class="kv"><span>🎯 ${t.name}（+${t.r}R）</span><b>${fmt(t.price)} <span class="hint">${esc(t.action)}</span></b></div>`).join("");
    const tg = (plan.targets || []).map(t => `<span class="pill">${fmt(t.price)}<br><span class="hint">${esc(t.why)}</span></span>`).join("");
    $("#plan-area").innerHTML = `
    <div class="card">
      <div class="card-title">進場掛單梯（限價 · 有效 48h）<span id="cd-validity" class="hint"></span></div>
      <table><thead><tr><th>檔位</th><th class="num">價格</th><th class="num">比重</th><th style="width:76px">歷史成交率</th></tr></thead>
      <tbody>${rows}</tbody></table>
      <div class="kv" style="margin-top:6px"><span>加權均價（全數成交）</span><b>${fmt(plan.avg_entry)}</b></div>
      <div class="kv"><span>🛑 停損（結構外 + ${LATEST.meta.params.stop_buffer_atr}×ATR 緩衝）</span>
        <b class="down">${fmt(plan.stop)}（-${plan.stop_pct}%）</b></div>
      ${tps}
      <div class="kv"><span>🏃 移動停損</span><b style="font-size:12.5px;font-weight:500">${esc(plan.trail_txt)}</b></div>
      ${tg ? `<div style="font-size:12px;color:var(--muted);margin-top:8px">磁吸參考目標</div><div class="pill-row">${tg}</div>` : ""}
      ${plan.warnings?.length ? `<div class="warn">⚠ ${plan.warnings.map(esc).join("；")}</div>` : ""}
      <div class="scen">
        <p class="main"><b>主劇本</b>：${esc(plan.scenarios.main)}</p>
        <p class="alt"><b>備援</b>：${esc(plan.scenarios.alt)}</p>
        <p class="invalid"><b>失效條件</b>：${esc(plan.scenarios.invalid)}</p>
      </div>
      <div class="countdown" id="cd-deadline"></div>
    </div>`;
    renderCalc(plan);
  } else {
    $("#plan-area").innerHTML = "";
    $("#calc-area").innerHTML = "";
  }

  renderChart(); renderFactors(); renderDiscipline();
}

/* ---------------- 倉位計算機 ---------------- */
function renderCalc(plan) {
  const savedEq = localStorage.getItem("eq") || 10000;
  $("#calc-area").innerHTML = `
  <div class="card">
    <div class="card-title">倉位計算機 <span class="hint">固定風險百分比法：先定可虧金額，反推倉位</span></div>
    <div class="calc-grid">
      <div><label>帳戶本金（USDT）</label><input id="calc-eq" type="number" inputmode="decimal" value="${savedEq}"></div>
      <div><label>單筆風險 %（建議 ${plan.risk_pct}%）</label><input id="calc-risk" type="number" inputmode="decimal" step="0.1" value="${plan.risk_pct}"></div>
    </div>
    <div class="calc-out" id="calc-out"></div>
    <div class="countdown" id="calc-note"></div>
  </div>`;
  const recompute = () => {
    const eq = +$("#calc-eq").value || 0, risk = +$("#calc-risk").value || 0;
    localStorage.setItem("eq", eq);
    const riskUsd = eq * risk / 100;
    const notional = riskUsd / (plan.stop_pct / 100);
    const margin = notional / plan.leverage;
    const qty = notional / plan.avg_entry;
    $("#calc-out").innerHTML = `
      <div class="cell"><b class="down">${fmt(riskUsd, 0)}</b><span>最大虧損 USDT</span></div>
      <div class="cell"><b>${fmt(notional, 0)}</b><span>名目倉位 USDT</span></div>
      <div class="cell"><b>${qty.toFixed(4)}</b><span>數量 BTC</span></div>
      <div class="cell"><b>${plan.leverage}x</b><span>建議槓桿</span></div>
      <div class="cell"><b>${fmt(margin, 0)}</b><span>所需保證金</span></div>
      <div class="cell"><b>${fmt(plan.liq_est)}</b><span>估計強平價</span></div>`;
    $("#calc-note").textContent =
      `逐檔數量：${plan.entries.map(e => `第${e.i + 1}檔 ${(qty * e.w).toFixed(4)} BTC`).join("、")}。` +
      `強平價比停損遠 ${(Math.abs(plan.liq_est - plan.stop) / plan.avg_entry * 100).toFixed(1)}% ✓（逐倉模式估算）`;
  };
  $("#calc-eq").addEventListener("input", recompute);
  $("#calc-risk").addEventListener("input", recompute);
  recompute();
}

/* ---------------- K 線圖 ---------------- */
function renderChart() {
  const box = $("#chart"); if (!box || !LATEST) return;
  const W = box.clientWidth || 340, H = box.clientHeight || 340;
  const C = LATEST.candles, plan = LATEST.signal.plan;
  const padR = 54, padT = 10, padB = 22, padL = 6;
  const n = C.length;
  let lo = Math.min(...C.map(c => c[3])), hi = Math.max(...C.map(c => c[2]));
  if (plan) { lo = Math.min(lo, plan.stop); hi = Math.max(hi, ...plan.tps.map(t => t.price)); }
  const span = hi - lo; lo -= span * .04; hi += span * .04;
  const X = i => padL + (i + .5) * (W - padL - padR) / n;
  const Y = p => padT + (hi - p) / (hi - lo) * (H - padT - padB);
  const green = css("--green"), red = css("--red"), muted = css("--muted"), border = css("--border"), accent = css("--accent");
  let s = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;

  // 價格格線與軸標
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const p = lo + (hi - lo) * i / ticks, y = Y(p);
    s += `<line x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="${border}" stroke-width="1"/>`;
    s += `<text x="${W - padR + 4}" y="${y + 3}" font-size="10" fill="${muted}">${Math.round(p / 1000)}k</text>`;
  }
  // 日期標
  for (let i = 0; i < n; i += Math.ceil(n / 5)) {
    const d = new Date(C[i][0]);
    s += `<text x="${X(i)}" y="${H - 6}" font-size="9.5" fill="${muted}" text-anchor="middle">${d.getUTCMonth() + 1}/${d.getUTCDate()}</text>`;
  }
  // 關鍵價位群（強度≥3 的前 6 個）
  (LATEST.levels || []).filter(l => l.strength >= 3 && l.price > lo && l.price < hi).slice(0, 6).forEach(l => {
    s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(l.price)}" y2="${Y(l.price)}" stroke="${muted}" stroke-width="0.8" stroke-dasharray="2 4" opacity="0.7"/>`;
  });
  // 計畫疊圖
  if (plan) {
    const eTop = Math.max(...plan.entries.map(e => e.price)), eBot = Math.min(...plan.entries.map(e => e.price));
    const zc = plan.direction === "LONG" ? green : red;
    s += `<rect x="${padL}" y="${Y(Math.max(eTop, eBot))}" width="${W - padL - padR}" height="${Math.abs(Y(eBot) - Y(eTop)) || 2}" fill="${zc}" opacity="0.12"/>`;
    plan.entries.forEach(e => {
      s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(e.price)}" y2="${Y(e.price)}" stroke="${zc}" stroke-width="1" stroke-dasharray="5 3" opacity="0.85"/>
            <text x="${W - padR + 4}" y="${Y(e.price) + 3}" font-size="9" fill="${zc}">E${e.i + 1}</text>`;
    });
    s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(plan.stop)}" y2="${Y(plan.stop)}" stroke="${red}" stroke-width="1.4"/>
          <text x="${W - padR + 4}" y="${Y(plan.stop) + 3}" font-size="9" fill="${red}">SL</text>`;
    plan.tps.forEach((t, i) => {
      s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(t.price)}" y2="${Y(t.price)}" stroke="${green}" stroke-width="1" stroke-dasharray="7 3"/>
            <text x="${W - padR + 4}" y="${Y(t.price) + 3}" font-size="9" fill="${green}">TP${i + 1}</text>`;
    });
  }
  // 未回補影線磁吸
  (LATEST.wicks || []).forEach(w => {
    if (w.mid > lo && w.mid < hi) {
      s += `<circle cx="${W - padR - 6}" cy="${Y(w.mid)}" r="3" fill="${accent}" opacity="0.9"/>`;
    }
  });
  // K 棒
  const bw = Math.max(1.6, (W - padL - padR) / n * 0.62);
  C.forEach((c, i) => {
    const [, o, h, l, cl] = c, up = cl >= o, col = up ? green : red, x = X(i);
    s += `<line x1="${x}" x2="${x}" y1="${Y(h)}" y2="${Y(l)}" stroke="${col}" stroke-width="1"/>`;
    s += `<rect x="${x - bw / 2}" y="${Y(Math.max(o, cl))}" width="${bw}" height="${Math.max(1, Math.abs(Y(o) - Y(cl)))}" fill="${col}" rx="0.5"/>`;
  });
  s += `</svg>`;
  box.innerHTML = s;
  $("#chart-legend").innerHTML = `
    <span><i style="background:${green}"></i>進場區/TP</span>
    <span><i style="background:${red}"></i>停損</span>
    <span><i style="background:${muted}"></i>關鍵價位群</span>
    <span><i style="background:${accent};height:8px;width:8px;border-radius:99px"></i>未回補影線(磁吸)</span>`;
}

/* ---------------- 因子 ---------------- */
function renderFactors() {
  const fs = LATEST.signal.factors || [];
  $("#factors").innerHTML = fs.map(f => {
    const w = Math.min(Math.abs(f.score), 100) / 2;
    const side = f.score >= 0 ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
    const col = f.score >= 0 ? css("--green") : css("--red");
    return `<div class="factor ${f.ok ? "" : "off"}">
      <div class="factor-top">
        <div class="factor-name">${esc(f.label)} <span class="w-chip">w ${f.weight}</span></div>
        <div class="factor-bar"><i style="${side};background:${col}"></i></div>
        <div class="factor-score" style="color:${f.score >= 0 ? "var(--green)" : "var(--red)"}">${f.score > 0 ? "+" : ""}${Math.round(f.score)}</div>
      </div>
      <div class="factor-note">${esc(f.note)}</div>
    </div>`;
  }).join("");
}

/* ---------------- 紀律卡 ---------------- */
function renderDiscipline() {
  const p = LATEST.meta.params, m = LATEST.stats_mini;
  $("#discipline").innerHTML = `
    <div class="card-title">風險紀律（系統強制執行）</div>
    <div class="pill-row">
      <span class="pill">單筆風險 ≤ ${p.risk_pct_base * 1.5}%</span>
      <span class="pill">槓桿上限 ${p.max_leverage}x</span>
      <span class="pill">連續 2 次停損 → 冷卻 1 日</span>
      <span class="pill">重大事件 24h 內不開新倉</span>
      <span class="pill">持倉上限 ${p.max_hold_days} 天</span>
      <span class="pill">TP1 後停損移保本</span>
    </div>
    <div style="font-size:12px;color:var(--muted);margin-top:10px">
      系統累積：${m.n_closed} 筆結案 · 勝率 ${pct(m.win_rate)} · 期望值 ${fmtR(m.expectancy_r)} ·
      掛單成交率 ${pct(m.fill_rate)} · 累積 ${fmtR(m.total_r)}
    </div>`;
}

/* ---------------- 復盤 ---------------- */
function renderReview() {
  document.querySelectorAll("#mode-seg button").forEach(b => {
    b.onclick = () => {
      MODE = b.dataset.mode;
      document.querySelectorAll("#mode-seg button").forEach(x => x.classList.toggle("on", x === b));
      renderReview();
    };
    b.classList.toggle("on", b.dataset.mode === MODE);
  });
  const S = MODE === "all" ? REVIEW.stats_all : MODE === "live" ? REVIEW.stats_live : REVIEW.stats_backtest;
  $("#kpis").innerHTML = [
    ["勝率", pct(S.win_rate), S.win_rate >= .5 ? "up" : ""],
    ["期望值", fmtR(S.expectancy_r), (S.expectancy_r || 0) > 0 ? "up" : "down"],
    ["盈虧比 PF", S.profit_factor ?? "—", (S.profit_factor || 0) >= 1.5 ? "up" : ""],
    ["掛單成交率", pct(S.fill_rate), ""],
    ["累積 R", fmtR(S.total_r), (S.total_r || 0) > 0 ? "up" : "down"],
    ["最大回撤", fmtR(S.max_dd_r), "down"],
  ].map(([k, v, c]) => `<div class="kpi"><b class="${c}">${v}</b><span>${k}</span></div>`).join("");

  renderEquity();

  const cal = S.calibration || [];
  $("#calibration").innerHTML = cal.length ? `<table class="mini-table"><thead>
    <tr><th>信心區間</th><th class="num">筆數</th><th class="num">勝率</th><th class="num">平均 R</th></tr></thead><tbody>
    ${cal.map(c => `<tr><td>${c.bucket}</td><td class="num">${c.n}</td><td class="num">${pct(c.win)}</td>
     <td class="num" style="color:${c.avg_r >= 0 ? "var(--green)" : "var(--red)"}">${fmtR(c.avg_r)}</td></tr>`).join("")}
    </tbody></table>
    <div class="hint" style="margin-top:6px">高信心區勝率應高於低信心區；若倒掛，優化器將提高門檻。</div>` :
    `<div class="skeleton" style="padding:12px 0">樣本不足</div>`;

  const trades = (REVIEW.trades || []).filter(t => MODE === "all" || t.mode === MODE);
  const openIds = new Set((REVIEW.open_trades || []).map(t => t.id));
  const sorted = [...trades].sort((a, b) => (openIds.has(b.id) - openIds.has(a.id)) || b.date.localeCompare(a.date));
  $("#trades").innerHTML = sorted.length ? sorted.map(t => tradeCard(t, openIds.has(t.id))).join("") :
    `<div class="card skeleton">此範圍尚無交易</div>`;
}

function tradeCard(t, isOpen) {
  const dirCls = t.direction === "LONG" ? "long" : "short";
  const rCol = t.r == null ? "var(--muted)" : t.r > 0 ? "var(--green)" : "var(--red)";
  const status = isOpen ? (t.status === "pending" ? "⏳ 掛單中" : "🟢 持倉中")
    : t.status === "cancelled" ? "未成交" : "已結案";
  const modeChip = t.mode === "backtest" ? `<span class="badge badge-slate">回測</span>` : `<span class="badge badge-green">實盤</span>`;
  const exits = (t.exits || []).map(e => {
    const rn = { tp1: "TP1", tp2: "TP2", stop: "停損", be_stop: "保本出場", trail_stop: "移動停損", time: "到期平倉", reverse_signal: "反向訊號離場" }[e.reason] || e.reason;
    return `${rn} @ ${fmt(e.price)}（${Math.round(e.frac * 100)}%）`;
  }).join("；");
  return `<details class="trade" ${isOpen ? "open" : ""}>
    <summary>
      <span class="t-dir ${dirCls}">${DIR[t.direction]}</span>
      <span class="t-date">${t.date} ${modeChip} <span class="hint">${status}</span></span>
      <span class="t-r" style="color:${rCol}">${t.status === "cancelled" ? "—" : fmtR(t.r)}</span>
    </summary>
    <div class="t-body">
      <div class="kv"><span>信心 / 分數</span><b>${t.confidence} / ${t.score > 0 ? "+" : ""}${t.score}</b></div>
      <div class="kv"><span>掛單</span><b style="font-weight:500;font-size:12.5px">${t.plan.entries.map(e => fmt(e.price)).join(" / ")}</b></div>
      <div class="kv"><span>實際均價（成交 ${Math.round((t.filled_w || 0) * 100)}%）</span><b>${fmt(t.avg_fill)}</b></div>
      <div class="kv"><span>停損 ${isOpen ? "（目前）" : ""}</span><b>${fmt(isOpen ? t.stop_now : t.plan.stop)}</b></div>
      ${exits ? `<div class="kv"><span>出場</span><b style="font-weight:500;font-size:12.5px">${exits}</b></div>` : ""}
      <div class="kv"><span>MFE / MAE</span><b><span class="up">${fmtR(t.mfe_r)}</span> / <span class="down">${fmtR(t.mae_r)}</span></b></div>
      <div class="kv"><span>風險 / 槓桿</span><b>${t.plan.risk_pct}% / ${t.plan.leverage}x</b></div>
      ${(t.lessons || []).map(l => `<span class="lesson">📝 ${esc(l)}</span>`).join("")}
    </div>
  </details>`;
}

function renderEquity() {
  const box = $("#equity-chart"); if (!box || !REVIEW) return;
  const eq = (REVIEW.equity || []).filter(p => MODE === "all" || p.mode === MODE);
  if (eq.length < 2) { box.innerHTML = `<div class="skeleton">樣本不足</div>`; return; }
  const W = box.clientWidth || 340, H = box.clientHeight || 200;
  const padR = 40, padT = 8, padB = 18, padL = 6;
  const ys = eq.map(p => p.r);
  let lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
  const span = (hi - lo) || 1; lo -= span * .1; hi += span * .1;
  const X = i => padL + i * (W - padL - padR) / (eq.length - 1);
  const Y = v => padT + (hi - v) / (hi - lo) * (H - padT - padB);
  const green = css("--green"), muted = css("--muted"), border = css("--border"), accent = css("--accent");
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(0)}" y2="${Y(0)}" stroke="${border}"/>`;
  [lo + span * .1, hi - span * .1].forEach(v => {
    s += `<text x="${W - padR + 4}" y="${Y(v) + 3}" font-size="10" fill="${muted}">${v.toFixed(1)}R</text>`;
  });
  let pathBT = "", pathLV = "";
  eq.forEach((p, i) => {
    const pt = `${X(i)},${Y(p.r)}`;
    if (p.mode === "backtest") pathBT += (pathBT ? " L" : "M") + pt;
    else pathLV += (pathLV ? " L" : "M") + pt;
  });
  // 連接回測末點與實盤首點
  const lastBT = eq.map((p, i) => [p, i]).filter(x => x[0].mode === "backtest").pop();
  const firstLV = eq.map((p, i) => [p, i]).filter(x => x[0].mode === "live")[0];
  if (lastBT && firstLV && pathLV) pathLV = `M${X(lastBT[1])},${Y(lastBT[0].r)} L` + pathLV.slice(1);
  if (pathBT) s += `<path d="${pathBT}" fill="none" stroke="${muted}" stroke-width="1.8"/>`;
  if (pathLV) s += `<path d="${pathLV}" fill="none" stroke="${accent}" stroke-width="2.2"/>`;
  const last = eq[eq.length - 1];
  s += `<circle cx="${X(eq.length - 1)}" cy="${Y(last.r)}" r="3.5" fill="${last.r >= 0 ? green : css("--red")}"/>`;
  s += `<text x="${padL}" y="${H - 5}" font-size="9.5" fill="${muted}">${eq[0].date}</text>`;
  s += `<text x="${W - padR}" y="${H - 5}" font-size="9.5" fill="${muted}" text-anchor="end">${last.date}（${fmtR(last.r)}｜$${fmt(last.eq)}）</text>`;
  box.innerHTML = s + "</svg>";
}

/* ---------------- 系統 ---------------- */
function renderSystem() {
  const logs = [...(OPT.history || [])].reverse();
  $("#opt-log").innerHTML = logs.length ? logs.slice(0, 40).map(l => `
    <div class="log-item">
      <div class="d">${l.date} · v${OPT.version}</div>
      <div class="chg-line"><b>${esc(l.param)}</b>：${esc(String(l.old))} → <b style="color:var(--accent)">${esc(String(l.new))}</b></div>
      <div class="why">${esc(l.reason)}</div>
    </div>`).join("") :
    `<div class="skeleton" style="padding:12px 0">尚無調參紀錄——累積足夠結案樣本後，每週自動檢討</div>`;

  const W = OPT.weights || {}, E = OPT.factor_edges || {};
  const labels = { trend_daily: "日線趨勢", trend_4h: "4小時結構", momentum: "動能", funding: "資金費率", oi_price: "OI×價格", taker_flow: "CVD 主動流", rvol: "相對量能", wick_magnet: "影線磁吸", levels: "關鍵價位", squeeze_setup: "軋空醞釀" };
  $("#weights").innerHTML = Object.entries(W).map(([k, v]) => {
    const e = E[k] || {};
    return `<div class="wrow"><span class="nm">${labels[k] || k}</span>
      <div class="wbar"><i style="width:${v / 1.6 * 100}%"></i></div>
      <span class="val">${v.toFixed(2)}｜${e.edge != null ? "命中 " + pct(e.edge) : "樣本不足"}</span></div>`;
  }).join("");

  renderTouch();

  const P = OPT.params || {};
  const pLabels = [
    ["entry_offsets_atr", "掛單深度（ATR 倍數）", v => v.map(x => x.toFixed(2)).join(" / ")],
    ["entry_depth_mult", "深度倍率（優化器調整）", v => "×" + v],
    ["stop_buffer_atr", "停損緩衝（ATR）", v => v],
    ["trail_atr_mult", "移動停損（ATR）", v => v + "×"],
    ["risk_pct_base", "基準單筆風險", v => v + "%"],
    ["score_threshold", "出手分數門檻", v => "±" + v],
    ["confidence_floor", "信心門檻", v => v],
    ["entry_validity_hours", "掛單有效期", v => v + " 小時"],
    ["max_hold_days", "最長持倉", v => v + " 天"],
    ["max_leverage", "槓桿上限", v => v + "x"],
  ];
  $("#params").innerHTML = pLabels.map(([k, lbl, f]) =>
    `<div class="kv"><span>${lbl}</span><b>${f(P[k])}</b></div>`).join("") +
    `<div class="hint" style="margin-top:6px">模型版本 v${OPT.version} · 最近調參 ${OPT.tuned_at || "—"}</div>`;

  $("#philosophy").innerHTML = [
    ["大賺小賠的結構", "停損固定小（1~2% 帳戶風險），獲利用「TP1 保本 → TP2 收割 → 移動停損讓利潤奔跑」拉長右尾。虧損永遠是計畫內的小數字，獲利上不封頂。"],
    ["高掛單成功率的來源", "掛單不追價：吸附在支撐/壓力群前緣，配合歷史觸價機率選深度。成交率與成本是蹺蹺板，優化器依近 20 筆成交率自動調整深度。"],
    ["逆向與擁擠度（GCR 反身性）", "資金費率極端分位＝人群擁擠訊號。空頭付費增倉但價格拒跌＝軋空燃料；多頭狂熱滯漲＝多殺多前兆。在共識最擁擠處找結構脆弱點。"],
    ["訂單流驗證（OI×CVD）", "突破要有新資金（OI↑）與主動買盤（CVD↑）共振才是真突破；價漲量縮、OI 下滑的突破視為誘多，不追。"],
    ["影線磁吸（CrypNuevo）", "流動性真空造成的長影線，市場傾向回補 50%。未回補影線是進場埋伏區與止盈磁吸目標。"],
    ["紀律鐵律", "連續 2 次停損強制冷卻 1 日（防報復性交易）；FOMC/CPI/非農前 48h 降槓桿、24h 內不開新倉；持倉最長 7 天，到期平倉不戀戰；邏輯破壞無條件離場不凹單。"],
    ["為何是模擬追蹤", "本系統輸出「推薦與復盤」，不自動下單。所有統計以保守規則模擬（同棒先進後損、含手續費滑價），寧可低估不高估。"],
  ].map(([t, c]) => `<details class="phil"><summary>${t}</summary><p>${c}</p></details>`).join("");

  $("#about").innerHTML = `
    <b>資料來源</b>：K 線 ${esc(LATEST.src.klines)} · 衍生品 ${esc(LATEST.src.deriv)}（每日 UTC 00:07 由 GitHub Actions 自動更新）<br>
    <b>免責聲明</b>：本工具為研究與教育用途的訊號模擬器，非投資建議。加密貨幣合約具極高風險，
    槓桿交易可能損失全部本金。過去績效（含回測）不代表未來表現，請自行評估並僅以可承受損失的資金參與。<br>
    <a href="https://github.com/kenhuangads/btc-trader" style="color:var(--accent)">GitHub 原始碼</a> · 引擎 v${OPT.version}`;
}

function renderTouch() {
  const box = $("#touch-chart"); if (!box || !OPT?.touch_probs?.long) return;
  const L = OPT.touch_probs.long, S = OPT.touch_probs.short;
  const keys = Object.keys(L).map(Number).sort((a, b) => a - b);
  if (!keys.length) { box.innerHTML = ""; return; }
  const W = box.clientWidth || 340, H = box.clientHeight || 200;
  const padR = 10, padT = 10, padB = 20, padL = 34;
  const X = k => padL + (k - keys[0]) / (keys[keys.length - 1] - keys[0]) * (W - padL - padR);
  const Y = p => padT + (1 - p) * (H - padT - padB);
  const green = css("--green"), red = css("--red"), muted = css("--muted"), border = css("--border");
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  [0, .25, .5, .75, 1].forEach(p => {
    s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(p)}" y2="${Y(p)}" stroke="${border}"/>
          <text x="4" y="${Y(p) + 3}" font-size="9.5" fill="${muted}">${p * 100}%</text>`;
  });
  [0.5, 1.0, 1.5, 2.0, 2.5].forEach(k => {
    if (k >= keys[0] && k <= keys[keys.length - 1])
      s += `<text x="${X(k)}" y="${H - 6}" font-size="9.5" fill="${muted}" text-anchor="middle">${k}×ATR</text>`;
  });
  const line = (obj, col) => {
    let d = "";
    keys.forEach(k => { const v = obj[k.toFixed(1)]; if (v != null) d += (d ? " L" : "M") + `${X(k)},${Y(v)}`; });
    return `<path d="${d}" fill="none" stroke="${col}" stroke-width="2"/>`;
  };
  s += line(L, green) + line(S, red);
  s += `<text x="${W - 80}" y="${padT + 10}" font-size="10" fill="${green}">— 做多掛單</text>`;
  s += `<text x="${W - 80}" y="${padT + 24}" font-size="10" fill="${red}">— 做空掛單</text>`;
  box.innerHTML = s + "</svg>";
}

/* ---------------- 即時價 ---------------- */
async function fetchLive() {
  const tryGet = async (url, pick) => {
    const ctl = new AbortController(); const to = setTimeout(() => ctl.abort(), 4000);
    try { const j = await (await fetch(url, { signal: ctl.signal })).json(); return pick(j); }
    finally { clearTimeout(to); }
  };
  const sources = [
    ["https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", j => +j.price],
    ["https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT", j => +j.data[0].last],
    ["https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT", j => +j.result.list[0].lastPrice],
  ];
  for (const [u, p] of sources) { try { const v = await tryGet(u, p); if (v) return v; } catch (e) { /* next */ } }
  return null;
}

async function livePriceLoop() {
  const update = async () => {
    if (document.hidden) return;
    const p = await fetchLive();
    if (!p || !LATEST) return;
    $("#live-price").textContent = fmt(p);
    const chg = (p / LATEST.price.close - 1) * 100;
    const el = $("#live-chg");
    el.textContent = `${chg >= 0 ? "▲" : "▼"}${Math.abs(chg).toFixed(2)}%`;
    el.className = "chg " + (chg >= 0 ? "up" : "down");
    const plan = LATEST.signal.plan;
    if (plan) {
      const e1 = plan.entries[0].price;
      const d = (p - e1) / p * 100;
      const hint = plan.direction === "LONG"
        ? (d > 0 ? `現價距第 1 檔掛單 -${d.toFixed(2)}%` : `已低於第 1 檔掛單（可能已成交）`)
        : (d < 0 ? `現價距第 1 檔掛單 +${(-d).toFixed(2)}%` : `已高於第 1 檔掛單（可能已成交）`);
      $("#cd-validity").textContent = "｜" + hint;
    }
  };
  update(); setInterval(update, 30000);
}

function tickCountdown() {
  const plan = LATEST?.signal?.plan; if (!plan) return;
  const el = $("#cd-deadline"); if (!el) return;
  const now = Date.now();
  const fmtDur = ms => { const h = Math.floor(ms / 3600e3), m = Math.floor(ms % 3600e3 / 60e3); return `${h}h ${m}m`; };
  const v = plan.validity_ms - now, d = plan.deadline_ms - now;
  el.textContent = (v > 0 ? `⏳ 掛單有效期剩 ${fmtDur(v)}` : "⌛ 掛單有效期已過（未成交部分應撤單）") +
    (d > 0 ? ` · 持倉時間上限剩 ${fmtDur(d)}` : "");
}

/* ---------------- 頁籤 ---------------- */
document.querySelectorAll("#nav button").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    $("#tab-" + b.dataset.tab).classList.add("active");
    window.scrollTo({ top: 0 });
    if (b.dataset.tab === "review") { renderEquity(); }
    if (b.dataset.tab === "system") { renderTouch(); }
  });
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => { });
boot();

/* BTC 波段訊號儀表板（純前端，資料由 GitHub Actions 產出的 JSON 提供） */
"use strict";
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n, d = 0) => n == null || isNaN(n) ? "—" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtR = r => r == null ? "—" : (r > 0 ? "+" : "") + Number(r).toFixed(2) + "R";
const pct = (x, d = 0) => x == null ? "—" : (x * 100).toFixed(d) + "%";
const DIR = { LONG: "做多", SHORT: "做空", FLAT: "觀望" };
const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const FNAMES = {
  trend_daily: "📈 大趨勢(日線)", trend_4h: "🕐 短趨勢(4H)", momentum: "⚡ 動能",
  funding: "💰 多空擁擠度", oi_price: "🏦 大戶資金(OI)", taker_flow: "🌊 買賣力道",
  rvol: "📊 量能", wick_magnet: "🕯️ 影線磁吸", levels: "🧱 關鍵價位", squeeze_setup: "🔥 軋空醞釀",
};
const FGLOSS = {
  trend_daily: "中長期方向。均線向上排列＝多頭市場，順著做勝率高。",
  trend_4h: "幾天內的小趨勢，與大趨勢同向時進場品質較好。",
  momentum: "漲跌的速度感（RSI/MACD）。衝太快容易回調。",
  funding: "做多的人太多→費率飆高＝擁擠＝危險；反過來就是軋空燃料。",
  oi_price: "市場資金進出。上漲＋資金流入＝健康；上漲但資金撤退＝虛漲。",
  taker_flow: "主動買單和主動賣單誰比較兇。價漲但買盤轉弱＝背離警訊。",
  rvol: "今天成交量比平常大多少。沒有量的突破常是假突破。",
  wick_magnet: "長影線＝插針痕跡，市場常會回頭「補」那個價位，像磁鐵。",
  levels: "整數關卡、前高前低——大家都盯著的價位，容易有反應。",
  squeeze_setup: "一邊人擠爆又被大戶吸收→可能瞬間往反方向噴出。",
};

let LATEST = null, REVIEW = null, OPT = null, MODE = "all", LIVE_PRICE = null;
let LADDER = null, CHARTX = null;
let CHART_DAYS = +(localStorage.getItem("chartDays") || 60);

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
  window.addEventListener("resize", debounce(() => { renderChart(); renderLadder(); renderEquity(); renderRBars(); renderTouch(); }, 200));
  if (!localStorage.getItem("onboarded")) openModal();
}
function debounce(fn, ms) { let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }

/* ---------------- Header / Modal ---------------- */
function renderHeader() {
  const age = Date.now() - LATEST.generated_at;
  $("#data-time").textContent = `更新：${LATEST.generated_taipei}（訊號每日 08:07 · 持倉追蹤每 4 小時）`;
  if (LATEST.stale || age > 30 * 3600e3) $("#stale-badge").hidden = false;
  $("#live-price").textContent = fmt(LATEST.price.close);
}
function openModal() { const m = $("#modal"); m.hidden = false; m.style.display = "flex"; document.body.style.overflow = "hidden"; }
function closeModal() { const m = $("#modal"); m.hidden = true; m.style.display = "none"; document.body.style.overflow = ""; try { localStorage.setItem("onboarded", "1"); } catch (e) { } }
$("#help-btn").addEventListener("click", openModal);
$("#modal-close").addEventListener("click", closeModal);
$("#modal").addEventListener("click", e => { if (e.target === $("#modal")) closeModal(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

/* ---------------- 今日 ---------------- */
function renderToday() {
  const S = LATEST.signal;
  const evs = (S.macro?.events || []).filter(e => e.hours_until >= 0);
  $("#macro-strip").innerHTML = evs.length
    ? `<div class="card" style="padding:10px 14px"><span class="badge badge-amber">⚠ 大事件</span>
       <span style="font-size:13px;margin-left:6px">${evs.map(e => `${esc(e.name)} ${e.date}（${Math.round(e.hours_until)}h 後）`).join("、")}
       <span class="hint">公布前後波動大，系統自動降風險</span></span></div>` : "";
  renderHero(); renderTug(); renderLadder(); renderCalcMaybe(); renderChart(); renderFactors(); renderDiscipline();
}

function renderHero() {
  const S = LATEST.signal;
  const cls = S.direction === "LONG" ? "long" : S.direction === "SHORT" ? "short" : "flat";
  let notes = "";
  if (S.gates?.length) notes += S.gates.map(g => `<li class="gate">${esc(g)}</li>`).join("");
  if (S.direction === "FLAT" && S.watch?.length) notes += S.watch.map(w => `<li>${esc(w)}</li>`).join("");
  if (S.position_note) notes += `<li class="gate">${esc(S.position_note)}</li>`;
  const floor = LATEST.meta.params.confidence_floor;
  $("#hero").className = `card hero ${cls}`;
  $("#hero").innerHTML = `
    <div class="gauge-wrap">${gaugeSVG(S.score, LATEST.meta.params.score_threshold, S.direction)}</div>
    <div class="hero-sub">綜合分數 ${S.score > 0 ? "+" : ""}${Math.round(S.score)}（出手門檻 ±${LATEST.meta.params.score_threshold}）</div>
    <div class="headline">${esc(S.headline || "")}</div>
    <div class="hero-sub">訊號日 ${LATEST.signal_date} · 收盤 ${fmt(LATEST.price.close)}
      <span class="${LATEST.price.chg_1d >= 0 ? "up" : "down"}">${LATEST.price.chg_1d > 0 ? "+" : ""}${LATEST.price.chg_1d}%</span></div>
    <div class="conf-row">
      <span style="font-size:12px;color:var(--muted)">把握度</span>
      <div class="meter"><i style="width:${S.confidence}%"></i><span class="gate-tick" style="left:${floor}%"></span></div>
      <span class="conf-num">${S.confidence}<span style="font-size:11px;color:var(--muted)">/100</span></span>
    </div>
    <div class="hero-sub" style="margin-top:4px">把握度需超過刻度線 ${floor} 才會出手</div>
    ${notes ? `<ul class="hero-notes">${notes}</ul>` : ""}`;
}

function gaugeSVG(score, th, dir) {
  const W = 340, H = 198, cx = 170, cy = 158, R = 112;
  const green = css("--green"), red = css("--red"), muted = css("--muted"), card2 = css("--card2");
  const pt = (s, r) => {
    const a = (180 - (s + 100) * 0.9) * Math.PI / 180;
    return [cx + r * Math.cos(a), cy - r * Math.sin(a)];
  };
  const arc = (s1, s2, col, w) => {
    const [x1, y1] = pt(s1, R), [x2, y2] = pt(s2, R);
    return `<path d="M${x1.toFixed(1)},${y1.toFixed(1)} A${R},${R} 0 0 1 ${x2.toFixed(1)},${y2.toFixed(1)}"
      fill="none" stroke="${col}" stroke-width="${w}" stroke-linecap="butt"/>`;
  };
  let s = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
  s += arc(-100, -th, red, 20) + arc(-th, th, card2, 20) + arc(th, 100, green, 20);
  // 門檻刻度
  [-th, th].forEach(t => {
    const [x1, y1] = pt(t, R + 13), [x2, y2] = pt(t, R - 13);
    s += `<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" stroke="${muted}" stroke-width="1.6"/>`;
  });
  // 區域標籤：休息在弧頂上方（留足空間）、看空/看多在弧的兩端下方
  s += `<text x="${cx}" y="30" font-size="12" fill="${muted}" text-anchor="middle">休息區</text>`;
  s += `<text x="${cx - R}" y="${cy + 26}" font-size="13" fill="${red}" text-anchor="middle" font-weight="700">看空</text>`;
  s += `<text x="${cx + R}" y="${cy + 26}" font-size="13" fill="${green}" text-anchor="middle" font-weight="700">看多</text>`;
  // 大字放在弧內上方，指針縮短、不與文字相交
  const ncol = dir === "LONG" ? green : dir === "SHORT" ? red : muted;
  s += `<text x="${cx}" y="86" font-size="30" font-weight="900" fill="${ncol}" text-anchor="middle">${DIR[dir]}</text>`;
  const sc = Math.max(-100, Math.min(100, score));
  const [nx, ny] = pt(sc, R - 54);
  s += `<line x1="${cx}" y1="${cy}" x2="${nx}" y2="${ny}" stroke="${ncol}" stroke-width="4" stroke-linecap="round"/>`;
  s += `<circle cx="${cx}" cy="${cy}" r="7" fill="${ncol}"/>`;
  return s + "</svg>";
}

/* ---------------- 多空拔河 ---------------- */
function renderTug() {
  const fs = (LATEST.signal.factors || []).filter(f => f.ok);
  let pos = 0, neg = 0, nPos = 0, nNeg = 0;
  fs.forEach(f => {
    const v = f.score * (f.weight || 1);
    if (f.score >= 8) { pos += v; nPos++; }
    else if (f.score <= -8) { neg += -v; nNeg++; }
  });
  const total = pos + neg || 1;
  const pw = pos / total * 50, nw = neg / total * 50;
  $("#tug").innerHTML = `
    <div class="tug-bar">
      <div class="neg" style="width:${nw}%"></div>
      <div class="pos" style="width:${pw}%"></div>
      <div class="mid"></div>
    </div>
    <div class="tug-labels">
      <span class="down">◀ 偏空 ${Math.round(neg)}<span class="n">${nNeg} 項因子</span></span>
      <span class="up">偏多 ${Math.round(pos)} ▶<span class="n">${nPos} 項因子</span></span>
    </div>`;
}

/* ---------------- 進出場地圖（價格梯） ---------------- */
function renderLadder() {
  const box = $("#ladder"); if (!box || !LATEST) return;
  const plan = LATEST.signal.plan;
  const W = box.clientWidth || 340;
  const green = css("--green"), red = css("--red"), muted = css("--muted"),
    accent = css("--accent"), border = css("--border"), text = css("--text"), card = css("--card");
  const close = LIVE_PRICE || LATEST.price.close;
  let rows = [], zones = [], H, title, note;

  if (plan) {
    H = 420;
    const sgn = plan.direction === "LONG" ? 1 : -1;
    const eWord = plan.direction === "LONG" ? "買點" : "空點";
    title = `進出場地圖 <span class="hint">${plan.direction === "LONG" ? "做多" : "做空"}計畫 · 照著掛單即可</span>`;
    plan.tps.slice().reverse().forEach((t, idx) => {
      const i = plan.tps.length - idx;
      rows.push({ p: t.price, col: green, chip: `🎯 目標${i}（+${t.r}R）`, sub: i === 1 ? "平30%＋停損移到成本" : "平30%＋啟動移動停損" });
    });
    plan.entries.forEach((e, i) => {
      rows.push({ p: e.price, col: accent, chip: `🟢 ${eWord}${i + 1}（${Math.round(e.w * 100)}%資金）`, sub: e.prob != null ? `歷史成交率 ${Math.round(e.prob * 100)}%` : "" });
    });
    rows.push({ p: plan.stop, col: red, chip: "🛑 停損（最大虧損處）", sub: `-${plan.stop_pct}% · 碰到就全部出場` });
    const eLo = Math.min(...plan.entries.map(e => e.price)), eHi = Math.max(...plan.entries.map(e => e.price));
    const tpLo = Math.min(...plan.tps.map(t => t.price)), tpHi = Math.max(...plan.tps.map(t => t.price));
    zones = [
      { a: tpLo, b: tpHi, col: green, op: .10, label: "獲利區" },
      { a: eLo, b: eHi, col: accent, op: .12, label: "進場區" },
      sgn === 1 ? { a: -Infinity, b: plan.stop, col: red, op: .10, label: "危險區" }
        : { a: plan.stop, b: Infinity, col: red, op: .10, label: "危險區" },
    ];
    note = `掛單有效 48 小時；成交後最多持有 7 天。<span id="cd-validity"></span><span id="cd-deadline" style="display:block"></span>`;
  } else {
    H = 330;
    title = `關鍵價位地圖 <span class="hint">今天觀望 · 這是現價附近的支撐與壓力</span>`;
    const lv = LATEST.levels || [];
    const res = lv.filter(l => l.price > close).sort((a, b) => a.price - b.price).slice(0, 2);
    const sup = lv.filter(l => l.price < close).sort((a, b) => b.price - a.price).slice(0, 2);
    res.forEach(l => rows.push({ p: l.price, col: red, chip: `🧱 壓力（${l.strength} 條線重疊）`, sub: l.srcs.slice(0, 2).join("+") }));
    sup.forEach(l => rows.push({ p: l.price, col: green, chip: `🧱 支撐（${l.strength} 條線重疊）`, sub: l.srcs.slice(0, 2).join("+") }));
    (LATEST.wicks || []).slice(-2).forEach(w => {
      if (Math.abs(w.mid - close) / close < 0.12)
        rows.push({ p: w.mid, col: accent, chip: "🧲 影線磁吸點", sub: "插針未回補，價格易被吸過去" });
    });
    note = "突破壓力看多、跌破支撐看空——系統確認共振後才會給進出場計畫。";
  }
  $("#ladder-title").innerHTML = title;
  $("#ladder-note").innerHTML = note;

  const prices = rows.map(r => r.p).concat([close]);
  let lo = Math.min(...prices), hi = Math.max(...prices);
  const pad = (hi - lo) * 0.08 || close * 0.01;
  lo -= pad; hi += pad;
  const Y = p => 14 + (hi - p) / (hi - lo) * (H - 28);
  LADDER = { lo, hi, Y, W, H };

  const GUT = 88;                      // 右側價格專用欄（虛線不進入，文字永不被劃過）
  const lineEnd = W - GUT;
  let s = `<svg class="ladder-svg" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
  zones.forEach(z => {
    const a = Math.max(lo, z.a === -Infinity ? lo : z.a), b = Math.min(hi, z.b === Infinity ? hi : z.b);
    if (b <= a) return;
    s += `<rect x="0" y="${Y(b)}" width="${lineEnd}" height="${Y(a) - Y(b)}" fill="${z.col}" opacity="${z.op}" rx="8"/>`;
    s += `<text x="${lineEnd - 8}" y="${Y(b) + 14}" font-size="10" fill="${z.col}" text-anchor="end" opacity=".9">${z.label}</text>`;
  });
  // 價位列（label 防重疊：由上而下最小間距 36px；文字與虛線保持垂直距離）
  rows.sort((a, b) => b.p - a.p);
  let lastY = -99;
  rows.forEach(r => {
    const yTrue = Y(r.p);
    const y = Math.max(yTrue, lastY + 36);
    lastY = y;
    s += `<line x1="4" x2="${lineEnd}" y1="${yTrue}" y2="${yTrue}" stroke="${r.col}" stroke-width="1.3" stroke-dasharray="6 4" opacity=".9"/>`;
    if (Math.abs(y - yTrue) > 4) s += `<line x1="150" x2="150" y1="${yTrue}" y2="${y - 10}" stroke="${r.col}" stroke-width="1" opacity=".4"/>`;
    s += `<text x="8" y="${y - 9}" font-size="12" font-weight="700" fill="${r.col}">${r.chip}</text>`;
    if (r.sub) s += `<text x="8" y="${y + 15}" font-size="10.5" fill="${muted}">${r.sub}</text>`;
    s += `<text x="${W - 8}" y="${y - 1}" font-size="13" font-weight="800" fill="${text}" text-anchor="end">${fmt(r.p)}</text>`;
    s += `<text x="${W - 8}" y="${y + 13}" font-size="10" fill="${muted}" text-anchor="end" class="lv-dist" data-p="${r.p}">${distTxt(r.p, close)}</text>`;
  });
  // 現價：左右兩顆實心膠囊（畫在最上層，遇到相近價位也讀得清楚）
  const cy2 = Y(close);
  s += `<g id="ladder-now">
    <line x1="4" x2="${W - 4}" y1="${cy2}" y2="${cy2}" stroke="${text}" stroke-width="1.6"/>
    <rect x="4" y="${cy2 - 10}" rx="6" width="74" height="20" fill="${text}"/>
    <text x="41" y="${cy2 + 4}" font-size="11" font-weight="800" fill="${card}" text-anchor="middle">▶ 現價</text>
    <rect x="${W - GUT + 4}" y="${cy2 - 10}" rx="6" width="${GUT - 8}" height="20" fill="${text}"/>
    <text x="${W - GUT / 2}" y="${cy2 + 4}" font-size="11.5" font-weight="800" fill="${card}" text-anchor="middle" id="ladder-now-p">${fmt(close)}</text>
  </g>`;
  box.innerHTML = s + "</svg>";
}
const distTxt = (p, now) => {
  const d = (p / now - 1) * 100;
  return (d >= 0 ? "↑" : "↓") + Math.abs(d).toFixed(1) + "%";
};
function updateLadderLive(p) {
  if (!LADDER) return;
  const g = $("#ladder-now"); if (!g) return;
  const { lo, hi, Y } = LADDER;
  if (p < lo || p > hi) { renderLadder(); return; }
  const y = Y(p);
  const [line, lrect, ltxt, rrect] = [g.children[0], g.children[1], g.children[2], g.children[3]];
  line.setAttribute("y1", y); line.setAttribute("y2", y);
  lrect.setAttribute("y", y - 10); ltxt.setAttribute("y", y + 4);
  if (rrect) rrect.setAttribute("y", y - 10);
  const pl = $("#ladder-now-p"); if (pl) { pl.setAttribute("y", y + 4); pl.textContent = fmt(p); }
  document.querySelectorAll(".lv-dist").forEach(el => { el.textContent = distTxt(+el.dataset.p, p); });
}

/* ---------------- 倉位計算機 ---------------- */
function renderCalcMaybe() {
  const plan = LATEST.signal.plan;
  if (!plan) { $("#calc-area").innerHTML = ""; return; }
  const savedEq = localStorage.getItem("eq") || 10000;
  $("#calc-area").innerHTML = `
  <div class="card">
    <div class="card-title">該下多少？<span class="hint">先決定敢虧多少，其他系統幫你算</span></div>
    <div class="calc-grid three">
      <div><label>帳戶本金 USDT</label><input id="calc-eq" type="number" inputmode="decimal" value="${savedEq}"></div>
      <div><label>這筆敢虧 %（建議 ${plan.risk_pct}）</label><input id="calc-risk" type="number" inputmode="decimal" step="0.1" value="${plan.risk_pct}"></div>
      <div><label>槓桿倍數（建議 ${plan.leverage}）</label><input id="calc-lev" type="number" inputmode="decimal" step="0.5" min="1" max="50" value="${plan.leverage}"></div>
    </div>
    <div class="calc-out" id="calc-out"></div>
    <div id="calc-liq-warn" class="hint-block"></div>
    <div class="countdown" id="calc-note"></div>
    <div class="card-title" style="margin:14px 0 6px">這筆的可能結局 <span class="hint">照計畫執行時，各種劇本大約賺賠多少</span></div>
    <div id="calc-payoff"></div>
    <div id="calc-funding" class="hint-block"></div>
  </div>`;
  const recompute = () => {
    const eq = +$("#calc-eq").value || 0, risk = +$("#calc-risk").value || 0;
    const lev = Math.min(50, Math.max(1, +$("#calc-lev").value || plan.leverage));
    localStorage.setItem("eq", eq);
    const sgn = plan.direction === "LONG" ? 1 : -1;
    const riskUsd = eq * risk / 100;
    const notional = riskUsd / (plan.stop_pct / 100);
    const margin = notional / lev;
    const qty = notional / plan.avg_entry;
    // 逐倉強平價估算（維持保證金 ~0.6%）與停損距離比
    const liq = plan.avg_entry * (1 - sgn * (1 / lev - 0.006));
    const ratio = Math.abs(plan.avg_entry - liq) / Math.abs(plan.avg_entry - plan.stop);
    let liqCls = "up", liqTxt = "比停損遠 ✓", warn = "✅ 安全：就算插針到停損，也還碰不到強平價。";
    if (ratio < 1.0) {
      liqCls = "down"; liqTxt = "比停損還近 ⛔";
      warn = "⛔ 危險：這個槓桿下「強平價」比你的停損還近——行情還沒走到停損就先被強平，" +
        "計畫中的小虧會變成保證金全部歸零。請把槓桿降到安全區。";
    } else if (ratio < 1.3) {
      liqCls = ""; liqTxt = "貼近停損 ⚠";
      warn = "⚠ 偏險：強平價離停損太近，插針或滑價可能先掃到強平。建議再降一點槓桿。";
    }
    $("#calc-out").innerHTML = `
      <div class="cell"><b class="down">${fmt(riskUsd, 0)}</b><span>最多虧 USDT<br>(碰停損時)</span></div>
      <div class="cell"><b>${fmt(notional, 0)}</b><span>倉位總值 USDT</span></div>
      <div class="cell"><b>${qty.toFixed(4)}</b><span>總數量 BTC</span></div>
      <div class="cell"><b>${lev}x</b><span>你選的槓桿</span></div>
      <div class="cell"><b>${fmt(margin, 0)}</b><span>需要保證金</span></div>
      <div class="cell"><b class="${liqCls}">${fmt(liq)}</b><span>估計強平價<br>(${liqTxt})</span></div>`;
    $("#calc-liq-warn").textContent = warn +
      "（提醒：改槓桿不會改變倉位大小與賺賠，只改變鎖住的保證金和強平價）";
    $("#calc-note").textContent =
      `逐檔下單量：${plan.entries.map((e, i) => `第${i + 1}檔 ${(qty * e.w).toFixed(4)} BTC`).join("、")}（逐倉模式估算）`;

    // 可能結局試算（以全數成交計；部分成交時金額等比縮小，結構不變）
    const r1 = plan.tps[0]?.r ?? 0.7, r2 = plan.tps[1]?.r ?? (r1 + 1);
    const runnerR = 0.3 * r1 + 0.3 * r2 + 0.4 * 3;
    const money = r => {
      const v = r * riskUsd;
      return `<b class="${v >= 0.5 ? "up" : v <= -0.5 ? "down" : ""}">${v >= 0 ? "+" : "−"}${fmt(Math.abs(v), 0)}</b>`;
    };
    $("#calc-payoff").innerHTML = [
      ["🛑 看錯：碰到停損", "全部出場，虧損固定在計畫內", -1, money(-1)],
      ["😐 沒行情：停滯/到期出場", "進場 5 天沒進展就先離場，賺賠約打平", 0, `<b>≈ 0</b>`],
      [`🎯 到目標1（+${r1}R）後回落`, "先落袋 30%、剩餘保本出場，小賺", 0.3 * r1, money(0.3 * r1)],
      [`🎯🎯 到目標2（+${r2}R）後回落`, "落袋 60%、剩餘保本出場", 0.3 * r1 + 0.3 * r2, money(0.3 * r1 + 0.3 * r2)],
      [`🚀 趨勢展開（尾倉假設跑到 +3R）`, "移動停損讓 40% 尾倉奔跑（示意，上不封頂）", runnerR, money(runnerR)],
    ].map(([t, sub, _r, m]) => `
      <div class="kv"><span>${t}<br><small style="font-size:11px">${sub}</small></span>
      <span style="text-align:right;flex:none">${m}<br><small style="font-size:11px;color:var(--muted)">${_r > 0 ? "+" : _r < 0 ? "−" : "±"}${Math.abs(_r).toFixed(2)}R</small></span></div>`).join("");

    // 資金費率持倉成本（8h 費率 ×3 ≈ 每日；多單付正費率、空單收正費率）
    const f8 = LATEST.price.funding;
    if (f8 != null) {
      const daily = f8 * 3 * notional * (plan.direction === "LONG" ? 1 : -1);
      const word = daily > 0 ? `約付 ${fmt(Math.abs(daily), 1)}` : `約收 ${fmt(Math.abs(daily), 1)}`;
      $("#calc-funding").textContent =
        `💰 資金費率：目前 8 小時 ${(f8 * 100).toFixed(4)}%，此倉位每日${word} USDT（費率隨時變動，僅供概估；復盤統計已計入）`;
    } else $("#calc-funding").textContent = "";
  };
  ["calc-eq", "calc-risk", "calc-lev"].forEach(id => $("#" + id).addEventListener("input", recompute));
  recompute();
}

/* ---------------- K 線圖 ---------------- */
function renderChart() {
  const box = $("#chart"); if (!box || !LATEST) return;
  const W = box.clientWidth || 340, H = box.clientHeight || 340;
  const C = LATEST.candles.slice(-CHART_DAYS), plan = LATEST.signal.plan;
  const padR = 54, padT = 10, padB = 22, padL = 6;
  const n = C.length;
  let lo = Math.min(...C.map(c => c[3])), hi = Math.max(...C.map(c => c[2]));
  if (plan) { lo = Math.min(lo, plan.stop); hi = Math.max(hi, ...plan.tps.map(t => t.price)); }
  const span = hi - lo; lo -= span * .04; hi += span * .04;
  const X = i => padL + (i + .5) * (W - padL - padR) / n;
  const Y = p => padT + (hi - p) / (hi - lo) * (H - padT - padB);
  CHARTX = { X, Y, n, W, H, padL, padR, lo, hi, candles: C };
  const green = css("--green"), red = css("--red"), muted = css("--muted"), border = css("--border"), accent = css("--accent"), text = css("--text");
  let s = `<svg viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg">`;
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const p = lo + (hi - lo) * i / ticks, y = Y(p);
    s += `<line x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="${border}" stroke-width="1"/>`;
    s += `<text x="${W - padR + 4}" y="${y + 3}" font-size="10" fill="${muted}">${Math.round(p / 1000)}k</text>`;
  }
  for (let i = 0; i < n; i += Math.ceil(n / (n <= 40 ? 4 : 5))) {
    const d = new Date(C[i][0]);
    s += `<text x="${X(i)}" y="${H - 6}" font-size="9.5" fill="${muted}" text-anchor="middle">${d.getUTCMonth() + 1}/${d.getUTCDate()}</text>`;
  }
  // 關鍵價位線：只畫最強 4 條，且彼此至少隔 0.6 ATR，避免雜訊
  const atrNow = LATEST.price.atr || (hi - lo) / 10;
  const lvls = (LATEST.levels || []).filter(l => l.strength >= 3 && l.price > lo && l.price < hi)
    .sort((a, b) => b.strength - a.strength);
  const drawn = [];
  lvls.forEach(l => {
    if (drawn.length >= 4 || drawn.some(p => Math.abs(p - l.price) < 0.6 * atrNow)) return;
    drawn.push(l.price);
    s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(l.price)}" y2="${Y(l.price)}" stroke="${muted}" stroke-width="0.8" stroke-dasharray="2 4" opacity="0.7"/>`;
  });
  if (plan) {
    const eTop = Math.max(...plan.entries.map(e => e.price)), eBot = Math.min(...plan.entries.map(e => e.price));
    const zc = plan.direction === "LONG" ? green : red;
    s += `<rect x="${padL}" y="${Y(Math.max(eTop, eBot))}" width="${W - padL - padR}" height="${Math.abs(Y(eBot) - Y(eTop)) || 2}" fill="${zc}" opacity="0.12"/>`;
    plan.entries.forEach((e, i) => {
      s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(e.price)}" y2="${Y(e.price)}" stroke="${zc}" stroke-width="1" stroke-dasharray="5 3" opacity="0.85"/>
            <text x="${W - padR + 4}" y="${Y(e.price) + 3}" font-size="9" fill="${zc}">E${i + 1}</text>`;
    });
    s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(plan.stop)}" y2="${Y(plan.stop)}" stroke="${red}" stroke-width="1.4"/>
          <text x="${W - padR + 4}" y="${Y(plan.stop) + 3}" font-size="9" fill="${red}">SL</text>`;
    plan.tps.forEach((t, i) => {
      s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(t.price)}" y2="${Y(t.price)}" stroke="${green}" stroke-width="1" stroke-dasharray="7 3"/>
            <text x="${W - padR + 4}" y="${Y(t.price) + 3}" font-size="9" fill="${green}">TP${i + 1}</text>`;
    });
  }
  (LATEST.wicks || []).forEach(w => {
    if (w.mid > lo && w.mid < hi) s += `<circle cx="${W - padR - 6}" cy="${Y(w.mid)}" r="3" fill="${accent}" opacity="0.9"/>`;
  });
  // EMA20/50 疊圖（用全部 120 根算，只畫顯示範圍，避免視窗切換造成暖機偏移）
  const allC = LATEST.candles, off = allC.length - n;
  const emaPath = (span, col, dash) => {
    const k = 2 / (span + 1);
    let e = allC[0][4], d = "";
    allC.forEach((c2, j) => {
      e = j ? c2[4] * k + e * (1 - k) : e;
      const i = j - off;
      if (i >= 0 && e > lo && e < hi) d += (d ? " L" : "M") + `${X(i).toFixed(1)},${Y(e).toFixed(1)}`;
    });
    return d ? `<path d="${d}" fill="none" stroke="${col}" stroke-width="1.3" ${dash ? 'stroke-dasharray="1 3"' : ""} opacity=".85"/>` : "";
  };
  s += emaPath(20, accent, false) + emaPath(50, css("--amber"), true);
  const bw = Math.max(1.6, (W - padL - padR) / n * 0.62);
  C.forEach((c, i) => {
    const [, o, h, l, cl] = c, up = cl >= o, col = up ? green : red, x = X(i);
    s += `<line x1="${x}" x2="${x}" y1="${Y(h)}" y2="${Y(l)}" stroke="${col}" stroke-width="1"/>`;
    s += `<rect x="${x - bw / 2}" y="${Y(Math.max(o, cl))}" width="${bw}" height="${Math.max(1, Math.abs(Y(o) - Y(cl)))}" fill="${col}" rx="0.5"/>`;
  });
  // 十字游標 + 現價線（由 JS 更新）
  s += `<line id="xhair" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="${text}" stroke-width="0.8" opacity="0" stroke-dasharray="3 3"/>`;
  s += `<g id="nowline"></g>`;
  s += `</svg>`;
  box.innerHTML = s;
  $("#chart-legend").innerHTML = `
    <span><i style="background:${green}"></i>上漲日</span>
    <span><i style="background:${red}"></i>下跌日</span>
    <span><i style="background:${accent}"></i>EMA20</span>
    <span><i style="background:${css("--amber")}"></i>EMA50</span>
    <span><i style="background:${muted}"></i>關鍵價位</span>
    <span><i style="background:${accent};height:8px;width:8px;border-radius:99px"></i>影線磁吸</span>`;
  attachCrosshair(box);
  updateChartNow(LIVE_PRICE || LATEST.price.close);
}

function attachCrosshair(box) {
  const tip = $("#chart-tip");
  const move = ev => {
    if (!CHARTX) return;
    const rect = box.getBoundingClientRect();
    const px = (ev.touches ? ev.touches[0].clientX : ev.clientX) - rect.left;
    const { X, n, padL, W, padR } = CHARTX;
    const plotW = W - padL - padR;
    const i = Math.max(0, Math.min(n - 1, Math.floor((px / rect.width * W - padL) / plotW * n)));
    const c = CHARTX.candles[i]; if (!c) return;
    const d = new Date(c[0]);
    const chg = (c[4] / c[1] - 1) * 100;
    tip.hidden = false;
    tip.innerHTML = `<b>${d.getUTCFullYear()}/${d.getUTCMonth() + 1}/${d.getUTCDate()}</b>　<span class="${chg >= 0 ? "up" : "down"}">${chg >= 0 ? "+" : ""}${chg.toFixed(1)}%</span><br>
      高 ${fmt(c[2])} · 低 ${fmt(c[3])}<br>開 ${fmt(c[1])} · 收 ${fmt(c[4])}`;
    const tw = tip.offsetWidth;
    tip.style.left = Math.min(Math.max(4, px - tw / 2), rect.width - tw - 4) + "px";
    tip.style.top = "8px";
    const xl = box.querySelector("#xhair");
    if (xl) { const sx = X(i); xl.setAttribute("x1", sx); xl.setAttribute("x2", sx); xl.setAttribute("opacity", "0.7"); }
  };
  const leave = () => { tip.hidden = true; const xl = box.querySelector("#xhair"); if (xl) xl.setAttribute("opacity", "0"); };
  box.onpointermove = move; box.onpointerdown = move; box.onpointerleave = leave;
}

function updateChartNow(p) {
  if (!CHARTX) return;
  const g = document.querySelector("#nowline"); if (!g) return;
  const { Y, W, padL, padR, lo, hi } = CHARTX;
  if (!p || p < lo || p > hi) { g.innerHTML = ""; return; }
  const text = css("--text"), card = css("--card");
  const y = Y(p);
  g.innerHTML = `<line x1="${padL}" x2="${W - padR}" y1="${y}" y2="${y}" stroke="${text}" stroke-width="1" stroke-dasharray="2 3" opacity=".85"/>
    <rect x="${W - padR + 1}" y="${y - 8}" width="${padR - 3}" height="16" rx="4" fill="${text}"/>
    <text x="${W - padR + 26}" y="${y + 3.5}" font-size="9" font-weight="800" fill="${card}" text-anchor="middle">${Math.round(p / 1000)}k 現價</text>`;
}

/* ---------------- 因子 ---------------- */
function factorRow(f, withGloss) {
  const w = Math.min(Math.abs(f.score), 100) / 2;
  const side = f.score >= 0 ? `left:50%;width:${w}%` : `right:50%;width:${w}%`;
  const col = f.score >= 0 ? css("--green") : css("--red");
  const dirTxt = !f.ok ? "無資料" : Math.abs(f.score) < 8 ? "中立" : (f.score > 0 ? "幫多方" : "幫空方");
  return `<div class="factor ${f.ok ? "" : "off"}">
    <div class="factor-top">
      <div class="factor-name">${FNAMES[f.name] || esc(f.label)}</div>
      <div class="factor-bar"><i style="${side};background:${col}"></i></div>
      <div class="factor-score" style="color:${Math.abs(f.score) < 8 ? "var(--muted)" : f.score >= 0 ? "var(--green)" : "var(--red)"}">${dirTxt}</div>
    </div>
    <div class="factor-note">${esc(f.note)}</div>
    ${withGloss ? `<div class="factor-gloss">ⓘ ${FGLOSS[f.name] || ""}</div>` : ""}
  </div>`;
}
function renderFactors() {
  const fs = (LATEST.signal.factors || []).slice();
  const top = fs.filter(f => f.ok).sort((a, b) => Math.abs(b.score * b.weight) - Math.abs(a.score * a.weight)).slice(0, 3);
  $("#factors-top").innerHTML = top.map(f => factorRow(f, false)).join("");
  $("#factors-all").innerHTML = fs.map(f => factorRow(f, true)).join("");
}

/* ---------------- 紀律卡 ---------------- */
function renderDiscipline() {
  const p = LATEST.meta.params, m = LATEST.stats_mini;
  $("#discipline").innerHTML = `
    <div class="card-title">保命規則（系統強制執行）</div>
    <div class="pill-row">
      <span class="pill">🛡 一筆最多虧 ${Math.min(p.risk_pct_base * 1.3, 2)}%</span>
      <span class="pill">⚖️ 槓桿最高 ${p.max_leverage} 倍</span>
      <span class="pill">🧊 連虧 2 筆休息 1 天</span>
      <span class="pill">📅 大事件前不進場</span>
      <span class="pill">⏰ 最多抱 ${p.max_hold_days} 天</span>
      <span class="pill">⏳ ${p.stagnation_days} 天沒進展先離場</span>
      <span class="pill">🔒 到目標1後這筆穩不虧</span>
      <span class="pill">📉 連續回撤自動降風險</span>
    </div>
    <div style="font-size:12px;color:var(--muted);margin-top:10px">
      至今 ${m.n_closed} 筆結案 · 勝率 ${pct(m.win_rate)} · 平均每筆 ${fmtR(m.expectancy_r)} · 掛單成交率 ${pct(m.fill_rate)}
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
  const wrSub = S.win_rate_ex_scratch != null
    ? `去±0.1R平手後 ${pct(S.win_rate_ex_scratch)}` : "贏的比例";
  const sampleWarn = (S.n_closed || 0) < 30
    ? `<div style="grid-column:1/-1;font-size:11px;color:var(--amber);text-align:center">⚠ 此範圍僅 ${S.n_closed || 0} 筆結案樣本，統計噪音大，數字僅供參考</div>` : "";
  $("#kpis").innerHTML = [
    ["勝率", pct(S.win_rate), wrSub, S.win_rate >= .5 ? "up" : ""],
    ["期望值", fmtR(S.expectancy_r), "每筆平均賺賠", (S.expectancy_r || 0) > 0 ? "up" : "down"],
    ["盈虧比", S.profit_factor ?? "—", "總賺 ÷ 總虧", (S.profit_factor || 0) >= 1.5 ? "up" : ""],
    ["成交率", pct(S.fill_rate), "掛單有進場的比例", ""],
    ["累積", fmtR(S.total_r), "總成績", (S.total_r || 0) > 0 ? "up" : "down"],
    ["最大回撤", fmtR(S.max_dd_r), "最慘的連虧", "down"],
  ].map(([k, v, sub, c]) => `<div class="kpi"><b class="${c}">${v}</b><span><b style="font-size:11px;display:inline">${k}</b><br>${sub}</span></div>`).join("") +
    sampleWarn +
    `<div style="grid-column:1/-1;font-size:11px;color:var(--muted);text-align:center">R = 一筆願意虧的錢。賺 +2R = 賺到風險額的 2 倍</div>`;
  renderRBars(); renderEquity();

  const cal = S.calibration || [];
  $("#calibration").innerHTML = cal.length ? cal.map(c => `
    <div class="calib-row">
      <span class="nm">把握度 ${c.bucket}</span>
      <div class="bar"><i style="width:${c.win * 100}%"></i></div>
      <span class="val">勝率 ${pct(c.win)} · ${c.n} 筆</span>
    </div>`).join("") +
    `<div class="hint-block">把握度高的一排應該更長；若倒過來，系統會自動提高出手門檻。</div>` :
    `<div class="skeleton" style="padding:12px 0">樣本不足</div>`;

  const trades = (REVIEW.trades || []).filter(t => MODE === "all" || t.mode === MODE);
  const openIds = new Set((REVIEW.open_trades || []).map(t => t.id));
  const sorted = [...trades].sort((a, b) => (openIds.has(b.id) - openIds.has(a.id)) || b.date.localeCompare(a.date));
  $("#trades").innerHTML = sorted.length ? sorted.map(t => tradeCard(t, openIds.has(t.id))).join("") :
    `<div class="card skeleton">此範圍尚無交易</div>`;
}

function renderRBars() {
  const box = $("#rbars-chart"); if (!box || !REVIEW) return;
  const closed = (REVIEW.trades || []).filter(t => t.status === "closed" && t.r != null && (MODE === "all" || t.mode === MODE))
    .sort((a, b) => a.date.localeCompare(b.date)).slice(-40);
  if (closed.length < 2) { box.innerHTML = `<div class="skeleton">樣本不足</div>`; return; }
  const W = box.clientWidth || 340, H = box.clientHeight || 190;
  const padT = 14, padB = 16, padL = 30, padR = 6;
  const maxAbs = Math.max(0.5, ...closed.map(t => Math.abs(t.r)));
  const Y = v => padT + (maxAbs - v) / (2 * maxAbs) * (H - padT - padB);
  const bw = Math.min(18, (W - padL - padR) / closed.length * 0.7);
  const step = (W - padL - padR) / closed.length;
  const green = css("--green"), red = css("--red"), muted = css("--muted"), border = css("--border");
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(0)}" y2="${Y(0)}" stroke="${muted}" stroke-width="1"/>`;
  [maxAbs, -maxAbs].forEach(v => {
    s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(v)}" y2="${Y(v)}" stroke="${border}"/>
          <text x="2" y="${Y(v) + 4}" font-size="9.5" fill="${muted}">${v > 0 ? "+" : ""}${v.toFixed(1)}R</text>`;
  });
  let best = null, worst = null;
  closed.forEach((t, i) => {
    if (!best || t.r > best.r) best = { ...t, i };
    if (!worst || t.r < worst.r) worst = { ...t, i };
  });
  closed.forEach((t, i) => {
    const x = padL + i * step + (step - bw) / 2;
    const y0 = Y(Math.max(0, t.r)), y1 = Y(Math.min(0, t.r));
    s += `<rect x="${x}" y="${y0}" width="${bw}" height="${Math.max(2, y1 - y0)}" rx="3"
      fill="${t.r >= 0 ? green : red}"><title>${t.id}：${fmtR(t.r)}</title></rect>`;
  });
  [best, worst].forEach(t => {
    if (!t || Math.abs(t.r) < 0.01) return;
    const x = padL + t.i * step + step / 2;
    s += `<text x="${x}" y="${Y(t.r) + (t.r >= 0 ? -4 : 12)}" font-size="9.5" font-weight="700"
      fill="${t.r >= 0 ? green : red}" text-anchor="middle">${fmtR(t.r)}</text>`;
  });
  box.innerHTML = s + "</svg>";
}

function tradeCard(t, isOpen) {
  const dirCls = t.direction === "LONG" ? "long" : "short";
  const rCol = t.r == null ? "var(--muted)" : t.r > 0 ? "var(--green)" : "var(--red)";
  const status = isOpen ? (t.status === "pending" ? "⏳ 掛單中" : "🟢 持倉中")
    : t.status === "cancelled" ? "沒等到價（未進場）" : "已結案";
  const modeChip = t.mode === "backtest" ? `<span class="badge badge-slate">回測</span>` : `<span class="badge badge-green">實盤</span>`;
  const exits = (t.exits || []).map(e => {
    const rn = {
      tp1: "🎯目標1", tp2: "🎯目標2", stop: "🛑停損", be_stop: "保本出場",
      trail_stop: "移動停損", time: "到期平倉", reverse_signal: "反向訊號離場",
      stagnation: "⏳停滯出場(無進展)", protect_stop: "🔒鎖利停損",
    }[e.reason] || e.reason;
    return `${rn} @ ${fmt(e.price)}（${Math.round(e.frac * 100)}%）`;
  }).join("；");
  return `<details class="trade" ${isOpen ? "open" : ""}>
    <summary>
      <span class="t-dir ${dirCls}">${DIR[t.direction]}</span>
      <span class="t-date">${t.date} ${modeChip} <span class="hint">${status}</span></span>
      <span class="t-r" style="color:${rCol}">${t.status === "cancelled" ? "—" : fmtR(t.r)}</span>
    </summary>
    <div class="t-body">
      <div class="kv"><span>把握度 / 分數</span><b>${t.confidence} / ${t.score > 0 ? "+" : ""}${t.score}</b></div>
      <div class="kv"><span>掛單價</span><b style="font-weight:500;font-size:12.5px">${t.plan.entries.map(e => fmt(e.price)).join(" / ")}</b></div>
      <div class="kv"><span>實際進場均價（用了 ${Math.round((t.filled_w || 0) * 100)}% 的計畫）</span><b>${fmt(t.avg_fill)}</b></div>
      <div class="kv"><span>停損${isOpen ? "（目前）" : ""}</span><b>${fmt(isOpen ? t.stop_now : t.plan.stop)}</b></div>
      ${exits ? `<div class="kv"><span>出場</span><b style="font-weight:500;font-size:12.5px">${exits}</b></div>` : ""}
      <div class="kv"><span>過程中最大浮盈 / 最痛回檔</span><b><span class="up">${fmtR(t.mfe_r)}</span> / <span class="down">${fmtR(t.mae_r)}</span></b></div>
      ${t.funding_r && Math.abs(t.funding_r) >= 0.005 ? `<div class="kv"><span>持倉期間資金費率</span><b class="${t.funding_r > 0 ? "up" : "down"}">${fmtR(t.funding_r)}</b></div>` : ""}
      <div class="kv"><span>風險 / 槓桿</span><b>${t.plan.risk_pct}% / ${t.plan.leverage}x</b></div>
      ${(t.lessons || []).map(l => `<span class="lesson">📝 ${esc(l)}</span>`).join("")}
    </div>
  </details>`;
}

function renderEquity() {
  const box = $("#equity-chart"); if (!box || !REVIEW) return;
  const eq = (REVIEW.equity || []).filter(p => MODE === "all" || p.mode === MODE);
  if (eq.length < 2) { box.innerHTML = `<div class="skeleton">樣本不足</div>`; return; }
  const W = box.clientWidth || 340, H = box.clientHeight || 190;
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
      <div class="d">${l.date}</div>
      <div class="chg-line"><b>${esc(l.param)}</b>：${esc(String(l.old))} → <b style="color:var(--accent)">${esc(String(l.new))}</b></div>
      <div class="why">${esc(l.reason)}</div>
    </div>`).join("") :
    `<div class="skeleton" style="padding:12px 0">尚無調參紀錄——累積足夠結案樣本後，每週自動檢討</div>`;

  const W = OPT.weights || {}, E = OPT.factor_edges || {};
  $("#weights").innerHTML = Object.entries(W).map(([k, v]) => {
    const e = E[k] || {};
    return `<div class="wrow"><span class="nm">${FNAMES[k] || k}</span>
      <div class="wbar"><i style="width:${v / 1.6 * 100}%"></i></div>
      <span class="val">${v.toFixed(2)}｜${e.edge != null ? "命中 " + pct(e.edge) : "樣本不足"}</span></div>`;
  }).join("");

  renderTouch();

  const P = OPT.params || {};
  const pLabels = [
    ["entry_offsets_atr", "掛單深度（ATR 倍數）", v => v.map(x => x.toFixed(2)).join(" / ")],
    ["entry_depth_mult", "深度倍率（優化器調整）", v => "×" + v],
    ["stop_buffer_atr", "停損緩衝（ATR）", v => v],
    ["tp1_r", "第一目標距離", v => "+" + v + "R（對齊 7 日行情空間分布）"],
    ["trail_atr_mult", "移動停損（ATR）", v => v + "×（TP1 後啟動）"],
    ["ratchet_mfe_r", "鎖利棘輪", v => `浮盈 ${v}R 後，停損上移到 −${P.ratchet_lock_r}R`],
    ["stagnation_days", "停滯出場", v => `${v} 天無進展（<${P.stagnation_mfe_r}R）就離場`],
    ["risk_pct_base", "基準單筆風險", v => v + "%"],
    ["score_threshold", "出手分數門檻", v => "±" + v],
    ["confidence_floor", "把握度門檻", v => v],
    ["entry_validity_hours", "掛單有效期", v => v + " 小時"],
    ["max_hold_days", "最長持倉", v => v + " 天"],
    ["max_leverage", "槓桿上限", v => v + "x"],
  ];
  $("#params").innerHTML = pLabels.map(([k, lbl, f]) =>
    `<div class="kv"><span>${lbl}</span><b>${f(P[k])}</b></div>`).join("") +
    `<div class="hint" style="margin-top:6px">模型版本 v${OPT.version} · 最近調參 ${OPT.tuned_at || "—"}</div>`;

  $("#philosophy").innerHTML = [
    ["大賺小賠的結構", "停損固定小（1~2% 帳戶風險）；第一目標對齊行情實際給的空間（7 日 MFE 分布 65-70 百分位）先落袋 30% 並保本，第二目標再收 30%，餘下 40% 用移動停損讓利潤奔跑。部分成交時各比例等比縮放，結構永不變形。"],
    ["浮盈不變虧、死單不戀戰", "浮盈曾達 0.6R 的單，停損自動上移到 −0.25R（鎖利棘輪）；進場 5 天毫無進展直接離場（停滯出場）——資金與注意力留給會動的行情。"],
    ["高掛單成功率的來源", "掛單不追價：吸附在支撐/壓力群前緣，配合歷史觸價機率選深度。成交率與成本是蹺蹺板，優化器依近 20 筆成交率自動調整深度。"],
    ["逆向與擁擠度（GCR 反身性）", "資金費率極端分位＝人群擁擠訊號。空頭付費增倉但價格拒跌＝軋空燃料；多頭狂熱滯漲＝多殺多前兆。在共識最擁擠處找結構脆弱點。"],
    ["訂單流驗證（OI×CVD）", "突破要有新資金（OI↑）與主動買盤（CVD↑）共振才是真突破；價漲量縮、OI 下滑的突破視為誘多，不追。"],
    ["影線磁吸（CrypNuevo）", "流動性真空造成的長影線，市場傾向回補 50%。未回補影線是進場埋伏區與止盈磁吸目標。"],
    ["紀律鐵律", "連續 2 次實質停損強制冷卻 1 日（防報復性交易）；近 10 筆累虧超過 3R 自動降風險到 6 成（回撤時部位最小）；FOMC/CPI/非農前 48h 降槓桿、24h 內不開新倉；持倉最長 7 天；邏輯破壞無條件離場不凹單。"],
    ["為何是模擬追蹤", "本系統輸出「推薦與復盤」，不自動下單。所有統計以保守規則模擬（同棒先進後損、不計新成交當根止盈、含手續費滑價與資金費率），寧可低估不高估。"],
  ].map(([t, c]) => `<details class="phil"><summary>${t}</summary><p>${c}</p></details>`).join("");

  $("#about").innerHTML = `
    <b>資料來源</b>：K 線 ${esc(LATEST.src.klines)} · 衍生品 ${esc(LATEST.src.deriv)}（GitHub Actions 自動更新：訊號每日 08:07、持倉追蹤每 4 小時）<br>
    <b>免責聲明</b>：本工具為研究與教育用途的訊號模擬器，非投資建議。加密貨幣合約具極高風險，
    槓桿交易可能損失全部本金。過去績效（含回測）不代表未來表現，請自行評估並僅以可承受損失的資金參與。<br>
    <a href="https://github.com/kenhuangads/btc-trader" style="color:var(--accent)">GitHub 原始碼</a> · 引擎 v${OPT.version}`;
}

function renderTouch() {
  const box = $("#touch-chart"); if (!box || !OPT?.touch_probs?.long) return;
  const L = OPT.touch_probs.long, S = OPT.touch_probs.short;
  const keys = Object.keys(L).map(Number).sort((a, b) => a - b);
  if (!keys.length) { box.innerHTML = ""; return; }
  const W = box.clientWidth || 340, H = box.clientHeight || 190;
  const padR = 10, padT = 10, padB = 20, padL = 34;
  const X = k => padL + (k - keys[0]) / (keys[keys.length - 1] - keys[0]) * (W - padL - padR);
  const Y = p => padT + (1 - p) * (H - padT - padB);
  const green = css("--green"), red = css("--red"), muted = css("--muted"), border = css("--border");
  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  [0, .5, 1].forEach(p => {
    s += `<line x1="${padL}" x2="${W - padR}" y1="${Y(p)}" y2="${Y(p)}" stroke="${border}"/>
          <text x="4" y="${Y(p) + 3}" font-size="9.5" fill="${muted}">${p * 100}%</text>`;
  });
  [0.5, 1.0, 1.5, 2.0, 2.5].forEach(k => {
    if (k >= keys[0] && k <= keys[keys.length - 1])
      s += `<text x="${X(k)}" y="${H - 6}" font-size="9.5" fill="${muted}" text-anchor="middle">掛 ${k} 檔遠</text>`;
  });
  const line = (obj, col) => {
    let d = "";
    keys.forEach(k => { const v = obj[k.toFixed(1)]; if (v != null) d += (d ? " L" : "M") + `${X(k)},${Y(v)}`; });
    return `<path d="${d}" fill="none" stroke="${col}" stroke-width="2"/>`;
  };
  s += line(L, green) + line(S, red);
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
    LIVE_PRICE = p;
    $("#live-price").textContent = fmt(p);
    const chg = (p / LATEST.price.close - 1) * 100;
    const el = $("#live-chg");
    el.textContent = `${chg >= 0 ? "▲" : "▼"}${Math.abs(chg).toFixed(2)}%`;
    el.className = "chg " + (chg >= 0 ? "up" : "down");
    updateLadderLive(p);
    updateChartNow(p);
  };
  update(); setInterval(update, 30000);
}

function tickCountdown() {
  const plan = LATEST?.signal?.plan; if (!plan) return;
  const v = plan.validity_ms - Date.now(), d = plan.deadline_ms - Date.now();
  const fmtDur = ms => { const h = Math.floor(ms / 3600e3), m = Math.floor(ms % 3600e3 / 60e3); return `${h} 小時 ${m} 分`; };
  const ev = $("#cd-validity"), ed = $("#cd-deadline");
  if (ev) ev.textContent = v > 0 ? `⏳ 掛單剩 ${fmtDur(v)}` : "⌛ 掛單已過期（沒成交的單請撤掉）";
  if (ed) ed.textContent = d > 0 ? `⏰ 持倉時間上限剩 ${fmtDur(d)}` : "";
}

/* ---------------- K 線圖範圍切換 ---------------- */
document.querySelectorAll("#range-seg button").forEach(b => {
  b.classList.toggle("on", +b.dataset.days === CHART_DAYS);
  b.addEventListener("click", () => {
    CHART_DAYS = +b.dataset.days;
    localStorage.setItem("chartDays", CHART_DAYS);
    document.querySelectorAll("#range-seg button").forEach(x => x.classList.toggle("on", x === b));
    renderChart();
  });
});

/* ---------------- 頁籤 ---------------- */
document.querySelectorAll("#nav button").forEach(b => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    $("#tab-" + b.dataset.tab).classList.add("active");
    window.scrollTo({ top: 0 });
    if (b.dataset.tab === "review") { renderEquity(); renderRBars(); }
    if (b.dataset.tab === "system") { renderTouch(); }
  });
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js").catch(() => { });
boot();

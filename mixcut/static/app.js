"use strict";
// 门店混剪：文案逐句配画面 → 逐句编辑 → 预览 → 导出。视频只在浏览器里处理，不上传。

const W = 1080, H = 1920;
const MAX_SEC = 30;            // 建议总时长上限
const CHUNK_MAX = 16;          // 每个镜头最多几个字（按逗号切开后再合并）
const CHARS_PER_SEC = 4.6;     // 没有配音时按字数估算时长
const GAP = 0.12;              // 句与句之间的停顿
const VIDEO_EXT = /\.(mp4|mov|m4v|webm)$/i;
const AUDIO_EXT = /\.(mp3|m4a|aac|wav|ogg)$/i;
const FONT = '"PingFang SC","Microsoft YaHei","Noto Sans CJK SC","Source Han Sans SC",sans-serif';

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const rand = () => Math.random();

let cfg = { voice: false, dialects: ["普通话"] };
let stores = [];
let projects = [];
let openId = null;
const clips = new Map();       // id(相对路径) → { id, file, url, owner, kind: people|broll|shared, tags }
let music = [];
const audioCache = new Map();  // `${store}|${rate}|${text}` → { url, duration }

// ---------- 后台接口 ----------
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).error || msg; } catch {}
    throw new Error(msg);
  }
  return res;
}

// ---------- 标签页 ----------
document.querySelectorAll(".tab").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === b));
  $("tab-stores").hidden = b.dataset.tab !== "stores";
  $("tab-tasks").hidden = b.dataset.tab !== "tasks";
  if (b.dataset.tab === "tasks") renderProjects();
});

// =====================================================================
// 门店资料
// =====================================================================
const storeByName = name => stores.find(s => s.name === name);
let saveTimer = null;
function saveStoresSoon() {
  $("saveState").textContent = "保存中…";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const res = await api("/api/stores", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(stores) });
      stores = await res.json();
      $("saveState").textContent = "已保存";
    } catch (e) { $("saveState").textContent = "保存失败：" + e.message; }
  }, 500);
}

function renderStores() {
  const tb = $("storeRows");
  tb.innerHTML = "";
  if (!stores.length) tb.innerHTML = '<tr><td colspan="7" class="hint">还没有门店。点「加门店」，或者导入文案 Excel 时一键新建。</td></tr>';
  stores.forEach((s, i) => {
    const tr = document.createElement("tr");
    const voice = s.voice_status || (s.voice_id ? "处理中" : "未复刻");
    const vcls = voice === "可用" ? "ok" : voice === "未复刻" || voice === "失败" ? "bad" : "warn";
    tr.innerHTML = `
      <td><input type="text" data-k="name"></td>
      <td><input type="text" data-k="city" placeholder="合肥"></td>
      <td><input type="text" data-k="nickname" placeholder="许师傅"></td>
      <td><select data-k="gender"><option></option><option>男</option><option>女</option></select></td>
      <td><select data-k="dialect">${cfg.dialects.map(d => `<option>${d}</option>`).join("")}</select></td>
      <td><span class="tag ${vcls}">${esc(voice)}</span>
        <label class="btn small">上传声音<input type="file" accept="audio/*,.m4a,.mp3,.wav,.aac" hidden></label>
        ${s.voice_id && voice !== "可用" ? '<button class="small" data-act="refresh">刷新</button>' : ""}
        ${voice === "可用" ? '<button class="small" data-act="listen">试听</button>' : ""}</td>
      <td><button class="small" data-act="del">删除</button></td>`;
    tr.querySelectorAll("[data-k]").forEach(el => {
      const k = el.dataset.k;
      el.value = s[k] || (k === "dialect" ? "普通话" : "");
      if (k === "name" || k === "city" || k === "nickname") el.classList.toggle("missing", !el.value);
      el.onchange = () => {
        if (k === "name" && s.name && s.name !== el.value.trim() && s.voice_id &&
            !confirm("改门店名会让已复刻的声音失效，需要重新上传。确定要改吗？")) { el.value = s.name; return; }
        s[k] = el.value.trim();
        el.classList.toggle("missing", (k === "name" || k === "city" || k === "nickname") && !s[k]);
        saveStoresSoon(); rematchProjects();
      };
    });
    tr.querySelector("input[type=file]").onchange = e => uploadVoice(s, e.target.files[0]);
    tr.querySelector('[data-act="del"]').onclick = () => {
      if (confirm(`删除门店「${s.name}」？`)) { stores.splice(i, 1); saveStoresSoon(); renderStores(); rematchProjects(); }
    };
    const refresh = tr.querySelector('[data-act="refresh"]');
    if (refresh) refresh.onclick = () => refreshVoice(s);
    const listen = tr.querySelector('[data-act="listen"]');
    if (listen) listen.onclick = async () => {
      try {
        const a = await ttsFor(s.name, `大家好，我是${s.city}的${s.nickname}，今天给大家看看我们翻新的效果。`, 1);
        new Audio(a.url).play();
      } catch (e) { alert(e.message); }
    };
    tb.appendChild(tr);
  });
}

$("addStore").onclick = () => { stores.push({ name: "", city: "", nickname: "", gender: "", dialect: "普通话" }); renderStores(); };

async function uploadVoice(s, file) {
  if (!file) return;
  if (!cfg.voice) { alert("服务器还没配置阿里云密钥，暂时不能复刻声音。见使用说明。"); return; }
  if (!s.name) { alert("先填门店名"); return; }
  await flushStores();
  const fd = new FormData(); fd.append("file", file);
  try {
    await api(`/api/stores/${encodeURIComponent(s.name)}/voice`, { method: "POST", body: fd });
    await loadStores();
    setTimeout(() => refreshVoice(storeByName(s.name)), 8000);
  } catch (e) { alert(e.message); }
}

async function refreshVoice(s) {
  if (!s) return;
  try {
    const r = await (await api(`/api/stores/${encodeURIComponent(s.name)}/voice`)).json();
    s.voice_status = r.voice_status;
    renderStores();
    if (r.voice_status === "处理中") setTimeout(() => refreshVoice(storeByName(s.name)), 10000);
  } catch (e) { alert(e.message); }
}

async function flushStores() {
  if (!saveTimer) return;
  clearTimeout(saveTimer); saveTimer = null;
  const res = await api("/api/stores", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(stores) });
  stores = await res.json();
}

async function loadStores() {
  stores = await (await api("/api/stores")).json();
  renderStores();
}

// =====================================================================
// 素材文件夹
// =====================================================================
function tagsOf(parts) {
  // parts: 相对路径去掉最外层文件夹和门店/通用层之后剩下的部分
  const words = [];
  parts.forEach((p, i) => {
    const base = i === parts.length - 1 ? p.replace(/\.[^.]+$/, "") : p;
    base.split(/[_\-\s.,，、+（）()]+/).forEach(w => { if (w.length >= 2 && !/^\d+$/.test(w)) words.push(w); });
  });
  return words;
}

$("folder").onchange = e => {
  for (const c of clips.values()) URL.revokeObjectURL(c.url);
  clips.clear(); music.forEach(m => URL.revokeObjectURL(m.url)); music = [];
  for (const f of e.target.files) {
    const parts = f.webkitRelativePath.split("/").slice(1);   // 去掉「素材」这一层
    if (parts.length < 2) continue;
    const id = parts.join("/");
    const owner = parts[0];
    if (owner === "音乐") {
      if (AUDIO_EXT.test(f.name)) music.push({ id, file: f, url: URL.createObjectURL(f), name: f.name });
      continue;
    }
    if (!VIDEO_EXT.test(f.name)) continue;
    let kind, tagParts;
    if (owner === "通用") { kind = "shared"; tagParts = parts.slice(1); }
    else if (parts[1] === "人物") { kind = "people"; tagParts = parts.slice(2); }
    else { kind = "broll"; tagParts = parts.slice(parts[1] === "空镜" ? 2 : 1); }
    clips.set(id, { id, file: f, url: URL.createObjectURL(f), owner, kind, tags: tagsOf(tagParts) });
  }
  const owners = new Set([...clips.values()].filter(c => c.kind !== "shared").map(c => c.owner));
  const shared = [...clips.values()].filter(c => c.kind === "shared").length;
  $("materialInfo").textContent = `${owners.size} 家门店的素材，${shared} 个通用镜头，${music.length} 首音乐`;
  // 已有任务：缺镜头的自动补上
  projects.forEach(p => { if (p.sentences.some(r => !clips.has(r.clipId))) autoAssign(p, true); });
  saveProjects(); renderProjects();
};

const clipsOf = (store, kind) => [...clips.values()].filter(c => c.owner === store && c.kind === kind);
const sharedClips = () => [...clips.values()].filter(c => c.kind === "shared");

// =====================================================================
// 文案 → 任务
// =====================================================================
function splitScript(text) {
  const clean = text.replace(/[“”"]/g, "").replace(/\s+/g, "");
  const sentences = clean.split(/(?<=[。！？!?；;…])/).filter(s => s.replace(/[。！？!?；;…，,、]/g, ""));
  const chunks = [];
  for (const s of sentences) {
    const pieces = s.split(/(?<=[，,、：:])/).filter(Boolean);
    let cur = "";
    for (const p of pieces) {
      if (cur && (cur + p).length > CHUNK_MAX) { chunks.push(cur); cur = p; }
      else cur += p;
    }
    if (cur) chunks.push(cur);
  }
  return chunks.map(c => c.replace(/[，,、：:；;。]+$/, ""));
}

$("excel").onchange = async e => {
  const f = e.target.files[0]; e.target.value = "";
  if (!f) return;
  if (projects.length && !confirm("导入新的文案会替换现在的任务（包括已经改过的内容），继续吗？")) return;
  const fd = new FormData(); fd.append("file", f);
  try {
    const rows = await (await api("/api/import", { method: "POST", body: fd })).json();
    if (!rows.length) { alert("Excel 里没找到文案。表头要有「人物」（或「门店」）和「改写脚本」（或「文案」）两列。"); return; }
    projects = rows.map((r, i) => {
      const p = { id: `p${Date.now()}_${i}`, who: r.who, place: r.place, nickname: r.nickname, gender: r.gender,
        store: r.store, cover: r.cover, showTitle: true, rate: 1, bgm: "auto", confirmed: false,
        sentences: splitScript(r.script).map(text => ({ text, clipId: "", offset: rand() })) };
      autoAssign(p);
      return p;
    });
    openId = projects[0].id;
    saveProjects(); renderProjects();
  } catch (err) { alert(err.message); }
};

function rematchProjects() {
  for (const p of projects) {
    if (p.store && storeByName(p.store)) continue;
    const s = stores.find(s => s.name && p.who.includes(s.name)) ||
              stores.find(s => s.nickname && p.who.includes(s.nickname) && s.city && p.who.includes(s.city));
    p.store = s ? s.name : "";
    if (s) autoAssign(p, true);
  }
  saveProjects();
  if (!$("tab-tasks").hidden) renderProjects();
}

// 自动配画面：第一句和最后一句用主播出镜，中间按文案里的词找镜头，本店空镜优先
function autoAssign(p, onlyMissing = false) {
  const people = shuffle(clipsOf(p.store, "people"));
  const own = shuffle(clipsOf(p.store, "broll"));
  const shared = shuffle(sharedClips());
  const used = new Set(onlyMissing ? p.sentences.map(r => r.clipId) : []);
  const n = p.sentences.length;
  let pi = 0;
  p.sentences.forEach((r, i) => {
    if (onlyMissing && clips.has(r.clipId)) return;
    const wantPeople = i === 0 || (i === n - 1 && n >= 4);
    let pick = null;
    if (wantPeople && people.length) pick = people[pi++ % people.length];
    if (!pick) {
      const matches = c => c.tags.some(t => r.text.includes(t));
      const order = [own.filter(matches), shared.filter(matches), own, shared];
      for (const list of order) { pick = list.find(c => !used.has(c.id)); if (pick) break; }
      if (!pick) pick = [...own, ...shared, ...people][i % Math.max(1, own.length + shared.length + people.length)] || null;
    }
    r.clipId = pick ? pick.id : "";
    r.offset = rand();
    if (pick) used.add(pick.id);
  });
}

function shuffle(a) {
  a = a.slice();
  for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(rand() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; }
  return a;
}

// 记住改过的内容（刷新页面不丢；素材文件夹需要重新选）
function saveProjects() {
  try { localStorage.setItem("mixcut.projects", JSON.stringify({ projects, openId })); } catch {}
}
function loadProjects() {
  try {
    const d = JSON.parse(localStorage.getItem("mixcut.projects") || "null");
    if (d) { projects = d.projects || []; openId = d.openId; }
  } catch {}
}

// =====================================================================
// 时长、配音、检查
// =====================================================================
const audioKey = (p, r) => `${p.store}|${p.rate}|${r.text}`;
function rowDur(p, r) {
  const a = audioCache.get(audioKey(p, r));
  if (a) return a.duration + GAP;
  return Math.max(1.2, r.text.length / CHARS_PER_SEC / p.rate) + GAP;
}
const totalDur = p => p.sentences.reduce((s, r) => s + rowDur(p, r), 0);
const hasVoice = p => p.sentences.length > 0 && p.sentences.every(r => audioCache.has(audioKey(p, r)));
const voiceReady = p => { const s = storeByName(p.store); return cfg.voice && s && s.voice_status === "可用"; };

function checks(p) {
  const out = [];
  const s = storeByName(p.store);
  if (!s) out.push({ ok: false, block: true, text: "没对上门店" });
  const peopleCount = p.sentences.filter(r => clips.get(r.clipId)?.kind === "people").length;
  if (!clips.size) out.push({ ok: false, block: true, text: "还没选择素材文件夹" });
  else if (!clipsOf(p.store, "people").length) out.push({ ok: false, block: true, text: "素材里没有这家店的人物镜头" });
  else out.push({ ok: peopleCount > 0, block: true, text: peopleCount ? `主播出镜 ${peopleCount} 次` : "主播没有出镜" });
  const missing = p.sentences.filter(r => !clips.has(r.clipId)).length;
  if (missing) out.push({ ok: false, block: true, text: `${missing} 句没有画面` });
  const t = totalDur(p);
  out.push({ ok: t <= MAX_SEC, block: false, text: `${t.toFixed(1)} 秒${t > MAX_SEC ? `，超过 ${MAX_SEC} 秒，建议删减或加快语速` : ""}` });
  if (hasVoice(p)) out.push({ ok: true, block: false, text: `${s?.dialect || ""}配音已生成` });
  else out.push({ ok: false, block: false, text: voiceReady(p) ? "还没生成配音" : "这家店还没有可用的复刻声音，导出的视频没有配音" });
  return out;
}
const canConfirm = p => !checks(p).some(c => c.block && !c.ok);

async function ttsFor(store, text, rate) {
  const key = `${store}|${rate}|${text}`;
  if (audioCache.has(key)) return audioCache.get(key);
  const res = await api("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ store, text, rate }) });
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const duration = await new Promise((ok, fail) => {
    const a = new Audio(); a.preload = "metadata";
    a.onloadedmetadata = () => ok(a.duration); a.onerror = () => fail(new Error("配音文件打不开"));
    a.src = url;
  });
  const v = { url, duration };
  audioCache.set(key, v);
  return v;
}

async function generateVoice(p, btn) {
  btn.disabled = true;
  try {
    for (let i = 0; i < p.sentences.length; i++) {
      btn.textContent = `配音中 ${i + 1}/${p.sentences.length}`;
      await ttsFor(p.store, p.sentences[i].text, p.rate);
    }
  } catch (e) { alert(e.message); }
  renderProjects();
}

// =====================================================================
// 任务列表 + 编辑页
// =====================================================================
function renderProjects() {
  const box = $("projects");
  box.innerHTML = "";
  $("voiceWarn").innerHTML = cfg.voice ? "" :
    '<div class="warnbox">服务器还没配置声音复刻，现在按字数估算时长，导出的视频没有配音。可以先用来排画面、看效果。</div>';
  if (!projects.length) {
    box.innerHTML = '<div class="card hint">先导入文案 Excel，每一行会变成一条要剪的视频。</div>';
    $("exportCard").hidden = true;
    return;
  }
  for (const p of projects) box.appendChild(projectCard(p));
  $("exportCard").hidden = false;
  $("exportAll").disabled = busy || !projects.some(p => p.confirmed);
}

function projectCard(p) {
  const card = document.createElement("div");
  card.className = "card project";
  const s = storeByName(p.store);
  const cs = checks(p);
  const bad = cs.filter(c => !c.ok);
  card.innerHTML = `
    <div class="head">
      <b>${esc(p.store || p.who)}</b>
      ${s ? `<span class="tag">${esc(s.city)}</span><span class="tag">${esc(s.nickname)}</span><span class="tag">${esc(s.dialect)}</span>` : ""}
      <span class="grow hint">${esc(p.cover)}</span>
      ${p.confirmed ? '<span class="tag ok">已确认</span>' : bad.length ? `<span class="tag warn">${esc(bad[0].text)}</span>` : '<span class="tag">待确认</span>'}
      <span class="hint">${totalDur(p).toFixed(1)} 秒</span>
    </div>`;
  card.querySelector(".head").onclick = () => { openId = openId === p.id ? null : p.id; saveProjects(); renderProjects(); };
  if (!s) {
    const fix = document.createElement("div");
    fix.className = "row";
    fix.innerHTML = `<span class="tag bad">「${esc(p.who)}」没对上门店</span>
      <select><option value="">选一家已有门店…</option>${stores.map(x => `<option>${esc(x.name)}</option>`).join("")}</select>
      <button class="small">按这行新建门店</button>`;
    fix.querySelector("select").onchange = e => { if (e.target.value) { p.store = e.target.value; autoAssign(p); saveProjects(); renderProjects(); } };
    fix.querySelector("button").onclick = () => {
      const name = prompt("新门店的名字（要和素材文件夹里的门店文件夹名一致）", `${p.place}店`);
      if (!name) return;
      const city = p.place.slice(0, 2);
      stores.push({ name, city, nickname: p.nickname, gender: p.gender, dialect: guessDialect(p.place) });
      saveStoresSoon(); renderStores();
      p.store = name; autoAssign(p); saveProjects(); renderProjects();
    };
    card.appendChild(fix);
    return card;
  }
  if (openId === p.id) card.appendChild(editor(p));
  return card;
}

function guessDialect(place) {
  const map = { 广东话: "广州深圳珠海佛山东莞中山江门惠州肇庆香港澳门", 四川话: "成都重庆绵阳德阳宜宾南充泸州乐山眉山",
    安徽话: "合肥芜湖蚌埠安庆阜阳六安淮南马鞍山", 湖南话: "长沙株洲湘潭衡阳岳阳常德", 河南话: "郑州洛阳开封新乡南阳许昌",
    陕西话: "西安咸阳宝鸡渭南", 山东话: "济南青岛烟台潍坊临沂淄博", 东北话: "沈阳大连长春哈尔滨吉林鞍山" };
  for (const [d, cities] of Object.entries(map)) if (cities.includes(place.slice(0, 2))) return d;
  return "普通话";
}

function clipOptions(p, selected) {
  const group = (label, list) => list.length ? `<optgroup label="${label}">${list.map(c =>
    `<option value="${esc(c.id)}" ${c.id === selected ? "selected" : ""}>${esc(c.id.split("/").slice(1).join("/"))}</option>`).join("")}</optgroup>` : "";
  return `<option value="">（选画面）</option>` + group("主播人物", clipsOf(p.store, "people")) +
    group("本店空镜", clipsOf(p.store, "broll")) + group("通用", sharedClips());
}

function editor(p) {
  const el = document.createElement("div");
  el.className = "editor";
  const t = totalDur(p);
  el.innerHTML = `
    <div class="settings">
      <div><label>顶部标题（封面标题）</label><input type="text" data-k="cover"></div>
      <div><label>显示标题</label><br><input type="checkbox" data-k="showTitle"></div>
      <div><label>语速</label><select data-k="rate">${[0.9, 1, 1.1, 1.2, 1.3, 1.4].map(v => `<option value="${v}">${v}×</option>`).join("")}</select></div>
      <div><label>背景音乐</label><select data-k="bgm"><option value="auto">随机</option><option value="none">不要</option>
        ${music.map(m => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("")}</select></div>
    </div>
    <div class="checks">${checks(p).map(c => `<span class="tag ${c.ok ? "ok" : c.block ? "bad" : "warn"}">${c.ok ? "✔" : "✘"} ${esc(c.text)}</span>`).join("")}</div>
    <div class="table-wrap"><table class="rows">
      <thead><tr><th>#</th><th>文案（也是字幕）</th><th>时长</th><th>画面</th><th></th></tr></thead>
      <tbody></tbody>
      <tfoot><tr><td></td><td><button class="small" data-act="add">+ 加一句</button></td>
        <td class="total ${t > MAX_SEC ? "over" : ""}">${t.toFixed(1)}s</td><td colspan="2" class="hint">建议不超过 ${MAX_SEC} 秒</td></tr></tfoot>
    </table></div>
    <div class="row">
      <button data-act="voice" ${voiceReady(p) && !hasVoice(p) ? "" : "disabled"}>生成配音</button>
      <button data-act="shuffle">重新配画面</button>
      <button data-act="preview">▶ 预览</button>
      <button data-act="cover-img">下载封面</button>
      <span style="flex:1"></span>
      ${p.confirmed ? '<button data-act="unconfirm">取消确认</button>' :
        `<button class="primary" data-act="confirm" ${canConfirm(p) ? "" : "disabled"}>确认这条</button>`}
    </div>`;

  el.querySelectorAll(".settings [data-k]").forEach(inp => {
    const k = inp.dataset.k;
    if (inp.type === "checkbox") inp.checked = !!p[k]; else inp.value = String(p[k]);
    inp.onchange = () => {
      p[k] = inp.type === "checkbox" ? inp.checked : k === "rate" ? Number(inp.value) : inp.value;
      p.confirmed = false; saveProjects(); renderProjects();
    };
  });

  const tb = el.querySelector("tbody");
  p.sentences.forEach((r, i) => {
    const c = clips.get(r.clipId);
    const tr = document.createElement("tr");
    if (c?.kind === "people") tr.className = "people";
    tr.innerHTML = `
      <td class="num">${i + 1}</td>
      <td><textarea rows="1"></textarea></td>
      <td class="dur">${rowDur(p, r).toFixed(1)}s${audioCache.has(audioKey(p, r)) ? " 🔊" : ""}</td>
      <td class="clip"><div class="pick">${c ? `<video muted preload="auto" src="${c.url}#t=0.5"></video>` : ""}<select>${clipOptions(p, r.clipId)}</select></div></td>
      <td class="ops"><button class="small" data-op="up" title="上移">↑</button><button class="small" data-op="down" title="下移">↓</button><button class="small" data-op="del" title="删除">✕</button></td>`;
    const ta = tr.querySelector("textarea");
    ta.value = r.text;
    ta.onchange = () => { r.text = ta.value.trim(); p.confirmed = false; saveProjects(); renderProjects(); };
    tr.querySelector("select").onchange = e => { r.clipId = e.target.value; r.offset = rand(); p.confirmed = false; saveProjects(); renderProjects(); };
    const v = tr.querySelector("video");
    if (v) { v.onmouseenter = () => v.play().catch(() => {}); v.onmouseleave = () => v.pause(); }
    tr.querySelectorAll("[data-op]").forEach(b => b.onclick = () => {
      const op = b.dataset.op, a = p.sentences;
      if (op === "up" && i > 0) [a[i - 1], a[i]] = [a[i], a[i - 1]];
      if (op === "down" && i < a.length - 1) [a[i + 1], a[i]] = [a[i], a[i + 1]];
      if (op === "del") a.splice(i, 1);
      p.confirmed = false; saveProjects(); renderProjects();
    });
    tb.appendChild(tr);
  });

  const on = (act, fn) => { const b = el.querySelector(`[data-act="${act}"]`); if (b) b.onclick = () => fn(b); };
  on("add", () => { p.sentences.push({ text: "新的一句", clipId: "", offset: rand() }); autoAssign(p, true); p.confirmed = false; saveProjects(); renderProjects(); });
  on("voice", b => generateVoice(p, b));
  on("shuffle", () => { autoAssign(p); p.confirmed = false; saveProjects(); renderProjects(); });
  on("preview", () => playProject(p, { record: false }).catch(e => alert(e.message)));
  on("cover-img", () => coverImage(p).then(blob => downloadBlob(blob, `${p.store}_封面.png`)).catch(e => alert(e.message)));
  on("confirm", () => {
    p.confirmed = true;
    const next = projects.find(x => !x.confirmed && x !== p);
    openId = next ? next.id : null;
    saveProjects(); renderProjects();
  });
  on("unconfirm", () => { p.confirmed = false; saveProjects(); renderProjects(); });
  return el;
}

// =====================================================================
// 渲染（预览和导出共用）
// =====================================================================
const canvas = $("stage");
const ctx = canvas.getContext("2d");
let audioCtx = null;
let busy = false;
let stopRequested = false;

function waitEvent(el, name) {
  return new Promise((ok, fail) => {
    const done = () => { cleanup(); ok(); };
    const bad = () => { cleanup(); fail(new Error("视频打不开：" + (el.dataset.name || ""))); };
    const cleanup = () => { el.removeEventListener(name, done); el.removeEventListener("error", bad); };
    el.addEventListener(name, done); el.addEventListener("error", bad);
  });
}

async function openVideo(clip) {
  const v = document.createElement("video");
  v.muted = true; v.loop = true; v.playsInline = true; v.preload = "auto";
  v.dataset.name = clip.id;
  v.src = clip.url;
  await waitEvent(v, "loadeddata");
  return v;
}

async function seek(v, t) {
  if (Math.abs(v.currentTime - t) < 0.02) return;
  const p = waitEvent(v, "seeked"); v.currentTime = t; await p;
}

async function decode(url) {
  const buf = await (await fetch(url)).arrayBuffer();
  return await audioCtx.decodeAudioData(buf);
}

function drawCover(v) {
  const vw = v.videoWidth, vh = v.videoHeight;
  if (!vw || !vh) return;
  const scale = Math.max(W / vw, H / vh);
  ctx.drawImage(v, (W - vw * scale) / 2, (H - vh * scale) / 2, vw * scale, vh * scale);
}

function strokeText(text, x, y, size, fill, weight = 900) {
  ctx.font = `${weight} ${size}px ${FONT}`;
  ctx.lineJoin = "round"; ctx.lineWidth = size * 0.16; ctx.strokeStyle = "#000";
  ctx.strokeText(text, x, y); ctx.fillStyle = fill; ctx.fillText(text, x, y);
}

function fitSize(text, maxW, size, min = 36) {
  ctx.font = `900 ${size}px ${FONT}`;
  while (ctx.measureText(text).width > maxW && size > min) { size -= 4; ctx.font = `900 ${size}px ${FONT}`; }
  return size;
}

// 标题按空格分行，例如「合肥卫生间翻新不用砸 1天就能焕新 包工包料」
function titleLines(title) {
  const parts = title.trim().split(/\s+/).filter(Boolean);
  if (parts.length <= 1) return parts;
  if (parts.length === 2) return parts;
  return [parts[0], parts.slice(1).join(" ")];
}

function drawTitle(title) {
  if (!title) return;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  titleLines(title).forEach((line, i) => {
    const size = fitSize(line, W - 120, i === 0 ? 88 : 72);
    strokeText(line, W / 2, 200 + i * 112, size, i === 0 ? "#FFE100" : "#FFFFFF");
  });
}

function drawSubtitle(text) {
  if (!text) return;
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  const lines = text.length > 11 ? splitHalf(text) : [text];
  lines.forEach((line, i) => strokeText(line, W / 2, H * 0.74 + i * 96, fitSize(line, W - 140, 76), "#FFFFFF"));
}

// 字幕太长时分两行，断在词语之间（不把「卫生间」拆开）。文案里打一个空格可以手动指定断行位置
const PARTICLES = "的了得地就也都又在把给和与着过吗呢吧啊嘛噻撒哈";
function splitHalf(text) {
  if (/\s/.test(text.trim())) {
    const i = text.trim().search(/\s/);
    return [text.trim().slice(0, i), text.trim().slice(i).trim()];
  }
  const mid = text.length / 2;
  let best = Math.ceil(mid);
  if (window.Intl && Intl.Segmenter) {
    const cuts = [];
    let prevLen = 0;
    for (const seg of new Intl.Segmenter("zh", { granularity: "word" }).segment(text)) {
      // 单个字跟在词语后面（如「卫生|间」）、助词（如「旧|了」）都粘在前面
      const glue = seg.segment.length === 1 && prevLen > 0 && (prevLen >= 2 || PARTICLES.includes(seg.segment));
      if (glue && cuts.length) cuts.pop();
      cuts.push(seg.index + seg.segment.length);
      prevLen = glue ? prevLen + 1 : seg.segment.length;
    }
    let bestGap = Infinity;
    for (const pos of cuts) if (pos < text.length && Math.abs(pos - mid) < bestGap) { bestGap = Math.abs(pos - mid); best = pos; }
  }
  return [text.slice(0, best), text.slice(best)].filter(Boolean);
}

function drawAiLabel() {
  ctx.textAlign = "right"; ctx.textBaseline = "alphabetic";
  ctx.font = `600 36px ${FONT}`; ctx.lineWidth = 6; ctx.strokeStyle = "rgba(0,0,0,.6)";
  ctx.strokeText("内容由AI生成", W - 40, H - 70); ctx.fillStyle = "#fff"; ctx.fillText("内容由AI生成", W - 40, H - 70);
}

function pickMime() {
  const list = ["video/mp4;codecs=avc1.640028,mp4a.40.2", "video/mp4;codecs=avc1,mp4a.40.2", "video/mp4;codecs=avc1,opus",
                "video/mp4", "video/webm;codecs=vp9,opus", "video/webm"];
  return list.find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || "";
}

function bgmFor(p) {
  if (p.bgm === "none" || !music.length) return null;
  if (p.bgm !== "auto") return music.find(m => m.id === p.bgm) || null;
  let h = 0; for (const ch of p.store + p.cover) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return music[h % music.length];
}

async function playProject(p, { record }) {
  if (busy) throw new Error("正在播放或导出，请稍等");
  if (p.sentences.some(r => !clips.has(r.clipId))) throw new Error("有的句子没有画面，或者还没选择素材文件夹");
  busy = true; stopRequested = false;
  $("player").hidden = false;
  $("playState").textContent = "准备中…";
  audioCtx = audioCtx || new AudioContext();
  await audioCtx.resume();
  const opened = [];
  const sources = [];
  try {
    // 时间轴
    let t = 0;
    const segs = p.sentences.map(r => { const d = rowDur(p, r); const s = { r, start: t, end: t + d }; t += d; return s; });
    const total = t;
    for (const s of segs) {
      const v = await openVideo(clips.get(s.r.clipId));
      const len = s.end - s.start;
      await seek(v, (s.r.offset || 0) * Math.max(0, v.duration - len - 0.1));
      s.video = v; opened.push(v);
    }

    // 声音：逐句配音 + 背景音乐
    const out = record ? audioCtx.createMediaStreamDestination() : audioCtx.destination;
    const t0 = audioCtx.currentTime + 0.25;
    const withVoice = hasVoice(p);
    if (withVoice) {
      for (const s of segs) {
        const src = audioCtx.createBufferSource();
        src.buffer = await decode(audioCache.get(audioKey(p, s.r)).url);
        src.connect(out); src.start(t0 + s.start); sources.push(src);
      }
    }
    const bgm = bgmFor(p);
    if (bgm) {
      const src = audioCtx.createBufferSource();
      src.buffer = await decode(bgm.url); src.loop = true;
      const gain = audioCtx.createGain();
      gain.gain.setValueAtTime(withVoice ? 0.12 : 0.5, t0);
      gain.gain.setValueAtTime(withVoice ? 0.12 : 0.5, t0 + total - 1);
      gain.gain.linearRampToValueAtTime(0, t0 + total);
      src.connect(gain).connect(out); src.start(t0, 0, total + 0.5); sources.push(src);
    }

    let rec = null, chunks = [], stopped = null, mime = "";
    if (record) {
      mime = pickMime();
      const tracks = [...canvas.captureStream(30).getVideoTracks(), ...out.stream.getAudioTracks()];
      rec = new MediaRecorder(new MediaStream(tracks), { mimeType: mime || undefined, videoBitsPerSecond: 8_000_000, audioBitsPerSecond: 160_000 });
      rec.ondataavailable = e => e.data.size && chunks.push(e.data);
      stopped = new Promise(ok => rec.onstop = ok);
      rec.start(1000);
    }

    const title = p.showTitle ? p.cover : "";
    let current = null;
    await new Promise(resolve => {
      const frame = () => {
        const now = Math.max(0, audioCtx.currentTime - t0);
        if (now >= total || stopRequested) { resolve(); return; }
        const seg = segs.find(s => now < s.end) || segs[segs.length - 1];
        if (seg !== current) {
          if (current) current.video.pause();
          if (audioCtx.currentTime >= t0) seg.video.play().catch(() => {});
          current = audioCtx.currentTime >= t0 ? seg : null;
        }
        ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
        drawCover(seg.video);
        drawTitle(title);
        drawSubtitle(seg.r.text.replace(/[。！？!?…，,]+$/g, "").replace(/[。！？!?…，,]/g, " ").trim());
        drawAiLabel();
        $("playState").textContent = `${record ? "导出中" : "预览中"} ${now.toFixed(1)} / ${total.toFixed(1)} 秒`;
        requestAnimationFrame(frame);
      };
      frame();
    });

    if (rec) {
      rec.stop(); await stopped;
      rec.stream.getTracks().forEach(tr => tr.stop());
      if (stopRequested) return null;
      return { blob: new Blob(chunks, { type: (mime || "video/webm").split(";")[0] }), total };
    }
    return null;
  } finally {
    sources.forEach(s => { try { s.stop(); } catch {} });
    opened.forEach(v => { v.pause(); v.removeAttribute("src"); v.load(); });
    busy = false;
    $("playState").textContent = stopRequested ? "已停止" : "播放完了";
    if (!record) setTimeout(() => { if (!busy) $("player").hidden = true; }, 1500);
    renderProjectsKeepScroll();
  }
}
$("stopPlay").onclick = () => { stopRequested = true; };

function renderProjectsKeepScroll() { const y = window.scrollY; renderProjects(); window.scrollTo(0, y); }

async function coverImage(p) {
  const first = p.sentences.find(r => clips.get(r.clipId)?.kind === "people") || p.sentences[0];
  if (!first || !clips.has(first.clipId)) throw new Error("还没有画面");
  const v = await openVideo(clips.get(first.clipId));
  await seek(v, (first.offset || 0) * Math.max(0, v.duration - 0.5));
  ctx.fillStyle = "#000"; ctx.fillRect(0, 0, W, H);
  drawCover(v);
  ctx.fillStyle = "rgba(0,0,0,.25)"; ctx.fillRect(0, 0, W, H);
  ctx.textAlign = "center"; ctx.textBaseline = "middle";
  const lines = p.cover.trim().split(/\s+/).filter(Boolean);
  lines.forEach((line, i) => {
    const y = H / 2 - (lines.length - 1) * 75 + i * 150;
    strokeText(line, W / 2, y, fitSize(line, W - 100, i === 0 ? 120 : 96), i === 0 ? "#FFE100" : "#FFFFFF");
  });
  v.removeAttribute("src"); v.load();
  return await new Promise(ok => canvas.toBlob(ok, "image/png"));
}

// =====================================================================
// 导出
// =====================================================================
const outputs = [];
function today() {
  const d = new Date();
  return `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, "0")}${String(d.getDate()).padStart(2, "0")}`;
}
function downloadBlob(blob, name) {
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; a.click();
}

$("exportAll").onclick = async () => {
  const list = projects.filter(p => p.confirmed);
  $("downloadAll").disabled = true;
  for (let i = 0; i < list.length; i++) {
    const p = list[i];
    $("exportState").textContent = `第 ${i + 1}/${list.length} 条：${p.store}`;
    try {
      const r = await playProject(p, { record: true });
      if (!r) break;
      const ext = r.blob.type.includes("mp4") ? "mp4" : "webm";
      const base = `${p.store}_${today()}`;
      addResult(`${base}.${ext}`, r.blob, r.total);
      outputs.push({ name: `${base}_封面.png`, blob: await coverImage(p) });
    } catch (e) {
      console.error(e);
      $("exportState").textContent = `${p.store} 导出失败：${e.message}`;
      break;
    }
  }
  if (!stopRequested) $("exportState").textContent = `导出完成，共 ${list.length} 条`;
  $("player").hidden = true;
  $("downloadAll").disabled = !outputs.length;
};

function addResult(name, blob, total) {
  outputs.push({ name, blob });
  const url = URL.createObjectURL(blob);
  const fig = document.createElement("figure");
  fig.innerHTML = `<video controls preload="metadata"></video><figcaption><a download></a><br><span class="hint">${total.toFixed(1)} 秒</span></figcaption>`;
  fig.querySelector("video").src = url;
  const a = fig.querySelector("a"); a.href = url; a.download = name; a.textContent = "下载 " + name;
  $("results").prepend(fig);
}

$("downloadAll").onclick = async () => {
  for (const o of outputs) { downloadBlob(o.blob, o.name); await new Promise(r => setTimeout(r, 400)); }
};

document.addEventListener("visibilitychange", () => { document.title = busy && document.hidden ? "⚠ 请回到混剪页面" : "门店混剪"; });

// =====================================================================
// 启动
// =====================================================================
(async () => {
  if (!pickMime().includes("avc1")) {
    $("formatWarn").innerHTML = '<div class="warnbox">这个浏览器导出的视频格式视频号可能不认，请用最新版 Chrome 或 Edge。</div>';
  }
  try { cfg = await (await api("/api/config")).json(); } catch {}
  try { await loadStores(); } catch (e) { alert("读取门店资料失败：" + e.message); }
  loadProjects();
  renderProjects();
})();

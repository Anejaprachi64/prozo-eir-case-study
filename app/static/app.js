const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function cell(c){
  if (c && typeof c === "object"){
    let inner = esc(c.v);
    if (c.img) inner = `<a href="${c.img}" target="_blank"><img class="thumb" loading="lazy" src="${c.img}"></a>` + inner;
    return `<td class="${esc(c.cls||"")}">${inner}</td>`;
  }
  return `<td>${esc(c)}</td>`;
}
function table(t, idx){
  const rows = t.rows.slice(0, 2000);
  return `<h3>${esc(t.title)}</h3><input type="search" placeholder="Filter rows…" oninput="filt(this,${idx})"><div class="scroll"><table class="t" id="tb${idx}"><tr>${t.columns.map(c=>`<th>${esc(c)}</th>`).join("")}</tr>` +
    rows.map(r=>`<tr>${r.map(cell).join("")}</tr>`).join("") + `</table></div>` + (t.rows.length>2000?`<p class="muted">Showing first 2000 rows — download the file for all.</p>`:"");
}
window.filt = (el, i) => { const q = el.value.toLowerCase(); document.querySelectorAll(`#tb${i} tr`).forEach((tr,k)=>{ if(k) tr.style.display = tr.textContent.toLowerCase().includes(q) ? "" : "none"; }); };
function render(m){
  const r = m.result || {}; let h = "";
  h += `<div class="dl">` + (m.files||[]).map(f => `<a href="/jobs/${m.id}/download/${encodeURIComponent(f)}">⬇ ${esc(f)}</a>` + (f.endsWith(".html") ? `<a class="v" href="/jobs/${m.id}/view/${encodeURIComponent(f)}" target="_blank">preview</a>` : "")).join("") + `</div>`;
  h += `<div class="chips">` + (r.summary||[]).map(s=>`<div class="chip ${esc(s.cls||"")}"><b>${esc(s.value)}</b>${esc(s.label)}</div>`).join("") + `</div>`;
  (r.warnings||[]).forEach(w => h += `<div class="warn">⚠ ${esc(w)}</div>`);
  (r.tables||[]).forEach((t,i) => h += table(t,i));
  if (r.legend) h += `<details><summary>Status meanings</summary><table class="t">` + Object.entries(r.legend).map(([k,v])=>`<tr><td class="st-${k}">${k}</td><td>${esc(v)}</td></tr>`).join("") + `</table></details>`;
  if (r.notes) h += `<details><summary>Assumptions used</summary><ul>` + r.notes.map(n=>`<li>${esc(n)}</li>`).join("") + `</ul></details>`;
  if (r.targets) h += `<details><summary>Target ranges used (% of revenue)</summary><pre>${esc(JSON.stringify(r.targets,null,1))}</pre></details>`;
  $("#out").innerHTML = h;
}
async function poll(){
  let m;
  try { m = await (await fetch(`/api/jobs/${JID}`)).json(); } catch(e){ $("#msg").textContent = "Connection lost — retrying…"; return setTimeout(poll, 3000); }
  $("#title").textContent = m.title || "Job";
  const pct = m.total ? Math.round(100*m.done/m.total) : (m.status==="done"?100:5);
  $("#fill").style.width = pct + "%";
  $("#msg").textContent = m.status==="error" ? "" : `${m.message}${m.total?` (${pct}%)`:""}`;
  if (m.status === "done"){ $("#fill").style.width="100%"; $("#prog").style.display="none"; render(m); return; }
  if (m.status === "error"){ $("#prog").className="card err"; $("#prog").innerHTML = `<b>Could not complete this job.</b><p>${esc(m.message)}</p><p><a href="javascript:history.back()">← Back</a></p>`; return; }
  setTimeout(poll, 1200);
}
poll();

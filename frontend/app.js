const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let certs = [];

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const nav = document.querySelector("#nav");
const rows = document.querySelector("#rows");
const certRows = document.querySelector("#cert-rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const certForm = document.querySelector("#cert-form");
const certSelect = document.querySelector("#cert");
const certHint = document.querySelector("#cert-hint");
const pageReadings = document.querySelector("#page-readings");
const pageCerts = document.querySelector("#page-certs");
const navReadings = document.querySelector("#nav-readings");
const navCerts = document.querySelector("#nav-certs");

const isWriter = () => role === "writer";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]),
  );
}

function paintReadings(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${esc(r.site)}</td><td>${esc(r.ch4_pct)}</td>` +
        `<td class="${r.level === "报警" ? "alarm" : "ok"}">${esc(r.level)}</td>` +
        `<td>${esc(r.note)}</td><td>${esc(r.instrument_no || "—")}</td></tr>`,
    )
    .join("");
}

function paintCerts() {
  const controls = isWriter();
  certRows.innerHTML = certs
    .map((c) => {
      const canAct = controls && !c.revoked;
      const actCell = canAct
        ? `<input type="date" class="edit-expires" data-id="${c.id}" value="${esc(c.expires_on)}" />
           <button class="save-expires" data-id="${c.id}">保存</button>
           <button class="revoke" data-id="${c.id}">作废</button>`
        : c.revoked
          ? "已作废"
          : "";
      return `<tr><td>${esc(c.instrument_no)}</td><td>${esc(c.expires_on)}</td>` +
        `<td class="state-${esc(c.state)}">${esc(c.state)}</td><td>${actCell}</td></tr>`;
    })
    .join("");
}

function paintCertSelect() {
  // 下拉只列未作废的证；过期证保留并标注，交由服务端拒绝并写明过期原因
  const usable = certs.filter((c) => !c.revoked);
  const valid = usable.filter((c) => c.state === "有效");
  certSelect.innerHTML =
    `<option value="">选择校准证</option>` +
    usable
      .map((c) => {
        const tag = c.state === "有效" ? `截止 ${esc(c.expires_on)}` : `已过期（截止 ${esc(c.expires_on)}）`;
        return `<option value="${c.id}">${esc(c.instrument_no)}（${tag}）</option>`;
      })
      .join("");
  certSelect.hidden = !isWriter();
  if (!isWriter()) return;
  if (valid.length === 0) {
    certHint.textContent = "没有未作废且未过期的校准证，上报会被拒绝；可到「校准证」页新建证。";
  } else {
    certHint.textContent = "";
  }
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function showPage(page) {
  const onCerts = page === "certs";
  pageReadings.hidden = onCerts;
  pageCerts.hidden = !onCerts;
  navReadings.classList.toggle("active", !onCerts);
  navCerts.classList.toggle("active", onCerts);
}

function showApp() {
  loginBox.hidden = true;
  appBox.hidden = false;
  nav.hidden = false;
  document.querySelector("#who").textContent = isWriter() ? "检查员" : "旁观（只读）";
  document.querySelector("#out").hidden = false;
  form.hidden = !isWriter();
  certForm.hidden = !isWriter();
  showPage("readings");
  connect();
  loadReadings();
  loadCerts();
}

async function loadReadings() {
  paintReadings(await api("/api/readings"));
}

async function loadCerts() {
  certs = await api("/api/certificates");
  paintCerts();
  paintCertSelect();
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}（仪器 ${row.instrument_no}）`;
    loadReadings();
  };
}

document.querySelector("#go").onclick = async () => {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({
      username: document.querySelector("#user").value,
      password: document.querySelector("#pass").value,
    }),
  });
  token = data.access_token;
  role = data.role;
  localStorage.setItem(tokenKey, token);
  localStorage.setItem("methane_role", role);
  showApp();
};

form.onsubmit = async (e) => {
  e.preventDefault();
  live.textContent = "";
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
        certificate_id: Number(certSelect.value) || 0,
      }),
    });
  } catch (err) {
    live.textContent = "⛔ " + err.message;
  }
};

certForm.onsubmit = async (e) => {
  e.preventDefault();
  const instrument = document.querySelector("#new-instrument").value.trim();
  const expires = document.querySelector("#new-expires").value;
  if (!instrument || !expires) {
    alert("仪器编号和截止日都要填");
    return;
  }
  await api("/api/certificates", {
    method: "POST",
    body: JSON.stringify({ instrument_no: instrument, expires_on: expires }),
  });
  document.querySelector("#new-instrument").value = "";
  document.querySelector("#new-expires").value = "";
  await loadCerts();
};

certRows.addEventListener("click", async (e) => {
  const id = Number(e.target.dataset.id);
  if (!id) return;
  if (e.target.classList.contains("revoke")) {
    if (!confirm("确认作废这张校准证？作废后不能再用来上报。")) return;
    await api(`/api/certificates/${id}/revoke`, { method: "POST" });
    await loadCerts();
  }
  if (e.target.classList.contains("save-expires")) {
    const input = certRows.querySelector(`.edit-expires[data-id="${id}"]`);
    if (!input.value) {
      alert("请选择新的截止日");
      return;
    }
    await api(`/api/certificates/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ expires_on: input.value }),
    });
    await loadCerts();
  }
});

navReadings.onclick = () => showPage("readings");
navCerts.onclick = () => showPage("certs");

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();

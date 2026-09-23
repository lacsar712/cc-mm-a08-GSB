const tokenKey = "methane_token";
let token = localStorage.getItem(tokenKey) || "";
let role = localStorage.getItem("methane_role") || "";
let certs = [];

const loginBox = document.querySelector("#login");
const appBox = document.querySelector("#app");
const certBox = document.querySelector("#certs");
const nav = document.querySelector("#nav");
const rows = document.querySelector("#rows");
const live = document.querySelector("#live");
const form = document.querySelector("#form");
const certRows = document.querySelector("#certRows");
const certForm = document.querySelector("#certForm");
const certMsg = document.querySelector("#certMsg");
const certHint = document.querySelector("#certHint");
const certSelect = document.querySelector("#certSelect");

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]),
  );
}

function paint(list) {
  rows.innerHTML = list
    .map(
      (r) =>
        `<tr><td>${esc(r.site)}</td><td>${esc(r.ch4_pct)}</td><td class="${r.level === "报警" ? "alarm" : "ok"}">${esc(r.level)}</td><td>${esc(r.note)}</td></tr>`,
    )
    .join("");
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

function showView(name) {
  const onReadings = name === "readings";
  appBox.hidden = !onReadings;
  certBox.hidden = onReadings;
  document.querySelector("#navReadings").classList.toggle("active", onReadings);
  document.querySelector("#navCerts").classList.toggle("active", !onReadings);
  if (onReadings) load();
  else loadCerts();
}

function showApp() {
  loginBox.hidden = true;
  nav.hidden = false;
  document.querySelector("#who").textContent = role === "writer" ? "检查员" : "查看";
  document.querySelector("#out").hidden = false;
  form.hidden = role !== "writer";
  certForm.hidden = role !== "writer";
  showView("readings");
  connect();
  loadCerts();
}

async function load() {
  paint(await api("/api/readings"));
}

function paintCerts() {
  certRows.innerHTML = certs
    .map((c) => {
      const cls = c.status === "有效" ? "valid" : "dead";
      if (role === "writer" && !c.revoked) {
        return `<tr>
          <td>${esc(c.instrument_no)}</td>
          <td><input type="date" value="${esc(c.valid_until)}" data-id="${c.id}" class="due" /></td>
          <td class="${cls}">${esc(c.status)}</td>
          <td>
            <button data-id="${c.id}" class="saveDue">改期</button>
            <button data-id="${c.id}" class="revoke">作废</button>
          </td>
        </tr>`;
      }
      return `<tr>
        <td>${esc(c.instrument_no)}</td>
        <td>${esc(c.valid_until)}</td>
        <td class="${cls}">${esc(c.status)}</td>
        <td></td>
      </tr>`;
    })
    .join("");

  const valid = certs.filter((c) => c.status === "有效");
  certSelect.innerHTML = valid.length
    ? valid
        .map((c) => `<option value="${c.id}">${esc(c.instrument_no)}（截止 ${esc(c.valid_until)}）</option>`)
        .join("")
    : `<option value="">无可用校准证</option>`;
  const missing = valid.length === 0 && role === "writer";
  certHint.hidden = !missing;
  if (missing) certHint.textContent = "没有未作废且未过期的校准证，请先到「校准证」新建一张，否则上报会被拒绝。";
}

async function loadCerts() {
  certs = await api("/api/certificates");
  paintCerts();
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/alerts`);
  ws.onmessage = (ev) => {
    const row = JSON.parse(ev.data);
    live.textContent = `刚推送：${row.site} ${row.level}`;
    load();
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
  try {
    await api("/api/readings", {
      method: "POST",
      body: JSON.stringify({
        site: document.querySelector("#site").value,
        ch4_pct: Number(document.querySelector("#ch4").value),
        certificate_id: Number(certSelect.value) || null,
      }),
    });
    live.textContent = "上报成功";
  } catch (err) {
    live.textContent = err.message;
  }
};

certForm.onsubmit = async (e) => {
  e.preventDefault();
  certMsg.textContent = "";
  try {
    await api("/api/certificates", {
      method: "POST",
      body: JSON.stringify({
        instrument_no: document.querySelector("#instrumentNo").value,
        valid_until: document.querySelector("#validUntil").value,
      }),
    });
    document.querySelector("#instrumentNo").value = "";
    document.querySelector("#validUntil").value = "";
    await loadCerts();
    certMsg.textContent = "已新建校准证";
  } catch (err) {
    certMsg.textContent = err.message;
  }
};

certRows.onclick = async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  certMsg.textContent = "";
  try {
    if (btn.classList.contains("revoke")) {
      await api(`/api/certificates/${id}/revoke`, { method: "POST" });
    } else if (btn.classList.contains("saveDue")) {
      const due = certRows.querySelector(`input.due[data-id="${id}"]`).value;
      await api(`/api/certificates/${id}`, { method: "PATCH", body: JSON.stringify({ valid_until: due }) });
    }
    await loadCerts();
  } catch (err) {
    certMsg.textContent = err.message;
  }
};

document.querySelector("#navReadings").onclick = () => showView("readings");
document.querySelector("#navCerts").onclick = () => showView("certs");

document.querySelector("#out").onclick = () => {
  localStorage.clear();
  location.reload();
};

if (token) showApp();

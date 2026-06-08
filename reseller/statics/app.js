const API = {
  async request(path, { method = "GET", body, auth = true } = {}) {
    const headers = { "Content-Type": "application/json" };
    if (auth) {
      const t = sessionStorage.getItem("reseller_access_token");
      if (t) headers["Authorization"] = `Bearer ${t}`;
    }
    const res = await fetch(path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = typeof data === "string" ? data : (data.detail || "Request failed");
      const err = new Error(msg);
      err.status = res.status;
      err.data = data;
      throw err;
    }
    return data;
  },
};

function money(x) {
  const n = Number(x || 0);
  return n.toFixed(2);
}

function requireAuth() {
  const t = sessionStorage.getItem("reseller_access_token");
  if (!t) window.location.href = "/reseller/login";
}

async function loadNav() {
  const el = document.getElementById("nav-email");
  if (!el) return;
  try {
    const me = await API.request("/api/v1/reseller/me");
    el.textContent = me.email;
  } catch {
    // ignore
  }
}

function logout() {
  sessionStorage.removeItem("reseller_access_token");
  sessionStorage.removeItem("reseller_refresh_token");
  window.location.href = "/reseller/login";
}

window.ResellerApp = { API, money, requireAuth, loadNav, logout };


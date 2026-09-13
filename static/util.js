// Shared helpers used by both app.js and tracked.js. Load this before them.

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

async function apiCall(url, options = {}) {
  const resp = await fetch(url, { ...options });
  if (!resp.ok) {
    const body = await resp.text();
    let msg;
    try { msg = JSON.parse(body).detail || body; } catch { msg = body; }
    throw new Error(msg);
  }
  return resp;
}

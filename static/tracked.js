(() => {
  'use strict';

  const POLL_INTERVAL = 30000;
  const API = {
    list:        '/api/tracked',
    delete:      (username) => `/api/tracked/${encodeURIComponent(username)}`,
    thumbnail:   (username) => `/api/thumbnail/${encodeURIComponent(username)}`,
    startDownload: (username) => `/api/download/start?username=${encodeURIComponent(username)}&output_format=mp4`,
  };

  const $ = (sel) => document.querySelector(sel);
  const listEl = $('#tracked-list');
  const emptyEl = $('#tracked-empty');
  const countEl = $('#tracked-count');
  const refreshBtn = $('#tracked-refresh');

  if (!listEl || !countEl) return;

  let pollTimer = null;
  let lastRendered = null;

  function escapeHtml(str) {
    return String(str ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function formatRelative(ts) {
    if (!ts) return 'never';
    const diff = Date.now() / 1000 - ts;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  function formatAbsolute(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleString();
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

  function statusPill(row) {
    if (row.downloading) return '<span class="tracked__pill tracked__pill--recording">RECORDING</span>';
    if (row.last_status === 'public') return '<span class="tracked__pill tracked__pill--online">LIVE</span>';
    if (row.last_status === 'offline' || row.last_status === 'away') return '<span class="tracked__pill tracked__pill--offline">OFFLINE</span>';
    if (row.last_status === 'private') return '<span class="tracked__pill tracked__pill--busy">PRIVATE</span>';
    if (!row.last_status) return '<span class="tracked__pill tracked__pill--unknown">UNKNOWN</span>';
    return `<span class="tracked__pill tracked__pill--unknown">${escapeHtml(row.last_status.toUpperCase())}</span>`;
  }

  function renderCard(row) {
    const isLive = row.last_status === 'public';
    const isDownloading = !!row.downloading;
    const lastSeen = row.last_seen_online_at
      ? `last live ${formatRelative(row.last_seen_online_at)}`
      : 'never seen live';
    const lastDl = row.last_downloaded_at
      ? `last download ${formatRelative(row.last_downloaded_at)}`
      : '';
    const downloadDisabled = isDownloading || !isLive;

    const profileUrl = `https://chaturbate.com/${encodeURIComponent(row.username)}/`;

    return `
      <article class="tracked-card${isLive ? ' tracked-card--live' : ''}" data-username="${escapeHtml(row.username)}">
        <div class="tracked-card__thumb">
          <a class="tracked-card__thumb-link" href="${escapeHtml(profileUrl)}" target="_blank" rel="noopener noreferrer">
            <img loading="lazy"
                 alt="${escapeHtml(row.username)} thumbnail"
                 src="${API.thumbnail(row.username)}"
                 onerror="this.classList.add('tracked-card__thumb--missing'); this.removeAttribute('src');">
          </a>
          <div class="tracked-card__pill-wrap">${statusPill(row)}</div>
        </div>
        <div class="tracked-card__body">
          <div class="tracked-card__title">
            <a class="tracked-card__name" href="${escapeHtml(profileUrl)}" target="_blank" rel="noopener noreferrer">@${escapeHtml(row.username)}</a>
            <span class="tracked-card__count" title="Times downloaded">${row.download_count || 1}×</span>
          </div>
          <div class="tracked-card__meta" title="${escapeHtml(formatAbsolute(row.last_seen_online_at))}">${escapeHtml(lastSeen)}</div>
          ${lastDl ? `<div class="tracked-card__meta">${escapeHtml(lastDl)}</div>` : ''}
          <div class="tracked-card__actions">
            <button type="button"
                    class="tracked-card__btn tracked-card__btn--primary"
                    data-action="download"
                    ${downloadDisabled ? 'disabled' : ''}>
              ${isDownloading ? 'Recording…' : (isLive ? 'Download' : 'Offline')}
            </button>
            <button type="button"
                    class="tracked-card__btn tracked-card__btn--ghost"
                    data-action="delete"
                    title="Remove from tracked list">×</button>
          </div>
        </div>
      </article>
    `;
  }

  function render(rows) {
    const sig = JSON.stringify(rows.map((r) => [
      r.username, r.last_status, r.last_seen_online_at, r.downloading, r.download_count,
    ]));
    if (sig === lastRendered) return;
    lastRendered = sig;

    countEl.textContent = String(rows.length);
    if (rows.length === 0) {
      emptyEl.hidden = false;
      const cards = listEl.querySelectorAll('.tracked-card');
      cards.forEach((c) => c.remove());
      return;
    }
    emptyEl.hidden = true;
    listEl.innerHTML = rows.map(renderCard).join('');
  }

  async function fetchTracked() {
    try {
      const resp = await apiCall(API.list);
      const data = await resp.json();
      render(data.tracked || []);
    } catch (err) {
      console.warn('[tracked] fetch failed:', err.message);
    }
  }

  async function startDownload(username) {
    const card = listEl.querySelector(`.tracked-card[data-username="${CSS.escape(username)}"]`);
    const btn = card?.querySelector('[data-action="download"]');
    if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
    try {
      await apiCall(API.startDownload(username), { method: 'POST' });
      window.dispatchEvent(new CustomEvent('tracked:download-started', { detail: { username } }));
      fetchTracked();
    } catch (err) {
      alert(`Failed to start: ${err.message}`);
      if (btn) { btn.disabled = false; btn.textContent = 'Download'; }
    }
  }

  async function deleteTracked(username) {
    if (!confirm(`Remove @${username} from tracked list?`)) return;
    try {
      await apiCall(API.delete(username), { method: 'DELETE' });
      fetchTracked();
    } catch (err) {
      alert(`Failed to delete: ${err.message}`);
    }
  }

  listEl.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-action]');
    if (!btn) return;
    const card = btn.closest('.tracked-card');
    const username = card?.dataset.username;
    if (!username) return;
    if (btn.dataset.action === 'download') startDownload(username);
    else if (btn.dataset.action === 'delete') deleteTracked(username);
  });

  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      lastRendered = null;
      fetchTracked();
    });
  }

  fetchTracked();
  pollTimer = setInterval(fetchTracked, POLL_INTERVAL);

  window.addEventListener('beforeunload', () => {
    if (pollTimer) clearInterval(pollTimer);
  });
})();

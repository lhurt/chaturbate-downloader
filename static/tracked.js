(() => {
  'use strict';

  const POLL_INTERVAL = 30000;
  const API = {
    list:        '/api/tracked',
    add:         (username) => `/api/tracked?username=${encodeURIComponent(username)}`,
    delete:      (username) => `/api/tracked/${encodeURIComponent(username)}`,
    thumbnail:   (username) => `/api/thumbnail/${encodeURIComponent(username)}`,
    startDownload: (username) => `/api/download/start?username=${encodeURIComponent(username)}&output_format=mp4`,
    autoDownload: (username, enabled) => `/api/tracked/${encodeURIComponent(username)}/auto-download?enabled=${enabled}`,
  };

  const $ = (sel) => document.querySelector(sel);
  const listEl = $('#tracked-list');
  const emptyEl = $('#tracked-empty');
  const countEl = $('#tracked-count');
  const refreshBtn = $('#tracked-refresh');
  const addForm = $('#tracked-add-form');
  const addInput = $('#tracked-add-username');
  const addBtn = $('#tracked-add-btn');
  const viewToggle = $('#tracked-view-toggle');
  const listHeadEl = $('#tracked-list-head');

  if (!listEl || !countEl) return;

  let pollTimer = null;
  let lastRendered = null;
  let lastRows = [];
  let currentView = 'grid';

  const VIEW_STORAGE_KEY = 'tracked:view';

  function loadView() {
    try {
      return localStorage.getItem(VIEW_STORAGE_KEY) === 'compact' ? 'compact' : 'grid';
    } catch {
      return 'grid';
    }
  }

  function applyView(view) {
    currentView = view;
    listEl.classList.toggle('tracked-grid--rows', view === 'compact');
    if (listHeadEl) listHeadEl.hidden = view !== 'compact';
    if (viewToggle) {
      viewToggle.querySelectorAll('[data-view-set]').forEach((btn) => {
        const active = btn.dataset.viewSet === view;
        btn.classList.toggle('theme-toggle__btn--active', active);
        btn.setAttribute('aria-pressed', String(active));
      });
    }
  }

  function setView(view) {
    applyView(view);
    try { localStorage.setItem(VIEW_STORAGE_KEY, view); } catch {}
    lastRendered = null;
    render(lastRows);
  }

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
    const lastDl = row.download_count > 0
      ? `last download ${formatRelative(row.last_downloaded_at)}`
      : 'never downloaded';
    const downloadDisabled = isDownloading || !isLive;

    const profileUrl = `https://chaturbate.com/${encodeURIComponent(row.username)}/`;

    return `
      <article class="tracked-card${isLive || isDownloading ? ' tracked-card--live' : ''}" data-username="${escapeHtml(row.username)}">
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
            <span class="tracked-card__count" title="Times downloaded">${row.download_count || 0}×</span>
          </div>
          <div class="tracked-card__meta" title="${escapeHtml(formatAbsolute(row.last_seen_online_at))}">${escapeHtml(lastSeen)}</div>
          ${lastDl ? `<div class="tracked-card__meta">${escapeHtml(lastDl)}</div>` : ''}
          <label class="tracked-card__auto" title="Start recording automatically whenever this streamer is live">
            <span class="tracked-card__auto-label">Auto record</span>
            <input type="checkbox"
                   data-auto-download
                   ${row.auto_download ? 'checked' : ''}>
            <span class="tracked-card__switch" aria-hidden="true"><span></span></span>
          </label>
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

  function renderRow(row) {
    const isLive = row.last_status === 'public';
    const isDownloading = !!row.downloading;
    const lastSeen = row.last_seen_online_at ? formatRelative(row.last_seen_online_at) : 'never';
    const downloadDisabled = isDownloading || !isLive;
    const profileUrl = `https://chaturbate.com/${encodeURIComponent(row.username)}/`;

    return `
      <div class="tracked-row${isLive || isDownloading ? ' tracked-row--live' : ''}" data-username="${escapeHtml(row.username)}">
        <a class="tracked-row__thumb" href="${escapeHtml(profileUrl)}" target="_blank" rel="noopener noreferrer" tabindex="-1">
          <img loading="lazy"
               alt=""
               src="${API.thumbnail(row.username)}"
               onerror="this.classList.add('tracked-row__thumb-img--missing'); this.removeAttribute('src');">
        </a>
        <a class="tracked-row__name" href="${escapeHtml(profileUrl)}" target="_blank" rel="noopener noreferrer">
          <span>@${escapeHtml(row.username)}</span>
          <span class="tracked-row__count" title="Times downloaded">${row.download_count || 0}×</span>
        </a>
        <span class="tracked-row__status">${statusPill(row)}</span>
        <span class="tracked-row__seen" title="${escapeHtml(formatAbsolute(row.last_seen_online_at))}">${escapeHtml(lastSeen)}</span>
        <label class="tracked-row__auto" title="Start recording automatically whenever this streamer is live">
          <input type="checkbox"
                 data-auto-download
                 ${row.auto_download ? 'checked' : ''}>
          <span class="tracked-card__switch" aria-hidden="true"><span></span></span>
        </label>
        <div class="tracked-row__actions">
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
    `;
  }

  function render(rows) {
    lastRows = rows;
    const sig = JSON.stringify([currentView, rows.map((r) => [
      r.username, r.last_status, r.last_seen_online_at, r.downloading, r.download_count,
      r.auto_download,
    ])]);
    if (sig === lastRendered) return;
    lastRendered = sig;

    countEl.textContent = String(rows.length);
    if (rows.length === 0) {
      emptyEl.hidden = false;
      const cards = listEl.querySelectorAll('[data-username]');
      cards.forEach((c) => c.remove());
      return;
    }
    emptyEl.hidden = true;
    const renderFn = currentView === 'compact' ? renderRow : renderCard;
    listEl.innerHTML = rows.map(renderFn).join('');
  }

  async function fetchTracked() {
    try {
      const resp = await apiCall(API.list);
      const data = await resp.json();
      render(data.tracked || []);
      return true;
    } catch (err) {
      console.warn('[tracked] fetch failed:', err.message);
      return false;
    }
  }

  async function startDownload(username) {
    const card = listEl.querySelector(`[data-username="${CSS.escape(username)}"]`);
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

  async function addTracked(username) {
    if (addBtn) { addBtn.disabled = true; addBtn.textContent = 'Adding…'; }
    try {
      await apiCall(API.add(username), { method: 'POST' });
      if (addInput) addInput.value = '';
      lastRendered = null;
      fetchTracked();
    } catch (err) {
      alert(`Failed to add: ${err.message}`);
    } finally {
      if (addBtn) { addBtn.disabled = false; addBtn.textContent = 'Add'; }
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

  async function updateAutoDownload(username, input) {
    const enabled = input.checked;
    const label = input.closest('.tracked-card__auto')?.querySelector('.tracked-card__auto-label');
    if (label) label.textContent = 'Saving…';
    input.disabled = true;
    try {
      await apiCall(API.autoDownload(username, enabled), { method: 'PATCH' });
      lastRendered = null;
      const refreshed = await fetchTracked();
      if (!refreshed) {
        input.disabled = false;
        if (label) label.textContent = 'Auto record';
      }
    } catch (err) {
      input.checked = !enabled;
      input.disabled = false;
      if (label) label.textContent = 'Auto record';
      alert(`Failed to update auto record: ${err.message}`);
    }
  }

  listEl.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-action]');
    if (!btn) return;
    const card = btn.closest('[data-username]');
    const username = card?.dataset.username;
    if (!username) return;
    if (btn.dataset.action === 'download') startDownload(username);
    else if (btn.dataset.action === 'delete') deleteTracked(username);
  });

  listEl.addEventListener('change', (ev) => {
    const input = ev.target.closest('[data-auto-download]');
    if (!input) return;
    const username = input.closest('[data-username]')?.dataset.username;
    if (username) updateAutoDownload(username, input);
  });

  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => {
      lastRendered = null;
      fetchTracked();
    });
  }

  if (addForm) {
    addForm.addEventListener('submit', (ev) => {
      ev.preventDefault();
      const username = addInput?.value.trim().toLowerCase();
      if (username) addTracked(username);
    });
  }

  if (viewToggle) {
    viewToggle.addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-view-set]');
      if (btn) setView(btn.dataset.viewSet);
    });
  }
  applyView(loadView());

  fetchTracked();
  pollTimer = setInterval(fetchTracked, POLL_INTERVAL);

  window.addEventListener('beforeunload', () => {
    if (pollTimer) clearInterval(pollTimer);
  });
})();

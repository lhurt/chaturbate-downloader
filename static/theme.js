(() => {
  'use strict';

  const THEME_KEY = 'theme';
  const THEME_SELECTOR = '[data-theme-set]';
  const ACTIVE_CLASS = 'theme-toggle__btn--active';

  const $ = (sel) => document.querySelectorAll(sel);

  let currentMode = 'auto';
  let mediaQuery = null;

  function readStoredTheme() {
    try {
      const stored = localStorage.getItem(THEME_KEY);
      return stored === 'light' || stored === 'dark' ? stored : 'auto';
    } catch {
      return 'auto';
    }
  }

  function getInitialMode() {
    const fromDom = document.documentElement.dataset.themeMode;
    if (fromDom === 'light' || fromDom === 'dark' || fromDom === 'auto') return fromDom;
    return readStoredTheme();
  }

  function setThemeAttributes(mode) {
    const root = document.documentElement;
    if (mode === 'auto') {
      root.removeAttribute('data-theme');
      root.setAttribute('data-theme-mode', 'auto');
      return;
    }

    root.setAttribute('data-theme', mode);
    root.setAttribute('data-theme-mode', mode);
  }

  function syncButtons(mode) {
    const buttons = $(THEME_SELECTOR);
    buttons.forEach((button) => {
      const isActive = button.dataset.themeSet === mode;
      button.classList.toggle(ACTIVE_CLASS, isActive);
      button.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    });
  }

  function persistTheme(mode) {
    try {
      if (mode === 'auto') {
        localStorage.removeItem(THEME_KEY);
      } else {
        localStorage.setItem(THEME_KEY, mode);
      }
    } catch {}
  }

  function applyMode(mode, persist = false) {
    currentMode = mode;
    if (persist) persistTheme(mode);
    setThemeAttributes(mode);
    syncButtons(mode);
  }

  function handleSystemThemeChange() {
    if (currentMode !== 'auto') return;
    setThemeAttributes('auto');
    syncButtons('auto');
  }

  function init() {
    const buttons = $(THEME_SELECTOR);
    if (!buttons.length) return;

    currentMode = getInitialMode();
    applyMode(currentMode, false);

    buttons.forEach((button) => {
      button.addEventListener('click', () => {
        applyMode(button.dataset.themeSet === 'light' || button.dataset.themeSet === 'dark' ? button.dataset.themeSet : 'auto', true);
      });
    });

    mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    if (mediaQuery && typeof mediaQuery.addEventListener === 'function') {
      mediaQuery.addEventListener('change', handleSystemThemeChange);
    } else if (mediaQuery && typeof mediaQuery.addListener === 'function') {
      mediaQuery.addListener(handleSystemThemeChange);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();

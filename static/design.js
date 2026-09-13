(() => {
  'use strict';

  const DESIGN_KEY = 'design';
  const DESIGN_SELECTOR = '[data-design-set]';
  const ACTIVE_CLASS = 'design-toggle__btn--active';

  const $ = (sel) => document.querySelectorAll(sel);

  function readStoredDesign() {
    try {
      const stored = localStorage.getItem(DESIGN_KEY);
      return stored === 'classic' ? 'classic' : 'modern';
    } catch {
      return 'modern';
    }
  }

  function getInitialDesign() {
    const fromDom = document.documentElement.dataset.design;
    if (fromDom === 'classic' || fromDom === 'modern') return fromDom;
    return readStoredDesign();
  }

  function setDesignAttribute(design) {
    document.documentElement.setAttribute('data-design', design);
  }

  function syncButtons(design) {
    $(DESIGN_SELECTOR).forEach((button) => {
      const isActive = button.dataset.designSet === design;
      button.classList.toggle(ACTIVE_CLASS, isActive);
      button.setAttribute('aria-pressed', isActive ? 'true' : 'false');
    });
  }

  function persistDesign(design) {
    try {
      localStorage.setItem(DESIGN_KEY, design);
    } catch {}
  }

  function applyDesign(design, persist = false) {
    setDesignAttribute(design);
    syncButtons(design);
    if (persist) persistDesign(design);
  }

  function init() {
    const buttons = $(DESIGN_SELECTOR);
    if (!buttons.length) return;

    applyDesign(getInitialDesign(), false);

    buttons.forEach((button) => {
      button.addEventListener('click', () => {
        applyDesign(button.dataset.designSet === 'classic' ? 'classic' : 'modern', true);
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init, { once: true });
  } else {
    init();
  }
})();

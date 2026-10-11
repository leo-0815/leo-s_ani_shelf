/* Reuse one search input: no mirrored values, scroll polling, or extra requests. */
(() => {
  const node = id => document.getElementById(id);
  const searchable = new Set(['library', 'wishlist', 'collection', 'series']);
  let getView, switchView, ready;
  function mount(view) {
    const docked = view === 'library';
    const target = node(docked ? 'catalogSearchSlot' : 'headerSearchSlot');
    const input = node('searchBox');
    if (input.parentElement !== target) target.appendChild(input);
    node('catalogSearchTools').classList.toggle('hidden', !searchable.has(view));
    node('catalogSearchTools').classList.toggle('is-search-docked', docked);
    node('catalogSearchRow').classList.toggle('hidden', !docked);
  }
  function focusSearch() {
    const view = getView();
    if (!searchable.has(view) && view !== 'home') switchView('library');
    const input = node(view === 'home' ? 'homeSearchInput' : 'searchInput');
    const rect = input.getBoundingClientRect();
    if (rect.top < 0 || rect.bottom > window.innerHeight) input.scrollIntoView({block: 'center', behavior: 'auto'});
    input.focus({preventScroll: true});
  }
  function showFilters() {
    if (getView() !== 'library') return;
    const toolbar = node('catalogToolbar');
    const dock = node('catalogSearchTools');
    const offset = getComputedStyle(dock).position === 'sticky' ? dock.getBoundingClientRect().height + 24 : 12;
    const top = Math.max(0, window.scrollY + toolbar.getBoundingClientRect().top - offset);
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({top, behavior: reduced ? 'auto' : 'smooth'});
    node('typeFilter').focus({preventScroll: true});
  }
  function init(config) {
    ({getView, switchView, ready} = config);
    node('showCatalogFilters').addEventListener('click', showFilters);
    document.addEventListener('keydown', event => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.isComposing || event.key.toLowerCase() !== 'k') return;
      // Do not navigate away from a modal or intercept login input.
      if (!ready() || document.querySelector('dialog[open]')) return;
      event.preventDefault();
      focusSearch();
    });
  }
  window.AniShelfSearchNavigation = {mount, init};
})();

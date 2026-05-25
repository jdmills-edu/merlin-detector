/* Live grid mode.
 *
 * The journal's own "Live · X species" status text in the masthead is the
 * mode toggle — tap it to flip between the rotating journal and a 2x4
 * grid of the most recent detections. The grid is fed by an SSE
 * connection to /api/v2/events. Tiles fall off after TILE_TTL_MS so the
 * grid reflects what's actually calling now.
 *
 * Reuses globals from bird-dashboard.html:
 *   - enrich(bird)        → Wikipedia image + blurb (cached in infoCache)
 *   - showBird(bird)      → bind a bird to the journal stage + detail page
 *   - showDetail()        → slide to the detail page
 */
(function(){
  const VIEW_KEY    = "merlin.view";   // "journal" | "grid"
  const MAX_TILES   = 8;
  const TILE_TTL_MS = 60 * 1000;       // how long a detection stays on screen
  const FADE_MS     = 1400;            // tail of the TTL spent fading out
  const SWEEP_MS    = 500;             // how often we re-check expiry

  let view  = localStorage.getItem(VIEW_KEY) || "journal";
  let tiles = [];   // newest-first
  let evtSrc = null;
  let sweepTimer = null;

  /* ---- DOM scaffolding ---- */
  const gridRoot = document.createElement("div");
  gridRoot.id = "liveGrid";
  gridRoot.className = "live-grid";
  gridRoot.addEventListener("click", e => e.stopPropagation());

  function mount(){
    document.body.appendChild(gridRoot);

    // The masthead status block is our toggle. Make it click-able and
    // wire it up here so the user has one obvious affordance.
    const statusBlock = document.querySelector(".masthead .status");
    if(statusBlock){
      statusBlock.classList.add("status-toggle");
      statusBlock.addEventListener("click", e => {
        e.stopPropagation();
        setView(view === "grid" ? "journal" : "grid");
      });
    }

    // Mirror the journal's own detail-open state so the grid steps aside
    // while the detail page is showing and returns when it closes.
    const pages = document.getElementById("pages");
    if(pages){
      new MutationObserver(() => {
        document.body.classList.toggle(
          "detail-open",
          pages.classList.contains("show-detail")
        );
      }).observe(pages, {attributes: true, attributeFilter: ["class"]});
    }

    // SSE stays open in both views: in journal mode it still drives the
    // status-dot pop animation, and in grid mode it also fills the tiles.
    openSSE();

    if(view === "grid") setView("grid");
  }

  /* Brief "pop" of the masthead status dot on every detection. Uses the
   * Web Animations API so it's immune to the dashboard's setStatus() call
   * that periodically rewrites dot.className. */
  function popDot(){
    const dot = document.getElementById("dot");
    if(!dot || typeof dot.animate !== "function") return;
    const blue = "#29b6f6";
    const peak = {
      transform: "scale(2.2)",
      background: blue,
      boxShadow: "0 0 16px 5px rgba(41, 182, 246, .55)"
    };
    dot.animate([
      { transform: "scale(1)", offset: 0 },
      { ...peak, offset: 0.2 },    // snap up
      { ...peak, offset: 0.4 },    // brief hold
      { transform: "scale(1)", offset: 1 }
    ], { duration: 1200, easing: "ease-out" });
  }

  /* ---- view switching ---- */
  function setView(next){
    view = next;
    localStorage.setItem(VIEW_KEY, view);
    if(view === "grid"){
      document.body.classList.add("view-grid");
      primeGrid();
      startSweep();
      renderGrid();
    } else {
      document.body.classList.remove("view-grid");
      stopSweep();
    }
  }

  /* ---- tile data ---- */
  function tileFromDetection(d, addedAt){
    const common     = d.common_name || d.commonName || "Unknown";
    const scientific = d.scientific_name || d.scientificName || "";
    const mic        = d.mic_name || d.micName || "";
    const key        = (scientific || common).toLowerCase().trim();
    const latest     = d.date || d.ts_utc || new Date().toISOString();
    return {
      key, common, scientific, latest,
      mics: mic ? [mic] : [],   // most-recent first
      addedAt: addedAt || Date.now(),
      imageUrl: null,
      _bird: {
        common, scientific,
        count: 0, hourly: null, highConf: true,
        latest, first: "",
        newYear: false, newSeason: false,
        thumb: null,
        key
      }
    };
  }

  /* Merge a new detection of the same species into an existing tile:
   * promote the firing mic to the front of the mic list and reset its
   * TTL. Returns true if `t` was merged into `existing`. */
  function mergeMics(existing, t){
    const mic = t.mics[0];
    if(mic){
      const lower = mic.toLowerCase();
      existing.mics = [mic, ...existing.mics.filter(m => m.toLowerCase() !== lower)];
    }
    existing.latest = t.latest;
    existing.addedAt = t.addedAt;
    existing.expiring = false;
  }

  function addDetection(d){
    const t = tileFromDetection(d);
    const existing = tiles.find(x => x.key === t.key);
    if(existing){
      mergeMics(existing, t);
      tiles = [existing, ...tiles.filter(x => x !== existing)];
    } else {
      tiles.unshift(t);
      if(tiles.length > MAX_TILES) tiles.length = MAX_TILES;
      fetchImage(t);
    }
    renderGrid();
  }

  /* Fade an existing tile element out via Web Animations API so the
   * effect survives re-renders (we re-apply with the remaining duration
   * after any renderGrid call). */
  function fadeOutTile(el, duration){
    if(!el || typeof el.animate !== "function") return;
    el.style.pointerEvents = "none";
    el.animate([
      { opacity: 1, transform: "scale(1)" },
      { opacity: 0, transform: "scale(.88)" }
    ], { duration: duration || FADE_MS, easing: "ease-out", fill: "forwards" });
  }

  function sweepExpired(){
    const now = Date.now();

    // 1. Kick off the fade for tiles that just crossed into the fade window.
    //    Marking is done in-place on the data so a subsequent renderGrid
    //    can resume the animation on the new DOM element.
    for(let i = 0; i < tiles.length; i++){
      const t = tiles[i];
      if(!t.expiring && now - t.addedAt >= TILE_TTL_MS - FADE_MS){
        t.expiring = true;
        const el = gridRoot.querySelector('.tile[data-i="' + i + '"]');
        if(el) fadeOutTile(el);
      }
    }

    // 2. Drop fully-expired tiles and re-render if any went.
    const before = tiles.length;
    tiles = tiles.filter(t => now - t.addedAt < TILE_TTL_MS);
    if(tiles.length !== before) renderGrid();
  }

  function startSweep(){
    if(sweepTimer) return;
    sweepTimer = setInterval(sweepExpired, SWEEP_MS);
  }
  function stopSweep(){
    if(sweepTimer){ clearInterval(sweepTimer); sweepTimer = null; }
  }

  async function primeGrid(){
    if(tiles.length) return;
    try{
      const r = await fetch("/api/v2/detections?numResults=" + (MAX_TILES * 4));
      const list = await r.json();
      const cutoff = Date.now() - TILE_TTL_MS;
      const bySpecies = new Map();
      for(const d of list){
        const ts = Date.parse(d.date || d.ts_utc || "") || 0;
        if(ts < cutoff) continue;
        const t = tileFromDetection(d, ts);
        const existing = bySpecies.get(t.key);
        if(existing){
          // detections come newest-first, so existing is more recent — just
          // append this older firing's mic to the tail.
          const mic = t.mics[0];
          if(mic && !existing.mics.some(m => m.toLowerCase() === mic.toLowerCase())){
            existing.mics.push(mic);
          }
          continue;
        }
        if(bySpecies.size >= MAX_TILES) continue;
        bySpecies.set(t.key, t);
        tiles.push(t);
      }
      tiles.forEach(fetchImage);
      renderGrid();
    }catch(e){ /* will fill on next SSE event */ }
  }

  async function fetchImage(tile){
    if(tile.imageUrl) return;
    if(typeof enrich !== "function") return;
    try{
      const info = await enrich(tile._bird);
      tile.imageUrl = (info && info.imageUrl) || null;
      renderGrid();
    }catch(e){ /* leave placeholder */ }
  }

  /* ---- render ---- */
  function escapeHtml(s){
    return String(s).replace(/[&<>"]/g, c =>
      ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  }

  function renderGrid(){
    if(view !== "grid") return;

    // Empty-state: a single centered "Listening…" headline, no faint circles.
    if(tiles.length === 0){
      gridRoot.classList.add("listening");
      gridRoot.innerHTML = '<div class="live-empty">Listening\u2026</div>';
      return;
    }

    gridRoot.classList.remove("listening");
    const parts = [];
    for(let i = 0; i < MAX_TILES; i++){
      const t = tiles[i];
      if(!t){
        // empty slots keep their grid cell for layout but render nothing
        parts.push('<div class="tile empty"></div>');
        continue;
      }
      const micLabel = (t.mics && t.mics.length) ? t.mics.join(" \u00b7 ") : "";
      parts.push(
        '<div class="tile" data-i="' + i + '">' +
          '<div class="tile-img' + (t.imageUrl ? '' : ' placeholder') + '"></div>' +
          '<div class="tile-name">' + escapeHtml(t.common) + '</div>' +
          (micLabel ? '<div class="tile-mic">' + escapeHtml(micLabel) + '</div>' : '') +
        '</div>'
      );
    }
    gridRoot.innerHTML = parts.join("");
    const now = Date.now();
    gridRoot.querySelectorAll(".tile[data-i]").forEach(el => {
      const i = Number(el.dataset.i);
      const t = tiles[i];
      if(!t) return;
      if(t.imageUrl){
        el.querySelector(".tile-img").style.backgroundImage =
          "url(" + JSON.stringify(t.imageUrl) + ")";
      }
      el.addEventListener("click", ev => {
        ev.stopPropagation();
        openDetail(t);
      });
      // If this tile is already mid-fade, resume from where we are so a
      // mid-fade re-render (from a new SSE event) doesn't reset opacity.
      if(t.expiring){
        const remaining = Math.max(0, TILE_TTL_MS - (now - t.addedAt));
        fadeOutTile(el, remaining);
      }
    });
  }

  async function openDetail(tile){
    if(!tile) return;
    if(typeof showBird !== "function" || typeof showDetail !== "function") return;
    try{
      await showBird(tile._bird);
      showDetail();
    }catch(e){ /* swallow — journal will recover on its own */ }
  }

  /* ---- SSE ---- */
  function openSSE(){
    if(evtSrc) return;
    evtSrc = new EventSource("/api/v2/events");
    evtSrc.addEventListener("detection", ev => {
      popDot();
      try{ addDetection(JSON.parse(ev.data)); }catch(e){}
    });
    // EventSource reconnects automatically on error.
  }

  if(document.readyState === "loading"){
    document.addEventListener("DOMContentLoaded", mount);
  } else {
    mount();
  }
})();

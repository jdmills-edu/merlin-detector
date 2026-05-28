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

    // Inject the "play call" buttons on both the journal and detail pages.
    // The journal rotates birds on a timer, so we also watch #commonName
    // for changes and stop playback if the species swaps mid-clip.
    setupCallButtons();

    // SSE stays open in both views: in journal mode it still drives the
    // status-dot pop animation, and in grid mode it also fills the tiles.
    openSSE();

    if(view === "grid") setView("grid");
  }

  /* ---- bird-call playback (xeno-canto via /api/v2/call) ----
   *
   * No local clips yet, so the "Play call" buttons fetch a reference
   * recording from xeno-canto through the shim. Lookups are cached per
   * species both server- and client-side. We use our own <audio> element
   * (not the dashboard's #birdAudio) because the upstream code pauses
   * #birdAudio when the detail page opens — which is exactly when the
   * user is most likely to hit play.
   */
  const callCache    = new Map();   // sciKey → {url, recordist, license, page} | null
  let   callAudio    = null;
  let   callPlayingKey = null;
  const callButtons  = new Set();   // every .call-btn we render, for state sync

  function sciKey(scientific, common){
    return (scientific || common || "").toLowerCase().trim();
  }

  function getCallAudio(){
    if(callAudio) return callAudio;
    callAudio = document.createElement("audio");
    callAudio.id = "merlinCallAudio";
    callAudio.preload = "none";
    callAudio.crossOrigin = "anonymous";
    const clear = () => setCallPlayingKey(null);
    callAudio.addEventListener("ended", clear);
    callAudio.addEventListener("error", clear);
    callAudio.addEventListener("pause", () => {
      // "pause" fires on natural end too; ignore unless we genuinely stopped.
      if(callAudio.ended || callAudio.currentTime === 0) clear();
    });
    document.body.appendChild(callAudio);
    return callAudio;
  }

  function setCallPlayingKey(key){
    callPlayingKey = key;
    callButtons.forEach(b => {
      const matches = key && b.dataset.callKey === key;
      b.classList.toggle("playing", !!matches);
    });
    // .loading / .missing are owned by playCall — don't touch them here, or
    // we'd wipe the spinner off a button mid-fetch when the previous clip
    // gets paused to make room.
  }

  async function lookupCall(scientific, common){
    const key = sciKey(scientific, common);
    if(!key) return null;
    if(callCache.has(key)) return callCache.get(key);
    // xeno-canto matches on Latin binomial; without one we can't reliably
    // find a recording, so don't even ask.
    if(!scientific){ callCache.set(key, null); return null; }
    try{
      const r = await fetch("/api/v2/call?scientific=" + encodeURIComponent(scientific));
      if(!r.ok){ callCache.set(key, null); return null; }
      const j = await r.json();
      callCache.set(key, j);
      return j;
    } catch(e){
      callCache.set(key, null);
      return null;
    }
  }

  async function playCall(scientific, common, btn){
    const key = sciKey(scientific, common);
    if(!key) return;
    const el = getCallAudio();
    if(btn) btn.dataset.callKey = key;

    // Toggle: pressing play on whatever's already playing stops it.
    if(callPlayingKey === key && !el.paused){
      el.pause();
      el.currentTime = 0;
      setCallPlayingKey(null);
      return;
    }

    el.pause();
    el.currentTime = 0;
    if(btn){ btn.classList.add("loading"); btn.classList.remove("missing"); }
    const info = await lookupCall(scientific, common);
    if(btn) btn.classList.remove("loading");

    if(!info || !info.url){
      if(btn){
        btn.classList.add("missing");
        setTimeout(() => btn.classList.remove("missing"), 2000);
      }
      return;
    }

    el.src = info.url;
    try{
      await el.play();
      setCallPlayingKey(key);
    } catch(e){
      setCallPlayingKey(null);
      if(btn){
        btn.classList.add("missing");
        setTimeout(() => btn.classList.remove("missing"), 2000);
      }
    }
  }

  function stopCall(){
    if(!callAudio) return;
    callAudio.pause();
    callAudio.currentTime = 0;
    setCallPlayingKey(null);
  }

  function makeCallButton(extraClass){
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "call-btn " + (extraClass || "");
    btn.setAttribute("aria-label", "Play this bird's call");
    btn.innerHTML =
      '<span class="call-icon" aria-hidden="true"></span>' +
      '<span class="call-label">Play call</span>';
    callButtons.add(btn);
    return btn;
  }

  function readSpecies(commonId, sciId){
    return {
      common:     (document.getElementById(commonId)?.textContent || "").trim(),
      scientific: (document.getElementById(sciId)?.textContent || "").trim(),
    };
  }

  function setupCallButtons(){
    // Journal (page 1): drop the button between sciName and tagline.
    const sciName = document.getElementById("sciName");
    if(sciName && !sciName.parentElement.querySelector(".call-btn.journal-call")){
      const btn = makeCallButton("journal-call");
      btn.addEventListener("click", ev => {
        ev.stopPropagation();
        const {common, scientific} = readSpecies("commonName", "sciName");
        playCall(scientific, common, btn);
      });
      sciName.insertAdjacentElement("afterend", btn);

      // Stop playback (and clear the button's playing state) when the
      // journal rotates to a different species mid-clip.
      const commonName = document.getElementById("commonName");
      if(commonName){
        new MutationObserver(() => {
          const {common, scientific} = readSpecies("commonName", "sciName");
          const key = sciKey(scientific, common);
          if(callPlayingKey && callPlayingKey !== key) stopCall();
        }).observe(commonName, {childList: true, characterData: true, subtree: true});
      }
    }

    // Detail (page 2): drop the button below detailSci.
    const detailSci = document.getElementById("detailSci");
    if(detailSci && !detailSci.parentElement.querySelector(".call-btn.detail-call")){
      const btn = makeCallButton("detail-call");
      btn.addEventListener("click", ev => {
        ev.stopPropagation();
        const {common, scientific} = readSpecies("detailName", "detailSci");
        playCall(scientific, common, btn);
      });
      detailSci.insertAdjacentElement("afterend", btn);
    }
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
    // The previous render's call buttons are about to be wiped out by
    // innerHTML; drop them from the state-sync set so we don't leak refs.
    callButtons.forEach(b => {
      if(b.classList.contains("tile-call")) callButtons.delete(b);
    });
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
          '<div class="tile-img' + (t.imageUrl ? '' : ' placeholder') + '">' +
            '<button type="button" class="call-btn tile-call" ' +
              'aria-label="Play call" ' +
              'data-call-key="' + escapeHtml(t.key) + '">' +
              '<span class="call-icon" aria-hidden="true"></span>' +
              '<span class="call-label">Play call</span>' +
            '</button>' +
          '</div>' +
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
      const callBtn = el.querySelector(".call-btn.tile-call");
      if(callBtn){
        callButtons.add(callBtn);
        if(callPlayingKey === callBtn.dataset.callKey) callBtn.classList.add("playing");
        callBtn.addEventListener("click", ev => {
          ev.stopPropagation();      // don't open the detail page
          ev.preventDefault();
          playCall(t.scientific, t.common, callBtn);
        });
      }
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

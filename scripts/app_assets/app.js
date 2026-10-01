// Touch behaviour for scripts/dash_app.py:
// - swipe left/right on the content to move between tabs
// - go back to the Today tab after a few minutes without a touch
// - thick scroll rails (arrow buttons and a draggable bar) for the page, dropdowns and weather strip
// - press-and-drag scrolling for touch screens that reach the browser as a mouse

(function () {
  const IDLE_MS = 3 * 60 * 1000;
  const SWIPE_MIN_PX = 80;
  const DRAG_START_PX = 8;
  // Areas that scroll sideways or handle their own gestures
  const NO_SWIPE = ".h-scroll, .mantine-Select-dropdown, .mantine-SegmentedControl-root, .scroll-rail";
  // Things that scroll up and down, innermost first
  const V_SCROLLERS = ".mantine-Select-dropdown .mantine-ScrollArea-viewport, .app-main";

  function tabs() {
    return Array.from(document.querySelectorAll(".app-nav [data-tab]")).map((b) => b.dataset.tab);
  }

  function setTab(tab) {
    if (window.dash_clientside && tab && tab !== window._tab) {
      window.dash_clientside.set_props("tab", { data: tab });
    }
  }

  function swipeTab(dx) {
    const order = tabs();
    setTab(order[order.indexOf(window._tab || "today") + (dx < 0 ? 1 : -1)]);
  }

  // ── Touch swipes ────────────────────────────────────────────────────────────

  let start = null;
  document.addEventListener("touchstart", (e) => {
    const t = e.touches[0];
    start = {
      x: t.clientX,
      y: t.clientY,
      swipe: !!e.target.closest(".app-main") && !e.target.closest(NO_SWIPE),
      nativeX: !!e.target.closest(".h-scroll"),
    };
  }, { passive: true });

  // Stop the browser turning sideways swipes into back/forward navigation,
  // which would leave the dashboard. Strips that scroll sideways keep working.
  document.addEventListener("touchmove", (e) => {
    if (!start || start.nativeX) return;
    const t = e.touches[0];
    if (Math.abs(t.clientX - start.x) > Math.abs(t.clientY - start.y)) e.preventDefault();
  }, { passive: false });

  document.addEventListener("touchend", (e) => {
    if (!start || !start.swipe) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - start.x;
    const dy = t.clientY - start.y;
    start = null;
    if (Math.abs(dx) < SWIPE_MIN_PX || Math.abs(dx) < 2 * Math.abs(dy)) return;
    swipeTab(dx);
  }, { passive: true });

  // ── Mouse-style dragging ────────────────────────────────────────────────────
  // Some touch screens reach the browser as a mouse, and dragging a mouse doesn't
  // scroll. Make press-and-drag scroll (and swipe tabs) the way a finger would.

  let drag = null;
  let suppressClick = false;

  document.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "mouse" || e.button !== 0) return;
    if (e.target.closest(".scroll-rail, input, textarea")) return;
    const strip = e.target.closest(".h-scroll");
    const scroller = e.target.closest(V_SCROLLERS);
    if (!strip && !scroller) return;
    drag = {
      x: e.clientX, y: e.clientY, lastX: e.clientX, lastY: e.clientY, axis: null, strip, scroller,
      swipe: !!e.target.closest(".app-main") && !e.target.closest(NO_SWIPE),
    };
  });

  document.addEventListener("pointermove", (e) => {
    if (!drag) return;
    if (!drag.axis) {
      const dx = e.clientX - drag.x;
      const dy = e.clientY - drag.y;
      if (Math.max(Math.abs(dx), Math.abs(dy)) < DRAG_START_PX) return;
      drag.axis = Math.abs(dx) > Math.abs(dy) ? "x" : "y";
    }
    if (drag.axis === "y" && drag.scroller) drag.scroller.scrollTop -= e.clientY - drag.lastY;
    if (drag.axis === "x" && drag.strip) drag.strip.scrollLeft -= e.clientX - drag.lastX;
    drag.lastX = e.clientX;
    drag.lastY = e.clientY;
  });

  document.addEventListener("pointerup", (e) => {
    if (!drag) return;
    if (drag.axis) {
      suppressClick = true;  // a drag isn't a tap on whatever it started on
      setTimeout(() => { suppressClick = false; }, 50);
      const dx = e.clientX - drag.x;
      if (drag.axis === "x" && !drag.strip && drag.swipe && Math.abs(dx) >= SWIPE_MIN_PX) swipeTab(dx);
    }
    drag = null;
  });

  window.addEventListener("click", (e) => {
    if (suppressClick) {
      e.stopPropagation();
      e.preventDefault();
      suppressClick = false;
    }
  }, true);
  document.addEventListener("dragstart", (e) => e.preventDefault());

  // ── Scroll rails ────────────────────────────────────────────────────────────

  function railScroller(rail) {
    return rail._scroller || document.getElementById(rail.dataset.target);
  }

  // Rails scroll up/down, or left/right when they have the rail-x class
  function axis(rail, s) {
    return rail.classList.contains("rail-x")
      ? { pos: "scrollLeft", view: s.clientWidth, total: s.scrollWidth, size: "width", translate: "translateX", client: "clientX", start: "left", length: "width", by: "left" }
      : { pos: "scrollTop", view: s.clientHeight, total: s.scrollHeight, size: "height", translate: "translateY", client: "clientY", start: "top", length: "height", by: "top" };
  }

  function updateRail(rail) {
    const s = railScroller(rail);
    const track = rail.querySelector(".rail-track");
    const thumb = rail.querySelector(".rail-thumb");
    if (!s || !track || !thumb) return;
    const a = axis(rail, s);
    const overflow = a.total - a.view;
    // Rails take no space when there's nothing to scroll, so content can use the full width
    rail.classList.toggle("idle", overflow <= 2);
    if (overflow <= 2) return;
    const trackLen = track.getBoundingClientRect()[a.length];
    const thumbLen = Math.max(56, trackLen * a.view / a.total);
    thumb.style[a.size] = thumbLen + "px";
    thumb.style.transform = `${a.translate}(${(trackLen - thumbLen) * s[a.pos] / overflow}px)`;
    rail.querySelector(".rail-back").classList.toggle("disabled", s[a.pos] <= 1);
    rail.querySelector(".rail-fwd").classList.toggle("disabled", s[a.pos] >= overflow - 1);
  }

  function updateAllRails() {
    document.querySelectorAll(".scroll-rail").forEach(updateRail);
  }

  let holdTimer = null;
  function stopHold() {
    clearTimeout(holdTimer);
    holdTimer = null;
  }

  // Keep focus where it is, so tapping a dropdown's rail doesn't close the dropdown
  document.addEventListener("mousedown", (e) => {
    if (e.target.closest(".scroll-rail")) e.preventDefault();
  }, true);

  document.addEventListener("pointerdown", (e) => {
    const rail = e.target.closest(".scroll-rail");
    if (!rail) return;
    e.preventDefault();
    const s = railScroller(rail);
    if (!s) return;
    const a = axis(rail, s);
    const btn = e.target.closest(".rail-btn");
    if (btn) {
      // Tap to move most of a screen; hold to keep going
      const dir = btn.classList.contains("rail-back") ? -1 : 1;
      const step = () => s.scrollBy({ [a.by]: dir * a.view * 0.6, behavior: "smooth" });
      step();
      stopHold();
      const repeat = () => { step(); holdTimer = setTimeout(repeat, 300); };
      holdTimer = setTimeout(repeat, 450);
      return;
    }
    const track = e.target.closest(".rail-track");
    if (!track) return;
    // Drag the bar; tapping the track jumps there first
    const thumb = track.querySelector(".rail-thumb");
    const trackRect = track.getBoundingClientRect();
    const thumbLen = thumb.getBoundingClientRect()[a.length];
    const ratio = (a.total - a.view) / Math.max(1, trackRect[a.length] - thumbLen);
    if (e.target !== thumb) {
      s[a.pos] = (e[a.client] - trackRect[a.start] - thumbLen / 2) * ratio;
    }
    const startAt = e[a.client];
    const startPos = s[a.pos];
    track.setPointerCapture(e.pointerId);
    const move = (ev) => { s[a.pos] = startPos + (ev[a.client] - startAt) * ratio; };
    const end = () => {
      track.removeEventListener("pointermove", move);
      track.removeEventListener("pointerup", end);
      track.removeEventListener("pointercancel", end);
    };
    track.addEventListener("pointermove", move);
    track.addEventListener("pointerup", end);
    track.addEventListener("pointercancel", end);
  });
  ["pointerup", "pointercancel"].forEach((ev) => document.addEventListener(ev, stopHold));

  document.addEventListener("scroll", (e) => {
    const rail = document.querySelector(`.scroll-rail[data-target="${e.target.id}"]`);
    if (rail) updateRail(rail);
    const dropdown = e.target.closest && e.target.closest(".mantine-Select-dropdown");
    if (dropdown && dropdown._rail) updateRail(dropdown._rail);
  }, true);

  // Give each open dropdown its own rail
  const CHEVRON = (d) =>
    `<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="${d}"/></svg>`;

  function newRail() {
    const rail = document.createElement("div");
    rail.className = "scroll-rail dropdown-rail";
    rail.innerHTML =
      `<button class="rail-btn rail-back">${CHEVRON("M6 15l6-6 6 6")}</button>` +
      '<div class="rail-track"><div class="rail-thumb"></div></div>' +
      `<button class="rail-btn rail-fwd">${CHEVRON("M6 9l6 6 6-6")}</button>`;
    return rail;
  }

  function addDropdownRails() {
    document.querySelectorAll(".mantine-Select-dropdown").forEach((dropdown) => {
      if (dropdown._rail) return;
      const viewport = dropdown.querySelector(".mantine-ScrollArea-viewport");
      if (!viewport) return;
      const rail = newRail();
      rail._scroller = viewport;
      dropdown._rail = rail;
      dropdown.classList.add("has-rail");
      dropdown.appendChild(rail);
      requestAnimationFrame(() => updateRail(rail));
    });
  }

  new MutationObserver(addDropdownRails).observe(document.body, { childList: true, subtree: true });
  setInterval(updateAllRails, 400);  // content and tabs change without scroll events

  // ── Cooking timer alarm ─────────────────────────────────────────────────────
  // The page has been tapped to start the timer, so the browser allows sound.

  let audio = null;
  let ringTimer = null;
  function beep() {
    audio = audio || new (window.AudioContext || window.webkitAudioContext)();
    [0, 0.25, 0.5].forEach((offset) => {
      const osc = audio.createOscillator();
      const gain = audio.createGain();
      osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.4, audio.currentTime + offset);
      gain.gain.exponentialRampToValueAtTime(0.001, audio.currentTime + offset + 0.2);
      osc.connect(gain).connect(audio.destination);
      osc.start(audio.currentTime + offset);
      osc.stop(audio.currentTime + offset + 0.2);
    });
  }
  window.timerRing = function (on) {
    if (on && !ringTimer) {
      wake();
      beep();
      ringTimer = setInterval(beep, 1500);
    } else if (!on && ringTimer) {
      clearInterval(ringTimer);
      ringTimer = null;
    }
  };

  // ── Night: dim the screen ───────────────────────────────────────────────────
  // From 22:30 to 06:30 the screen dims after a couple of minutes untouched; a tap
  // wakes it (and that tap doesn't also press whatever was under it).

  const NIGHT_FROM = 22 * 60 + 30;
  const NIGHT_TO = 6 * 60 + 30;
  const WAKE_MS = 2 * 60 * 1000;
  let awakeUntil = 0;

  function isNight() {
    const now = new Date();
    const minutes = now.getHours() * 60 + now.getMinutes();
    return minutes >= NIGHT_FROM || minutes < NIGHT_TO;
  }
  function updateNight() {
    document.body.classList.toggle("night", isNight() && Date.now() > awakeUntil);
  }
  function wake() {
    awakeUntil = Date.now() + WAKE_MS;
    updateNight();
  }
  document.addEventListener("pointerdown", (e) => {
    if (document.body.classList.contains("night")) {
      e.preventDefault();
      e.stopPropagation();
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 400);
    }
    wake();
  }, true);
  setInterval(updateNight, 10 * 1000);
  wake();  // start bright, even at night, and dim only after a while untouched

  // ── Idle: back to Today ─────────────────────────────────────────────────────

  let idleTimer = null;
  function resetIdle() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      setTab("today");
      const main = document.getElementById("app-main");
      if (main) main.scrollTop = 0;
    }, IDLE_MS);
  }
  ["pointerdown", "touchstart", "keydown"].forEach((ev) =>
    document.addEventListener(ev, resetIdle, { passive: true })
  );
  resetIdle();
})();

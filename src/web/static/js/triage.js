/* Matches (/) at phone width: one pane at a time. The list fills the screen;
   opening a match swaps in its detail and adds a same-URL history entry, so
   the "← Matches" button and the phone's back gesture both return to the list
   where you left it. Also collapses the filter bar behind a toggle. Desktop
   CSS ignores every class set here, so crossing the breakpoint needs no JS. */
(function () {
  var panes = document.querySelector(".panes");
  if (!panes) { return; }
  var phone = window.matchMedia("(max-width: 47.99rem)");
  var listScroll = 0;
  // js-triage itself is added by an inline script right after this file's
  // <script> tag in inbox.html — before the browser paints the body — so the
  // filter bar never flashes open before folding behind its toggle. Adding it
  // again here would just be a slightly-late no-op, so it isn't repeated.

  // iOS Safari restores scroll position on its own after a popstate, racing
  // the rAF-scheduled restore below. Taking manual control here means only
  // our restore ever runs.
  if ("scrollRestoration" in history) { history.scrollRestoration = "manual"; }

  function showDetail() {
    // A status change re-renders #detail too; it must not stack a second entry.
    if (panes.classList.contains("show-detail")) { return; }
    listScroll = window.scrollY;
    panes.classList.add("show-detail");
    document.documentElement.classList.add("triage-detail");
    window.scrollTo(0, 0);
    history.pushState({ triageDetail: true }, "");
  }

  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.detail.target && e.detail.target.id === "detail" && phone.matches) { showDetail(); }
  });
  // Back gesture and the button share this path. After a reload the extra
  // same-URL entry has no detail open, so back is simply a no-op here.
  window.addEventListener("popstate", function () {
    if (!panes.classList.contains("show-detail")) { return; }
    // Read before removing: the class change below can affect layout, and a
    // rotation/resize to desktop width while in detail must not jump the
    // list's scroll position on the way out.
    var wasPhone = phone.matches;
    panes.classList.remove("show-detail");
    document.documentElement.classList.remove("triage-detail");
    if (wasPhone) {
      // #list was just display:none; its scrollable height hasn't been
      // laid out yet, so scrolling in this same tick clamps straight back
      // to 0. Wait for the layout pass the class change just triggered.
      requestAnimationFrame(function () { window.scrollTo(0, listScroll); });
    }
  });
  document.addEventListener("click", function (e) {
    if (e.target.closest(".detail-back") && panes.classList.contains("show-detail")) { history.back(); }
  });

  var toggle = document.getElementById("filters-toggle");
  var filters = document.getElementById("filters");
  if (toggle && filters) {
    toggle.addEventListener("click", function () {
      var open = toggle.getAttribute("aria-expanded") !== "true";
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      filters.classList.toggle("filters-open", open);
    });
  }
})();

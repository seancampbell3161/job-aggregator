/* Chip input. Progressive enhancement over the server-rendered chips box
   (_settings_macros.html `chips`): a .chips[data-path] div holding one text
   input per value plus a blank one. Without this script that box still
   works — one new value per save.

   Enhanced, each value becomes a chip (a hidden input with the field's
   name) and a single entry box takes new ones: Enter adds; a pasted list
   splits on new lines; commas are kept — company names like "Acme, Inc."
   contain them. Backspace on an empty entry removes the last chip.

   The entry KEEPS the field's name. forms.decode() reads a chips path that
   is absent from the post as "not on this page — leave it alone", and one
   posted blank as "cleared". With every chip removed, the named (blank)
   entry is what still says "cleared". On submit any half-typed text is
   committed as a chip first, so it is saved de-duplicated, not lost. */
(function () {
  "use strict";
  var uid = 0;

  function chipValues(box) {
    return Array.from(box.querySelectorAll('.chip input[type="hidden"]'))
      .map(function (i) { return i.value.toLowerCase(); });
  }

  function announce(box, text) {
    var live = box.querySelector(".chip-live");
    if (live) { live.textContent = text; }
  }

  function makeChip(path, value) {
    var li = document.createElement("li");
    li.className = "chip";
    var label = document.createElement("span");
    label.textContent = value;
    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "chip-remove";
    remove.setAttribute("aria-label", "Remove " + value);
    remove.textContent = "×";
    var hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = path;
    hidden.value = value;
    li.append(label, remove, hidden);
    return li;
  }

  // Adds each new, non-blank, not-already-present value; returns those added.
  function addValues(box, values) {
    var have = chipValues(box);
    var list = box.querySelector(".chip-list");
    var added = [];
    values.forEach(function (raw) {
      var value = raw.trim();
      if (!value || have.indexOf(value.toLowerCase()) !== -1) { return; }
      list.appendChild(makeChip(box.dataset.path, value));
      have.push(value.toLowerCase());
      added.push(value);
    });
    if (added.length) { announce(box, "Added " + added.join(", ")); }
    return added;
  }

  function removeChip(li) {
    var box = li.closest(".chips");
    var value = li.querySelector('input[type="hidden"]').value;
    li.remove();
    announce(box, "Removed " + value);
    box.querySelector(".chip-entry").focus();
  }

  function commitEntry(box) {
    var entry = box.querySelector(".chip-entry");
    if (entry.value.trim()) { addValues(box, [entry.value]); }
    entry.value = "";
  }

  function enhance(box) {
    if (box.dataset.enhanced) { return; }
    box.dataset.enhanced = "1";
    var path = box.dataset.path;
    var initial = Array.from(box.querySelectorAll("input")).map(function (i) { return i.value; });
    var id = "chips-" + (++uid);

    box.textContent = "";
    box.classList.add("chip-input");

    var list = document.createElement("ul");
    list.className = "chip-list";
    var entry = document.createElement("input");
    entry.type = "text";
    entry.className = "chip-entry";
    entry.name = path;
    entry.id = id;
    entry.autocomplete = "off";
    var hint = document.createElement("span");
    hint.className = "chip-hint";
    hint.id = id + "-hint";
    hint.textContent = "Press Enter to add";
    var live = document.createElement("span");
    live.className = "sr-only chip-live";
    live.setAttribute("aria-live", "polite");
    box.append(list, entry, hint, live);

    // Name the entry after the field's own label (ui.field renders a
    // <span class="field-label"> for chips — a group, not one control).
    // Some chips boxes aren't wrapped in a .field at all (e.g. the
    // résumé-content draft's Skills fieldset) — fall back to the nearest
    // plain <fieldset>'s <legend> so the entry is still named.
    var field = box.closest(".field");
    var label = field && field.querySelector(".field-label");
    if (!label) {
      var fieldset = box.closest("fieldset");
      label = fieldset && fieldset.querySelector("legend");
    }
    if (label) {
      if (!label.id) { label.id = id + "-label"; }
      entry.setAttribute("aria-labelledby", label.id);
    }
    entry.setAttribute("aria-describedby", hint.id);

    addValues(box, initial);
    announce(box, "");   // the initial fill is not news

    entry.addEventListener("keydown", function (e) {
      if (e.isComposing || e.keyCode === 229) { return; }
      if (e.key === "Enter") {
        e.preventDefault();   // Enter here adds a chip; it never submits the form
        commitEntry(box);
      } else if (e.key === "Backspace" && entry.value === "") {
        var chips = list.querySelectorAll(".chip");
        if (chips.length) { removeChip(chips[chips.length - 1]); }
      }
    });
    entry.addEventListener("paste", function (e) {
      var text = (e.clipboardData || window.clipboardData).getData("text");
      if (!/\r?\n/.test(text)) { return; }
      e.preventDefault();
      addValues(box, text.split(/\r?\n/));
    });
  }

  function enhanceAll(root) {
    root.querySelectorAll(".chips[data-path]").forEach(enhance);
  }

  document.addEventListener("click", function (e) {
    var remove = e.target.closest && e.target.closest(".chip-remove");
    if (remove) { removeChip(remove.closest(".chip")); return; }
    // One-click variant sets (_settings_macros.html `chip_sets`).
    var set = e.target.closest && e.target.closest(".chip-set");
    if (set) {
      var box = document.querySelector('.chips[data-path="' + CSS.escape(set.dataset.target) + '"]');
      if (box) { enhance(box); addValues(box, JSON.parse(set.dataset.values)); }
    }
  });

  // Capture phase: runs before the form is serialized.
  document.addEventListener("submit", function (e) {
    e.target.querySelectorAll(".chip-input").forEach(commitEntry);
  }, true);

  enhanceAll(document);
  document.body.addEventListener("htmx:afterSwap", function (e) { enhanceAll(e.target); });
})();

// Apply-kit autofill matcher. Baked into the /kit bookmarklet with the user's
// facts. Keyword-matches each fact to a form field by the field's visible label
// text, fills via the React-safe native setter, highlights, toasts. Never
// submits.
function __APPLY_FILL(facts) {
  facts = (facts || []).filter(function (f) {
    return f && f.value != null && String(f.value).trim() !== "";
  });

  var scope = document.querySelector("form") || document.body;
  var SKIP = { password: 1, file: 1, hidden: 1, submit: 1, button: 1,
               checkbox: 1, reset: 1, image: 1 };

  var nodes = scope.querySelectorAll("input, textarea, select");
  var fields = [];
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    var type = (el.getAttribute("type") || el.type || "text").toLowerCase();
    if (el.disabled || el.readOnly || SKIP[type]) continue;
    if (type !== "radio" && el.getClientRects().length === 0) continue; // not visible
    fields.push(el);
  }

  function labelText(el) {
    var parts = [];
    if (el.id) {
      var labs = document.getElementsByTagName("label");
      for (var k = 0; k < labs.length; k++) {
        if (labs[k].htmlFor === el.id) parts.push(labs[k].textContent);
      }
    }
    var anc = el.closest ? el.closest("label") : null;
    if (anc) parts.push(anc.textContent);
    var fs = el.closest ? el.closest("fieldset") : null;
    if (fs) { var lg = fs.querySelector("legend"); if (lg) parts.push(lg.textContent); }
    var rg = el.closest ? el.closest('[role="radiogroup"]') : null;
    if (rg && rg.getAttribute("aria-label")) parts.push(rg.getAttribute("aria-label"));
    var lb = el.getAttribute("aria-labelledby");
    if (lb) lb.split(/\s+/).forEach(function (id) {
      var n = document.getElementById(id); if (n) parts.push(n.textContent);
    });
    parts.push(el.getAttribute("aria-label") || "");
    parts.push(el.getAttribute("placeholder") || "");
    parts.push(el.getAttribute("name") || "");
    parts.push(el.id || "");
    return parts.join(" ");
  }
  function ownLabel(el) {
    var t = "";
    if (el.id) {
      var labs = document.getElementsByTagName("label");
      for (var k = 0; k < labs.length; k++) { if (labs[k].htmlFor === el.id) { t = labs[k].textContent; break; } }
    }
    if (!t) { var anc = el.closest ? el.closest("label") : null; if (anc) t = anc.textContent; }
    if (!t) t = el.getAttribute("aria-label") || el.value || "";
    return t.trim().toLowerCase();
  }
  function toks(s) {
    return (s || "").toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
  }

  // Synonyms so a fact label ("Work authorization") reaches an ATS-phrased
  // field ("Are you legally authorized to work…"). Tune from the recon fixtures.
  var SYN = {
    authorization: ["authorized", "authorize", "eligible", "eligibility", "legally"],
    sponsorship: ["sponsor", "sponsorship", "visa"],
    site: ["website", "portfolio", "personal"],
    location: ["city", "located", "based"],
    veteran: ["veteran", "protected"],
    disability: ["disability", "disabled"]
  };
  function factToks(label) {
    var t = toks(label), out = t.slice();
    t.forEach(function (w) { if (SYN[w]) out = out.concat(SYN[w]); });
    return out;
  }

  var meta = fields.map(function (el) { return { el: el, t: toks(labelText(el)) }; });
  function score(ft, t) {
    if (!t.length) return 0;
    var h = 0;
    for (var a = 0; a < ft.length; a++) if (t.indexOf(ft[a]) >= 0) h++;
    return h;
  }

  function nativeSet(el, val) {
    var proto = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype
              : window.HTMLInputElement.prototype;
    var desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, val); else el.value = val;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function mark(el) { try { el.style.outline = "2px solid #22c55e"; } catch (e) {} }

  function fillOne(el, value) {
    var v = String(value);
    if (el.tagName === "SELECT") {
      var lv = v.toLowerCase(), opts = el.options, pick = -1;
      for (var o = 0; o < opts.length; o++) {                 // pass 1: exact match anywhere
        var ot = (opts[o].textContent || "").trim().toLowerCase();
        var ov = (opts[o].value || "").toLowerCase();
        if (ot === lv || ov === lv) { pick = o; break; }
      }
      if (pick < 0 && lv.length >= 3) {                        // pass 2: option text contains value (guarded)
        for (var o2 = 0; o2 < opts.length; o2++) {
          var ot2 = (opts[o2].textContent || "").trim().toLowerCase();
          if (ot2 && ot2.indexOf(lv) >= 0) { pick = o2; break; }
        }
      }
      if (pick < 0) return false;
      el.selectedIndex = pick;
      el.dispatchEvent(new Event("change", { bubbles: true }));
      mark(el); return true;
    }
    if ((el.getAttribute("type") || "").toLowerCase() === "radio") {
      var name = el.name, lv2 = v.toLowerCase();
      var radios = name ? scope.querySelectorAll('input[type="radio"]') : [el];
      var exact = null, partial = null;
      for (var r = 0; r < radios.length; r++) {
        if (name && radios[r].name !== name) continue;
        var own = ownLabel(radios[r]);
        var rv = (radios[r].value || "").toLowerCase();
        if (own === lv2 || rv === lv2) { exact = radios[r]; break; }   // exact, any length ("No" ok)
        if (!partial && lv2.length >= 3 && own.indexOf(lv2) >= 0) partial = radios[r];
      }
      var target = exact || partial;
      if (!target) return false;
      target.click(); mark(target); return true;
    }
    nativeSet(el, v); mark(el); return true;
  }

  var used = [], filled = 0, unmatched = [];
  facts.forEach(function (f) {
    var ft = factToks(f.label), bi = -1, bs = 0;
    for (var m = 0; m < meta.length; m++) {
      if (used.indexOf(m) >= 0) continue;
      var s = score(ft, meta[m].t);
      if (s > bs) { bs = s; bi = m; }
    }
    if (bi < 0 || bs < 1) { unmatched.push(f.label); return; }
    var ok;
    try { ok = fillOne(meta[bi].el, f.value); } catch (e) { ok = false; }
    if (ok) { used.push(bi); filled++; } else { unmatched.push(f.label); }
  });

  __apply_toast(filled, unmatched);
}

function __apply_toast(filled, unmatched) {
  var old = document.getElementById("__apply_toast");
  if (old) old.parentNode.removeChild(old);
  var d = document.createElement("div");
  d.id = "__apply_toast";
  var msg = filled === 0
    ? "Apply Autofill — no matching fields filled" +
      (unmatched && unmatched.length ? " (" + unmatched.length + " facts unmatched)" : "")
    : "Apply Autofill — filled " + filled +
      (unmatched && unmatched.length ? " · unmatched " + unmatched.length + ": " + unmatched.join(", ") : "");
  d.textContent = msg;
  d.style.cssText = "position:fixed;z-index:2147483647;bottom:16px;right:16px;" +
    "max-width:360px;background:#111;color:#fff;padding:12px 14px;border-radius:8px;" +
    "font:13px/1.4 sans-serif;box-shadow:0 2px 12px rgba(0,0,0,.4);cursor:pointer";
  d.onclick = function () { if (d.parentNode) d.parentNode.removeChild(d); };
  document.body.appendChild(d);
  setTimeout(function () { if (d.parentNode) d.parentNode.removeChild(d); }, 6000);
}

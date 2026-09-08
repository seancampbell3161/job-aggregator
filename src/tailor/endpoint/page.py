"""Self-contained mobile HTML for the tailor endpoint. The loading page renders
instantly with a spinner, then its JS fetches `?run=1` (the ~30s tailor) and
swaps in the download link, cover letter, and fit. No external assets."""

from __future__ import annotations

import html as _html

_STYLE = """
  body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}
  .wrap{max-width:640px;margin:0 auto;padding:20px}
  h1{font-size:18px;margin:.2em 0}.sub{color:#94a3b8;font-size:14px;margin-bottom:18px}
  .spinner{width:34px;height:34px;border:4px solid #334155;border-top-color:#5fa39c;border-radius:50%;animation:spin 1s linear infinite;margin:30px auto}
  @keyframes spin{to{transform:rotate(360deg)}}
  a.btn{display:block;text-align:center;background:#0f766e;color:#fff;text-decoration:none;padding:14px;border-radius:10px;margin:14px 0;font-weight:600}
  pre{white-space:pre-wrap;background:#1e293b;padding:12px;border-radius:8px;font-size:13px}
  .err{color:#fca5a5}.muted{color:#94a3b8;font-size:13px}
  button.copy{background:#334155;color:#e2e8f0;border:0;border-radius:8px;padding:8px 12px;font-size:13px}
"""


def loading_page(job_id: str, token: str, title: str, company: str,
                 templates: list[tuple[str, str]] | None = None, active: str = "") -> str:
    t = _html.escape(title or "Tailoring your résumé")
    c = _html.escape(company or "")
    sub = f"{c}" if c else ""
    picker = ""
    if templates and len(templates) > 1:
        opts = "".join(
            f'<option value="{_html.escape(slug)}"{" selected" if slug == active else ""}>'
            f"{_html.escape(name)}</option>"
            for slug, name in templates
        )
        picker = (f'<p class="muted" style="text-align:center">Template: '
                  f'<select id="tpl">{opts}</select></p>')
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Tailored résumé</title>
<style>{_STYLE}</style></head><body><div class="wrap">
<h1>{t}</h1><div class="sub">{sub}</div>
{picker}
<div id="status"><div class="spinner"></div><p class="muted" style="text-align:center">Tailoring your résumé… (~30s)</p></div>
<div id="result"></div>
<p class="muted" style="text-align:center"><a href="/kit" style="color:#5fa39c">Apply kit →</a></p>
<script>
const loc = window.location.href;
const sep = loc.includes('?') ? '&' : '?';
function show(d){{
  document.getElementById('status').style.display='none';
  const out = document.getElementById('result');
  const esc = s => (s||'').replace(/</g,'&lt;');
  if(d.error){{ out.innerHTML = '<p class="err">'+esc(d.error)+'</p>'; return; }}
  let h = '<a class="btn" href="'+d.pdf_url+'">⬇ Download tailored résumé (PDF)</a>';
  if(d.fit_warning){{ h += '<p class="err">⚠ '+esc(d.fit_warning)+'</p>'; }}
  if(d.cover_letter){{ h += '<h1>Cover letter</h1><button class="copy" onclick="navigator.clipboard.writeText(document.getElementById(\\'cl\\').innerText)">Copy</button><pre id="cl">'+esc(d.cover_letter)+'</pre>'; }}
  if(d.fit){{ h += '<h1>Fit</h1><pre>Matches:\\n- '+esc((d.fit.matches||[]).join('\\n- '))+'\\n\\nGaps:\\n- '+esc((d.fit.gaps||[]).join('\\n- '))+'</pre>'; }}
  out.innerHTML = h;
}}
function fail(){{ document.getElementById('status').innerHTML='<p class="err">Something went wrong. Try again.</p>'; }}
function go(extra){{
  document.getElementById('status').style.display='';
  fetch(loc + sep + 'run=1' + extra).then(r=>r.json()).then(show).catch(fail);
}}
document.getElementById('tpl')?.addEventListener('change',
  e => go('&template=' + encodeURIComponent(e.target.value)));
go('');
</script></div></body></html>"""


def error_page(message: str) -> str:
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Tailored résumé</title>
<style>{_STYLE}</style></head><body><div class="wrap">
<h1>Tailored résumé</h1><p class="err">{_html.escape(message)}</p></div></body></html>"""

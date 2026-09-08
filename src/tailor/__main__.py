"""CLI to dogfood the tailoring engine locally.

    JOB_AGG_NTFY_TOPIC_URL=x JOB_AGG_DISCORD_WEBHOOK_URL=x JOB_AGG_OLLAMA_API_KEY=<key> \\
      python -m src.tailor --jd jd.txt --job-id some-id

Writes tailored/<job-id>/{content.json,cover_letter.md,fit.md}. The content.json
is the render input sub-project B consumes. (load_config requires the dummy
NTFY/DISCORD vars even though tailoring never notifies.)"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from src.config import load_config
from src.tailor import build_tailor_engine
from src.tailor.content import load_content
from src.tailor.models import TailorResult, render_input_dict


def _builder_settings():
    """Store-backed settings; any failure (no DB, no table) -> defaults."""
    from src.tailor.render.settings import BuilderSettings, settings_from_dict
    try:
        from src.stores import build_stores
        store = build_stores().builder
        return settings_from_dict(store.get() if store else None)
    except Exception:  # noqa: BLE001 — CLI must run without a DB
        return BuilderSettings()


def _fit_md(result: TailorResult) -> str:
    lines = ["# Fit analysis", "", "## Matches"]
    lines += [f"- {m}" for m in result.fit.matches] or ["- (none)"]
    lines += ["", "## Gaps"]
    lines += [f"- {g}" for g in result.fit.gaps] or ["- (none)"]
    lines += ["", "## Overall", result.fit.overall]
    return "\n".join(lines) + "\n"


def _write_outputs(job_id: str, result: TailorResult) -> Path:
    out = Path("tailored") / job_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "content.json").write_text(json.dumps(render_input_dict(result), indent=2, ensure_ascii=False))
    (out / "cover_letter.md").write_text(result.cover_letter)
    (out / "fit.md").write_text(_fit_md(result))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.tailor")
    ap.add_argument("--jd", required=True, help="path to a job-description text file")
    ap.add_argument("--job-id", required=True, help="id used for the tailored/<job-id>/ output dir")
    ap.add_argument("--no-pdf", action="store_true", help="skip rendering the PDF")
    ap.add_argument("--template", default="", help="template pack slug (default: the active template)")
    args = ap.parse_args(argv)

    cfg = load_config()
    engine = build_tailor_engine(cfg)
    if engine is None:
        print("tailoring unavailable (disabled, missing ollama key, or unreadable artifacts)", file=sys.stderr)
        return 1

    jd_text = Path(args.jd).read_text()
    result = asyncio.run(engine.tailor(job_id=args.job_id, jd_text=jd_text))
    out = _write_outputs(args.job_id, result)
    status = "FALLBACK (LLM unavailable)" if result.is_fallback else "ok"
    print(f"[{status}] wrote {out}/")

    if not args.no_pdf:
        try:
            from src.tailor.render import render_with_fallback
            from src.tailor.render.registry import get_template
            settings = _builder_settings()
            pack = get_template(args.template or settings.active_template)
            content = load_content(cfg.tailoring.content_path)
            rendered = render_with_fallback(content, result, pack=pack, settings=settings)
            (out / "resume.pdf").write_bytes(rendered.pdf)
            msg = f"[{status}] wrote {out}/resume.pdf (template: {rendered.template})"
            if rendered.trimmed:
                msg += f" (trimmed {len(rendered.trimmed)} bullet(s))"
            if rendered.fit_warning:
                msg += f"\nWARNING: {rendered.fit_warning}"
            print(msg)
        except ImportError:
            print("PDF skipped: WeasyPrint not installed (pip install -e '.[render]' + brew install pango)",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — never crash the run over the PDF
            print(f"PDF render failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

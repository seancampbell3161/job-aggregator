from tests.auth_helpers import signed_in_client

from src.web.app import create_app
from tests.conftest import requires_weasyprint
from tests.settings_helpers import WEB_TEST_SETTINGS, make_service


def _app(tmp_path, monkeypatch, service=None):
    monkeypatch.setenv("JOB_AGG_SQLITE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("JOB_AGG_TEMPLATES_DIR", str(tmp_path / "templates"))
    monkeypatch.setenv("JOB_AGG_TAILORED_DIR", str(tmp_path / "tailored"))
    return create_app(service=service if service is not None else make_service(WEB_TEST_SETTINGS))


def test_builder_page_lists_builtins_and_settings(tmp_path, monkeypatch):
    c = signed_in_client(_app(tmp_path, monkeypatch))
    r = c.get("/builder")
    assert r.status_code == 200
    assert "Classic" in r.text and "Headless" in r.text
    assert 'name="max_pages"' in r.text


def test_save_settings_roundtrip(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = signed_in_client(app)
    r = c.post("/builder/settings", data={
        "max_bullets_per_experience": "4", "max_bullets_per_project": "",
        "min_bullets_per_entry": "2", "max_pages": "2",
        "page_size": "a4", "margins": "0.5in",
    })
    assert r.status_code == 200
    s = app.state.builder.settings()
    assert s.max_bullets_per_experience == 4
    assert s.max_bullets_per_project is None
    assert s.max_pages == 2 and s.page_size == "a4"


def test_save_settings_rejects_invalid(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/builder/settings", data={
        "min_bullets_per_entry": "3", "max_bullets_per_experience": "2", "max_pages": "1",
    })
    assert r.status_code == 200
    assert "min_bullets" in r.text or "cap" in r.text.lower()  # inline error shown
    assert app.state.builder.settings().max_bullets_per_experience is None  # not saved


def test_activate_switches_active_template(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = signed_in_client(app)
    r = c.post("/builder/activate", data={"slug": "headless"})
    assert r.status_code == 200
    assert app.state.builder.settings().active_template == "headless"


def test_delete_rejects_builtin(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).post("/builder/delete", data={"slug": "classic"})
    assert r.status_code == 400


@requires_weasyprint
def test_delete_filesystem_error_does_not_500(tmp_path, monkeypatch):
    """A filesystem error (e.g. EROFS on a read-only templates mount) during
    delete must come back as an inline gallery error, not a 500."""
    app = _app(tmp_path, monkeypatch)
    c = signed_in_client(app)
    _upload(c, "My Modern CV.html", GOOD_TEMPLATE)

    import src.web.builder as builder_mod

    def boom(*a, **kw):
        raise OSError("Read-only file system")
    monkeypatch.setattr(builder_mod.shutil, "rmtree", boom)

    r = c.post("/builder/delete", data={"slug": "my-modern-cv"})
    assert r.status_code == 200
    assert 'class="bad"' in r.text


@requires_weasyprint
def test_preview_returns_pdf(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    r = signed_in_client(app).get("/builder/preview", params={"slug": "classic"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"


def test_nav_links_builder(tmp_path, monkeypatch):
    r = signed_in_client(_app(tmp_path, monkeypatch)).get("/builder")
    assert '<a href="/builder">Builder</a>' in r.text


def test_delete_rejects_traversal_slug(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    r = signed_in_client(app).post("/builder/delete", data={"slug": "../victim"})
    assert r.status_code == 400
    assert (victim / "keep.txt").exists()


def test_preview_rejects_traversal_slug(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "template.html.j2").write_text("<html>{{ doc.name }}</html>")
    r = signed_in_client(app).get("/builder/preview", params={"slug": "../outside", "pending": "1"})
    assert r.status_code == 400


def test_preview_error_detail_is_generic(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    tpl_dir = tmp_path / "templates" / "broken"
    tpl_dir.mkdir(parents=True)
    (tpl_dir / "template.html.j2").write_text("{% not_a_tag %}")
    r = signed_in_client(app).get("/builder/preview", params={"slug": "broken"})
    assert r.status_code == 422
    assert r.json()["detail"] == "preview failed — template did not render; see server logs"


def test_slugify_produces_safe_slug_for_traversal_input():
    from src.web.builder import SLUG_RE, slugify
    assert SLUG_RE.match(slugify("../Evil Näme.html-ish"))


def _upload(client, filename, data, content_type="application/octet-stream"):
    return client.post("/builder/upload", files={"file": (filename, data, content_type)})


GOOD_TEMPLATE = (b"<!DOCTYPE html><html><body>{{ doc.name }}"
                 b"{% for e in doc.experiences %}<p>{{ e.role }}</p>{% endfor %}</body></html>")


@requires_weasyprint
def test_upload_html_template_appears_in_gallery(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    c = signed_in_client(app)
    r = _upload(c, "My Modern CV.html", GOOD_TEMPLATE)
    assert r.status_code == 200
    from src.tailor.render.registry import list_templates
    assert "my-modern-cv" in {t.slug for t in list_templates()}
    # and it validates + renders
    p = c.get("/builder/preview", params={"slug": "my-modern-cv"})
    assert p.content[:5] == b"%PDF-"


def test_upload_rejects_bad_jinja(tmp_path, monkeypatch):
    c = signed_in_client(_app(tmp_path, monkeypatch))
    r = _upload(c, "broken.j2", b"{% for %}")
    assert r.status_code == 200  # inline error in the fragment
    assert 'class="bad"' in r.text
    from src.tailor.render.registry import list_templates
    assert "broken" not in {t.slug for t in list_templates()}


def test_upload_rejects_builtin_slug_collision(tmp_path, monkeypatch):
    c = signed_in_client(_app(tmp_path, monkeypatch))
    r = _upload(c, "classic.html", GOOD_TEMPLATE)
    assert "already exists" in r.text


def test_upload_rejects_oversize(tmp_path, monkeypatch):
    c = signed_in_client(_app(tmp_path, monkeypatch))
    r = _upload(c, "big.html", b"x" * (10 * 1024 * 1024 + 1))
    assert "10 MB" in r.text


@requires_weasyprint
def test_upload_zip_pack_with_fonts(tmp_path, monkeypatch):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("template.html.j2", GOOD_TEMPLATE.decode())
        z.writestr("meta.yaml", "name: Zipped\n")
        z.writestr("fonts/Fake.ttf", "notreallyafont")
    c = signed_in_client(_app(tmp_path, monkeypatch))
    r = _upload(c, "zipped.zip", buf.getvalue(), "application/zip")
    assert r.status_code == 200
    from src.tailor.render.registry import get_template
    assert get_template("zipped").name == "Zipped"


@requires_weasyprint
def test_upload_zip_meta_source_is_forced_to_upload(tmp_path, monkeypatch):
    """A zip-supplied meta.yaml claiming `source: builtin` must not be
    believed — builtin source hides the Delete button in the gallery, so a
    crafted pack could otherwise make itself undeletable. name is kept."""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("template.html.j2", GOOD_TEMPLATE.decode())
        z.writestr("meta.yaml", "source: builtin\nname: Sneaky\n")
    c = signed_in_client(_app(tmp_path, monkeypatch))
    r = _upload(c, "sneaky.zip", buf.getvalue(), "application/zip")
    assert r.status_code == 200
    from src.tailor.render.registry import get_template
    t = get_template("sneaky")
    assert t.source == "upload"
    assert t.name == "Sneaky"


def test_upload_zip_rejects_decompression_bomb(tmp_path, monkeypatch):
    """A zip whose declared (uncompressed) member sizes total more than the
    50 MB cap must be rejected before any extraction — a highly-compressible
    member (e.g. zeros) can pack a huge on-disk footprint into a tiny
    upload."""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("template.html.j2", GOOD_TEMPLATE.decode())
        z.writestr("bomb.bin", b"\x00" * (60 * 1024 * 1024))
    c = signed_in_client(_app(tmp_path, monkeypatch))
    r = _upload(c, "bomb.zip", buf.getvalue(), "application/zip")
    assert r.status_code == 200
    assert "exceeds the limit" in r.text
    assert 'class="bad"' in r.text
    templates_dir = tmp_path / "templates"
    assert not (templates_dir / "bomb").exists()
    assert not (templates_dir / ".staging" / "bomb").exists()
    from src.tailor.render.registry import list_templates
    assert "bomb" not in {t.slug for t in list_templates()}


def test_upload_zip_rejects_path_traversal(tmp_path, monkeypatch):
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("../evil.txt", "boo")
        z.writestr("template.html.j2", GOOD_TEMPLATE.decode())
    c = signed_in_client(_app(tmp_path, monkeypatch))
    r = _upload(c, "evil.zip", buf.getvalue(), "application/zip")
    assert 'class="bad"' in r.text
    assert not (tmp_path / "evil.txt").exists()


def test_upload_unsupported_extension(tmp_path, monkeypatch):
    c = signed_in_client(_app(tmp_path, monkeypatch))
    r = _upload(c, "resume.pdf", b"%PDF-")
    assert "Unsupported" in r.text


def test_upload_zip_conflicting_members_never_500s(tmp_path, monkeypatch):
    """A malformed-but-non-traversal zip (a member `x` file, plus a member
    `x/y` nested under it) used to blow up mid-extraction with a bare
    FileExistsError -> 500, and leave a partial staging dir behind. It must
    now come back as an ordinary inline gallery error with no residue
    anywhere (neither the new .staging namespace nor the old .pending one,
    and no partial pack landed in the templates dir)."""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("template.html.j2", GOOD_TEMPLATE.decode())
        z.writestr("x", "a file")
        z.writestr("x/y", "a file nested under what is supposed to be a directory")
    app = _app(tmp_path, monkeypatch)
    c = signed_in_client(app)
    r = _upload(c, "conflict.zip", buf.getvalue(), "application/zip")
    assert r.status_code == 200
    assert 'class="bad"' in r.text

    templates_dir = tmp_path / "templates"
    staging_root = templates_dir / ".staging"
    pending_root = templates_dir / ".pending"
    assert not staging_root.exists() or not any(staging_root.iterdir())
    assert not pending_root.exists() or not any(pending_root.iterdir())
    assert not (templates_dir / "conflict").exists()
    from src.tailor.render.registry import list_templates
    assert "conflict" not in {t.slug for t in list_templates()}


@requires_weasyprint
def test_upload_does_not_disturb_pending_docx_review(tmp_path, monkeypatch):
    """Direct uploads stage under .staging, a namespace separate from
    .pending (the docx-import review area). An upload sharing a slug with an
    in-flight docx import must not be blocked by it and — critically — must
    not delete it."""
    app = _app(tmp_path, monkeypatch)
    c = signed_in_client(app)
    pending_dir = tmp_path / "templates" / ".pending" / "foo"
    pending_dir.mkdir(parents=True)
    (pending_dir / "template.html.j2").write_text("<html>awaiting review</html>")

    r = _upload(c, "foo.html", GOOD_TEMPLATE)
    assert r.status_code == 200
    assert 'class="bad"' not in r.text

    # the pending docx-import survives untouched
    assert (pending_dir / "template.html.j2").read_text() == "<html>awaiting review</html>"
    from src.tailor.render.registry import list_templates
    assert "foo" in {t.slug for t in list_templates()}


def test_upload_rechecks_slug_immediately_before_rename(tmp_path, monkeypatch):
    """TOCTOU guard: if the target slug gets claimed between the route's
    initial availability check and the final rename (e.g. by a concurrent
    upload or a docx-import accept that both finish validating around the
    same time), the second one to reach the rename must lose gracefully
    instead of clobbering the first."""
    app = _app(tmp_path, monkeypatch)
    c = signed_in_client(app)

    def racing_validate_pack(pack_dir, content):
        # Simulate another writer claiming the slug while this upload was
        # off validating.
        target = tmp_path / "templates" / "race-me"
        target.mkdir(parents=True, exist_ok=True)
        (target / "template.html.j2").write_text("<html>original</html>")
        return None

    import src.tailor.render.validate as validate_mod
    monkeypatch.setattr(validate_mod, "validate_pack", racing_validate_pack)

    r = _upload(c, "race-me.html", GOOD_TEMPLATE)
    assert "already exists" in r.text
    # the racing writer's content must survive untouched — no clobber
    assert (tmp_path / "templates" / "race-me" / "template.html.j2").read_text() == (
        "<html>original</html>")
    staging_root = tmp_path / "templates" / ".staging"
    assert not staging_root.exists() or not any(staging_root.iterdir())


GOOD_IMPORT = ("<!DOCTYPE html><html><body>{{ doc.name }}"
               "{% for e in doc.experiences %}<p>{{ e.role }}</p>{% endfor %}</body></html>")


class _FakeImporter:
    async def to_template(self, data):
        from src.tailor.render.docx_import import extract_docx
        return GOOD_IMPORT, extract_docx(data)


@requires_weasyprint
def test_docx_upload_creates_pending_then_accept(tmp_path, monkeypatch):
    from tests.tailor.render.test_docx_import import make_docx
    app = _app(tmp_path, monkeypatch)
    app.state.builder.importer = _FakeImporter()
    c = signed_in_client(app)
    r = _upload(c, "Headless Resume.docx", make_docx(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert "pending review" in r.text
    from src.tailor.render.registry import list_templates
    assert "headless-resume" not in {t.slug for t in list_templates()}  # not live yet
    # preview works from pending
    p = c.get("/builder/preview", params={"slug": "headless-resume", "pending": "1"})
    assert p.content[:5] == b"%PDF-"
    # accept goes live, with extracted fonts inside the pack
    r = c.post("/builder/pending/accept", data={"slug": "headless-resume"})
    assert "headless-resume" in {t.slug for t in list_templates()}
    assert (tmp_path / "templates" / "headless-resume" / "fonts" / "Play-regular.ttf").exists()


def test_docx_upload_discard_removes_pending(tmp_path, monkeypatch):
    from tests.tailor.render.test_docx_import import make_docx
    app = _app(tmp_path, monkeypatch)
    app.state.builder.importer = _FakeImporter()
    c = signed_in_client(app)
    _upload(c, "temp.docx", make_docx())
    r = c.post("/builder/pending/discard", data={"slug": "temp"})
    assert not (tmp_path / "templates" / ".pending" / "temp").exists()
    assert r.status_code == 200


def test_docx_upload_without_importer_shows_error(tmp_path, monkeypatch):
    from tests.tailor.render.test_docx_import import make_docx
    app = _app(tmp_path, monkeypatch)
    app.state.builder.importer = None
    r = _upload(signed_in_client(app), "x.docx", make_docx())
    assert "not configured" in r.text


@requires_weasyprint
def test_docx_upload_font_traversal_confined_to_pack(tmp_path, monkeypatch):
    """A crafted font member name (word/fonts/../../../../evil.ttf) must not
    let a docx import write outside the accepted pack's fonts/ dir — both
    extract_docx's Path(name).name and the write in _import_docx defend
    against it. Assert the font lands ONLY at
    templates/<slug>/fonts/evil.ttf and nowhere else under tmp_path."""
    from tests.tailor.render.test_docx_import import make_docx
    app = _app(tmp_path, monkeypatch)
    app.state.builder.importer = _FakeImporter()
    c = signed_in_client(app)
    data = make_docx(font_entry="word/fonts/../../../../evil.ttf")
    r = _upload(c, "Evil Resume.docx", data,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert "pending review" in r.text

    r = c.post("/builder/pending/accept", data={"slug": "evil-resume"})
    from src.tailor.render.registry import list_templates
    assert "evil-resume" in {t.slug for t in list_templates()}

    templates_dir = tmp_path / "templates"
    font_path = templates_dir / "evil-resume" / "fonts" / "evil.ttf"
    assert font_path.exists()

    # no evil.ttf anywhere else under tmp_path — the traversal must be fully
    # confined to the pack's own fonts/ dir
    hits = list(tmp_path.rglob("evil.ttf"))
    assert hits == [font_path]


def test_docx_upload_importer_typeerror_never_500s(tmp_path, monkeypatch):
    """A degenerate LLM response can make strip_code_fences(None) raise
    TypeError deep inside to_template (an empty/malformed response from the
    provider). That must come back as an ordinary inline gallery error, not
    a 500, and must leave no residue under the .pending review namespace."""
    class _FakeImporterBoom:
        async def to_template(self, data):
            raise TypeError("strip_code_fences(None) — empty LLM response")

    from tests.tailor.render.test_docx_import import make_docx
    app = _app(tmp_path, monkeypatch)
    app.state.builder.importer = _FakeImporterBoom()
    c = signed_in_client(app)
    r = _upload(c, "boom.docx", make_docx(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert r.status_code == 200
    assert 'class="bad"' in r.text
    assert not (tmp_path / "templates" / ".pending" / "boom").exists()


def test_docx_reimport_failure_preserves_pending_pack_of_same_slug(tmp_path, monkeypatch):
    """A failed re-import (e.g. the LLM call errors) must not destroy a
    pre-existing pending pack of the same slug from an earlier, still-pending
    import — cleanup on failure must only apply to staging THIS request
    actually (re)built, since the LLM call itself needs no staging dir."""
    class _FakeImporterRuntimeError:
        async def to_template(self, data):
            raise RuntimeError("import LLM call failed: boom")

    from tests.tailor.render.test_docx_import import make_docx
    app = _app(tmp_path, monkeypatch)
    app.state.builder.importer = _FakeImporterRuntimeError()
    c = signed_in_client(app)

    pending_dir = tmp_path / "templates" / ".pending" / "temp"
    pending_dir.mkdir(parents=True)
    (pending_dir / "template.html.j2").write_text("<html>pre-existing pending</html>")

    r = _upload(c, "temp.docx", make_docx(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert r.status_code == 200
    assert 'class="bad"' in r.text
    assert (pending_dir / "template.html.j2").read_text() == "<html>pre-existing pending</html>"


def test_builder_content_comes_from_the_settings_document(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    raw = json.loads(Path("resume/content.example.json").read_text())
    raw["name"] = "Saved Person"
    service = make_service({}, documents={"resume_content": json.dumps(raw)})
    app = _app(tmp_path, monkeypatch, service=service)
    assert app.state.builder.content().name == "Saved Person"


def test_builder_content_falls_back_to_the_example(tmp_path, monkeypatch):
    from src.tailor.content import load_content
    from src.web.builder import EXAMPLE_CONTENT_PATH
    app = _app(tmp_path, monkeypatch)
    assert app.state.builder.content() == load_content(EXAMPLE_CONTENT_PATH)


def test_builder_importer_follows_settings(tmp_path, monkeypatch):
    service = make_service({"relevance": {"provider": "anthropic"}})
    app = _app(tmp_path, monkeypatch, service=service)
    assert app.state.builder.importer is None
    service.save_settings({"relevance": {"provider": "ollama"}}, source="cli")  # local host: no key
    assert app.state.builder.importer is not None


def test_builder_importer_degrades_when_the_builder_raises(tmp_path, monkeypatch):
    """Ruling G: a build_docx_importer exception must not 500 /builder — the
    cache builder catches it, logs, and the importer degrades to None."""
    import src.tailor.render.docx_import as docx_import_mod

    def boom(cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(docx_import_mod, "build_docx_importer", boom)
    service = make_service({"relevance": {"provider": "ollama"}})  # local host: no key needed
    app = _app(tmp_path, monkeypatch, service=service)
    assert app.state.builder.importer is None
    r = signed_in_client(app).get("/builder")
    assert r.status_code == 200

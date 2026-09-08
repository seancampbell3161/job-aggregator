from pathlib import Path

from src.tailor.render.registry import get_template

# The classic pack is self-contained: its fonts/ dir is what actually gets
# embedded when the pack renders. The OFL license must be present both at
# the original resume/fonts/ source location and copied into the pack.
_PACK_FONTS = get_template("classic").path / "fonts"
_SOURCE_FONTS = Path("resume/fonts")


def test_gelasio_fonts_present_and_valid():
    for name in ("Gelasio.ttf", "Gelasio-Italic.ttf"):
        p = _PACK_FONTS / name
        assert p.exists(), f"missing {p}"
        head = p.read_bytes()[:4]
        # TrueType magic: 0x00010000 (variable/static TTF) or 'true'
        assert head in (b"\x00\x01\x00\x00", b"true"), f"{name} not a TTF ({head!r})"


def test_ofl_license_present():
    assert (_SOURCE_FONTS / "OFL.txt").exists()
    assert (_PACK_FONTS / "OFL.txt").exists(), f"missing OFL.txt in classic pack at {_PACK_FONTS}"
    headless_fonts = Path("src/tailor/render/templates/headless/fonts")
    assert (headless_fonts / "OFL.txt").exists(), f"missing OFL.txt in headless pack at {headless_fonts}"

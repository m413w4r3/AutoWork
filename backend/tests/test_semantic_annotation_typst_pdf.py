from __future__ import annotations

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HELPERS = _ROOT / "chpTypst" / "UTILS"


def _ppm_pixels(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    offset = 0

    def token() -> bytes:
        nonlocal offset
        while offset < len(data):
            if data[offset] == ord("#"):
                end = data.find(b"\n", offset)
                offset = len(data) if end < 0 else end + 1
            elif data[offset] in b" \t\r\n":
                offset += 1
            else:
                break
        start = offset
        while offset < len(data) and data[offset] not in b" \t\r\n":
            offset += 1
        value = data[start:offset]
        if offset < len(data):
            offset += 1
        return value

    assert token() == b"P6"
    width, height, maximum = (int(token()) for _ in range(3))
    assert maximum == 255
    return width, height, data[offset:]


def test_typst_pdf_renders_semantic_bold_italic_and_gray_styles(tmp_path: Path) -> None:
    typst = os.environ.get("TYPST_BINARY") or shutil.which("typst")
    font_root = Path(os.environ.get("FONT_BUNDLE_ROOT", str(_ROOT / "chpTypst")))
    pdffonts = shutil.which("pdffonts")
    pdftotext = shutil.which("pdftotext")
    pdftohtml = shutil.which("pdftohtml")
    pdftoppm = shutil.which("pdftoppm")

    assert typst and Path(typst).is_file(), "Typst binary is required for semantic PDF verification"
    assert (font_root / "HankenGrotesk").is_dir()
    assert (font_root / "CascadiaCode").is_dir()
    assert pdffonts and pdftotext and pdftohtml and pdftoppm

    workspace = tmp_path / "semantic-pdf"
    workspace.mkdir()
    shutil.copy2(_HELPERS / "helpers.typ", workspace / "helpers.typ")
    shutil.copy2(_HELPERS / "colors.typ", workspace / "colors.typ")
    source = workspace / "fixture.typ"
    source.write_text(
        """#import "helpers.typ": semantic-text
#set page(width: 210mm, height: 70mm, margin: 8mm)
#set text(font: "Hanken Grotesk", size: 10pt)
#semantic-text((
  (style: "semantic-tool", text: "ToolFixture"),
  (style: "semantic-plain", text: " "),
  (style: "semantic-malware", text: "MalwareFixture"),
  (style: "semantic-plain", text: " "),
  (style: "semantic-product", text: "ProductFixture"),
  (style: "semantic-plain", text: " "),
  (style: "semantic-actor", text: "ActorFixture"),
  (style: "semantic-plain", text: " "),
  (style: "semantic-campaign", text: "CampaignFixture"),
  (style: "semantic-plain", text: " "),
  (style: "semantic-proper-name", text: "NameFixture"),
  (style: "semantic-plain", text: " "),
  (style: "semantic-english-term", text: "EnglishFixture"),
  (style: "semantic-plain", text: " "),
  (style: "semantic-technical", text: "PortFixture443"),
))
""",
        encoding="utf-8",
    )
    pdf = workspace / "fixture.pdf"
    subprocess.run(
        (
            typst,
            "compile",
            "--font-path",
            str(font_root / "HankenGrotesk"),
            "--font-path",
            str(font_root / "CascadiaCode"),
            str(source),
            str(pdf),
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    fonts = subprocess.run((pdffonts, str(pdf)), check=True, capture_output=True, text=True)
    assert "HankenGrotesk" in fonts.stdout
    assert "HankenGrotesk-Bold" in fonts.stdout
    assert "HankenGrotesk-Italic" in fonts.stdout
    assert "CascadiaMono" in fonts.stdout

    bbox = subprocess.run(
        (pdftotext, "-bbox", str(pdf), "-"), check=True, capture_output=True, text=True
    ).stdout
    for visible_text in (
        "ToolFixture",
        "MalwareFixture",
        "ProductFixture",
        "ActorFixture",
        "CampaignFixture",
        "NameFixture",
        "EnglishFixture",
        "PortFixture443",
    ):
        assert visible_text in bbox

    html = subprocess.run(
        (pdftohtml, "-xml", "-stdout", str(pdf)), check=True, capture_output=True, text=True
    ).stdout
    root = ET.fromstring(html)
    font_specs = {
        element.attrib["id"]: element.attrib
        for element in root.findall(".//fontspec")
        if "id" in element.attrib
    }
    styles_by_text = {}
    for element in root.findall(".//text"):
        value = "".join(element.itertext()).strip()
        if value:
            styles_by_text[value] = (font_specs[element.attrib["font"]], element)

    _tool_style, tool_element = styles_by_text["ToolFixture"]
    _malware_style, malware_element = styles_by_text["MalwareFixture"]
    _product_style, product_element = styles_by_text["ProductFixture"]
    _actor_style, actor_element = styles_by_text["ActorFixture"]
    _campaign_style, campaign_element = styles_by_text["CampaignFixture"]
    _proper_style, proper_element = styles_by_text["NameFixture"]
    _english_style, english_element = styles_by_text["EnglishFixture"]
    technical_style, _technical_element = styles_by_text["PortFixture443"]
    assert any(child.tag == "b" for child in tool_element.iter())
    assert any(child.tag == "b" for child in malware_element.iter())
    assert any(child.tag == "b" for child in product_element.iter())
    assert any(child.tag == "i" for child in actor_element.iter())
    assert any(child.tag == "i" for child in campaign_element.iter())
    assert any(child.tag == "i" for child in proper_element.iter())
    assert any(child.tag == "i" for child in english_element.iter())
    assert "CascadiaMono" in technical_style.get("family", "")

    page_prefix = workspace / "fixture-page"
    subprocess.run(
        (
            pdftoppm,
            "-f",
            "1",
            "-l",
            "1",
            "-singlefile",
            "-r",
            "144",
            str(pdf),
            str(page_prefix),
        ),
        check=True,
        capture_output=True,
    )
    width, height, pixels = _ppm_pixels(page_prefix.with_suffix(".ppm"))
    assert len(pixels) == width * height * 3
    gray_fill_pixels = sum(
        1
        for red, green, blue in zip(pixels[0::3], pixels[1::3], pixels[2::3], strict=True)
        if 180 <= red <= 200 and abs(red - green) <= 1 and abs(green - blue) <= 1
    )
    assert gray_fill_pixels > 500

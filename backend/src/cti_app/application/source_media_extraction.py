from __future__ import annotations

import base64
import binascii
import re
import struct
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from typing import Any, Protocol
from urllib.parse import unquote_to_bytes, urljoin, urlsplit
from uuid import UUID

from cti_app.domain.discovery import canonicalize_http_url
from cti_app.domain.source_media import SourceMediaReasonCode


class SourceMediaSource(Protocol):
    @property
    def source_document_id(self) -> UUID: ...

    @property
    def source_url(self) -> str: ...

    @property
    def mime_type(self) -> str: ...

    @property
    def sha256(self) -> str: ...

    @property
    def byte_size(self) -> int: ...

    @property
    def content(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class SourceMediaObservation:
    source_document_id: UUID
    source_url: str
    source_sha256: str
    source_mime_type: str
    source_byte_size: int
    dom_locator: str
    original_url: str | None
    requested_url: str | None
    page: int | None = None
    page_bbox: dict[str, float] | None = None
    anchor: str | None = None
    alt_text: str | None = None
    caption_text: str | None = None
    nearby_heading_text: str | None = None
    context_before: str | None = None
    context_after: str | None = None
    in_article_body: bool = False
    image_bytes: bytes | None = None
    pre_exclusion_reason: SourceMediaReasonCode | None = None
    is_page_excerpt: bool = False
    extraction_error: str | None = None


_BOILERPLATE = re.compile(
    r"(?:^|[\W_])(?:logo|favicon|brand|avatar|social|facebook|twitter|linkedin|"
    r"youtube|instagram|menu|navbar|navigation|breadcrumb|search|subscribe|"
    r"header|footer|banner|icon|pixel|beacon|tracking|spacer|modal|close|share|"
    r"profile|author-photo)(?:$|[\W_])",
    re.IGNORECASE,
)
_TRACKING = re.compile(r"(?:pixel|tracking|beacon|spacer|1x1)(?:[._/?=-]|$)", re.IGNORECASE)
_RELATED_CONTENT = re.compile(
    r"(?:related|recent|popular|recommended|you\s+may\s+also\s+like|read\s+more|"
    r"more\s+stories|latest\s+articles)",
    re.IGNORECASE,
)
_DECORATIVE_ASSET = re.compile(
    r"(?:modal|close|share|profile|author-photo)",
    re.IGNORECASE,
)
_LAZY_URL_ATTRIBUTES = (
    "data-src",
    "data-lazy-src",
    "data-original",
    "data-original-src",
    "data-actualsrc",
    "data-url",
)
_LAZY_SRCSET_ATTRIBUTES = ("data-srcset", "data-lazy-srcset", "data-original-srcset")
_LANDMARK_TAGS = {"aside", "footer", "header", "menu", "nav"}
_LANDMARK = re.compile(r"(?:^|[\W_])(?:nav|menu|header|footer|sidebar|social)(?:$|[\W_])", re.I)
_DATA_URI = re.compile(r"^data:([^,]*?),(.*)$", re.IGNORECASE | re.DOTALL)


@dataclass(slots=True)
class _Frame:
    tag: str
    path: str
    attrs: dict[str, str | None]


@dataclass(slots=True)
class _FigureFrame:
    candidates: list[int]
    caption: list[str]
    in_caption: bool = False


@dataclass(slots=True)
class _PictureFrame:
    srcsets: list[str]


class _CandidateParser(HTMLParser):
    def __init__(
        self,
        source: SourceMediaSource,
        *,
        boilerplate_pattern: str = _BOILERPLATE.pattern,
        landmark_pattern: str = _LANDMARK.pattern,
        tracking_pattern: str = _TRACKING.pattern,
        related_content_heading_pattern: str = _RELATED_CONTENT.pattern,
    ) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.frames: list[_Frame] = []
        self._child_counts: dict[tuple[str, ...], dict[str, int]] = {}
        self.figures: list[_FigureFrame] = []
        self.pictures: list[_PictureFrame] = []
        self.candidates: list[SourceMediaObservation] = []
        self._figure_for_candidate: list[_FigureFrame | None] = []
        self._heading: list[str] = []
        self._active_heading: list[str] | None = None
        self._active_block: list[str] | None = None
        self._active_block_tag: str | None = None
        self._last_block: str | None = None
        self._awaiting_after: list[int] = []
        self._boilerplate = re.compile(boilerplate_pattern, re.I)
        self._landmark = re.compile(landmark_pattern, re.I)
        self._tracking = re.compile(tracking_pattern, re.I)
        self._related_content_heading = re.compile(related_content_heading_pattern, re.I)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        folded = tag.casefold()
        values = {name.casefold(): value for name, value in attrs}
        parent_path = tuple(frame.tag for frame in self.frames)
        siblings = self._child_counts.setdefault(parent_path, {})
        siblings[folded] = siblings.get(folded, 0) + 1
        path = "/" + "/".join(
            [
                *(f"{frame.tag}[{self._path_index(frame.path)}]" for frame in self.frames),
                f"{folded}[{siblings[folded]}]",
            ]
        )
        frame = _Frame(folded, path, values)
        if folded == "picture":
            self.pictures.append(_PictureFrame([]))
        if folded == "figure":
            self.figures.append(_FigureFrame([], []))
        if folded == "source" and self.pictures:
            srcset = (
                values.get("srcset") or values.get("data-srcset") or values.get("data-lazy-srcset")
            )
            if srcset:
                self.pictures[-1].srcsets.append(srcset)
        if folded in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._active_heading = []
        if folded in _TEXT_BLOCK_TAGS and self._active_block is None:
            self._active_block = []
            self._active_block_tag = folded
        if folded == "img":
            self._record_image(frame)
        if folded not in _VOID_TAGS:
            self.frames.append(frame)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if folded == "figcaption" and self.figures:
            self.figures[-1].in_caption = False
            caption = _clean_text(" ".join(self.figures[-1].caption))
            if caption:
                for index in self.figures[-1].candidates:
                    self.candidates[index] = _replace_observation(
                        self.candidates[index], caption_text=caption
                    )
        if folded == self._active_block_tag and self._active_block is not None:
            block = _clean_text(" ".join(self._active_block))
            self._active_block = None
            self._active_block_tag = None
            if block:
                self._last_block = block
                for index in self._awaiting_after:
                    self.candidates[index] = _replace_observation(
                        self.candidates[index], context_after=block
                    )
                self._awaiting_after.clear()
        if folded in {"h1", "h2", "h3", "h4", "h5", "h6"} and self._active_heading is not None:
            heading = _clean_text(" ".join(self._active_heading))
            if heading:
                self._heading.append(heading)
            self._active_heading = None
        for index in range(len(self.frames) - 1, -1, -1):
            if self.frames[index].tag == folded:
                del self.frames[index:]
                break
        if folded == "figure" and self.figures:
            self.figures.pop()
        if folded == "picture" and self.pictures:
            self.pictures.pop()

    def handle_data(self, data: str) -> None:
        if self._active_heading is not None:
            self._active_heading.append(data)
        if self._active_block is not None:
            self._active_block.append(data)
        if self.figures and any(frame.tag == "figcaption" for frame in self.frames):
            self.figures[-1].caption.append(data)

    def _record_image(self, frame: _Frame) -> None:
        values = frame.attrs
        srcsets = [
            value for name in (*_LAZY_SRCSET_ATTRIBUTES, "srcset") if (value := values.get(name))
        ]
        if self.pictures:
            srcsets = [*self.pictures[-1].srcsets, *srcsets]
        original_url = _best_srcset(srcsets) if srcsets else None
        if original_url is None:
            original_url = next(
                (values.get(name) for name in _LAZY_URL_ATTRIBUTES if values.get(name)), None
            ) or values.get("src")
        original_url = original_url.strip() if original_url and original_url.strip() else None
        requested_url = _resolve_url(self.source.source_url, original_url)
        alt = _clean_text(values.get("alt") or values.get("title") or "") or None
        classes = " ".join((values.get("class") or "", values.get("id") or ""))
        landmarks = [frame.tag for frame in self.frames]
        landmarks.extend(
            f"{ancestor.attrs.get('id') or ''} {ancestor.attrs.get('class') or ''}"
            for ancestor in self.frames
        )
        label_fields = " ".join(
            (
                original_url or "",
                alt or "",
                classes,
                values.get("role") or "",
            )
        )
        reason: SourceMediaReasonCode | None = None
        nearby_heading = self._heading[-1] if self._heading else ""
        region = " ".join(landmarks)
        if _DECORATIVE_ASSET.search(label_fields) or _DECORATIVE_ASSET.search(region):
            reason = SourceMediaReasonCode.DECORATIVE_ASSET
        elif self._related_content_heading.search(
            nearby_heading
        ) or self._related_content_heading.search(region):
            reason = SourceMediaReasonCode.RELATED_CONTENT_CARD
        elif any(tag in _LANDMARK_TAGS for tag in landmarks) or any(
            self._landmark.search(value) for value in landmarks
        ):
            reason = SourceMediaReasonCode.NAVIGATION_LANDMARK
        elif self._tracking.search(label_fields):
            reason = SourceMediaReasonCode.TRACKING_PIXEL
        elif self._boilerplate.search(label_fields) or self._boilerplate.search(region):
            reason = SourceMediaReasonCode.BOILERPLATE_PATTERN
        elif original_url is None:
            reason = SourceMediaReasonCode.MISSING_SOURCE_URL
        anchor = next(
            (
                " ".join(
                    part
                    for part in (
                        ancestor.attrs.get("href"),
                        ancestor.attrs.get("title"),
                        ancestor.attrs.get("aria-label"),
                    )
                    if part
                )
                for ancestor in reversed(self.frames)
                if ancestor.tag == "a"
            ),
            None,
        )
        observation = SourceMediaObservation(
            source_document_id=self.source.source_document_id,
            source_url=self.source.source_url,
            source_sha256=self.source.sha256,
            source_mime_type=self.source.mime_type,
            source_byte_size=self.source.byte_size,
            dom_locator=frame.path,
            original_url=original_url,
            requested_url=requested_url,
            anchor=_clean_text(anchor or "") or None,
            alt_text=alt,
            nearby_heading_text=nearby_heading or None,
            context_before=self._last_block,
            in_article_body=any(ancestor.tag in {"article", "main"} for ancestor in self.frames),
            pre_exclusion_reason=reason,
        )
        self._awaiting_after.append(len(self.candidates))
        self.candidates.append(observation)
        self._figure_for_candidate.append(self.figures[-1] if self.figures else None)
        if self.figures:
            self.figures[-1].candidates.append(len(self.candidates) - 1)

    @staticmethod
    def _path_index(path: str) -> int:
        match = re.search(r"\[(\d+)\]$", path)
        return int(match.group(1)) if match else 1


_TEXT_BLOCK_TAGS = frozenset({"p", "li", "blockquote"})

_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def extract_source_media_observations(
    sources: tuple[SourceMediaSource, ...],
    *,
    boilerplate_pattern: str = _BOILERPLATE.pattern,
    landmark_pattern: str = _LANDMARK.pattern,
    tracking_pattern: str = _TRACKING.pattern,
    related_content_heading_pattern: str = _RELATED_CONTENT.pattern,
) -> tuple[SourceMediaObservation, ...]:
    observations: list[SourceMediaObservation] = []
    for source in sorted(sources, key=lambda item: (item.source_url, item.source_document_id.hex)):
        mime = source.mime_type.split(";", 1)[0].strip().casefold()
        if mime in {"text/html", "application/xhtml+xml"}:
            parser = _CandidateParser(
                source,
                boilerplate_pattern=boilerplate_pattern,
                landmark_pattern=landmark_pattern,
                tracking_pattern=tracking_pattern,
                related_content_heading_pattern=related_content_heading_pattern,
            )
            try:
                parser.feed(source.content.decode("utf-8", errors="replace"))
                parser.close()
            except Exception:
                continue
            for observation in parser.candidates:
                if observation.original_url and observation.original_url.casefold().startswith(
                    "data:"
                ):
                    decoded = _decode_data_uri(observation.original_url)
                    observations.append(_replace_observation(observation, image_bytes=decoded))
                else:
                    observations.append(observation)
        elif mime == "application/pdf":
            observations.extend(_pdf_observations(source))
    return tuple(
        sorted(
            observations,
            key=lambda item: (
                item.source_document_id.hex,
                item.page or 0,
                item.dom_locator,
                item.requested_url or "",
            ),
        )
    )


def _pdf_observations(source: SourceMediaSource) -> tuple[SourceMediaObservation, ...]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(source.content), strict=False)
    except Exception:
        return ()
    found: list[SourceMediaObservation] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            box = page.mediabox
            page_bbox = {
                "left": float(box.left),
                "bottom": float(box.bottom),
                "right": float(box.right),
                "top": float(box.top),
            }
            images = tuple(_pdf_page_image_objects(page))
        except Exception:
            continue
        for image_index, (name, image_object) in enumerate(images, start=1):
            image_bytes, error = _extract_pdf_image_bytes(image_object)
            found.append(
                SourceMediaObservation(
                    source_document_id=source.source_document_id,
                    source_url=source.source_url,
                    source_sha256=source.sha256,
                    source_mime_type=source.mime_type,
                    source_byte_size=source.byte_size,
                    dom_locator=f"pdf:page[{page_number}]/image[{image_index}]:{name[:128]}",
                    original_url=None,
                    requested_url=None,
                    page=page_number,
                    page_bbox=page_bbox,
                    image_bytes=image_bytes,
                    extraction_error=error,
                )
            )
        if images:
            found.append(
                SourceMediaObservation(
                    source_document_id=source.source_document_id,
                    source_url=source.source_url,
                    source_sha256=source.sha256,
                    source_mime_type=source.mime_type,
                    source_byte_size=source.byte_size,
                    dom_locator=f"pdf:page[{page_number}]/excerpt",
                    original_url=None,
                    requested_url=None,
                    page=page_number,
                    page_bbox=page_bbox,
                    pre_exclusion_reason=SourceMediaReasonCode.PDF_PAGE_EXCERPT_NEEDED,
                    is_page_excerpt=True,
                )
            )
    return tuple(found)


def _pdf_page_image_objects(page: Any) -> tuple[tuple[str, Any], ...]:
    found: list[tuple[str, Any]] = []
    seen: set[int] = set()

    def visit(resources: Any, prefix: str, depth: int) -> None:
        if depth > 8:
            return
        resources = resources.get_object() if hasattr(resources, "get_object") else resources
        if not hasattr(resources, "get"):
            return
        xobjects = resources.get("/XObject")
        xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
        if not hasattr(xobjects, "items"):
            return
        for name, reference in xobjects.items():
            image = reference.get_object() if hasattr(reference, "get_object") else reference
            if id(image) in seen or not hasattr(image, "get"):
                continue
            seen.add(id(image))
            subtype = str(image.get("/Subtype", ""))
            label = f"{prefix}{str(name).lstrip('/')}"
            if subtype == "/Image":
                found.append((label, image))
            elif subtype == "/Form":
                visit(image.get("/Resources"), f"{label}/", depth + 1)

    try:
        resources = page.get("/Resources")
        if resources is None and hasattr(page, "get_inherited"):
            resources = page.get_inherited("/Resources")
        visit(resources, "", 0)
    except Exception:
        return ()
    return tuple(found)


def _extract_pdf_image_bytes(image: Any) -> tuple[bytes | None, str | None]:
    try:
        filters = image.get("/Filter")
        filter_names = (
            tuple(str(item) for item in filters)
            if isinstance(filters, (list, tuple))
            else (str(filters),)
            if filters is not None
            else ()
        )
        raw_data = getattr(image, "_data", None)
        if "/DCTDecode" in filter_names and isinstance(raw_data, bytes):
            return raw_data, None
        decoded = image.get_data()
        if _sniff_embedded_image(decoded) is not None:
            return decoded, None
        if filter_names and any(
            name not in {"/FlateDecode", "/ASCII85Decode"} for name in filter_names
        ):
            return None, "unsupported PDF image filter"
        width = int(image.get("/Width", 0))
        height = int(image.get("/Height", 0))
        bits = int(image.get("/BitsPerComponent", 0))
        color_space = str(image.get("/ColorSpace", ""))
        channels_and_type = {
            "/DeviceGray": (1, 0),
            "/DeviceRGB": (3, 2),
        }.get(color_space)
        if (
            channels_and_type is None
            or bits != 8
            or width < 1
            or height < 1
            or width * height > 24_000_000
        ):
            return None, "PDF image color space or bit depth is unsupported"
        channels, color_type = channels_and_type
        stride = width * channels
        if len(decoded) != stride * height:
            return None, "PDF image byte length does not match its dimensions"
        scanlines = b"".join(
            b"\x00" + decoded[row * stride : (row + 1) * stride] for row in range(height)
        )
        ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
        return _png_bytes(ihdr, scanlines), None
    except Exception as exc:
        return None, f"PDF image extraction failed: {type(exc).__name__}"


def _png_bytes(ihdr: bytes, scanlines: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = binascii.crc32(kind + data) & 0xFFFFFFFF
        return len(data).to_bytes(4, "big") + kind + data + crc.to_bytes(4, "big")

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )


def _sniff_embedded_image(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _best_srcset(srcsets: list[str]) -> str | None:
    ranked: list[tuple[float, int, str]] = []
    order = 0
    for srcset in srcsets:
        # Data URLs contain a comma, so retain the prefix and split only on
        # commas that introduce a new URL token.
        parts = re.split(r",\s*(?=(?:https?:|/|\.|[^\s,]+))", srcset.strip(), flags=re.I)
        for part in parts:
            tokens = part.strip().split()
            if not tokens:
                continue
            url = tokens[0].rstrip(",")
            descriptor = tokens[1].casefold() if len(tokens) > 1 else "1x"
            match = re.fullmatch(r"(\d+(?:\.\d+)?)(w|x)", descriptor)
            score = float(match.group(1)) if match else 1.0
            if match and match.group(2) == "x":
                score *= 10000
            ranked.append((score, order, url))
            order += 1
    return max(ranked, default=(0.0, 0, ""))[2] or None


def _resolve_url(base_url: str, value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme and parts.scheme.casefold() not in {"http", "https", "data"}:
            return None
        if parts.scheme.casefold() == "data":
            return value
        return canonicalize_http_url(urljoin(base_url, value))
    except (TypeError, ValueError):
        return None


def _decode_data_uri(value: str) -> bytes | None:
    match = _DATA_URI.fullmatch(value)
    if match is None:
        return None
    metadata, encoded = match.groups()
    base64_encoded = any(part.casefold() == "base64" for part in metadata.split(";")[1:])
    try:
        return (
            base64.b64decode(encoded, validate=True)
            if base64_encoded
            else unquote_to_bytes(encoded)
        )
    except (ValueError, binascii.Error):
        return None


def _replace_observation(
    observation: SourceMediaObservation,
    *,
    image_bytes: bytes | None = None,
    caption_text: str | None = None,
    context_after: str | None = None,
) -> SourceMediaObservation:
    values = {
        field: getattr(observation, field) for field in SourceMediaObservation.__dataclass_fields__
    }
    if image_bytes is not None:
        values["image_bytes"] = image_bytes
    if caption_text is not None:
        values["caption_text"] = caption_text
    if context_after is not None:
        values["context_after"] = context_after
    return SourceMediaObservation(**values)


def _clean_text(value: str) -> str:
    return " ".join(value.split())[:2000]

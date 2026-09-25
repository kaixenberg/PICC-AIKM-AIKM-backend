"""Document content extraction from files and URLs.

Supports: PDF, DOCX, plain text/markdown, and web pages.
PDF ingestion uses public VLM page extraction for exact page text plus figure
metadata. Non-PDF readers keep their parser-based behavior.
"""

import base64
import io
import json
import re

from src.config.settings import settings
from src.services import minio_storage
from src.utils.logger import get_logger

logger = get_logger(__name__)

_PAGE_RENDER_DPI = 200
_FIGURE_RENDER_DPI = 300
_MIN_FIGURE_AREA_RATIO = 0.01
_MAX_FIGURE_AREA_RATIO = 0.98
_MIN_DETECTED_FIGURE_CONFIDENCE = 0.45


def read_pdf_with_scanned_page_support(file_bytes: bytes, filename: str) -> str:
    """Extract PDF content as page-scoped VLM transcription text."""
    try:
        page_documents = read_pdf_page_documents_with_visual_assets(
            file_bytes,
            filename,
            detail_id=None,
        )
        return "\n\n".join(doc["content"] for doc in page_documents).strip()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[read_pdf_with_scanned_page_support] failed filename=%s; falling back to pypdf: %s",
            filename,
            exc,
        )
        return read_file(file_bytes, filename, "application/pdf")


def read_pdf_page_documents_with_visual_assets(
    file_bytes: bytes,
    filename: str,
    detail_id: str | None,
) -> list[dict]:
    """Extract PDF pages with VLM text, figure summaries, and figure crops."""
    try:
        import fitz
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[read_pdf_page_documents_with_visual_assets] PyMuPDF unavailable "
            "filename=%s; falling back to pypdf: %s",
            filename,
            exc,
        )
        text = read_file(file_bytes, filename, "application/pdf")
        return [{"content": text, "metadata": {"doc_name": filename}}] if text else []

    try:
        parser_page_texts = _extract_pdf_pages_with_pypdf(file_bytes)

        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            page_documents: list[dict] = []
            logger.info(
                "[read_pdf_page_documents_with_visual_assets] filename=%s pages=%d detail=%s",
                filename,
                doc.page_count,
                detail_id,
            )
            print(f"No. of pages in PDF: {doc.page_count}")
            for page_index in range(doc.page_count):
                page = doc.load_page(page_index)
                page_number = page_index + 1
                print(f"Processing page: {page_number}")
                image_coverage = _estimate_image_coverage(page)
                page_image_bytes = _render_page_png(page)
                extraction = _extract_pdf_page_with_public_vlm(
                    page_image_bytes,
                    filename,
                    page_number,
                )
                method = "public-vlm-page-json"
                page_text = str(extraction.get("page_text") or "").strip()
                detected_figures = extraction.get("figures") or []

                if not page_text:
                    fallback_text = (
                        parser_page_texts[page_index]
                        if page_index < len(parser_page_texts)
                        else ""
                    ).strip()
                    if not fallback_text:
                        logger.info(
                            "[read_pdf_page_documents_with_visual_assets] skipping blank page filename=%s page=%d",
                            filename,
                            page_number,
                        )
                        continue
                    method = "parser-vlm-fallback"
                    page_text = fallback_text

                images = []
                if detail_id:
                    images = _extract_and_upload_page_figures(
                        page,
                        detail_id,
                        filename,
                        page_number,
                        detected_figures,
                    )

                page_documents.append(
                    {
                        "content": _format_page_content(
                            page_number,
                            method,
                            page_text,
                            detected_figures,
                        ),
                        "metadata": {
                            "doc_name": filename,
                            "page_number": page_number,
                            "extraction_method": method,
                            "image_coverage": image_coverage,
                            "images": images,
                        },
                    }
                )
                print("Processing complete")

            return page_documents
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[read_pdf_page_documents_with_visual_assets] failed filename=%s: %s",
            filename,
            exc,
        )
        text = read_file(file_bytes, filename, "application/pdf")
        return [{"content": text, "metadata": {"doc_name": filename}}] if text else []


def _format_page_output(page_number: int, method: str, text: str) -> str:
    return f"[Page {page_number} | extraction_method={method}]\n{text.strip()}"


def _format_page_content(
    page_number: int,
    method: str,
    page_text: str,
    figures: list,
) -> str:
    parts = [_format_page_output(page_number, method, page_text)]
    figure_lines = []
    for index, figure in enumerate(figures, start=1):
        if not isinstance(figure, dict):
            continue
        summary = str(figure.get("summary") or "").strip()
        caption = str(figure.get("caption") or "").strip()
        label = str(figure.get("label") or f"Figure {index}").strip()
        if summary or caption:
            figure_lines.append(f"{label}: {summary or caption}")
    if figure_lines:
        parts.append("[Figure summaries]\n" + "\n".join(figure_lines))
    return "\n\n".join(parts).strip()


def _extract_and_upload_page_figures(
    page,
    detail_id: str,
    filename: str,
    page_number: int,
    detected_figures: list,
) -> list[dict]:
    try:
        figure_assets = []
        page_area = max(float(page.rect.width * page.rect.height), 1.0)
        figure_index = 0

        for detected in detected_figures:
            if not isinstance(detected, dict):
                continue
            bbox = _normalized_bbox_to_page_bbox(detected.get("bbox") or [], page)
            if not bbox:
                continue
            area_ratio = _bbox_area(bbox) / page_area
            if (
                area_ratio < _MIN_FIGURE_AREA_RATIO
                or area_ratio > _MAX_FIGURE_AREA_RATIO
            ):
                continue

            figure_index += 1
            asset = minio_storage.upload_pdf_figure_image(
                _render_bbox_png(page, bbox),
                detail_id,
                page_number,
                figure_index,
                caption=detected.get("caption"),
                bbox=bbox,
            )
            if asset:
                asset["doc_name"] = filename
                asset["label"] = detected.get("label") or asset["label"]
                asset["summary"] = detected.get("summary")
                figure_assets.append(asset)

        return figure_assets
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[_extract_and_upload_page_figures] failed detail=%s page=%s: %s",
            detail_id,
            page_number,
            exc,
        )
        return []


def _bbox_area(bbox: list[float]) -> float:
    x0, y0, x1, y1 = bbox
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def _render_bbox_png(page, bbox: list[float]) -> bytes:
    import fitz

    x0, y0, x1, y1 = bbox
    padding = 16
    clip = fitz.Rect(
        max(page.rect.x0, x0 - padding),
        max(page.rect.y0, y0 - padding),
        min(page.rect.x1, x1 + padding),
        min(page.rect.y1, y1 + padding),
    )
    pix = page.get_pixmap(dpi=_FIGURE_RENDER_DPI, alpha=False, clip=clip)
    return pix.tobytes("png")


def _normalized_bbox_to_page_bbox(values: list, page) -> list[float] | None:
    if not isinstance(values, list) or len(values) != 4:
        return None
    try:
        x0, y0, x1, y1 = [float(value) for value in values]
    except (TypeError, ValueError):
        return None

    if max(x0, y0, x1, y1) <= 1.0:
        scale = 1.0
    elif max(x0, y0, x1, y1) <= 1000.0:
        scale = 1000.0
    else:
        scale = None

    if scale:
        width = float(page.rect.width)
        height = float(page.rect.height)
        x0, x1 = x0 / scale * width, x1 / scale * width
        y0, y1 = y0 / scale * height, y1 / scale * height

    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    bbox = [
        max(float(page.rect.x0), x0),
        max(float(page.rect.y0), y0),
        min(float(page.rect.x1), x1),
        min(float(page.rect.y1), y1),
    ]
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        return None
    return bbox


def _page_extraction_prompt(filename: str, page_number: int) -> str:
    return (
        "Extract this PDF page for a RAG knowledge base. Return strict JSON only.\n\n"
        f"Document: {filename}\n"
        f"Page: {page_number}\n\n"
        "Requirements:\n"
        "1. page_text must be an exact transcription of all readable text on the "
        "page. Do not summarize, paraphrase, correct, explain, reorder, or omit "
        "readable page text. Preserve line breaks when useful. Preserve tables, "
        "formulas, labels, and Bengali/English text as accurately as possible. "
        "Use [unclear] only for unreadable text.\n"
        "2. figures must contain each standalone diagram, chart, apparatus drawing, "
        "or meaningful illustration. For each figure, provide bbox coordinates "
        "normalized from 0 to 1000 relative to the full page, plus label, caption, "
        "and a concise summary of the visual content. Summarize only figures/images, "
        "not page text.\n"
        "3. If a page contains text plus a smaller diagram, return the smaller "
        "diagram box only. If the entire page is one meaningful illustration, such "
        "as a full-page landscape or storybook image, return the full illustration box.\n\n"
        "JSON shape:\n"
        "{\"page_text\":\"exact transcribed text\","
        "\"figures\":[{\"bbox\":[x0,y0,x1,y1],\"label\":\"Fig. 1\","
        "\"caption\":\"short caption\",\"summary\":\"visual summary\","
        "\"confidence\":0.0}]}"
    )


def _extract_pdf_page_with_public_vlm(
    image_bytes: bytes,
    filename: str,
    page_number: int,
) -> dict:
    try:
        from openai import OpenAI

        if not settings.openai_api_key:
            raise ValueError("OPENAI_API_KEY is missing")

        encoded_image = base64.b64encode(image_bytes).decode("ascii")
        client = OpenAI(
            api_key=settings.openai_api_key,
            timeout=settings.get_summary_timeout_public + 120,
        )
        response = client.chat.completions.create(
            model=settings.pdf_vlm_public_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _page_extraction_prompt(filename, page_number)},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{encoded_image}",
                            },
                        },
                    ],
                }
            ],
        )
        if not response.choices:
            return {}
        data = _parse_json_object((response.choices[0].message.content or "").strip())
        if not isinstance(data, dict):
            return {}
        figures = data.get("figures") or []
        if not isinstance(figures, list):
            figures = []
        data["figures"] = [
            figure
            for figure in figures
            if isinstance(figure, dict)
            and float(figure.get("confidence") or 0.0) >= _MIN_DETECTED_FIGURE_CONFIDENCE
        ]
        return data
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[_extract_pdf_page_with_public_vlm] failed filename=%s page=%s: %s",
            filename,
            page_number,
            exc,
        )
        return {}


def _parse_json_object(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}


def _extract_pdf_pages_with_pypdf(file_bytes: bytes) -> list[str]:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        return [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001
        logger.warning("[_extract_pdf_pages_with_pypdf] failed: %s", exc)
        return []


def _estimate_image_coverage(page) -> float:
    try:
        page_area = max(float(page.rect.width * page.rect.height), 1.0)
        image_area = 0.0
        for block in page.get_text("dict").get("blocks", []):
            if block.get("type") == 1 and block.get("bbox"):
                x0, y0, x1, y1 = block["bbox"]
                image_area += max(0.0, float(x1 - x0)) * max(0.0, float(y1 - y0))
        return min(image_area / page_area, 1.0)
    except Exception:  # noqa: BLE001
        return 0.0


def _render_page_png(page) -> bytes:
    pix = page.get_pixmap(dpi=_PAGE_RENDER_DPI, alpha=False)
    return pix.tobytes("png")


def read_file(file_bytes: bytes, filename: str, content_type: str | None = None) -> str:
    """Extract text from uploaded file bytes, preferring MIME type with extension fallback."""
    try:
        mime = (content_type or "").lower().split(";", 1)[0].strip()
        lower = filename.lower()
        if mime == "application/pdf" or lower.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(file_bytes))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or lower.endswith(".docx"):
            from docx import Document

            doc = Document(io.BytesIO(file_bytes))
            return "\n".join(p.text for p in doc.paragraphs)
        return file_bytes.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[read_file] failed filename=%s content_type=%s: %s",
            filename,
            content_type,
            exc,
        )
        return ""


def read_url(url: str) -> str:
    """Fetch a web page and return its readable text content."""
    try:
        import httpx
        from bs4 import BeautifulSoup

        resp = httpx.get(url, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        tags = soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "article"])
        return "\n".join(tag.get_text(strip=True) for tag in tags if tag.get_text(strip=True))
    except Exception as exc:  # noqa: BLE001
        logger.error("[read_url] failed url=%s: %s", url, exc)
        return ""

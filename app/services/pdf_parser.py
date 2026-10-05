import io


class UnreadableUploadError(Exception):
    """The uploaded file cannot be read as the PDF/image its name says it is
    (corrupt, empty, password-protected). The caller's problem: HTTP 4xx."""


class OcrUnavailableError(Exception):
    """The file needs OCR but the OCR tools are not installed on this host.
    The server's problem: HTTP 503, never a silent empty extraction."""


_OCR_UNAVAILABLE = (
    "OCR is required to read this file, but the OCR tools (Tesseract and "
    "Poppler) are not available on the server."
)


def extract_text_from_upload(filename: str, content: bytes) -> str:
    """Return raw text from an uploaded invoice file (PDF, image, or plain
    text). Real invoices are often scans with no text layer at all, so PDFs
    fall back to OCR when pdfplumber comes back empty, and image uploads
    (a photographed/scanned invoice) go straight to OCR.
    """
    lower = filename.lower()

    if lower.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")):
        return _ocr_image_bytes(content)

    if lower.endswith(".pdf"):
        text = _extract_pdf_text_layer(content)
        if text.strip():
            return text
        # No text layer found (scanned/image-only PDF) — fall back to OCR.
        return _ocr_pdf_bytes(content)

    # Fallback: treat as plain text (.txt) — useful for quickly testing
    # synthetic invoices without generating PDFs.
    return content.decode("utf-8", errors="ignore")


def _extract_pdf_text_layer(content: bytes) -> str:
    import pdfplumber

    text_parts = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
    except Exception as exc:
        raise UnreadableUploadError(
            "The uploaded file could not be read as a PDF. It may be corrupt, "
            "empty or password-protected."
        ) from exc
    return "\n".join(text_parts)


def _ocr_pdf_bytes(content: bytes) -> str:
    try:
        from pdf2image import convert_from_bytes
        from pdf2image.exceptions import PDFInfoNotInstalledError
    except ImportError as exc:
        raise OcrUnavailableError(_OCR_UNAVAILABLE) from exc

    try:
        images = convert_from_bytes(content)
    except PDFInfoNotInstalledError as exc:
        raise OcrUnavailableError(_OCR_UNAVAILABLE) from exc
    except Exception as exc:
        raise UnreadableUploadError(
            "The uploaded PDF has no text layer and could not be converted to "
            "images for OCR. It may be corrupt."
        ) from exc
    return "\n".join(_ocr_image(img) for img in images)


def _ocr_image_bytes(content: bytes) -> str:
    try:
        from PIL import Image
    except ImportError as exc:
        raise OcrUnavailableError(_OCR_UNAVAILABLE) from exc

    try:
        img = Image.open(io.BytesIO(content))
        img.load()
    except Exception as exc:
        raise UnreadableUploadError(
            "The uploaded file could not be read as an image. It may be corrupt or empty."
        ) from exc
    return _ocr_image(img)


def _ocr_image(img) -> str:
    try:
        import pytesseract
    except ImportError as exc:
        raise OcrUnavailableError(_OCR_UNAVAILABLE) from exc

    try:
        return pytesseract.image_to_string(img)
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrUnavailableError(_OCR_UNAVAILABLE) from exc
    except pytesseract.TesseractError as exc:
        raise UnreadableUploadError("OCR could not read the uploaded file.") from exc

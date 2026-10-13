"""Convert PDF pages to images for OCR processing."""

import tempfile
from pathlib import Path

import fitz  # PyMuPDF


def pdf_page_to_image(
    pdf_path: Path, page_num: int, dpi: int = 300, output_path: Path | None = None
) -> Path:
    """Render a single PDF page to a PNG image.

    Args:
        pdf_path: Path to the PDF file.
        page_num: 0-indexed page number.
        dpi: Resolution in DPI (default 300).
        output_path: Optional output path. If None, creates a temp file.

    Returns:
        Path to the PNG image file.
    """
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_num]
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)

        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            output_path = Path(tmp.name)
            tmp.close()

        pix.save(str(output_path))
        return output_path
    finally:
        doc.close()


def pdf_to_images(
    pdf_path: Path, output_dir: Path, dpi: int = 300, pages: list[int] | None = None
) -> list[Path]:
    """Render PDF pages to PNG images.

    Args:
        pdf_path: Path to the PDF file.
        output_dir: Directory to save images.
        dpi: Resolution in DPI.
        pages: List of 0-indexed page numbers. If None, all pages.

    Returns:
        List of paths to PNG image files.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(pdf_path)
    try:
        if pages is None:
            pages = list(range(len(doc)))

        image_paths = []
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)

        for page_num in pages:
            page = doc[page_num]
            pix = page.get_pixmap(matrix=mat)
            output_path = output_dir / f"page_{page_num:04d}.png"
            pix.save(str(output_path))
            image_paths.append(output_path)

        return image_paths
    finally:
        doc.close()

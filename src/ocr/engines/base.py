"""Base interface for OCR engines."""

from abc import ABC, abstractmethod
from pathlib import Path


class OCREngine(ABC):
    """Abstract base class for OCR engines."""

    name: str = "base"

    @abstractmethod
    def ocr_image(self, image_path: Path) -> str:
        """Run OCR on a single image file and return extracted text.

        Args:
            image_path: Path to a PNG/JPEG image file.

        Returns:
            Extracted text as a string.
        """
        ...

    def ocr_pdf_page(self, pdf_path: Path, page_num: int, dpi: int = 300) -> str:
        """Run OCR on a single page of a PDF.

        Converts the page to an image, then runs ocr_image().

        Args:
            pdf_path: Path to the PDF file.
            page_num: 0-indexed page number.
            dpi: Resolution for rendering (default 300).

        Returns:
            Extracted text.
        """
        from src.ocr.engines.pdf_to_images import pdf_page_to_image

        image_path = pdf_page_to_image(pdf_path, page_num, dpi=dpi)
        try:
            return self.ocr_image(image_path)
        finally:
            image_path.unlink(missing_ok=True)

    def ocr_pdf(self, pdf_path: Path, dpi: int = 300) -> str:
        """Run OCR on all pages of a PDF, concatenating results.

        Args:
            pdf_path: Path to the PDF file.
            dpi: Resolution for rendering.

        Returns:
            Full text from all pages.
        """
        import fitz

        doc = fitz.open(pdf_path)
        page_count = len(doc)
        doc.close()

        pages = []
        for i in range(page_count):
            text = self.ocr_pdf_page(pdf_path, i, dpi=dpi)
            pages.append(text)

        return "\n\n".join(pages)

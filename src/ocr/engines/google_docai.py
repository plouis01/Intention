"""Google Document AI OCR engine.

Uses the Document AI API (OCR processor) for high-quality text extraction.
Free tier: 1,000 pages/month.

Setup:
    pip install -e ".[google-ocr]"
    python scripts/ocr_pipeline/setup_google_docai.py
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src.ocr.engines.base import OCREngine

load_dotenv()

logger = logging.getLogger(__name__)


class GoogleDocumentAIEngine(OCREngine):
    """OCR engine using Google Document AI."""

    name = "google-docai"

    def __init__(
        self,
        project_id: str | None = None,
        location: str | None = None,
        processor_id: str | None = None,
    ):
        self.project_id = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT")
        self.location = location or os.environ.get("DOCAI_LOCATION", "eu")
        self.processor_id = processor_id or os.environ.get("DOCAI_PROCESSOR_ID")

        if not self.project_id or not self.processor_id:
            raise ValueError(
                "Google Document AI requires GOOGLE_CLOUD_PROJECT and DOCAI_PROCESSOR_ID. "
                "Run: python scripts/ocr_pipeline/setup_google_docai.py"
            )

        self._client = None
        self._processor_name = None

    def _get_client(self):
        if self._client is None:
            from google.api_core.client_options import ClientOptions
            from google.cloud import documentai_v1 as documentai

            opts = ClientOptions(api_endpoint=f"{self.location}-documentai.googleapis.com")
            self._client = documentai.DocumentProcessorServiceClient(client_options=opts)
        return self._client

    def _get_processor_name(self) -> str:
        if self._processor_name is None:
            client = self._get_client()
            self._processor_name = client.processor_path(
                self.project_id, self.location, self.processor_id
            )
        return self._processor_name

    def _process_document(
        self, content: bytes, mime_type: str, max_retries: int = 3
    ) -> str:
        import time

        from google.api_core.exceptions import GoogleAPICallError
        from google.cloud import documentai_v1 as documentai

        client = self._get_client()
        resource_name = self._get_processor_name()

        raw_document = documentai.RawDocument(content=content, mime_type=mime_type)
        request = documentai.ProcessRequest(name=resource_name, raw_document=raw_document)

        for attempt in range(max_retries):
            try:
                result = client.process_document(request=request)
                return result.document.text
            except (GoogleAPICallError, ConnectionError, OSError) as e:
                if attempt < max_retries - 1:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        f"Transient error (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait}s: {e}"
                    )
                    time.sleep(wait)
                else:
                    raise

    def ocr_image(self, image_path: Path) -> str:
        image_path = Path(image_path)
        content = image_path.read_bytes()

        suffix = image_path.suffix.lower()
        mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
        mime_type = mime_map.get(suffix, "image/png")

        return self._process_document(content, mime_type)

    def ocr_pdf_page(self, pdf_path: Path, page_num: int, dpi: int = 300) -> str:
        """Process a single PDF page by extracting it and sending to Document AI."""
        import fitz

        doc = fitz.open(pdf_path)
        single_pdf = fitz.open()
        single_pdf.insert_pdf(doc, from_page=page_num, to_page=page_num)

        pdf_bytes = single_pdf.tobytes()
        single_pdf.close()
        doc.close()

        return self._process_document(pdf_bytes, "application/pdf")

    def ocr_pdf(self, pdf_path: Path, dpi: int = 300) -> str:
        """Process entire PDF via Document AI (up to 15 pages per request)."""
        content = Path(pdf_path).read_bytes()
        return self._process_document(content, "application/pdf")

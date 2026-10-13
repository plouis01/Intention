"""Ollama-based VLM OCR engine.

Supports any vision model available through Ollama:
- glm-ocr (0.9B) - #1 OmniDocBench
- lightonocr-2 (1B) - French company, fast
- qwen3-vl (8B) - Best contextual understanding
- olmocr (7B) - Trained on historical/legal docs
"""

import base64
import json
import logging
import subprocess
from pathlib import Path

from src.ocr.engines.base import OCREngine

logger = logging.getLogger(__name__)

# Default OCR prompts per model (can be overridden)
DEFAULT_PROMPTS = {
    "glm-ocr": None,  # GLM-OCR doesn't need a prompt, it's OCR-native
    "lightonocr-2": None,  # Same, OCR-native
    "qwen3-vl": (
        "Transcris exactement le texte de cette page de débat parlementaire français "
        "(Journal Officiel). Préserve l'ordre de lecture : colonne gauche d'abord, "
        "puis colonne droite. Préserve tous les accents. Ne modifie rien, ne résume rien. "
        "Retourne uniquement le texte transcrit."
    ),
    "olmocr": (
        "Extract all text from this scanned page of a French parliamentary debate "
        "(Journal Officiel). Preserve reading order (left column first, then right column). "
        "Preserve all French accents. Return only the raw text."
    ),
}

# Ollama model name mapping (user-friendly name -> ollama model tag)
MODEL_TAGS = {
    "glm-ocr": "glm-ocr",
    "lightonocr-2": "maternion/LightOnOCR-2",
    "qwen3-vl": "qwen3-vl",
    "olmocr": "olmocr",
}


class OllamaVLMEngine(OCREngine):
    """OCR engine using Ollama vision models."""

    def __init__(
        self,
        model: str = "glm-ocr",
        prompt: str | None = None,
        host: str = "http://localhost:11434",
        num_ctx: int = 16384,
    ):
        """Initialize the Ollama VLM engine.

        Args:
            model: Model name (glm-ocr, lightonocr-2, qwen3-vl, olmocr).
            prompt: Custom prompt. If None, uses default for the model.
            host: Ollama API host.
            num_ctx: Context window size (GLM-OCR needs >= 16384).
        """
        self.model = model
        self.name = model
        self.model_tag = MODEL_TAGS.get(model, model)
        self.prompt = prompt or DEFAULT_PROMPTS.get(model, "Extract all text from this image.")
        self.host = host
        self.num_ctx = num_ctx

    def _check_model_available(self) -> bool:
        """Check if the model is available in Ollama."""
        try:
            import httpx
            resp = httpx.get(f"{self.host}/api/tags", timeout=5)
            if resp.status_code == 200:
                models = resp.json().get("models", [])
                return any(self.model_tag in m.get("name", "") for m in models)
        except Exception:
            pass
        return False

    def ocr_image(self, image_path: Path) -> str:
        """Run OCR on an image using Ollama vision model.

        Args:
            image_path: Path to PNG/JPEG image.

        Returns:
            Extracted text.
        """
        import httpx

        # Read and encode image as base64
        image_bytes = image_path.read_bytes()
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        # Build request
        payload = {
            "model": self.model_tag,
            "stream": False,
            "options": {
                "num_ctx": self.num_ctx,
            },
        }

        if self.prompt:
            payload["prompt"] = self.prompt
            payload["images"] = [image_b64]
        else:
            # For OCR-native models (glm-ocr, lightonocr-2), just send the image
            payload["prompt"] = "OCR"
            payload["images"] = [image_b64]

        # Call Ollama API (long timeout for large images on Apple Silicon)
        try:
            resp = httpx.post(
                f"{self.host}/api/generate",
                json=payload,
                timeout=600.0,
            )
            resp.raise_for_status()
            result = resp.json()
            return result.get("response", "").strip()
        except httpx.TimeoutException:
            logger.warning(f"Timeout for {self.model} on {image_path.name}")
            return ""
        except Exception as e:
            logger.error(f"Ollama error for {self.model}: {e}")
            return ""

    @staticmethod
    def pull_model(model: str, host: str = "http://localhost:11434") -> bool:
        """Pull a model from Ollama registry.

        Args:
            model: Model name to pull.
            host: Ollama API host.

        Returns:
            True if successful.
        """
        tag = MODEL_TAGS.get(model, model)
        try:
            result = subprocess.run(
                ["ollama", "pull", tag],
                capture_output=True,
                text=True,
                timeout=600,
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Failed to pull {tag}: {e}")
            return False

    @staticmethod
    def list_available(host: str = "http://localhost:11434") -> list[str]:
        """List available Ollama models.

        Returns:
            List of model names.
        """
        try:
            import httpx
            resp = httpx.get(f"{host}/api/tags", timeout=5)
            if resp.status_code == 200:
                return [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            pass
        return []

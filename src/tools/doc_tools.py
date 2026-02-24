import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


def ingest_pdf(pdf_path: str) -> Dict[str, Any]:

    result = {
        "full_text": "",
        "chunks": [],
        "total_pages": 0,
        "total_chunks": 0,
        "error": None,
    }

    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        result["error"] = f"PDF not found at path: {pdf_path}"
        return result

    # Try pdfplumber
    try:
        import pdfplumber

        pages_text = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            result["total_pages"] = len(pdf.pages)
            for i, page in enumerate(pdf.pages):
                text = page.extract_text() or ""
                pages_text.append((i + 1, text))

        full_text = "\n\n".join(text for _, text in pages_text)
        result["full_text"] = full_text
        result["chunks"] = _chunk_pages(pages_text, chunk_size=500, overlap=50)
        result["total_chunks"] = len(result["chunks"])
        logger.info(
            "Ingested PDF: %d pages, %d chunks",
            result["total_pages"],
            result["total_chunks"],
        )
        return result

    except ImportError:
        logger.warning("pdfplumber not available, falling back to pypdf")
    except Exception as exc:
        logger.warning("pdfplumber failed: %s, trying pypdf", exc)

    # Fallback: pypdf
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        result["total_pages"] = len(reader.pages)
        pages_text = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            pages_text.append((i + 1, text))

        full_text = "\n\n".join(text for _, text in pages_text)
        result["full_text"] = full_text
        result["chunks"] = _chunk_pages(pages_text, chunk_size=500, overlap=50)
        result["total_chunks"] = len(result["chunks"])
        return result

    except ImportError:
        result["error"] = (
            "No PDF library available. Install pdfplumber: uv add pdfplumber"
        )
        return result
    except Exception as exc:
        result["error"] = f"PDF parse failed: {exc}"
        return result


def _chunk_pages(
    pages_text: List[Tuple[int, str]],
    chunk_size: int = 500,
    overlap: int = 50,
) -> List[Dict[str, Any]]:
    """
    Split page text into overlapping word-based chunks.
    Each chunk retains its source page number for citation.
    """
    chunks = []
    chunk_index = 0

    for page_num, text in pages_text:
        words = text.split()
        if not words:
            continue

        i = 0
        while i < len(words):
            chunk_words = words[i : i + chunk_size]
            chunk_text = " ".join(chunk_words)
            chunks.append(
                {
                    "text": chunk_text,
                    "page": page_num,
                    "chunk_index": chunk_index,
                }
            )
            chunk_index += 1
            i += chunk_size - overlap  # slide with overlap

    return chunks


# Key concepts the DocAnalyst must verify per rubric.json
FORENSIC_CONCEPTS = {
    "dialectical_synthesis": [
        "dialectical synthesis",
        "dialectical",
        "thesis antithesis",
        "debate",
        "conflicting",
    ],
    "fan_in_fan_out": [
        "fan-in",
        "fan-out",
        "fan in",
        "fan out",
        "parallel branch",
        "synchronization node",
        "evidence aggregator",
    ],
    "metacognition": [
        "metacognition",
        "metacognitive",
        "thinking about thinking",
        "self-evaluation",
        "evaluating its own",
    ],
    "state_synchronization": [
        "state synchronization",
        "state sync",
        "reducer",
        "operator.ior",
        "operator.add",
        "parallel write",
        "race condition",
    ],
}


def query_pdf_for_concept(
    ingested: Dict[str, Any],
    concept_key: str,
    top_k: int = 3,
) -> Dict[str, Any]:

    keywords = FORENSIC_CONCEPTS.get(concept_key, [concept_key.replace("_", " ")])
    total_keywords = len(keywords)

    result = {
        "concept": concept_key,
        "found": False,
        "keyword_drop_warning": False,
        "substantive_explanation": False,
        "top_chunks": [],
        "total_keywords": total_keywords,
        "chunks_with_hits": 0,
        "max_page_with_hit": 0,
    }

    if ingested.get("error") or not ingested.get("chunks"):
        return result

    # Score each chunk: count how many distinct keywords appear in it
    scored = []
    for chunk in ingested["chunks"]:
        text_lower = chunk["text"].lower()
        raw_hits = sum(1 for kw in keywords if kw in text_lower)
        if raw_hits > 0:
            scored.append(
                {
                    **chunk,
                    "raw_keyword_hits": raw_hits,
                    "normalised_score": round(raw_hits / total_keywords, 3),
                }
            )

    if not scored:
        return result

    result["found"] = True
    result["chunks_with_hits"] = len(scored)
    result["max_page_with_hit"] = max(c["page"] for c in scored)

    scored.sort(key=lambda x: x["raw_keyword_hits"], reverse=True)
    result["top_chunks"] = scored[:top_k]

    # Keyword drop warning: every hit is on page 1 — exec summary only.
    # This is the "Keyword Dropping" failure mode described in the rubric.
    all_found_pages = [c["page"] for c in scored]
    if all(p <= 1 for p in all_found_pages):
        result["keyword_drop_warning"] = True

    # Substantive explanation check:
    # Require that a concept keyword AND an explanatory marker appear in the
    # SAME chunk — not just somewhere in the document.
    # A generic word like "how" on a different page from the concept does not
    # indicate an explanation of the concept.
    explanation_markers = [
        "because",
        "implemented",
        "achieved",
        "works by",
        "by using",
        "this means",
        "specifically",
        "the reason",
        "which means",
        "in order to",
        "this is how",
        "the way",
        "via the",
        "through the",
    ]
    for chunk in scored[:top_k]:
        text_lower = chunk["text"].lower()
        # Concept keyword is already confirmed present (chunk is in scored).
        # Now check if an explanatory marker also appears in this same chunk.
        if any(marker in text_lower for marker in explanation_markers):
            result["substantive_explanation"] = True
            break

    return result


def verify_all_forensic_concepts(ingested: Dict[str, Any]) -> Dict[str, Any]:
    """
    Run concept verification for all four rubric-required forensic concepts.
    Returns a combined report for the DocAnalyst Evidence output.
    """
    return {
        concept: query_pdf_for_concept(ingested, concept)
        for concept in FORENSIC_CONCEPTS
    }


def extract_file_paths_from_text(text: str) -> List[str]:
    """
    Extract all file paths mentioned in the PDF text.
    Used to cross-reference claimed files against the RepoInvestigator's manifest.

    Patterns matched:
    - src/nodes/judges.py
    - src/tools/repo_tools.py
    - pyproject.toml
    - .env.example
    - README.md
    - rubric.json
    """
    patterns = [
        r"`?(src/[a-zA-Z0-9_/]+\.(?:py|toml|md|json|txt))`?",
        r"`?([a-zA-Z0-9_]+\.(?:toml|md|json|txt|env))`?",
        r"`?(\.env[a-zA-Z0-9._]*)`?",
    ]
    found = set()
    for pattern in patterns:
        matches = re.findall(pattern, text)
        found.update(matches)

    # Clean up and filter noise
    cleaned = []
    for path in found:
        path = path.strip("`").strip()
        if path and len(path) > 2:
            cleaned.append(path)

    return sorted(cleaned)


def cross_reference_paths(
    claimed_paths: List[str],
    repo_file_manifest: List[str],
) -> Dict[str, Any]:

    manifest_set = set(repo_file_manifest)

    # Normalize: strip leading "./" if present
    def normalize(p: str) -> str:
        return p.lstrip("./").strip()

    verified = []
    hallucinated = []

    for path in claimed_paths:
        norm = normalize(path)
        # Check exact match and partial match (report may use relative paths)
        if norm in manifest_set or any(norm in m for m in manifest_set):
            verified.append(path)
        else:
            hallucinated.append(path)

    total = len(claimed_paths)
    accuracy = (len(verified) / total) if total > 0 else 1.0

    return {
        "verified": verified,
        "hallucinated": hallucinated,
        "hallucination_count": len(hallucinated),
        "accuracy_score": round(accuracy, 2),
    }


def extract_images_from_pdf(pdf_path: str) -> List[Dict[str, Any]]:
    """
    Extract all images from a PDF for VisionInspector analysis.

    Returns a list of dicts:
        {"image_bytes": bytes, "page": int, "index": int, "format": str}

    Falls back to empty list if extraction fails — VisionInspector
    handles missing images as low-confidence evidence.
    """
    images = []
    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        logger.warning("PDF not found for image extraction: %s", pdf_path)
        return images

    try:
        import pdfplumber

        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                for img_index, img in enumerate(page.images):
                    try:
                        # pdfplumber returns image metadata; use pypdf for bytes
                        images.append(
                            {
                                "page": page_num,
                                "index": img_index,
                                "x0": img.get("x0"),
                                "y0": img.get("y0"),
                                "width": img.get("width"),
                                "height": img.get("height"),
                                "image_bytes": None,  # Populated below if pypdf available
                                "format": "unknown",
                            }
                        )
                    except Exception:
                        pass

    except Exception as exc:
        logger.warning("pdfplumber image extraction failed: %s", exc)

    # Try to get actual image bytes via pypdf
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        byte_images = []
        for page_num, page in enumerate(reader.pages, start=1):
            for img_index, img_ref in enumerate(page.images):
                byte_images.append(
                    {
                        "page": page_num,
                        "index": img_index,
                        "image_bytes": img_ref.data,
                        "format": (
                            img_ref.name.split(".")[-1]
                            if "." in img_ref.name
                            else "png"
                        ),
                    }
                )
        if byte_images:
            images = byte_images

    except Exception as exc:
        logger.warning("pypdf image byte extraction failed: %s", exc)

    logger.info("Extracted %d images from PDF", len(images))
    return images

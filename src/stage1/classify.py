"""Page classification — a verbatim mirror of the pipeline's ``_classify``.

Kept byte-for-byte identical to ``sv-kb/pipeline/shared/svkb_pipeline/pdf.py`` so Stage 2 maps
``page_class`` 1:1 (Spec §5 note). Re-implemented here (rather than imported) to keep this tool's
dependency surface to PyMuPDF + stdlib only (Spec §3.8). If the pipeline rule changes, update both.
"""

from __future__ import annotations

# page_class values (Spec §4 / pipeline pdf.py)
OCR_BACKING_SCAN = "ocr_backing_scan"    # clean text layer, no images
FULL_PAGE_VISUAL = "full_page_visual"    # a scanned page / single full-page image
MIXED_CONTENT = "mixed_content"          # text + embedded images
EMBEDDED_SUBIMAGE = "embedded_subimage"  # little/no text, embedded sub-images


def classify(text_layer: str, image_count: int) -> str:
    has_text = len(text_layer.strip()) >= 50
    if has_text and image_count == 0:
        return OCR_BACKING_SCAN
    if has_text and image_count > 0:
        return MIXED_CONTENT
    if not has_text and image_count == 1:
        return FULL_PAGE_VISUAL
    if not has_text and image_count > 1:
        return EMBEDDED_SUBIMAGE
    return FULL_PAGE_VISUAL

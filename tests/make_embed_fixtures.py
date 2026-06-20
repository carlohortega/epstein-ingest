"""Synthetic FROZEN Stage-6 output (``*.chunks.json``) for the Stage-7 (embed) acceptance tests.

Stage 7 reads ONLY ``chunks.json`` (never the PDF or the sidecar), so the fixtures are real chunk artifacts
produced by sv-kb's ``chunk_pages`` (so every ``content_sha`` is genuine and the round-trip test is
meaningful). We also write a placeholder ``.pdf`` + a sentinel sidecar ``.json`` next to each so the
read-only invariant has files to hash, and:

  * inject docA's first child into docB so the SAME ``content_sha`` appears in two files -> tests GLOBAL
    cross-file dedup (the cost lever, Handoff §3.3);
  * include ``media1.chunks.json`` with NO sibling ``.pdf`` (a transcript-shaped artifact) -> tests that
    collection is source-agnostic (Handoff §8).
"""

from __future__ import annotations

import copy
import json
import os

# sv-kb's chunker — bootstrap it onto sys.path via the same resolver src/embed/collect.py uses (collect's
# import is lazy, so we must CALL it before importing svkb_pipeline here).
from src.embed.collect import _import_parents_from_dicts
_import_parents_from_dicts()
from svkb_pipeline.chunking import chunk_pages, parents_to_dicts  # noqa: E402

DATASET = "DataSet-88"
CASE = "VOL00088"
BASE = f"{DATASET}/{CASE}/IMAGES/0001"

_TEXT_A1 = ("Alpha bravo charlie delta echo. Foxtrot golf hotel india juliet. "
            "Kilo lima mike november oscar papa quebec.")
_TEXT_A2 = ("Romeo sierra tango uniform victor. Whiskey xray yankee zulu one. "
            "Two three four five six seven eight nine.")
_TEXT_B1 = ("Lorem ipsum dolor sit amet consectetur. Adipiscing elit sed do eiusmod tempor. "
            "Incididunt ut labore et dolore magna aliqua.")


def _chunks(document_name: str, pages: list[tuple[int, str]]) -> list[dict]:
    md = [(n, f"## Transcription\n\n{t}") for n, t in pages]
    return parents_to_dicts(chunk_pages(md, document_name=document_name, case_key=CASE, dataset_key=DATASET))


def _write(root: str, rel: str, chunks: list[dict], *, with_pdf: bool = True) -> None:
    base = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(base), exist_ok=True)
    if with_pdf:
        open(base + ".pdf", "wb").close()                     # placeholder for the read-only hash set
        with open(base + ".json", "w", encoding="utf-8") as f:  # sentinel sidecar (Stage 7 must NOT read it)
            json.dump({"status": "ok", "sentinel": "DO_NOT_TOUCH"}, f, ensure_ascii=False, indent=2)
    with open(base + ".chunks.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(chunks))


def build_embed(root: str) -> dict:
    """Lay down the fixture corpus. Returns a small dict of facts the test asserts against (the shared
    content_sha and the document layout)."""
    doc_a = _chunks("docA", [(1, _TEXT_A1), (2, _TEXT_A2)])
    doc_b = _chunks("docB", [(1, _TEXT_B1)])

    # Inject docA's first child into docB's first parent -> the same content_sha now lives in BOTH files.
    shared = copy.deepcopy(doc_a[0]["children"][0])
    doc_b[0]["children"].append(shared)

    _write(root, f"{BASE}/docA", doc_a)
    _write(root, f"{BASE}/docB", doc_b)

    # A media-shaped chunks.json with NO sibling .pdf (source-agnostic collection, §8).
    media = _chunks("media1", [(1, "Spoken words one two three four. Five six seven eight nine ten eleven.")])
    _write(root, f"{DATASET}/media/media1", media, with_pdf=False)

    return {"shared_content_sha": shared["content_sha"],
            "shared_embed_input": f"{shared['context_prefix']} {shared['text']}"}

"""Production ingestion package for CAG Inspection Report PDFs (Phase 2).

Pipeline per PDF:
  excel_index -> layout (Docling, x-column split) -> headings -> segmenter
  -> textrepair -> tables -> quality -> (reconstruct) -> store/embed
  -> validate -> report

Run:  python -m ingest --pdf-dir data --excel "data/....xlsx"
"""

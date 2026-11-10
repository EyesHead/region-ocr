# Formula bands in a ruled scan table

The detector must retain a closed table whose internal column rules end at the header. Full-width body cells may contain independent positioned equation packets. A blank source gap can separate those packets into logical display rows; a multi-line packet remains atomic.

Portable checks (no PDF or OCR models):

    python -B -m unittest discover -s regression/formula_bands

Private integration snapshot, copied to a separate CASE directory:

- source.pdf and normalized.pdf
- baseline/docling_before_export.json
- baseline/surya_records.json
- baseline/input.json

    python -B regression/formula_bands/replay_snapshot.py --case CASE --pages 12 --output CASE/replayed

Set REGION_OCR_FORMULA_REPLAY to CASE and rerun the tests for the optional integration assertion. The reported GOST R 21.514-2025 table B.3 is on PDF page 12: expected 14 columns, 2 header rows, 7 body rows each spanning all columns, 8 body equations, no duplicate outside equations and no uncovered body ink. Actual source files and generated outputs are intentionally private.

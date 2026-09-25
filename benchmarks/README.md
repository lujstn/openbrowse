# Benchmark results

Each folder is one batch: the answer key captured from the live board (`key.json`), every raw session export (`<model>-<effort>-<variant>-<n>.json`) and the scores (`scores.json`). Variant A names every field; B leaves `visaSponsorship` unnamed.

Re-score a batch with:

```bash
python -m scripts.benchmark score benchmarks/<batch>/*-[AB]-*.json --key benchmarks/<batch>/key.json
```

Run one against your own server with `python -m scripts.benchmark run --model <model> --effort <effort> --variant A -o <file>`, and capture a fresh key with `python -m scripts.benchmark key -o <file>`.

## `2026-09-25-v1-prompt`

Seven runs of the first prompt (`spec.json`), which named six fields and asked for an `extra` array its schema didn't have. They ran between 00:32 and 01:48 London time and are scored against a key captured at 18:28 the same day, limited to the 12 roles listed at the time. They're kept to compare the two prompts, not as published results. Re-score them with `--spec benchmarks/2026-09-25-v1-prompt/spec.json --spec-variant "v1 prompt"`.

## `2026-09-25-trial`

One run of variant A, `gpt-5.6-terra` at effort `none`, to try the new prompt and scorer before a full batch. It found all 14 roles and invented nothing, but filled only the fields `read_pages` drafted from the pages' structured data, then marked the rest absent: 53.5% accuracy.

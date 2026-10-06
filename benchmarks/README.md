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

One run of variant A of the version 2 prompt, `gpt-5.6-terra` at effort `none`, to try the new prompt and scorer before a full batch. It found all 14 roles and invented nothing, but filled only the fields `read_pages` drafted from the pages' structured data, then marked the rest absent. By its own prompt that is 67.5% accuracy, and 54.8% by version 3's rules, which ask for visa sponsorship, skills and the company description wherever the description states them (`scores-as-v3.json`). The keys in this folder and in `2026-09-25-v1-prompt` record where each value lives, added after the runs from the same captures.

## `2026-10-07-rerun`

The same run as the trial, on version 3 of the prompt and with `fill_from_pages` in place: all 12 roles, 98.2% accuracy and 100% proactive in 1m 45s for $0.44, against the trial's 54.8% by the same rules for $0.24. It got two values wrong when it read the pages at effort `none`: a seniority taken from a requirement ("senior data science individual contributor") instead of the title's "Head", and `payPeriod: FIXED_TERM` on a fixed-term role that shows no pay, which counts as invented.

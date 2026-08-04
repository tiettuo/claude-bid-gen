---
name: bid-gen
description: Run and improve the bid-gen-v3 engineering bid generation pipeline for Chinese construction tender technical proposals. Use when Codex needs to process a tender/RFP project directory, extract bid requirements, build project_brain/chapter_plan/disqualification_rules, generate or review construction organization design chapters, assemble DOCX output, run compliance/format/similarity gates, debug bid-gen scripts, or apply bid-gen memory feedback.
---

# Bid Gen

Use this skill for the local bid-gen-v3 project at `$BID_GEN_HOME`. It is a script-backed pipeline, not a generic proposal-writing prompt.

## First Moves

1. Treat the user's tender directory as the project input. If no path is provided, ask for it.
2. Set the repo root mentally as `BID_GEN_HOME=$BID_GEN_HOME`.
3. Read `commands/bid-gen.md` for the full Step 0-6 operating prompt before running a real bid.
4. Read `references/extraction-methodology.md` when working on Step 1, Step 1.5, Step 2, or extraction/plan verification bugs.
5. For code changes, scan relevant `memory/feedback_bid_*.md` files first. These are production lessons and should override generic instincts.

## Operating Commands

Use these commands from `$BID_GEN_HOME`:

```bash
python3 scripts/bid_run.py --doctor
python3 scripts/bid_status.py "/path/to/tender-project"
python3 scripts/bid_run.py "/path/to/tender-project"
python3 scripts/bid_run.py "/path/to/tender-project" --resume
```

For manual step control:

```bash
python3 scripts/bid_extract.py "/path/to/tender-project"
python3 scripts/bid_skeleton_extract.py "/path/to/tender-project"
python3 scripts/bid_plan_verify.py "/path/to/tender-project"
python3 scripts/bid_writer.py "/path/to/tender-project"
python3 scripts/bid_reviewer.py "/path/to/tender-project"
python3 scripts/bid_assemble.py "/path/to/tender-project/output/"
python3 scripts/bid_compliance.py "/path/to/tender-project"
python3 scripts/bid_similarity.py "/path/to/tender-project"
```

## Non-Negotiable Bid Rules

- Chapter anchors equal `technical scoring table ∪ mandatory construction-organization outline`; do not use a fixed chapter template.
- For pass/fail technical proposals, mandatory outline topics must map 1:1 to chapters. Do not merge, absorb, or omit topics such as "其他".
- `project_brain.md` sections 5, 6, and 7 must be translated from `evaluation_method.json`, `disqualification.json`, and `mandatory_outline.json`; do not rewrite from memory.
- Before page/word budgeting, manually search extracted text for implicit hard constraints such as `不得超过`, `总页数`, `不得少于`, `必须包含`, `必须涵盖`, `至少`, `视为`.
- If the tender defines a dedicated `附图及附表` chapter, put all required charts/tables there and reference them from body chapters.
- Use Word-rendered page counts for final page validation on macOS; LibreOffice page counts are not authoritative for this pipeline.
- Do not hardcode fonts, sizes, margins, or line spacing in assembly code. Route formatting through parsed rules and `fmt`.

## Editing Guidance

- Preserve the Step 0-6 contract described in `commands/bid-gen.md`.
- Keep new scripts in `scripts/` import-compatible with sibling modules.
- Put new configuration in `configs/`.
- Put reports under `output/reports/`, logs under `output/logs/`, charts under `output/charts/`, chart data under `output/charts_data/`.
- Keep chapter markdown files directly under `output/`; downstream scripts scan that root.
- After modifying pipeline scripts, run the most relevant pytest file plus `python3 scripts/bid_run.py --doctor`.

## Key References

- `commands/bid-gen.md`: full operational prompt and workflow.
- `references/extraction-methodology.md`: v2 extraction closure methodology.
- `docs/chart_schemas.md`: chart renderer schemas.
- `memory/feedback_bid_pass_fail_anchor.md`: pass/fail outline anchoring.
- `memory/feedback_bid_b3_implicit_constraint.md`: implicit hard-constraint extraction.
- `memory/feedback_bid_attachment_chapter.md`: attachment/charts chapter placement.
- `memory/feedback_bid_no_hardcode_size.md`: formatting code safety.
- `memory/feedback_bid_output_layout.md`: output directory layout.

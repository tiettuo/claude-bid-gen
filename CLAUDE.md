# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`bid-gen-v3` generates Chinese construction-tender technical proposals (施工组织设计). It is a **script-backed pipeline orchestrated by a prompt**, not a generic proposal-writing assistant. The full operating contract lives in [commands/bid-gen.md](commands/bid-gen.md) (~37KB, Step 0–6); the agent-facing entry is [skills/bid-gen/SKILL.md](skills/bid-gen/SKILL.md). Run a real bid by pointing the orchestrator at a tender directory, not by improvising prose.

## Common commands

Run from repo root (`BID_GEN_HOME` defaults to this directory — `scripts/`'s parent — and rarely needs setting).

```bash
# Step −1: ingest a raw tender .zip → normalized tender dir (GBK filenames, recursive
# container unwrap for .XZZ/.GKZJ/.ZBWJ, canonical 招标文件.pdf promoted to the root)
python3 scripts/bid_ingest_zip.py "<项目>.zip" --out-dir "<parent dir>"

# Static-TOC page backfill (Word→PDF → PyMuPDF locate headings → fill @@TOCPGn@@).
# Never write w:updateFields into a generated DOCX — Word then always shows a modal
# "fields may refer to other files" alert that no preference can disable.
python3 scripts/bid_toc_fill.py "<tender_dir>"

# Health check (packages, scripts present, API keys). Run after any dep/env change.
python3 scripts/bid_run.py --doctor

# Initialize LLM config template if configs/bid_llm_config.json is missing
python3 scripts/bid_llm_client.py --init

# Full pipeline (auto-detects single vs. batch mode by presence of extracted/variants.json)
python3 scripts/bid_run.py "<tender_dir>"
python3 scripts/bid_run.py "<tender_dir>" --resume      # continue from last failed step
python3 scripts/bid_run.py "<tender_dir>" --from write --to assemble
python3 scripts/bid_run.py "<tender_dir>" --dry-run
python3 scripts/bid_run.py "<tender_dir>" --force-continue   # ignore review/similarity gates

# Loop orchestration: retry loop + budget circuit-breaker + failure ledger + Feishu alert
python3 scripts/bid_loop.py "<tender_dir>" [--max-cycles 3]

# Read-only progress / blockers
python3 scripts/bid_status.py "<tender_dir>"

# Chart renderer self-check (should be 14/14)
python3 scripts/bid_charts_selftest.py
```

**Tests** — plain pytest from repo root; no `pyproject.toml`/`pytest.ini`. [tests/conftest.py](tests/conftest.py) prepends `scripts/` to `sys.path` so tests `import bid_*` directly.

```bash
pytest -q
pytest tests/test_bid_loop.py -q                          # one file
pytest tests/test_bid_loop.py::test_name -q                # one test
```

After editing pipeline scripts, run the most relevant test file **plus** `bid_run.py --doctor`.

**System deps:** `pip install -r requirements.txt`; `pandoc` is a hard binary requirement for Step 4 (DOCX assembly) — `brew install pandoc` / `apt-get install pandoc`. Chinese fonts (PingFang/Noto CJK) needed for matplotlib chart rendering. Optional packages (simhash, jieba, PyMuPDF) degrade gracefully when missing.

## Architecture

### Pipeline and its two orchestration layers

The pipeline is the ordered `STEPS` list in [scripts/bid_run.py](scripts/bid_run.py) — **that list is the source of truth**, not the README (which lags behind). Current order (single-shot per tender dir):

```
extract → template → facts → derive → skeleton → special → input_assemble →
fingerprint → variants → plan_variants → chart_specs → write → review →
coherence_pass → assemble → verify → compliance → similarity → summary
```

> **连贯性主线注入(散装→茅台)**:`coherence_pass` 是可选工序(受 `configs/bid_coherence.json enabled` 开关控制,默认 off)。启用后还会触发工序A(`deployment_synthesize` 在 Step2 链 + plan 派生 `chapter_narrative.json` + writer Block1 难点精准注入)和工序B(post-writer `bid_chapter_intro` 重写引言回指主线)。详见 [memory/feedback_bid_coherence.md](memory/feedback_bid_coherence.md)。

Two layers coordinate this:

- **`bid_run.py`** — single-shot orchestrator. Writes per-step status (`ok`/`fail`/`skip`/`running`/`interrupted`) to `output/.bid_run.state.json` (the checkpoint). `--resume` reruns from the last non-`ok` step; stale `running` steps older than 2h are auto-repaired to `interrupted`.
- **`bid_loop.py`** — wraps `bid_run.py --loop-mode` in a retry loop. Owns the **quality gates + budget circuit-breaker + failure ledger + escalation**. Writes `output/.bid_loop.state.json`. Loop exit-code convention (in `bid_run.py`/`bid_loop.py`): `0` success / `2` hard env failure (fingerprint/directory mismatch — no retry) / `3` budget exceeded (no retry) / other nonzero = retryable step failure.

### Quality gates (the recent work — `--loop-mode`)

Gates run after the relevant step and, when `--loop-mode` is on, attempt auto-fix before failing. Policy is data-driven from [configs/bid_loop_policy.json](configs/bid_loop_policy.json):

- **verify** (`bid_verify.py`, Step 4.5): opens the final DOCX and checks cover present, body font/size, page count ≤ hard cap, multi-variant formatting genuinely differentiated (anti-collusion), and scans assemble logs for danger keywords. `auto_fixable` (e.g. `cover_missing`→`reassemble_variant`, `page_over_cap`→`concise_then_reassemble`) retries up to N; `requires_human` (e.g. `format_identical`, `missing_artifact`) aborts.
- **compliance** (`bid_compliance.py`, Step 5): blind-bid violations, format severity, fabricated standards, schedule violations. `response_keyword_miss`/`chart_missing` auto-rewrite the offending chapter then rescan; `blind_violation`/`standards_fabricated` etc. require human.
- **review** (Step 3.5) and **similarity** (Step 6): `major` review or similarity-over-threshold triggers `bid_writer.py --force` rewrite of the offending chapter (max 2 attempts each).
- **Budget circuit-breaker**: token/cost/wall-clock caps (`bid_loop_policy.json` budget_policy) → exit 3, no retry. Token tracking covers `writer`/`concise` (the cost heads); the synthesize chain is small single calls, not counted (<5% error).

Gate decisions are recorded into the state files for post-hoc tracing. `--force-continue` (single-shot) skips the review/similarity gates only.

### v2 skeleton-first methodology (non-negotiable)

Three historical projects failed because Step 2 let the model write `project_brain.md` from memory. The fix: **Step 1.5 (`bid_skeleton_extract.py`) emits three JSONs first, and Step 2's brain sections 5/6/7 are 1:1 translations of them — never rewritten.**

- `extracted/evaluation_method.json` → brain §5 (scoring table) — sole source
- `extracted/disqualification.json` → brain §6 (disqualification clauses, each with `source_page`) — sole source
- `extracted/mandatory_outline.json` → brain §7 (mandatory outline topics) — sole source

Methodology details: [skills/bid-gen/references/extraction-methodology.md](skills/bid-gen/references/extraction-methodology.md) (four-phase closure + eight iron rules + historical failure cases).

### Step 2 is auto-synthesized in v3

In v3 the default is the **auto-synth chain**, not hand-writing: after Step 1 the orchestrator calls `bid_difficulty_synthesize.py` → `bid_brain_translate.py` (`project_brain.md`) → `bid_rules_synthesize.py` (`disqualification_rules.md`) → `bid_plan_synthesize.py` (`chapter_plan.md`), then `bid_plan_verify.py`. The model's role is **review + adjust the drafts**, not write from scratch. Use `--manual-step2` for the old hand-written path.

### Cross-script sharing: one common module

All ~37 `scripts/bid_*.py` are co-located and import each other directly (`from bid_common import ...`), relying on directory co-location. **Cross-script utilities live in [scripts/bid_common.py](scripts/bid_common.py)** — the canonical implementation for: logging (`make_script_logger`/`setup_logger`), `parse_chapter_plan`, blind-bid detection (`detect_blind_bid`/`scan_blind_bid_violations`/`BLIND_PATTERNS`), `collect_chapter_files`, heading/keyword extraction. Do not duplicate these — extend the module.

### LLM client

[scripts/bid_llm_client.py](scripts/bid_llm_client.py) wraps OpenAI-compatible endpoints (GLM / DeepSeek / Qwen / Kimi / MiniMax / 豆包), configured in [configs/bid_llm_config.json](configs/bid_llm_config.json). API keys load from `.env` (default `~/.env`; override with `LLM_ENV_FILE=`). Per-model `output_char_cap` is enforced — text generation must respect it. Token usage is tracked via `LLMStats`/`LLMUsage` and flushed to `output/logs/llm_stats.json`, feeding the budget circuit-breaker.

[scripts/bid_vision.py](scripts/bid_vision.py) + [configs/bid_vision_config.json](configs/bid_vision_config.json) drive layered visual OCR (Qwen-VL-Max primary, GLM-OCR fallback) over 7 keyword classes. Three classes — 废标条款 / 暗标-排版-装订 / 评分办法 — force-trigger the vision model even when pdfplumber already extracted text, because they decide disqualification/score.

## Non-negotiable bid rules (domain constraints)

These come from the operating prompt and override generic instincts. Read the relevant [memory/feedback_bid_*.md](memory/) before changing extraction/plan/assembly code — they are production lessons from real failed bids.

- **Chapter anchors = 「技术标评分表」∪「施组编制大纲」** (semantic union, deduped). No fixed chapter template. For scoring projects the scoring table dominates; for pass/fail projects (技术标总分=0) the mandatory outline dominates.
- **Pass/fail proposals:** mandatory-outline topics map 1:1 to chapters. Do not merge, absorb, or omit topics — including "其他".
- **Copy chapter titles verbatim** from the scoring table / outline; never invent or paraphrase.
- **brain §5/6/7 are 1:1 JSON translations** (see methodology above).
- **Before page/word budgeting**, manually grep extracted text for implicit hard constraints: `不得超过`, `总页数`, `不得少于`, `必须包含`, `必须涵盖`, `至少`, `视为`.
- **Do not hardcode** fonts, sizes, margins, or line spacing in assembly code (`bid_assemble.py`/`bid_concise.py`/etc.). Route all formatting through parsed rules and the `fmt` structure.
- **Word-rendered page counts are authoritative** for final page validation on macOS; LibreOffice counts are not.

## Conventions

- **New scripts** go in `scripts/`, must stay import-compatible with siblings (keep `sys.path.insert(0, str(Path(__file__).resolve().parent))`).
- **New config** goes in `configs/`, referenced as `Path(os.environ.get("BID_GEN_HOME", str(Path(__file__).resolve().parent.parent))) / "configs" / "..."`. Add new deps to both `requirements.txt` and `REQUIRED_PKGS`/`OPTIONAL_PKGS` in `bid_run.py`.
- **Output layout under `output/`** (gitignored): chapter markdown at the `output/` root (downstream scripts scan that root); batch variants under `output/VXX/`; reports → `output/reports/`; logs → `output/logs/`; charts → `output/charts/`; chart data → `output/charts_data/`. Do not nest chapter files — collection scans the root.
- **memory/** holds experience (feedback = must-read production lessons; reference = methodology; project = past-case studies; user = persona). Read relevant `feedback_bid_*.md` first when touching extraction/plan/assembly.

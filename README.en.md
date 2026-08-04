# bid-gen — Construction Tender Technical Proposal Generator

[中文版](README.md)

An end-to-end pipeline that generates **施工组织设计** (construction organization design / technical proposals) for Chinese construction tenders — from raw tender PDF/DOCX extraction, through machine review and compliance checks, to batch multi-bidder similarity screening against collusion detection.

**Form**: script pipeline + agent orchestration. Portable to any agent that supports prompts + bash tool calls (Claude Code / Codex / pi / hermas / OpenClaw, etc.).

---

## Highlights

- **Extraction-first, not memory-first**: key facts (evaluation method, mandatory outline, disqualification clauses) are extracted by scripts into structured JSON before any prose is written. The LLM never improvises chapter anchors from memory — historical projects failed exactly this way.
- **Chapter anchors = semantic union of the scoring table and the mandatory outline** (deduplicated), copied verbatim from source. No fixed chapter templates.
- **Two evaluation modes**: score-based (评分制) vs pass/fail (合格制) projects are handled with different anchoring strategies, auto-detected from the extracted evaluation method.
- **Batch multi-bidder mode**: ≥2 bidders get differentiated fingerprints (chapter titles, format sets, writing styles) plus cross-bidder similarity checks to avoid 围标/串标 (bid-rigging) detection.
- **Blind-bid (暗标) compliance**: no company names, logos, real names, decorative characters, or markdown italics — enforced per-chapter with no whitelist.
- **Gate reports**: compliance scan, format audit, similarity matrix, and expert-review scoring against the tender's scoring table.
- **264 passing tests** covering extraction, planning, writing, assembly, compliance, and chart rendering.
- **Battle-tested on real tenders**: the `memory/` directory is a distilled knowledge base of production lessons — each `feedback_*.md` documents a real failure and its fix.

---

## Directory Structure

```
bid-gen/
├── README.md                    # English: this file (README.en.md) | 中文: README.md
├── requirements.txt             # Python dependencies (required + optional)
├── .env.example                 # API key template
│
├── commands/
│   └── bid-gen.md               # Master prompt (Step 0–6 workflow definition)
│
├── skills/bid-gen/references/
│   └── extraction-methodology.md # v2 methodology: four-phase closure + eight iron rules + historical failures
│
├── scripts/                     # 46 Python scripts (pipeline implementation)
│   ├── bid_run.py               # One-shot orchestrator (Step 1–6)
│   ├── bid_status.py            # Read-only progress / blockers
│   ├── bid_extract.py           # Step 1: PDF/Word/Excel extraction + layered visual OCR
│   ├── bid_vision.py            #         Visual model wrapper (Qwen-VL / GLM-OCR)
│   ├── bid_fingerprint.py       #         PDF fingerprint classification
│   ├── bid_skeleton_extract.py  # Step 1.5: skeleton JSONs (evaluation/outline/disqualification)
│   ├── bid_facts_extract.py     #           13-field fact extraction
│   ├── bid_derive.py            #           Tier 2 derived fields
│   ├── bid_special_extract.py   #           Tier 4 special extraction
│   ├── bid_template_match.py    #           Tier 0 template matching
│   ├── bid_brain_translate.py   #           JSON → project_brain.md translation
│   ├── bid_plan_verify.py       # Step 2: iron-rules verification
│   ├── bid_chart_specs.py       #         Chart spec derivation
│   ├── bid_variants.py          # Step 2.5: batch bidder configuration
│   ├── bid_writer.py            # Step 3: chapter generation (downstream LLMs)
│   ├── bid_expand.py            #         Chapter expansion helper
│   ├── bid_reviewer.py          # Step 3.5: machine review + severity
│   ├── bid_assemble.py          # Step 4: DOCX assembly (largest script)
│   ├── bid_charts.py            #         7 chart renderers
│   ├── bid_charts_selftest.py   #         Renderer self-check (14/14 should pass)
│   ├── bid_format_audit.py      #         Format audit (DOCX inspection layer)
│   ├── bid_input_assemble.py    #         Input assembly
│   ├── bid_compliance.py        # Step 5: disqualification compliance scan
│   ├── bid_similarity.py        # Step 6: cross-bidder similarity (batch only)
│   ├── bid_common.py            # Shared utilities
│   └── bid_llm_client.py        # LLM client wrapper
│
├── configs/
│   ├── bid_llm_config.json      # Model pool (GLM/DeepSeek/Qwen/Kimi/MiniMax/Doubao)
│   └── bid_vision_config.json   # Visual OCR keyword-trigger rules
│
├── docs/
│   ├── chart_schemas.md         # Exact schemas for the 7 chart renderers
│   └── bid-gen-flowchart.html   # Business flow diagram (Mermaid, open in browser)
│
└── memory/                      # Distilled experience (feedback lessons + sanitized case studies)
    ├── feedback_bid_*.md        # Production lessons (must-read before modifying code)
    ├── reference_bid_*.md       # Reference material
    └── project_*.md             # Historical case studies (sanitized: no client names/amounts/IDs)
```

---

## Installation

### 1. Install dependencies

```bash
pip install -r requirements.txt
# macOS: brew install pandoc
# Ubuntu: apt-get install pandoc fonts-noto-cjk
```

### 2. Configure API keys

```bash
cp .env.example .env
# Fill in at least one LLM API key (recommended: GLM_API_KEY + DASHSCOPE_API_KEY)
```

Scripts read `~/.env` by default; override with `LLM_ENV_FILE=/path/to/.env`.

### 3. Set BID_GEN_HOME (optional)

```bash
export BID_GEN_HOME=/absolute/path/to/bid-gen
```

**Not required** — every script falls back to `Path(__file__).parent.parent` (the repo root) and loads configs from `configs/`.

### 4. Health check

```bash
python $BID_GEN_HOME/scripts/bid_run.py --doctor
```

Checks: Python packages present, all scripts in place, API keys configured.

### 5. Run a real tender

```bash
python $BID_GEN_HOME/scripts/bid_run.py /path/to/tender-dir
```

`bid_run.py` keeps a per-step checkpoint at `<tender_dir>/output/.bid_run.state.json` (`ok/fail/skip`). **Step 2 must be completed by the LLM interactively** (brain/plan/rules) — the script detects missing files and waits.

---

## Agent Integration Points

The master prompt `commands/bid-gen.md` is designed as a Claude Code slash-command format using the `$ARGUMENTS` placeholder for the tender directory path. To integrate into hermas or another agent:

1. **Prompt loading**: load `commands/bid-gen.md` as the core system-prompt module.
2. **`$ARGUMENTS` substitution**: inject the user-supplied tender directory into the `$ARGUMENTS` placeholder.
3. **`$BID_GEN_HOME` substitution** (optional): if the agent does not expand env vars, substitute the repo's absolute path at load time.
4. **Memory loading**: load `memory/` as a persistent knowledge base (feedback files are rules to obey when writing code; project files are reference case studies).
5. **Tool permissions**: the agent needs Bash (run Python scripts) + Read/Write/Edit (operate brain/plan/rules markdown and chapter markdown).

---

## Workflow Overview (Step 0–6)

| Step | Main script | Input | Output |
|---|---|---|---|
| **Step 1** | `bid_extract.py` | Tender PDF/DOCX/XLSX | `extracted/*.txt` + visual OCR segments |
| **Step 1.5** | `bid_skeleton_extract.py` | extracted/ | `evaluation_method.json` / `mandatory_outline.json` / `disqualification.json` |
| **Step 2** | (LLM, interactive) | three JSONs | `project_brain.md` / `chapter_plan.md` / `disqualification_rules.md` |
| **Step 2.4** | `bid_plan_verify.py` | brain + plan | iron-rules verification report |
| **Step 2.5** | `bid_variants.py` | brain | `variants.json` (batch bidder config) |
| **Step 3** | `bid_writer.py` or LLM | plan + brain | `output/[VXX/]0X_*.md` |
| **Step 3.5** | `bid_reviewer.py` | chapter md | `review.json` + severity levels |
| **Step 4** | `bid_assemble.py` | chapter md | `*.docx` |
| **Step 5** | `bid_compliance.py` | chapter md + docx | compliance report |
| **Step 6** | `bid_similarity.py` | multiple docx (batch) | `similarity_matrix.json` + similarity report |

Full flow diagram: `docs/bid-gen-flowchart.html` (Mermaid, open in a browser).

---

## Core Principles (from the master prompt)

> **Chapter anchors = 「Technical scoring table」 ∪ 「Mandatory outline」 (semantic union, deduplicated). No fixed chapter template.**
> - **Score-based projects**: the scoring table is the primary anchor.
> - **Pass/fail projects**: the mandatory outline is the primary anchor.
> - Copy chapter titles **verbatim** from the source. Never invent or paraphrase.
> - Reviewers look up chapters against the scoring table/outline line by line; a missing chapter = point loss / disqualification.

**v2 core rule**: sections 5/6/7 of `project_brain.md` are **1:1 JSON translations** of the Step 1.5 skeleton JSONs — never rewrite them from memory (this was the root cause of three historical failures; see `skills/bid-gen/references/extraction-methodology.md`).

---

## Maintenance Notes

- **Co-located imports**: scripts import siblings directly (e.g. `from bid_common import ...`), so all `.py` files under `scripts/` must stay in the same directory.
- **New dependencies**: update both `requirements.txt` and `REQUIRED_PKGS` / `OPTIONAL_PKGS` in `scripts/bid_run.py`.
- **New configs**: put them in `configs/` and reference via `Path(os.environ.get("BID_GEN_HOME", str(Path(__file__).resolve().parent.parent))) / "configs" / "xxx.json"`.
- **Tests**: plain pytest from the repo root (no `pyproject.toml`; `tests/conftest.py` prepends `scripts/` to `sys.path`). After editing pipeline scripts, run the most relevant test file **plus** `python3 scripts/bid_run.py --doctor`.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Disclaimer

This project is a productivity tool for preparing tender documents. Users are responsible for complying with all applicable laws and regulations governing bidding in their jurisdiction. The sanitized case studies in `memory/` are provided for methodology reference only.

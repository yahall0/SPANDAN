# SPANDAN · Officer workspace

Component 1 is a local officer workspace and API for typed complaints. It imports text-based PDF, Word (`.docx` and `.doc`), and UTF-8 text files. It preserves the original bytes and SHA-256, processes passages individually, and releases checked English through a gated JSON handoff.

Component 2 turns checked passages into evidence-linked claims and a case timeline. It runs alongside intake using the same local models, with one model extracting claims and the other verifying them independently.

## Run locally

Install Python 3.11 or newer and [Ollama](https://ollama.com/download). The first-time setup downloads models, which are reused locally for every complaint.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m spandan.setup_models
.\.venv\Scripts\python.exe -m spandan
```

Open [the officer workspace](http://127.0.0.1:8765/). If you use a different Python version, substitute it in the first command. On macOS or Linux, use `python3 -m venv .venv` and `.venv/bin/python`. The packaged NLI download targets x86-64 CPUs with AVX2.

The server binds to loopback. The complaint database, original uploads and audit records live in `data/` by default. Use one app process per data folder. On Windows, legacy `.doc` import uses Microsoft Word if installed. Install LibreOffice for conversion on other systems, or set `SPANDAN_SOFFICE` to use a specific LibreOffice executable. For this typed milestone, PDFs need extractable Unicode text on each page. Word extraction covers body paragraphs and tables. If an import fails, save a fresh text-based PDF or `.docx`.

| Environment variable | Default |
| --- | --- |
| `SPANDAN_PORT` | `8765` |
| `SPANDAN_DATA_DIR` | `data` |
| `SPANDAN_NLI_DIR` | `models/nli` |
| `SPANDAN_MODEL_A` | `qwen2.5:3b` |
| `SPANDAN_MODEL_B` | `llama3.2:3b` |
| `SPANDAN_VERIFIER` | Same as path A |

## Review and handoff

- Upload a typed complaint and select whether the complainant is present or needs a call-back. The register updates as each passage finishes.
- The two locally installed 3B models translate independently. English source passages pass through as text. Code checks passage structure, placeholders and meaning anchors. MiniLM scores bidirectional entailment of paired English statements. The verifier extracts source and English slots separately for deterministic comparison. It uses identity for English source carried through verbatim.
- A format or inference failure can be retried once automatically. Other failed checks stop the passage for officer review. A clarification creates a new revision and runs the gate again. An unresolved passage remains outside the handoff. Feedback about a possible error also holds a previously verified passage for recheck.
- Each run keeps source offsets, PDF page references where available, translations, slot frames, check details, model versions and hashes, timing and review history. The app uses pattern-based local masking for identifiers and a small glossary. The application makes inference requests only to Ollama on loopback, and runs NLI in-process.

`GET /api/cases/{id}/handoff` returns passing passages and a list of pending IDs. Add `?finalize=true` to require every passage to pass. The browser UI uses the same handoff. The API schema is available at `/openapi.json`.

## Claims and timeline

Open a complaint and choose **Claims and timeline**. Add typed witness statements from the **Source statements** panel. Each witness document goes through the same intake checks and appears in its own lane. Reported accounts carry their named source where one was stated.

- The claim worker processes currently verified intake revisions in batches of up to five. It checks exact quotes, attribution, polarity, material slots, uncertainty and coverage. The independent local verifier and MiniLM entailment check run before automatic clearance. A failed passage can be repaired twice, and individual failures enter the review queue.
- Timeline arrows use explicit order cues or non-overlapping time intervals. Approximate clock times use a 30-minute interval on either side. Relative dates use the statement's recorded receipt time in India Standard Time. An ambiguous calendar date is held for clarification.
- Dotted reported-account styling preserves hearsay attribution. Dashed inferred connections are visible for investigation and stay separate from the cleared handoff. The graph can suggest that two statements concern the same event, while conflicting accounts remain available for review.
- Officers can confirm an escalation, correct a field, choose an account, merge compatible entities, or record an unresolved item. Officer clearance has a distinct status and an audit reason. A source passage reopened by intake is withdrawn from the current claims and handoff, and its earlier runs remain in the audit.

`GET /api/cases/{id}/graph` returns the current graph and review items. `/graph/audit` includes model versions, repair rounds, retired runs and officer decisions. `/graph/handoff` releases only cleared claims and their supported links. Add `?finalize=true` to require all outstanding items to be resolved. To add a witness by API, post to `/api/cases` with the file, `parent_case`, and `reporter` fields.

## Verification

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

The offline release evaluator accepts a JSON array of human-labelled fixtures and runs the validation gate without touching the complaint database.

```powershell
.\.venv\Scripts\python.exe -m spandan.evaluate C:\path\to\held-out.json
```

Each fixture has `id`, `complaint`, `format`, `language`, and `source`. For a labelled validation fixture, supply `a` and `b` in the same masked form as the translation paths. Add `error_type` and `critical` for known errors. Omit both candidates for a live smoke fixture. Use `polarity`, `speaker`, `evidentiality`, and `quantity` for the four mandatory critical error types. The report covers escapes, per-type catch rates, false alarms and p50/p95 live validation time grouped by complaint. An incomplete evaluation fails the gate. Live timing excludes document extraction, queue time and officer time.

The bundled tests exercise the workflow and injected regressions. A bilingual, independently labelled held-out corpus is needed to establish release accuracy and latency. The deterministic anchor lexicons focus on Hindi, Gujarati and English. The app holds discrepancies for officer review, including when the local models disagree on a valid Gujarati passage.

The Component 2 [development corpus](fixtures/component2_cases.jsonl) includes 50 fictional cases across ten scenarios, with expected claims and selected timeline or conflict outcomes. Hindi and Gujarati examples include authored checked English. The file labels document formats and carries extracted text for each statement. To run the local claim models against it:

```powershell
.\.venv\Scripts\python.exe -m spandan.evaluate_claims
```

Use `--limit 5` for a shorter smoke run. This evaluator starts at the pre-checked passage boundary. It measures the claim stage on development fixtures and reports discrepancies. Release accuracy and end-to-end latency require a separate, independently reviewed set of actual typed files.

## Recurring Xcos XML Patterns and Anomalies

### [Pattern] ScilabInteger Requirement (2026.0.1)
- **Trigger:** Blocks with `integerParameters`, `nbZerosCrossing`, or `nmode` (often labeled `model.nzcross` or `model.nmode` in `.sci`).
- **Known Fix:** Use `<ScilabInteger intPrecision="sci_int32">` with the `value` attribute.
- **Validation:** Scilab `c_pass1: flat failed` or "Error during block parameters update" usually indicates a `ScilabDouble` was used where a `ScilabInteger` was expected.
- **Example:**
```xml
<ScilabInteger as="integerParameters" height="1" width="1" intPrecision="sci_int32">
  <data column="0" line="0" value="1"/>
</ScilabInteger>
```

### [Pattern] Recursive Macro Lookup
- **Trigger:** AI reporting block source not found.
- **Known Fix:** Use `os.walk` to search subdirectories of `scicos_blocks/macros/`. Many blocks are categorized into folders like `linear`, `sinks`, `sources`, etc.
- **Affected File:** `intelligence.py` -> `get_xcos_block_source()`

### [Pattern] Mandatory Link Attributes
- **Trigger:** Link elements missing `style` or `value`.
- **Known Fix:** Every `<BasicLink>` or `<ExplicitLink>` MUST have `style="ExplicitLink"` (or similar) and `value=""`.

---

### [Pattern] Vite Production Build Kills CSS Spinner Animations

- **Trigger:** An icon or element uses `className="animate-spin"` (or any generic `spin`-named keyframe) and it rotates correctly in `npm run dev` but is static after `npm run build`.
- **Known Fix:** Rename the `@keyframes` to a project-unique name (e.g. `@keyframes xcospin`) and create a dedicated class (e.g. `.spin-icon { animation: xcospin 0.85s linear infinite; }`). Apply that class directly on the element, not via a utility that Vite might tree-shake.
- **Validation:** Run `npm run build` then serve the `dist/` folder and confirm the element rotates.
- **Affected Files:** `xcosgen/client/src/index.css`, `xcosgen/client/src/App.jsx`

---

### [Pattern] Rotation Style Injected into mxGeometry Attribute (Recurring)

- **Trigger:** `xcosDiagramToScilab: For input string: "40.0;rotation=180"` — Scilab SAX parser fails to parse a geometry attribute as a Double.
- **Root Cause:** AI writes `height="40.0;rotation=180"` — appends rotation style directly into `height`/`width` of `<mxGeometry>`. `JGraphXHandler.startElement` calls `Double.parseDouble("40.0;rotation=180")` → `NumberFormatException`.
- **Known Fix (two parts):**
  1. **Prompt** – Section F of `SYSTEM_PROMPT` forbids this. Rotation MUST go in the BLOCK's `style` attribute: `style="GAIN_f;rotation=180"`.
  2. **Validation** – Rule #7 in `validate_xml_structure()` calls `float(val)` on each mxGeometry attribute; `ValueError` triggers an `⛔ GEOMETRY ERROR` that is re-injected into the model.
- **Validation:** Restart backend and re-run; validator catches the error before Scilab sees the file.
- **Affected Files:** `xcosgen/server/intelligence.py`

---

### [Pattern] SCILIB vs SCILAB (Recurring Typo)

- **Trigger:** `No enum constant ... SimulationFunctionType.SCILIB`.
- **Cause:** AI confuses "Scilab" with "Scilib".
- **Known Fix:** Use `simulationFunctionType="SCILAB"`.
- **Validation:** Caught in Rule #8 of `validate_xml_structure` to provide early feedback.

### [Pattern] BARXY Simulation Naming (Recurring)

- **Trigger:** `scicosim : unknown block : barxy`.
- **Cause:** AI incorrectly assumes simulation name matches block name or uses lowercase.
- **Known Fix:** Use `simulationFunctionName="BARXY_sim"` and `simulationFunctionType="SCILAB"`.

### [META-PATTERN] Check Knowledge Log First

- **Rule:** Before any XML debugging, agents MUST check:
  1. `project-patterns.md` in this log skill.
  2. `Reference blocks/` folder for exact XML examples.
  3. `scicos_blocks/macros/` for `.sci` source code.
  4. `intelligence.py` for current `SYSTEM_PROMPT` rules.
- **Why:** Prevents repeated hallucinations and ensures alignment with Scilab 2026.0.1 strictness.

---
### 2026-03-23 13:52:41 UTC — Note
- **Summary:** Prefer subprocess Scilab validation for hosted Linux runtimes; keep poll mode only for local Windows workflows.
- Hugging Face Spaces-style hosting works best when the MCP server owns the HTTP port and launches scilab-cli directly for each verification job. The old /task + /result poll loop remains useful for local Scilab desktop setups but should not be the primary hosted path.
- **Files:** scilab-xcos-mcp-server/server.py, scilab-xcos-mcp-server/README.md


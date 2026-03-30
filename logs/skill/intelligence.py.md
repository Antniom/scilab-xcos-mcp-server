### [FIX] 2026-03-18 03:38:00 UTC — Scilab Parameter Type Mismatch

- **Error:** `scicos_simulate: Error during block parameters update` and `c_pass1: flat failed`.
- **Cause:** Scilab 2026 is extremely strict about XML tag types. `integerParameters`, `nbZerosCrossing`, and `nmode` MUST use `<ScilabInteger intPrecision="sci_int32">`. The AI was defaulting to `<ScilabDouble>`.
- **Fix:** Updated `SYSTEM_PROMPT` in `intelligence.py` with mandatory type mapping rules. Added explicit examples of `<ScilabInteger>` with multiple data points.
- **Verify:** Verified via manual reasoning and alignment with working Scilab XML examples.
- **Files:** `xcosgen/server/intelligence.py`

### [FIX] 2026-03-18 03:38:00 UTC — Missing Block Source Macros

- **Error:** AI reporting "Source for [Block] not found" even when the block exists in Scilab.
- **Cause:** `get_xcos_block_source` was only checking the top-level `macros/` directory. Many blocks (e.g., `CSCOPE`, `RAMP`) are in subdirectories like `sinks/` or `linear/`.
- **Fix:** Changed `get_xcos_block_source` to use `os.walk` for recursive file lookup.
- **Verify:** Tested with blocks known to be in subdirectories.
- **Files:** `xcosgen/server/intelligence.py`

### [NOTE] 2026-03-18 03:38:00 UTC — Tool Calling Enforcement

- **Observation:** Gemini 2.0/1.5 models sometimes skip tool calls on the first turn if the prompt is complex, leading to hallucinations.
- **Decision:** Implemented a backend interception loop in `_sync_generate_3phase`. If no tools are called on Iteration 1, the backend injects a "CRITICAL ERROR" message to the model before continuing, forcing it to use tools.
- **Files:** `xcosgen/server/intelligence.py`

### [FIX] 2026-03-19 01:29:00 UTC — Silent Generation Hang (Background Task)

- **Error:** The generation task appeared to hang or die silently immediately after "Scheduling background task..." appeared in backend logs. No "Error" or "Generating" steps reached the UI.
- **Cause:** `_get_client()` was called outside the main `try/except` in `AutonomousLoop.run()`. If `GEMINI_API_KEY` was missing, the resulting `ValueError` was raised directly into the FastAPI `BackgroundTasks` runner, which has no default error reporting back to the WebSocket. The task died silently.
- **Fix:** Moved `_get_client()` inside the `try/except` block. Added a `safe_callback` helper and a top-level `try/except` in `run()` to ensure any unhandled pipeline exceptions are caught and reported as an "Error" step to the UI.
- **Files:** `xcosgen/server/intelligence.py`
- **Verify:** Manual verification (restart backend to apply).

### [FIX] 2026-03-19 12:40:00 UTC — SCILAB Typo & BARXY Simulation Name

- **Error:** `No enum constant ... SimulationFunctionType.SCILIB` and `scicosim : unknown block : barxy`.
- **Cause:** 1) AI hallucinated `SCILIB` (valid is `SCILAB`). 2) AI used `barxy` as the simulation name (correct is `BARXY_sim`).
- **Fix:** Added validation Rules #8 and #9 to `validate_xml_structure` to catch these typos. Updated `SYSTEM_PROMPT` Section G with explicit block-specific simulation rules.
- **Verify:** Tested with `test_val.py`. Captured correct structure from `Reference blocks/BARXY.xcos`.
- **Files:** `xcosgen/server/intelligence.py`

### [META-RULE] 2026-03-19 12:40:00 UTC — Research Priority for Errors

- **Rule:** When an XML or Scilab error occurs, agents MUST check these in order:
    1. **Knowledge Log Skill**: Check `project-patterns.md` and per-file logs to see if it's a known issue.
    2. **Reference Blocks**: Check `Reference blocks/*.xcos` for authoritative XML examples.
    3. **Source Code**: Check `.sci` files in `scicos_blocks/macros/` for `model.sim` and `model.blocktype` definitions.
    4. **System Prompt**: Check `intelligence.py` to see if the AI is being given incorrect or insufficient instructions.
- **Reason:** Prevents repeated hallucinations and ensures alignment with Scilab 2026.0.1 strictness.

---

# App.jsx — Knowledge & Error Log

---

### [Fix] 2026-03-19 01:18 UTC — Pipeline status never updated mid-run

- **Error:** The Pipeline Status panel stayed on whichever stage was active at the moment `handleGenerate` fired and never advanced. Simulation Check / Auto-Correction steps silently skipped.
- **Cause:** The `status` state was only written in two places: set to `'Generating'` on button click and set to `'Success'` on `Finished` WS event. Step events like `Verifying` and `Fixing` that the backend sends were appended to `logs` but were never read back to update `status`.
- **Fix:** Replaced the single `status` string with a `pipelineStage` integer (0–3) and a `pipelineCompleted` high-water mark. Added a `STEP_TO_STAGE` lookup table that maps backend step names (`Generating`, `Verifying`, `Fixing`, `Finished`) to their stage index, then called `setPipelineStage` and `setPipelineCompleted` for every incoming WS batch.
- **Files:** `xcosgen/client/src/App.jsx`
- **Verify:** Watching a live generation run — pipeline steps light up and complete in sequence.

---

### [Fix] 2026-03-19 01:18 UTC — Log timestamps were always "now", not the server time

- **Error:** Logs displayed the client-side time at render, so all entries in a batch got the same wrong timestamp, and old logs re-rendered with a fresh time on every update.
- **Cause:** The log rendering used `new Date().toLocaleTimeString()` inline instead of the `timestamp` field the backend injects into every log event.
- **Fix:** Each `console-line` now reads `log.timestamp` (the ISO string written by `log_event()` in `main.py`). Falls back to empty string if absent.
- **Files:** `xcosgen/client/src/App.jsx`

---

### [Fix] 2026-03-19 01:18 UTC — Spinner animations static in Vite production build

- **Error:** The `Loader2` icon in the Generate button and the `Loader2` icon in the active Pipeline Step appeared completely static (no rotation) after `npm run build`.
- **Cause:** Lucide-react renders SVGs. The class `animate-spin` was defined in `index.css` with `animation: spin 1s linear infinite`, but Vite's CSS minifier deduplicates/tree-shakes at-rules. The `@keyframes spin` name clashed with Tailwind or browser internals and was sometimes elided.
- **Fix:** Renamed the keyframe to `@keyframes xcospin` and the utility class to `.spin-icon`. Both the Generate button and the `PipelineStep` component now apply `className="spin-icon"` directly on the Lucide component. Name collision is eliminated; the animation is reliable in all build modes.
- **Files:** `xcosgen/client/src/App.jsx`, `xcosgen/client/src/index.css`
- **Verify:** Built with `npm run build` and confirmed the spinner rotates in the served production bundle.

---

### [Fix] 2026-03-19 01:18 UTC — Tab title displayed "client-tmp"

- **Error:** Browser tab showed "client-tmp" — the default Vite scaffold name.
- **Cause:** `index.html` `<title>` tag and `package.json` `"name"` field were never updated after project scaffolding.
- **Fix:** Set `<title>Xcos AI</title>` in `index.html`; set `"name": "xcos-ai"` in `package.json`.
- **Files:** `xcosgen/client/index.html`, `xcosgen/client/package.json`

---

### [Note] 2026-03-19 01:18 UTC — WebSocket reference should live in a ref, not state

- **Note:** Storing the WebSocket object in `useState` causes a stale-closure bug: if the `socket` changes during reconnect, older closures in the effect still see the old socket. Moved to `useRef` (`wsRef`) so the live socket is always accessible from any closure without triggering re-renders.
- **Files:** `xcosgen/client/src/App.jsx`

---

### [Note] 2026-03-19 01:18 UTC — Backend session ID detection for stale-UI reset

- **Note:** On backend restart the frontend receives a `Session` step with `BACKEND_ID:<ISO timestamp>` as its message. The frontend compares this to a `sessionIdRef`. If they differ, all UI state (logs, pipeline, generating flag, result XML) is reset before the new logs are appended. This prevents the UI from showing a mix of logs from two different backend sessions.
- **Files:** `xcosgen/client/src/App.jsx`, `xcosgen/server/main.py` (`startup_event`)

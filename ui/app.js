import { App } from "https://esm.sh/@modelcontextprotocol/ext-apps@1.2.2?bundle";

const app = new App({
    name: "Scilab Xcos MCP App",
    version: "1.0.0"
});

const locale = document.documentElement.lang || navigator.language || "en-US";

const hideAllWidgets = () => {
    document.querySelectorAll('.widget-container').forEach(el => {
        el.classList.remove('active');
    });
};

const showWidget = (widgetId) => {
    hideAllWidgets();
    const el = document.getElementById(`widget-${widgetId}`);
    if (el) el.classList.add('active');
};

const escapeHtml = (value) => String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

const extractJsonPayload = (text) => {
    if (!text) return null;

    try {
        return JSON.parse(text);
    } catch (_) {
        const start = text.indexOf("{");
        const end = text.lastIndexOf("}");
        if (start === -1 || end === -1 || end <= start) {
            return null;
        }
        return JSON.parse(text.slice(start, end + 1));
    }
};

const getToolPayload = (result) => {
    if (result?._meta?.widget?.widget_type) {
        return result._meta.widget;
    }

    if (result?.structuredContent?.widget_type) {
        return result.structuredContent;
    }

    const textChunks = result?.content
        ?.filter(chunk => chunk.type === "text")
        ?.map(chunk => chunk.text)
        ?.filter(Boolean) || [];

    for (const chunk of textChunks) {
        const parsed = extractJsonPayload(chunk);
        if (parsed?.widget_type) {
            return parsed;
        }
    }

    return null;
};

const canRequestDisplayMode = () => typeof window.openai?.requestDisplayMode === "function";

const syncHostControls = () => {
    const topologyExpand = document.getElementById("topology-expand");
    if (topologyExpand) {
        topologyExpand.hidden = !canRequestDisplayMode();
    }
};

const requestFullscreenDisplay = async () => {
    if (!canRequestDisplayMode()) return;

    try {
        await window.openai.requestDisplayMode({ mode: "fullscreen" });
    } catch (error) {
        console.warn("Unable to request fullscreen display mode.", error);
    }
};

const formatTime = (date) =>
    new Intl.DateTimeFormat(locale, { hour: "numeric", minute: "2-digit" }).format(date);

const showAppError = (message, detail = "") => {
    showWidget("waiting");
    const waiting = document.getElementById("widget-waiting");
    if (!waiting) return;

    waiting.innerHTML = `
        <div class="empty-state">
            <h2>Widget Error</h2>
            <p>${escapeHtml(message)}</p>
            ${detail ? `<pre style="text-align:left; max-width:100%; overflow:auto;">${escapeHtml(detail)}</pre>` : ""}
        </div>
    `;
};

const getBlockFallbackLabel = (blockName) => {
    const compact = String(blockName || "")
        .replace(/[_-]+/g, " ")
        .trim()
        .split(/\s+/)
        .filter(Boolean);

    if (compact.length === 0) return "?";
    if (compact.length === 1) return compact[0].slice(0, 2).toUpperCase();
    return `${compact[0][0] || ""}${compact[1][0] || ""}`.toUpperCase();
};

const createCatalogueCard = (block) => {
    const blockEl = document.createElement("article");
    blockEl.className = "card transition-all catalogue-card";

    const preview = document.createElement("div");
    preview.className = "catalogue-card__preview";

    if (block.image_data_uri) {
        const image = document.createElement("img");
        image.className = "catalogue-card__image";
        image.src = block.image_data_uri;
        image.alt = `${block.name} block`;
        image.loading = "lazy";
        preview.appendChild(image);
    } else {
        const fallback = document.createElement("div");
        fallback.className = "catalogue-card__fallback";
        fallback.textContent = getBlockFallbackLabel(block.name);
        preview.appendChild(fallback);
    }

    const body = document.createElement("div");
    body.className = "catalogue-card__body";

    const title = document.createElement("h4");
    title.className = "catalogue-card__title";
    title.textContent = block.name;

    const badge = document.createElement("div");
    badge.className = "badge badge-neutral";
    badge.textContent = block.type;

    const description = document.createElement("p");
    description.className = "text-secondary catalogue-card__description";
    description.textContent = block.description || "No description available.";

    body.append(title, badge, description);
    blockEl.append(preview, body);
    return blockEl;
};

app.ontoolresult = (result) => {
    try {
        const data = getToolPayload(result);
        if (!data) {
            showAppError("The tool returned data in an unexpected format.", JSON.stringify(result, null, 2));
            return;
        }

        const widgetType = data.widget_type;
        const payload = data.payload || {};

        if (widgetType === "status") {
            showWidget('status');
            document.getElementById('status-time').textContent = `Fetched at: ${formatTime(new Date())}`;
            
            // Scilab Engine
            const scilabSuccess = payload.scilab_success;
            document.getElementById('scilab-version').textContent = payload.scilab_output || 'Unknown Error';
            const scilabPulse = document.getElementById('scilab-pulse');
            scilabPulse.className = `pulse-dot ${scilabSuccess ? '' : 'danger'}`;
            
            // Environment context
            document.getElementById('env-status').textContent = payload.env_context || 'N/A';
            const envPulse = document.getElementById('env-pulse');
            envPulse.className = `pulse-dot ${payload.env_context ? 'neutral' : 'warning'}`;
            
            // Drafts
            document.getElementById('draft-count').textContent = payload.active_drafts || '0';
            
            // Overall
            const badge = document.getElementById('status-badge');
            if (scilabSuccess) {
                badge.className = 'badge badge-success';
                badge.textContent = 'Operational';
            } else {
                badge.className = 'badge badge-danger';
                badge.textContent = 'Disconnected';
            }

        } else if (widgetType === "workflow") {
            showWidget('workflow');
            document.getElementById('workflow-id').textContent = payload.workflow_id || 'Global View';
            
            const phasesContainer = document.getElementById('workflow-phases');
            phasesContainer.innerHTML = '';
            
            if (payload.phases) {
                payload.phases.forEach(phase => {
                    const phaseEl = document.createElement('div');
                    phaseEl.className = 'phase-card';
                    
                    let statusColor = 'var(--text-tertiary)';
                    let statusText = 'Pending';
                    if (phase.status === 'completed') { statusColor = 'var(--success)'; statusText = 'Completed'; }
                    else if (phase.status === 'in_progress') { statusColor = 'var(--accent)'; statusText = 'In Progress'; }
                    else if (phase.status === 'awaiting_approval') { statusColor = 'var(--warning)'; statusText = 'Awaiting Review'; }
                    
                    phaseEl.innerHTML = `
                        <div class="phase-card__header">
                            <strong>${escapeHtml(phase.label)}</strong>
                            <span class="badge" style="color: ${statusColor}; background: ${statusColor}22">${statusText}</span>
                        </div>
                    `;
                    phasesContainer.appendChild(phaseEl);
                });
            } else {
                phasesContainer.innerHTML = '<p class="text-muted">No specific workflow loaded.</p>';
            }

        } else if (widgetType === "catalogue") {
            showWidget('catalogue');
            document.getElementById('catalogue-category').textContent = `Category: ${payload.category || 'All'}`;
            
            const list = document.getElementById('catalogue-list');
            list.innerHTML = '';
            
            if (payload.blocks && payload.blocks.length > 0) {
                payload.blocks.forEach(block => {
                    list.appendChild(createCatalogueCard(block));
                });
            } else {
                list.innerHTML = '<p class="text-muted">No blocks found.</p>';
            }

        } else if (widgetType === "topology") {
            showWidget('topology');
            document.getElementById('topology-stats').textContent =
                payload.error
                    ? 'Unable to render topology preview'
                    : `${payload.block_count || 0} blocks, ${payload.link_count || 0} links (Session: ${payload.session_id})`;
            
            const svgContainer = document.getElementById('topology-svg');
            if (payload.error) {
                svgContainer.innerHTML = `<div class="validation-message validation-message--error">${escapeHtml(payload.error)}</div>`;
            } else if (payload.svg) {
                svgContainer.innerHTML = payload.svg;
            } else if (payload.mermaid) {
                svgContainer.innerHTML = `<pre class="validation-message">${escapeHtml(payload.mermaid)}</pre>`;
            } else {
                svgContainer.innerHTML = '<p class="text-muted">No visualization available.</p>';
            }

        } else if (widgetType === "validation") {
            showWidget('validation');
            const badge = document.getElementById('validation-badge');
            const details = document.getElementById('validation-details');
            
            if (payload.success) {
                badge.className = 'badge badge-success';
                badge.textContent = 'Success';
                details.innerHTML = `<h3 class="validation-title validation-title--success">Validation Passed</h3>
                                     <p>The Xcos XML syntax is valid and ready for the next phase.</p>`;
            } else {
                badge.className = 'badge badge-danger';
                badge.textContent = 'Failed';
                details.innerHTML = `<h3 class="validation-title validation-title--error">Validation Errors</h3>
                                     <pre class="validation-message validation-message--error">${escapeHtml(payload.error || 'Unknown syntactic error')}</pre>`;
            }
        } else {
            showAppError(`Unsupported widget type: ${widgetType || "unknown"}`);
        }
    } catch (err) {
        console.error("Error processing tool result:", err);
        showAppError("The widget failed to render.", err?.stack || String(err));
    }
    syncHostControls();
};

document.getElementById("topology-expand")?.addEventListener("click", () => {
    void requestFullscreenDisplay();
});

app.connect().then(() => {
    syncHostControls();
    console.log("Xcos MCP App Connected via Ext Apps protocol.");
}).catch(err => {
    console.error("Failed to connect App:", err);
});

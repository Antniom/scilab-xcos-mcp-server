import { App } from "https://esm.sh/@modelcontextprotocol/ext-apps@1.2.2?bundle";

const app = new App({
    name: "Scilab Xcos MCP App",
    version: "1.0.0"
});

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
            document.getElementById('status-time').textContent = `Fetched at: ${new Date().toLocaleTimeString()}`;
            
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
                    phaseEl.style.padding = '16px';
                    phaseEl.style.border = '1px solid var(--panel-border)';
                    phaseEl.style.borderRadius = 'var(--radius-sm)';
                    
                    let statusColor = 'var(--text-tertiary)';
                    let statusText = 'Pending';
                    if (phase.status === 'completed') { statusColor = 'var(--success)'; statusText = 'Completed'; }
                    else if (phase.status === 'in_progress') { statusColor = 'var(--accent)'; statusText = 'In Progress'; }
                    else if (phase.status === 'awaiting_approval') { statusColor = 'var(--warning)'; statusText = 'Awaiting Review'; }
                    
                    phaseEl.innerHTML = `
                        <div class="flex-row space-between">
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
                    const blockEl = document.createElement('div');
                    blockEl.className = 'card transition-all';
                    blockEl.style.padding = '16px';
                    blockEl.innerHTML = `
                        <h4 style="margin:0 0 4px 0">${escapeHtml(block.name)}</h4>
                        <div class="badge badge-neutral">${escapeHtml(block.type)}</div>
                    `;
                    list.appendChild(blockEl);
                });
            } else {
                list.innerHTML = '<p class="text-muted">No blocks found.</p>';
            }

        } else if (widgetType === "topology") {
            showWidget('topology');
            document.getElementById('topology-stats').textContent = 
                `${payload.block_count || 0} blocks, ${payload.link_count || 0} links (Session: ${payload.session_id})`;
            
            const svgContainer = document.getElementById('topology-svg');
            // If the server provides SVG we can just inject it
            if (payload.svg) {
                svgContainer.innerHTML = payload.svg;
            } else if (payload.mermaid) {
                // Future expansion: render mermaid text client-side if needed
                svgContainer.innerHTML = `<pre style="text-align:left; font-size:12px;">${payload.mermaid}</pre>`;
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
                details.innerHTML = `<h3 style="color:var(--success)">Validation Passed</h3>
                                     <p>The Xcos XML syntax is valid and ready for the next phase.</p>`;
            } else {
                badge.className = 'badge badge-danger';
                badge.textContent = 'Failed';
                details.innerHTML = `<h3 style="color:var(--danger)">Validation Errors</h3>
                                     <pre style="background:#f9f9f9; padding:12px; border-radius:var(--radius-sm); border:1px solid #eee; overflow-x:auto;">${escapeHtml(payload.error || 'Unknown syntactic error')}</pre>`;
            }
        } else {
            showAppError(`Unsupported widget type: ${widgetType || "unknown"}`);
        }
    } catch (err) {
        console.error("Error processing tool result:", err);
        showAppError("The widget failed to render.", err?.stack || String(err));
    }
};

app.connect().then(() => {
    console.log("Xcos MCP App Connected via Ext Apps protocol.");
}).catch(err => {
    console.error("Failed to connect App:", err);
});

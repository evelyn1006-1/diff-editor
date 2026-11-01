/* Cell editing and one-shot execution. The notebook object remains authoritative,
 * including metadata, attachments, and MIME data that this UI cannot render. */
(() => {
    'use strict';
    const cellsEl = document.getElementById('notebook-cells');
    const statusEl = document.getElementById('notebook-status');
    const saveBtn = document.getElementById('notebook-save');
    const runBtn = document.getElementById('notebook-run');
    const modeBtn = document.getElementById('notebook-editor-mode');
    const undoBtn = document.getElementById('notebook-undo');
    const envSelect = document.getElementById('notebook-environment');
    const pythonInput = document.getElementById('notebook-python');
    const timeoutInput = document.getElementById('notebook-timeout');
    const resultLink = document.getElementById('notebook-result');
    const reloadBtn = document.getElementById('notebook-reload');
    const mobile = matchMedia('(max-width: 640px)');
    const preferenceKey = `notebook-environment:${FILE_PATH}`;
    let notebook = null;
    let revision = null;
    let dirty = false;
    let busy = false;
    let views = [];
    let undo = [];
    let activeCell = null;
    const editingMarkdown = new WeakSet();
    let job = null;
    let timer = null;
    let monacoPromise = null;
    let editorMode = mobile.matches ? 'textbox' : 'code';

    const text = value => Array.isArray(value) ? value.join('') : String(value ?? '');
    const ansiFree = value => text(value).replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '');
    const apiUrl = (path, params) => path + (params ? '?' + new URLSearchParams(params) : '');

    async function request(url, payload) {
        const response = await fetch(url, payload === undefined ? {} : {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF_TOKEN },
            body: JSON.stringify(payload),
        });
        const data = await response.json();
        if (!response.ok) {
            const error = new Error(data.error || `Request failed (${response.status})`);
            error.status = response.status;
            throw error;
        }
        return data;
    }

    function message(value, error = false) {
        statusEl.textContent = value;
        statusEl.parentElement.classList.toggle('error', error);
    }

    function controls() {
        saveBtn.disabled = !notebook || busy || !dirty;
        runBtn.disabled = !notebook || busy;
        undoBtn.disabled = busy || !undo.length;
        modeBtn.disabled = !notebook || busy;
        modeBtn.textContent = editorMode === 'code' ? 'Use textbox' : 'Use code editor';
        envSelect.disabled = !notebook || busy;
        pythonInput.disabled = busy;
        timeoutInput.disabled = busy;
        reloadBtn.disabled = busy;
        document.querySelectorAll('[data-add-cell]').forEach(button => { button.disabled = !notebook || busy; });
        views.forEach(view => {
            view.node.querySelectorAll('button, select').forEach(control => { control.disabled = busy; });
            view.textarea.readOnly = busy;
            view.editor?.updateOptions({ readOnly: busy });
        });
    }

    function changed() {
        dirty = true;
        message('Unsaved changes');
        saveBtn.disabled = busy || !notebook;
    }

    function resizeTextarea(textarea) {
        textarea.style.height = 'auto';
        textarea.style.height = `${Math.min(640, Math.max(110, textarea.scrollHeight))}px`;
    }

    function math(element) {
        if (window.renderMathInElement) {
            window.renderMathInElement(element, {
                throwOnError: false, trust: false,
                delimiters: [{ left: '$$', right: '$$', display: true },
                    { left: '\\[', right: '\\]', display: true },
                    { left: '$', right: '$', display: false },
                    { left: '\\(', right: '\\)', display: false }],
            });
        }
    }

    function richFrame(container, markup, title) {
        const frame = document.createElement('iframe');
        frame.className = 'notebook-rich-output';
        frame.title = title;
        // Same-origin permits measuring layout and authenticated local images;
        // scripts remain forbidden by both the sandbox and the frame's CSP.
        frame.setAttribute('sandbox', 'allow-same-origin');
        const csp = "default-src 'none'; script-src 'none'; style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; img-src 'self' data:; font-src 'self' https://cdnjs.cloudflare.com data:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'";
        frame.srcdoc = `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}"><style>body{margin:8px;font:15px/1.6 system-ui;color:#4a1942;overflow-wrap:anywhere}img,svg{max-width:100%;height:auto}pre{overflow:auto}table{border-collapse:collapse}td,th{padding:4px 8px;border:1px solid #ddd}main{padding:0!important;min-height:0!important;box-shadow:none!important}a{color:#a23790}</style></head><body>${markup}</body></html>`;
        frame.addEventListener('load', () => {
            try {
                const doc = frame.contentDocument;
                const style = doc.createElement('link');
                style.rel = 'stylesheet';
                style.href = 'https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.11/katex.min.css';
                doc.head.appendChild(style);
                math(doc.body);
                const resize = () => {
                    frame.style.height = `${Math.max(48, Math.min(doc.body.scrollHeight + 16, innerHeight * 0.65))}px`;
                };
                resize();
                const observer = new ResizeObserver(resize);
                observer.observe(doc.body);
                frame._resizeObserver = observer;
            } catch { /* Opaque embedded content remains scrollable. */ }
        });
        container.appendChild(frame);
        return frame;
    }

    async function markdown(container, source, attachments = {}) {
        const renderToken = {};
        container._markdownRenderToken = renderToken;
        container.textContent = 'Rendering Markdown…';
        try {
            const result = await request('api/notebook/markdown', { path: FILE_PATH, source, attachments });
            if (!container.isConnected || container._markdownRenderToken !== renderToken) return;
            container.replaceChildren();
            richFrame(container, result.html, 'Markdown preview');
        } catch (error) {
            if (container.isConnected && container._markdownRenderToken === renderToken) {
                container.textContent = `Markdown preview failed: ${error.message}`;
            }
        }
    }

    function setMarkdownEditing(view, editing, focus = false) {
        if (editing) editingMarkdown.add(view.cell);
        else editingMarkdown.delete(view.cell);
        view.textarea.hidden = !editing;
        view.output.hidden = editing;
        view.editButton.textContent = editing ? 'Done' : 'Edit';
        const label = editing ? `Done editing cell ${view.index + 1}` : `Edit cell ${view.index + 1}`;
        view.editButton.title = label;
        view.editButton.setAttribute('aria-label', label);
        view.editButton.setAttribute('aria-expanded', String(editing));
        if (editing) {
            resizeTextarea(view.textarea);
            if (focus) {
                activeCell = view.cell;
                view.textarea.focus();
            }
        } else {
            void markdown(view.output, text(view.cell.source), view.cell.attachments);
        }
    }

    function plain(container, value, error = false) {
        const pre = document.createElement('pre');
        pre.textContent = ansiFree(value);
        container.classList.toggle('error', error);
        container.appendChild(pre);
    }

    function outputs(container, cell) {
        for (const output of cell.outputs || []) {
            const block = document.createElement('div');
            block.className = 'notebook-output';
            container.appendChild(block);
            if (output.output_type === 'stream') {
                plain(block, output.text, output.name === 'stderr');
            } else if (output.output_type === 'error') {
                plain(block, output.traceback?.length ? output.traceback.join('\n') : `${output.ename}: ${output.evalue}`, true);
            } else {
                const data = output.data || {};
                if (data['text/html'] !== undefined) {
                    richFrame(block, text(data['text/html']), 'Saved HTML output');
                } else if (data['image/svg+xml'] !== undefined) {
                    richFrame(block, text(data['image/svg+xml']), 'Saved SVG output');
                } else if (['image/png', 'image/jpeg', 'image/gif', 'image/webp'].some(mime => data[mime] !== undefined)) {
                    const mime = ['image/png', 'image/jpeg', 'image/gif', 'image/webp'].find(type => data[type] !== undefined);
                    const image = document.createElement('img');
                    image.alt = 'Saved notebook output';
                    image.loading = 'lazy';
                    image.src = `data:${mime};base64,${text(data[mime]).replace(/\s/g, '')}`;
                    block.appendChild(image);
                } else if (data['text/markdown'] !== undefined) {
                    void markdown(block, text(data['text/markdown']));
                } else if (data['text/latex'] !== undefined) {
                    block.classList.add('notebook-latex');
                    block._latex = text(data['text/latex']).replace(/^\s*\$\$?|\$\$?\s*$/g, '');
                    if (window.katex) window.katex.render(block._latex, block, { throwOnError: false, trust: false, displayMode: true });
                    else plain(block, data['text/latex']);
                } else if (data['text/plain'] !== undefined) {
                    plain(block, data['text/plain']);
                } else if (data['application/json'] !== undefined) {
                    plain(block, JSON.stringify(data['application/json'], null, 2));
                } else {
                    block.textContent = `Saved output preserved (${Object.keys(data).join(', ') || output.output_type}); no static preview is available.`;
                }
            }
        }
    }

    async function loadMonaco() {
        if (window.monaco?.editor) return window.monaco;
        if (!monacoPromise) monacoPromise = new Promise((resolve, reject) => {
            const loader = document.createElement('script');
            loader.src = 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs/loader.js';
            loader.onerror = () => reject(new Error('Code editor unavailable; textboxes remain editable'));
            loader.onload = () => {
                window.require.config({ paths: { vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs' } });
                window.require(['vs/editor/editor.main'], () => resolve(window.monaco), reject);
            };
            document.head.appendChild(loader);
        });
        return monacoPromise;
    }

    async function enhance(view) {
        if (editorMode !== 'code' || view.cell.cell_type !== 'code' || view.editor || view.enhancing) return;
        view.enhancing = true;
        try {
            const monaco = await loadMonaco();
            if (!view.node.isConnected || editorMode !== 'code') return;
            view.monacoHost.hidden = false;
            view.monacoHost.style.height = `${Math.min(640, Math.max(110, text(view.cell.source).split('\n').length * 22 + 24))}px`;
            view.editor = monaco.editor.create(view.monacoHost, {
                value: text(view.cell.source), language: 'python', theme: 'vs',
                automaticLayout: true, minimap: { enabled: false }, wordWrap: 'on',
                scrollBeyondLastLine: false, fontSize: 14, readOnly: busy,
            });
            view.textarea.hidden = true;
            view.editor.onDidChangeModelContent(() => {
                view.cell.source = view.editor.getValue();
                view.textarea.value = view.cell.source;
                view.monacoHost.style.height = `${Math.min(640, Math.max(110, view.editor.getContentHeight()))}px`;
                changed();
            });
            view.editor.onDidFocusEditorText(() => { activeCell = view.cell; });
        } catch {
            if (editorMode === 'code') {
                editorMode = 'textbox';
                controls();
                message('Code editor unavailable; textboxes remain editable.');
            }
        } finally {
            view.enhancing = false;
        }
    }

    const visible = new IntersectionObserver(entries => entries.forEach(entry => {
        if (entry.isIntersecting) {
            const view = views.find(item => item.node === entry.target);
            if (view) void enhance(view);
        }
    }), { rootMargin: '250px' });

    function disposeViews() {
        visible.disconnect();
        for (const view of views) {
            const model = view.editor?.getModel();
            view.editor?.dispose();
            model?.dispose();
            view.node.querySelectorAll('iframe').forEach(frame => frame._resizeObserver?.disconnect());
        }
        views = [];
    }

    function structural(action) {
        if (busy) return;
        undo.push(notebook.cells.slice());
        action();
        renderCells();
        changed();
    }

    function button(label, title, handler) {
        const element = document.createElement('button');
        element.type = 'button';
        element.textContent = label;
        element.title = title;
        element.setAttribute('aria-label', title);
        element.addEventListener('click', handler);
        return element;
    }

    function renderCells() {
        disposeViews();
        cellsEl.replaceChildren();
        notebook.cells.forEach((cell, index) => {
            const node = document.createElement('section');
            node.className = 'notebook-cell';
            node.setAttribute('aria-label', `Cell ${index + 1}`);
            const toolbar = document.createElement('div');
            toolbar.className = 'notebook-cell-toolbar';
            const count = document.createElement('span');
            count.className = 'notebook-cell-count';
            count.textContent = `${index + 1}${cell.cell_type === 'code' ? `  [${cell.execution_count ?? ' '}]` : ''}`;
            toolbar.appendChild(count);
            const type = document.createElement('select');
            type.setAttribute('aria-label', `Cell ${index + 1} type`);
            for (const name of ['code', 'markdown', 'raw']) type.add(new Option(name === 'markdown' ? 'Markdown' : name === 'code' ? 'Code' : 'Raw', name));
            type.value = cell.cell_type;
            type.addEventListener('change', () => {
                const nextType = type.value;
                if (cell.outputs?.length || (nextType === 'code' && Object.keys(cell.attachments || {}).length)) {
                    if (!confirm('Changing the cell type removes its outputs or attachments. Continue?')) {
                        type.value = cell.cell_type;
                        return;
                    }
                }
                structural(() => {
                    const replacement = { cell_type: nextType, metadata: cell.metadata, source: cell.source };
                    if (cell.id) replacement.id = cell.id;
                    if (nextType === 'code') Object.assign(replacement, { outputs: [], execution_count: null });
                    else if (cell.attachments) replacement.attachments = cell.attachments;
                    notebook.cells[index] = replacement;
                    if (nextType === 'markdown') editingMarkdown.add(replacement);
                    activeCell = replacement;
                });
            });
            toolbar.appendChild(type);
            toolbar.appendChild(button('↑', `Move cell ${index + 1} up`, () => {
                if (index > 0) structural(() => { [notebook.cells[index - 1], notebook.cells[index]] = [cell, notebook.cells[index - 1]]; });
            }));
            toolbar.appendChild(button('↓', `Move cell ${index + 1} down`, () => {
                if (index < notebook.cells.length - 1) structural(() => { [notebook.cells[index + 1], notebook.cells[index]] = [cell, notebook.cells[index + 1]]; });
            }));
            toolbar.appendChild(button('Delete', `Delete cell ${index + 1}`, () => structural(() => { notebook.cells.splice(index, 1); })));
            node.appendChild(toolbar);
            const textarea = document.createElement('textarea');
            textarea.className = 'notebook-source';
            textarea.value = text(cell.source);
            textarea.spellcheck = false;
            textarea.setAttribute('autocapitalize', 'off');
            textarea.setAttribute('autocorrect', 'off');
            textarea.autocomplete = 'off';
            textarea.setAttribute('aria-label', `Cell ${index + 1} source`);
            textarea.id = `notebook-source-${index + 1}`;
            textarea.hidden = cell.cell_type === 'markdown' && !editingMarkdown.has(cell);
            textarea.addEventListener('focus', () => { activeCell = cell; });
            textarea.addEventListener('input', () => {
                cell.source = textarea.value;
                resizeTextarea(textarea);
                changed();
            });
            textarea.addEventListener('keydown', event => {
                if (event.key === 'Tab' && !busy) {
                    event.preventDefault();
                    textarea.setRangeText('    ', textarea.selectionStart, textarea.selectionEnd, 'end');
                    textarea.dispatchEvent(new Event('input'));
                }
            });
            node.appendChild(textarea);
            const monacoHost = document.createElement('div');
            monacoHost.className = 'notebook-monaco';
            monacoHost.hidden = true;
            node.appendChild(monacoHost);
            const output = document.createElement('div');
            output.className = 'notebook-outputs';
            node.appendChild(output);
            cellsEl.appendChild(node);
            const view = { node, cell, index, textarea, monacoHost, output, editor: null };
            views.push(view);
            resizeTextarea(textarea);
            if (cell.cell_type === 'markdown') {
                view.editButton = button('Edit', `Edit cell ${index + 1}`, () => {
                    if (!busy) setMarkdownEditing(view, !editingMarkdown.has(cell), true);
                });
                view.editButton.setAttribute('aria-controls', textarea.id);
                toolbar.appendChild(view.editButton);
                setMarkdownEditing(view, editingMarkdown.has(cell));
            } else if (cell.cell_type === 'code') {
                outputs(output, cell);
                visible.observe(node);
            }
        });
        if (!notebook.cells.length) cellsEl.textContent = 'No cells yet. Add a code or Markdown cell below.';
        controls();
    }

    function applyEnvironmentVisibility() {
        document.getElementById('notebook-python-field').classList.toggle('hidden', envSelect.value !== 'python');
    }

    function rememberEnvironment() {
        try {
            localStorage.setItem(preferenceKey, JSON.stringify({ choice: envSelect.value, python: pythonInput.value, timeout: timeoutInput.value }));
        } catch { /* Editing also works when storage is unavailable. */ }
    }

    async function loadNotebook(resume = true) {
        const data = await request(apiUrl('api/notebook', { path: FILE_PATH }));
        notebook = data.notebook;
        revision = data.revision;
        dirty = data.empty;
        undo = [];
        activeCell = null;
        envSelect.replaceChildren(...data.environments.map(item => new Option(item.label, item.id)), new Option('Python / virtualenv path…', 'python'));
        try {
            const stored = JSON.parse(localStorage.getItem(preferenceKey) || 'null');
            if (stored) {
                if ([...envSelect.options].some(option => option.value === stored.choice)) envSelect.value = stored.choice;
                pythonInput.value = stored.python || '';
                timeoutInput.value = stored.timeout ?? '600';
            }
        } catch { /* Use the default Python environment. */ }
        applyEnvironmentVisibility();
        renderCells();
        message(data.empty ? 'New notebook — save to initialize it.' : 'Notebook loaded');
        reloadBtn.classList.add('hidden');
        if (resume && data.job) {
            job = data.job;
            busy = true;
            controls();
            await poll();
        }
    }

    async function save() {
        if (busy || !notebook || !dirty) return;
        busy = true;
        controls();
        message('Saving notebook…');
        try {
            const data = await request('api/file', { path: FILE_PATH, content: JSON.stringify(notebook), revision });
            revision = data.revision;
            dirty = false;
            message('Notebook saved');
        } catch (error) {
            message(error.message, true);
            if (error.status === 409) reloadBtn.classList.remove('hidden');
        } finally {
            busy = false;
            controls();
        }
    }

    async function poll() {
        clearTimeout(timer);
        try {
            job = await request(`api/notebook/jobs/${encodeURIComponent(job.job_id)}`);
        } catch (error) {
            // Retry status reads only. Never replay an execution request.
            message(`Could not read execution status: ${error.message}. Checking again…`, true);
            timer = setTimeout(poll, 4000);
            return;
        }
        resultLink.classList.toggle('hidden', !job.result_available);
        resultLink.href = `api/notebook/jobs/${encodeURIComponent(job.job_id)}/result`;
        if (['queued', 'running'].includes(job.status)) {
            message(job.status === 'queued' ? `Starting Python kernel${job.elevated ? ' with sudo' : ''}…`
                : `Executing${job.cell ? ` cell ${job.cell} of ${job.total}` : ' notebook'}${job.elevated ? ' with sudo' : ''}…`);
            timer = setTimeout(poll, 1200);
        } else {
            let feedback = job.error || 'Execution complete — outputs saved.';
            let failed = job.status === 'failed';
            if (job.saved) {
                try { await loadNotebook(false); }
                catch (error) {
                    feedback = `Execution finished, but reloading the notebook failed: ${error.message}. The executed result is available to download.`;
                    failed = true;
                    reloadBtn.classList.remove('hidden');
                }
            }
            busy = false;
            message(feedback, failed);
            if (job.conflict) reloadBtn.classList.remove('hidden');
            controls();
        }
    }

    async function execute() {
        if (busy || !notebook) return;
        const timeout = Number(timeoutInput.value);
        if (!Number.isInteger(timeout) || timeout < 0 || timeout > 86400) {
            message('Cell timeout must be between 0 (unlimited) and 86400 seconds.', true);
            return;
        }
        busy = true;
        controls();
        message('Preparing execution…');
        rememberEnvironment();
        resultLink.classList.add('hidden');
        const requestId = globalThis.crypto?.randomUUID?.()
            || `run-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
        try {
            job = await request('api/notebook/execute', {
                path: FILE_PATH, notebook, revision, timeout, request_id: requestId,
                environment: envSelect.value, python_path: pythonInput.value,
            });
            revision = job.input_revision;
            dirty = false;
            await poll();
        } catch (error) {
            // Only an uncertain response to this exact launch can be recovered.
            // Another tab's job must never replace this tab's unsaved draft.
            try {
                if (!error.status || error.status >= 500) {
                    const remote = await request(apiUrl('api/notebook', { path: FILE_PATH }));
                    if (remote.job?.request_id === requestId) {
                        job = remote.job;
                        revision = job.input_revision;
                        dirty = false;
                        await poll();
                        return;
                    }
                }
            } catch { /* Keep the local edits and display the original error. */ }
            busy = false;
            message(error.message, true);
            if (error.status === 409) reloadBtn.classList.remove('hidden');
            controls();
        }
    }

    document.querySelectorAll('[data-add-cell]').forEach(add => add.addEventListener('click', () => {
        structural(() => {
            const cell = { cell_type: add.dataset.addCell, metadata: {}, source: '' };
            if (cell.cell_type === 'markdown') editingMarkdown.add(cell);
            if (notebook.nbformat_minor >= 5) cell.id = globalThis.crypto?.randomUUID?.() || `cell-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
            if (cell.cell_type === 'code') Object.assign(cell, { execution_count: null, outputs: [] });
            const activeIndex = notebook.cells.indexOf(activeCell);
            notebook.cells.splice(activeIndex < 0 ? notebook.cells.length : activeIndex + 1, 0, cell);
            activeCell = cell;
        });
        const view = views.find(item => item.cell === activeCell);
        view?.node.scrollIntoView({ block: 'nearest' });
        view?.textarea.focus();
    }));
    undoBtn.addEventListener('click', () => {
        if (busy || !undo.length) return;
        notebook.cells = undo.pop();
        renderCells();
        changed();
    });
    saveBtn.addEventListener('click', save);
    runBtn.addEventListener('click', execute);
    reloadBtn.addEventListener('click', async () => {
        if (dirty && !confirm('Discard local edits and reload the notebook from disk?')) return;
        try { await loadNotebook(); } catch (error) { message(error.message, true); }
    });
    envSelect.addEventListener('change', () => { applyEnvironmentVisibility(); rememberEnvironment(); });
    pythonInput.addEventListener('change', rememberEnvironment);
    timeoutInput.addEventListener('change', rememberEnvironment);
    modeBtn.addEventListener('click', () => {
        editorMode = editorMode === 'code' ? 'textbox' : 'code';
        renderCells();
    });
    mobile.addEventListener('change', () => {
        editorMode = mobile.matches ? 'textbox' : 'code';
        if (notebook) renderCells();
    });
    window.addEventListener('beforeunload', event => {
        if (dirty || busy) { event.preventDefault(); event.returnValue = ''; }
    });
    window.addEventListener('notebook-math-ready', () => {
        document.querySelectorAll('.notebook-latex').forEach(block => {
            window.katex.render(block._latex, block, { throwOnError: false, trust: false, displayMode: true });
        });
        document.querySelectorAll('.notebook-rich-output').forEach(frame => {
            try { math(frame.contentDocument.body); } catch { /* Keep the static preview. */ }
        });
    });
    document.addEventListener('keydown', event => {
        if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
            event.preventDefault();
            void save();
        }
    });
    loadNotebook().catch(error => {
        message(error.status === 503 ? 'Notebook tools are unavailable. Open JSON / Git diff to edit this file normally.' : error.message, true);
    });
})();

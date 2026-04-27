/**
 * Monaco-based diff editor component
 */

let diffEditor = null;
let originalContent = '';
let currentContent = '';
let isModified = false;
let wordWrapEnabled = false;
let fileLanguage = 'plaintext';
let isImageFile = false;
let isTextboxMode = false;
let savedScrollInfo = null;

// Configure Monaco loader
require.config({
    paths: {
        vs: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.45.0/min/vs'
    }
});

// Load Monaco and initialize editor
require(['vs/editor/editor.main'], function() {
    monaco.editor.defineTheme('cute-transparent', {
        base: 'vs',
        inherit: true,
        rules: [],
        colors: {
            // 50% alpha light pink background so the page backdrop shows through
            'editor.background': '#FFE6F080',
            'editorGutter.background': '#FFE6F080',
            'minimap.background': '#FFE6F080',
            'editor.lineHighlightBackground': '#FFD6E980',
            'editor.inactiveSelectionBackground': '#FFD6E980',
            'editor.selectionBackground': '#FFB3D980',
        },
    });
    initDiffEditor();
});

async function initDiffEditor() {
    const container = document.getElementById('editor-container');
    const statusEl = document.getElementById('status');
    const saveBtn = document.getElementById('btn-save');

    statusEl.textContent = 'Loading file...';

    try {
        const response = await fetch(`api/file?path=${encodeURIComponent(FILE_PATH)}`);
        const data = await response.json();

        if (!response.ok) {
            statusEl.textContent = data.error || 'Failed to load file';
            statusEl.className = 'status error';
            return;
        }

        originalContent = data.original;
        currentContent = data.content;
        fileLanguage = data.language;
        isImageFile = Boolean(data.is_image);

        if (isImageFile) {
            renderImagePreview(container, data.image_url);
            statusEl.textContent = data.is_git ? 'image preview (git tracked)' : 'image preview';
            statusEl.className = 'status';

            const wrapBtn = document.getElementById('btn-wrap');
            const aiReviewBtn = document.getElementById('btn-ai-review');
            const aiReviewCustomBtn = document.getElementById('btn-ai-review-custom');
            const textboxBtn = document.getElementById('btn-textbox');
            saveBtn.disabled = true;
            saveBtn.title = 'Image files are preview-only';
            wrapBtn.disabled = true;
            wrapBtn.title = 'Wrap is unavailable for image preview';
            aiReviewBtn.disabled = true;
            aiReviewBtn.title = 'AI review is unavailable for image preview';
            aiReviewCustomBtn.disabled = true;
            aiReviewCustomBtn.title = 'AI review is unavailable for image preview';
            textboxBtn.disabled = true;
            textboxBtn.title = 'Textbox mode is unavailable for image preview';
            return;
        }

        // Create diff editor with cute light theme
        diffEditor = monaco.editor.createDiffEditor(container, {
            theme: 'cute-transparent',
            automaticLayout: true,
            renderSideBySide: true,
            originalEditable: false,
            readOnly: false,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            fontSize: 14,
            lineNumbers: 'on',
            renderWhitespace: 'selection',
            wordWrap: 'off',
        });

        // Set models
        const originalModel = monaco.editor.createModel(originalContent, data.language);
        const modifiedModel = monaco.editor.createModel(currentContent, data.language);

        diffEditor.setModel({
            original: originalModel,
            modified: modifiedModel,
        });

        // Track modifications
        modifiedModel.onDidChangeContent(() => {
            const newContent = modifiedModel.getValue();
            isModified = newContent !== currentContent;
            updateStatus();
        });

        // Update status
        if (data.is_git) {
            statusEl.textContent = 'git tracked';
        } else {
            statusEl.textContent = 'comparing to original';
        }
        statusEl.className = 'status';

        if (!data.writable) {
            statusEl.textContent += ' (will use sudo)';
        }

        // Enable save button
        saveBtn.disabled = false;
        saveBtn.addEventListener('click', saveFile);

        // Wrap toggle button
        const wrapBtn = document.getElementById('btn-wrap');
        wrapBtn.addEventListener('click', toggleWordWrap);

        // Textbox mode toggle button
        const textboxBtn = document.getElementById('btn-textbox');
        textboxBtn.addEventListener('click', toggleTextboxMode);

        // Keyboard shortcut: Ctrl/Cmd + S to save
        document.addEventListener('keydown', (e) => {
            if ((e.ctrlKey || e.metaKey) && e.key === 's') {
                e.preventDefault();
                saveFile();
            }
        });

        // AI Review button
        const aiReviewBtn = document.getElementById('btn-ai-review');
        aiReviewBtn.addEventListener('click', requestAiReview);
        const aiReviewCustomBtn = document.getElementById('btn-ai-review-custom');
        aiReviewCustomBtn.addEventListener('click', async () => {
            const customPrompt = window.prompt(
                'Enter a custom review prompt (example: "check the code for security issues").',
                '',
            );
            if (customPrompt === null) return;
            if (!customPrompt.trim()) {
                window.alert('Custom prompt cannot be empty.');
                return;
            }
            requestAiReview({ customPrompt: customPrompt.trim(), forceNew: true });
        });
        initEffortSelector();

        // Close AI review panel
        const closeReviewBtn = document.getElementById('btn-close-review');
        closeReviewBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            document.getElementById('ai-review-panel').classList.add('hidden');
        });

        // Run button - show for runnable file types
        const runBtn = document.getElementById('btn-run');
        const runnableLanguages = ['python', 'javascript', 'shell', 'go', 'c', 'cpp', 'java', 'ruby', 'perl', 'rust', 'csharp', 'brainfuck', 'magma'];
        if (runnableLanguages.includes(fileLanguage)) {
            runBtn.classList.remove('hidden');
            runBtn.addEventListener('click', runFile);
        }

        // Run terminal modal controls
        const runTerminalOverlay = document.getElementById('run-terminal-overlay');
        const runTerminalFrame = document.getElementById('run-terminal-frame');
        const runTerminalRestore = document.getElementById('btn-run-terminal-restore');

        function closeRunTerminal() {
            runTerminalOverlay.classList.add('hidden');
            runTerminalRestore.classList.add('hidden');
            runTerminalFrame.src = 'about:blank';
        }

        function minimizeRunTerminal() {
            runTerminalOverlay.classList.add('hidden');
            runTerminalRestore.classList.remove('hidden');
        }

        function restoreRunTerminal() {
            runTerminalRestore.classList.add('hidden');
            runTerminalOverlay.classList.remove('hidden');
        }

        document.getElementById('btn-run-terminal-close').addEventListener('click', closeRunTerminal);
        document.getElementById('btn-run-terminal-minimize').addEventListener('click', minimizeRunTerminal);
        runTerminalRestore.addEventListener('click', restoreRunTerminal);
        document.getElementById('btn-run-terminal-popout').addEventListener('click', () => {
            const src = runTerminalFrame.src;
            if (src) window.open(src, '_blank');
            closeRunTerminal();
        });
        runTerminalOverlay.addEventListener('click', (e) => {
            if (e.target === runTerminalOverlay) minimizeRunTerminal();
        });

        // Run terminal resize handle (mouse + touch)
        const runTerminalPopup = runTerminalOverlay.querySelector('.run-terminal-popup');
        const resizeHandle = document.getElementById('run-terminal-resize-handle');

        function applyResizeHeight(clientY) {
            const newHeight = window.innerHeight - clientY;
            const clamped = Math.max(150, Math.min(newHeight, window.innerHeight - 40));
            runTerminalPopup.style.height = clamped + 'px';
        }

        resizeHandle.addEventListener('mousedown', (e) => {
            e.preventDefault();
            runTerminalFrame.style.pointerEvents = 'none';
            const onMouseMove = (e) => applyResizeHeight(e.clientY);
            const onMouseUp = () => {
                runTerminalFrame.style.pointerEvents = '';
                document.removeEventListener('mousemove', onMouseMove);
                document.removeEventListener('mouseup', onMouseUp);
            };
            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });

        resizeHandle.addEventListener('touchstart', (e) => {
            e.preventDefault();
            runTerminalFrame.style.pointerEvents = 'none';
            const onTouchMove = (e) => applyResizeHeight(e.touches[0].clientY);
            const onTouchEnd = () => {
                runTerminalFrame.style.pointerEvents = '';
                document.removeEventListener('touchmove', onTouchMove);
                document.removeEventListener('touchend', onTouchEnd);
            };
            document.addEventListener('touchmove', onTouchMove, { passive: false });
            document.addEventListener('touchend', onTouchEnd);
        }, { passive: false });

        // Auto-refresh for log files
        if (isAutoRefreshLogFile(FILE_PATH)) {
            const models = diffEditor.getModel();
            let logRefreshInFlight = false;
            setInterval(async () => {
                if (isModified || logRefreshInFlight) return;
                logRefreshInFlight = true;
                try {
                    const resp = await fetch(`api/file?path=${encodeURIComponent(FILE_PATH)}`);
                    if (!resp.ok) return;
                    const d = await resp.json();
                    if (d.content !== currentContent) {
                        const editor = diffEditor.getModifiedEditor();
                        const textarea = isTextboxMode ? document.getElementById('textbox-editor') : null;
                        const wasAtBottom = textarea
                            ? isElementNearBottom(textarea)
                            : isElementNearBottom(editor);

                        // Update currentContent BEFORE setValue so onDidChangeContent
                        // sees them as equal and doesn't mark the buffer dirty
                        currentContent = d.content;
                        models.modified.setValue(d.content);
                        isModified = false;
                        updateStatus();

                        // Keep textbox in sync if active
                        if (textarea) {
                            syncTextboxModeExternalContent(d.content, { followTail: wasAtBottom });
                        }

                        if (!textarea && wasAtBottom) {
                            editor.revealLine(models.modified.getLineCount());
                        }
                    }
                } catch {} finally {
                    logRefreshInFlight = false;
                }
            }, 1500);
        }

    } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
        statusEl.className = 'status error';
    }
}

function renderImagePreview(container, imageUrl) {
    container.innerHTML = '';

    const wrapper = document.createElement('div');
    wrapper.className = 'image-preview-container';

    if (!imageUrl) {
        const msg = document.createElement('div');
        msg.className = 'error-box';
        msg.textContent = 'Image preview unavailable';
        wrapper.appendChild(msg);
        container.appendChild(wrapper);
        return;
    }

    const img = document.createElement('img');
    img.className = 'image-preview';
    img.src = imageUrl;
    img.alt = 'Image preview';
    img.loading = 'eager';
    wrapper.appendChild(img);
    container.appendChild(wrapper);
}

function updateStatus() {
    const statusEl = document.getElementById('status');
    if (isModified) {
        statusEl.textContent = 'Modified';
        statusEl.className = 'status modified';
    } else {
        statusEl.textContent = 'No changes';
        statusEl.className = 'status';
    }
}

async function saveFile() {
    const statusEl = document.getElementById('status');
    const saveBtn = document.getElementById('btn-save');

    if (!diffEditor) return;

    const newContent = getCurrentModifiedContent();

    statusEl.textContent = 'Saving...';
    statusEl.className = 'status';
    saveBtn.disabled = true;

    try {
        const response = await fetch('api/file', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': CSRF_TOKEN,
            },
            body: JSON.stringify({
                path: FILE_PATH,
                content: newContent,
            }),
        });

        const data = await response.json();

        if (!response.ok) {
            statusEl.textContent = data.error || 'Save failed';
            statusEl.className = 'status error';
            return;
        }

        // Update current content reference
        currentContent = newContent;
        isModified = false;

        statusEl.textContent = data.message || 'Saved';
        statusEl.className = 'status saved';

        // Reset status after a moment
        setTimeout(() => {
            if (!isModified) {
                statusEl.textContent = 'No changes';
                statusEl.className = 'status';
            }
        }, 2000);

    } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
        statusEl.className = 'status error';
    } finally {
        saveBtn.disabled = false;
    }
}

function toggleWordWrap() {
    if (!diffEditor) return;

    wordWrapEnabled = !wordWrapEnabled;
    const wrapValue = wordWrapEnabled ? 'on' : 'off';

    // Update both editors in the diff view
    diffEditor.getOriginalEditor().updateOptions({ wordWrap: wrapValue });
    diffEditor.getModifiedEditor().updateOptions({ wordWrap: wrapValue });

    // Update textbox mode elements if active
    if (isTextboxMode) {
        const textarea = document.getElementById('textbox-editor');
        const highlightEl = document.getElementById('textbox-highlight');
        const mirror = document.getElementById('textbox-mirror');
        if (textarea) {
            textarea.style.whiteSpace = wordWrapEnabled ? 'pre-wrap' : 'pre';
            textarea.style.overflowX = wordWrapEnabled ? 'hidden' : 'auto';
        }
        if (highlightEl) {
            const highlightContentEl = highlightEl.querySelector('.textbox-highlight-content');
            if (highlightContentEl) {
                highlightContentEl.style.whiteSpace = wordWrapEnabled ? 'pre-wrap' : 'pre';
            }
        }
        if (mirror) {
            mirror.style.whiteSpace = wordWrapEnabled ? 'pre-wrap' : 'pre';
        }
        // Recalculate line numbers for new wrap state
        if (textboxUpdateLineNumbers) {
            textboxUpdateLineNumbers();
        }
        if (unifiedDiffEditor) {
            unifiedDiffEditor.getOriginalEditor().updateOptions({ wordWrap: wrapValue });
            unifiedDiffEditor.getModifiedEditor().updateOptions({ wordWrap: wrapValue });
        }
    }

    // Update button appearance
    const wrapBtn = document.getElementById('btn-wrap');
    const wrapBtnText = wrapBtn.querySelector('.btn-text');
    if (wrapBtnText) {
        wrapBtnText.textContent = wordWrapEnabled ? 'Wrap: On' : 'Wrap: Off';
    }
}

let currentReviewController = null;
const REVIEW_ID_STORAGE_KEY = `diff-editor-review-id:${FILE_PATH}`;
const REVIEW_ID_PATTERN = /^[A-Za-z0-9_-]{8,64}$/;
let currentReviewId = loadStoredReviewId();

// Reasoning effort level
const EFFORT_STORAGE_KEY = 'diff-editor-reasoning-effort';
const VALID_EFFORTS = ['low', 'medium', 'high', 'xhigh'];

function loadStoredEffort() {
    try {
        const saved = localStorage.getItem(EFFORT_STORAGE_KEY);
        return (saved && VALID_EFFORTS.includes(saved)) ? saved : 'medium';
    } catch (e) {
        return 'medium';
    }
}

function saveEffort(effort) {
    try {
        if (VALID_EFFORTS.includes(effort)) {
            localStorage.setItem(EFFORT_STORAGE_KEY, effort);
        }
    } catch (e) {
        // Ignore storage failures
    }
}

function getSelectedEffort() {
    const select = document.getElementById('reasoning-effort');
    return select ? select.value : 'medium';
}

function initEffortSelector() {
    const select = document.getElementById('reasoning-effort');
    if (!select) return;

    const saved = loadStoredEffort();
    select.value = saved;

    select.addEventListener('change', () => {
        saveEffort(select.value);
    });
}

function loadStoredReviewId() {
    try {
        const saved = localStorage.getItem(REVIEW_ID_STORAGE_KEY);
        return (saved && REVIEW_ID_PATTERN.test(saved)) ? saved : null;
    } catch (e) {
        return null;
    }
}

function setCurrentReviewId(reviewId) {
    const normalized = (reviewId && REVIEW_ID_PATTERN.test(reviewId)) ? reviewId : null;
    currentReviewId = normalized;
    try {
        if (normalized) {
            localStorage.setItem(REVIEW_ID_STORAGE_KEY, normalized);
        } else {
            localStorage.removeItem(REVIEW_ID_STORAGE_KEY);
        }
    } catch (e) {
        // Ignore storage failures
    }
}

function generateReviewId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        return window.crypto.randomUUID().replace(/-/g, '');
    }
    return `${Date.now().toString(16)}${Math.random().toString(16).slice(2, 10)}`;
}

async function sendAiReviewCancel(reviewId) {
    if (!reviewId) return;
    try {
        await fetch('api/ai-review/cancel', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': CSRF_TOKEN,
            },
            body: JSON.stringify({ review_id: reviewId }),
        });
    } catch (e) {
        // Ignore cancel errors
    }
}

async function fetchAiReviewStatus(reviewId) {
    if (!reviewId) return null;
    try {
        const response = await fetch(`api/ai-review/status?review_id=${encodeURIComponent(reviewId)}`);
        if (!response.ok) return null;
        const data = await response.json();
        return data.status || null;
    } catch (e) {
        return null;
    }
}

async function fetchLatestAiReviewId(filePath) {
    if (!filePath) return null;
    try {
        const response = await fetch(`api/ai-review/latest?file_path=${encodeURIComponent(filePath)}`);
        if (!response.ok) return null;
        const data = await response.json();
        const reviewId = data.review_id || null;
        return (reviewId && REVIEW_ID_PATTERN.test(reviewId)) ? reviewId : null;
    } catch (e) {
        return null;
    }
}

function uniqueReviewIds(...reviewIds) {
    const seen = new Set();
    const result = [];
    for (const reviewId of reviewIds) {
        if (!reviewId || seen.has(reviewId)) continue;
        seen.add(reviewId);
        result.push(reviewId);
    }
    return result;
}

function renderReviewContent(content, markdown, showCancelButton) {
    let html = renderMarkdown(markdown);
    if (showCancelButton) {
        html += '<button class="btn-cancel-inline" onclick="cancelAiReview()">Cancel</button>';
    }
    content.innerHTML = html;
}

function addNewReviewButton(content) {
    if (!content || content.querySelector('.btn-new-review-inline')) return;
    const btn = document.createElement('button');
    btn.className = 'btn-new-review-inline';
    btn.textContent = 'New Review';
    btn.addEventListener('click', requestNewAiReview);
    content.appendChild(btn);
}

function requestNewAiReview() {
    requestAiReview({ forceNew: true });
}

function showReviewCancelledIndicator() {
    const content = document.getElementById('ai-review-content');
    if (!content) return;

    const cancelBtn = content.querySelector('.btn-cancel-inline');
    if (cancelBtn) cancelBtn.remove();

    if (!content.querySelector('.cancelled-indicator')) {
        const indicator = document.createElement('div');
        indicator.className = 'cancelled-indicator';
        indicator.textContent = 'Review canceled';
        content.appendChild(indicator);
    }
    addNewReviewButton(content);
    content.scrollTop = content.scrollHeight;
}

function showReviewExpiredIndicator(content) {
    content.innerHTML = '<div class="error">Saved review was not found (it may have expired).</div>';
    addNewReviewButton(content);
}

async function streamExistingReview(reviewId) {
    const panel = document.getElementById('ai-review-panel');
    const content = document.getElementById('ai-review-content');
    panel.classList.remove('hidden');
    content.innerHTML = '<div class="loading">Loading saved review...</div>';

    const reviewController = new AbortController();
    currentReviewController = reviewController;

    try {
        const response = await fetch(`api/ai-review?review_id=${encodeURIComponent(reviewId)}`, {
            signal: reviewController.signal,
        });

        if (!response.ok) {
            if (currentReviewController !== reviewController) return;
            if (response.status === 404) {
                setCurrentReviewId(null);
                showReviewExpiredIndicator(content);
                return 'missing';
            }
            let message = 'Failed to load saved review';
            try {
                const errorData = await response.json();
                message = errorData.error || message;
            } catch (e) {
                // Ignore JSON decode errors.
            }
            content.innerHTML = `<div class="error">${message}</div>`;
            addNewReviewButton(content);
            return 'error';
        }

        const headerStatus = (response.headers.get('X-AI-Review-Status') || '').toLowerCase();
        const isRunning = headerStatus === 'running';
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let markdown = '';
        if (isRunning) {
            content.innerHTML = '<div class="loading">Reconnecting to running review...</div><button class="btn-cancel-inline" onclick="cancelAiReview()">Cancel</button>';
        } else {
            content.innerHTML = '';
        }

        while (true) {
            if (currentReviewController !== reviewController) break;
            const { done, value } = await reader.read();
            if (done) break;

            markdown += decoder.decode(value, { stream: true });
            renderReviewContent(content, markdown, isRunning);
            content.scrollTop = content.scrollHeight;
        }

        if (currentReviewController !== reviewController) return;
        renderReviewContent(content, markdown, false);

        let finalStatus = headerStatus;
        if (headerStatus === 'running') {
            const latestStatus = await fetchAiReviewStatus(reviewId);
            if (latestStatus) finalStatus = latestStatus;
        }

        if (finalStatus === 'cancelled') {
            showReviewCancelledIndicator();
        } else if (finalStatus === 'running') {
            renderReviewContent(content, markdown, true);
        } else {
            addNewReviewButton(content);
        }
        return 'ok';
    } catch (err) {
        if (err.name === 'AbortError') return 'aborted';
        if (currentReviewController !== reviewController) return 'aborted';
        content.innerHTML = `<div class="error">Error: ${err.message}</div>`;
        addNewReviewButton(content);
        return 'error';
    } finally {
        if (currentReviewController === reviewController) {
            currentReviewController = null;
        }
    }
}

async function requestAiReview(options = {}) {
    if (!diffEditor) return;
    const forceNew = Boolean(options.forceNew);
    const customPrompt = typeof options.customPrompt === 'string' ? options.customPrompt.trim() : '';

    const panel = document.getElementById('ai-review-panel');
    const content = document.getElementById('ai-review-content');
    // Get content from textarea if in textbox mode, otherwise from Monaco
    const textboxEditor = document.getElementById('textbox-editor');
    const modifiedContent = (isTextboxMode && textboxEditor)
        ? textboxEditor.value
        : diffEditor.getModifiedEditor().getModel().getValue();

    if (!forceNew) {
        if (currentReviewController) {
            panel.classList.remove('hidden');
            return;
        }
        const latestReviewId = await fetchLatestAiReviewId(FILE_PATH);
        for (const reviewId of uniqueReviewIds(latestReviewId, currentReviewId)) {
            if (reviewId !== currentReviewId) {
                setCurrentReviewId(reviewId);
            }
            const existingResult = await streamExistingReview(reviewId);
            if (existingResult !== 'missing') {
                return;
            }
        }
    }

    const previousReviewId = currentReviewId;
    const previousReviewController = currentReviewController;

    if (previousReviewController) {
        previousReviewController.abort();
        currentReviewController = null;
    }
    if (forceNew && previousReviewId) {
        await sendAiReviewCancel(previousReviewId);
    }

    const nextReviewId = generateReviewId();
    setCurrentReviewId(nextReviewId);

    panel.classList.remove('hidden');
    content.innerHTML = '<div class="loading">Analyzing changes...</div><button class="btn-cancel-inline" onclick="cancelAiReview()">Cancel</button>';

    const reviewController = new AbortController();
    currentReviewController = reviewController;

    const restorePreviousReviewId = () => {
        setCurrentReviewId(previousReviewId || null);
    };

    try {
        const response = await fetch('api/ai-review', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': CSRF_TOKEN,
            },
            body: JSON.stringify({
                original: originalContent,
                modified: modifiedContent,
                file_path: FILE_PATH,
                language: fileLanguage,
                review_id: nextReviewId,
                reasoning_effort: getSelectedEffort(),
                custom_prompt: customPrompt,
            }),
            signal: reviewController.signal,
        });

        if (!response.ok) {
            if (currentReviewController !== reviewController) return;
            if (response.status === 409) {
                await streamExistingReview(nextReviewId);
                return;
            }
            let message = 'Review failed';
            try {
                const errorData = await response.json();
                message = errorData.error || message;
            } catch (e) {
                // Ignore JSON decode errors.
            }
            restorePreviousReviewId();
            content.innerHTML = `<div class="error">${message}</div>`;
            addNewReviewButton(content);
            return;
        }

        const serverReviewId = response.headers.get('X-AI-Review-Id');
        if (serverReviewId) {
            setCurrentReviewId(serverReviewId);
        } else {
            setCurrentReviewId(null);
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let markdown = '';
        content.innerHTML = '';

        while (true) {
            if (currentReviewController !== reviewController) break;
            const { done, value } = await reader.read();
            if (done) break;

            markdown += decoder.decode(value, { stream: true });
            renderReviewContent(content, markdown, true);
            content.scrollTop = content.scrollHeight;
        }

        if (currentReviewController !== reviewController) return;
        renderReviewContent(content, markdown, false);
        addNewReviewButton(content);
    } catch (err) {
        if (err.name === 'AbortError') return;
        if (currentReviewController !== reviewController) return;
        restorePreviousReviewId();
        content.innerHTML = `<div class="error">Error: ${err.message}</div>`;
        addNewReviewButton(content);
    } finally {
        if (currentReviewController === reviewController) {
            currentReviewController = null;
        }
    }
}

async function cancelAiReview() {
    if (currentReviewController) {
        currentReviewController.abort();
        currentReviewController = null;
    }
    showReviewCancelledIndicator();
    await sendAiReviewCancel(currentReviewId);
}

function renderMarkdown(text) {
    // Escape HTML before lightweight markdown transforms so tags render as text.
    const safeText = escapeHtml(text);

    // Simple markdown rendering for code review output
    return safeText
        // Code blocks
        .replace(/```(\w+)?\n([\s\S]*?)```/g, '<pre><code>$2</code></pre>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        // Bold (must come before italic)
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
        // Italic (single asterisk, but not inside words)
        .replace(/(?<![*\w])\*([^*]+)\*(?![*\w])/g, '<em>$1</em>')
        // Headers
        .replace(/^### (.+)$/gm, '<h4>$1</h4>')
        .replace(/^## (.+)$/gm, '<h3>$1</h3>')
        .replace(/^# (.+)$/gm, '<h2>$1</h2>')
        // Lists
        .replace(/^- (.+)$/gm, '<li>$1</li>')
        .replace(/^(\d+)\. (.+)$/gm, '<li>$2</li>')
        // Paragraphs (double newlines)
        .replace(/\n\n/g, '</p><p>')
        // Single newlines to breaks
        .replace(/\n/g, '<br>')
        // Wrap in paragraph
        .replace(/^/, '<p>')
        .replace(/$/, '</p>')
        // Clean up empty paragraphs
        .replace(/<p><\/p>/g, '')
        .replace(/<p>(<h[234]>)/g, '$1')
        .replace(/(<\/h[234]>)<\/p>/g, '$1');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function fetchRunToolingStatus(language) {
    const response = await fetch(`api/run-tooling?language=${encodeURIComponent(language)}`);
    const data = await response.json();

    if (!response.ok) {
        throw new Error(data.error || 'Failed to check run tooling');
    }

    return data;
}

function shellQuote(value) {
    return `'${String(value).replace(/'/g, "'\"'\"'")}'`;
}

async function runFile() {
    const statusEl = document.getElementById('status');
    const runBtn = document.getElementById('btn-run');

    if (!diffEditor) return;

    // Build the run command based on file language
    const basename = FILE_PATH.split('/').pop().replace(/\.[^.]+$/, '');
    const filePathId = Array.from(FILE_PATH).reduce(
        (hash, char) => ((hash * 31) + char.charCodeAt(0)) >>> 0,
        0
    ).toString(16);
    const outPath = `/tmp/run_${basename}_${filePathId}`;
    const csharpProjectDir = `${outPath}_csproj`;
    const outExePath = `${outPath}.exe`;
    const quotedFilePath = shellQuote(FILE_PATH);
    const quotedOutPath = shellQuote(outPath);
    const quotedOutExePath = shellQuote(outExePath);
    const quotedCsharpProjectDir = shellQuote(csharpProjectDir);
    const quotedCsharpProjectFile = shellQuote(`${csharpProjectDir}/Runner.csproj`);
    const quotedCsharpProgramPath = shellQuote(`${csharpProjectDir}/Program.cs`);

    let toolingStatus;
    try {
        toolingStatus = await fetchRunToolingStatus(fileLanguage);
    } catch (err) {
        statusEl.textContent = `Error: ${err.message}`;
        statusEl.className = 'status error';
        return;
    }

    if (!toolingStatus.available) {
        const installSuffix = toolingStatus.install_command ? ` Install with: ${toolingStatus.install_command}` : '';
        statusEl.textContent = `${toolingStatus.error || 'Required runtime not available'}${installSuffix}`;
        statusEl.className = 'status error';
        return;
    }

    let runCommand;
    switch (fileLanguage) {
        case 'python':
            runCommand = `python3 ${quotedFilePath}`;
            break;
        case 'javascript':
            runCommand = `node ${quotedFilePath}`;
            break;
        case 'shell':
            runCommand = `bash ${quotedFilePath}`;
            break;
        case 'go':
            runCommand = `go run ${quotedFilePath}`;
            break;
        case 'c':
            runCommand = `gcc ${quotedFilePath} -o ${quotedOutPath} && ${quotedOutPath}`;
            break;
        case 'cpp':
            runCommand = `g++ ${quotedFilePath} -o ${quotedOutPath} && ${quotedOutPath}`;
            break;
        case 'java':
            // Java 11+ single-file source execution
            runCommand = `java ${quotedFilePath}`;
            break;
        case 'ruby':
            runCommand = `ruby ${quotedFilePath}`;
            break;
        case 'perl':
            runCommand = `perl ${quotedFilePath}`;
            break;
        case 'rust':
            runCommand = `rustc ${quotedFilePath} -o ${quotedOutPath} && ${quotedOutPath}`;
            break;
        case 'csharp':
            if (toolingStatus.runner === 'dotnet') {
                runCommand = `if [ ! -f ${quotedCsharpProjectFile} ]; then dotnet new console -n Runner -o ${quotedCsharpProjectDir} >/dev/null; fi && cp ${quotedFilePath} ${quotedCsharpProgramPath} && dotnet run --project ${quotedCsharpProjectFile} --no-restore`;
            } else if (toolingStatus.runner === 'csc') {
                runCommand = `${toolingStatus.compiler} -nologo -out:${quotedOutExePath} ${quotedFilePath} && mono ${quotedOutExePath}`;
            } else if (toolingStatus.runner === 'mcs') {
                runCommand = `mcs ${quotedFilePath} -out:${quotedOutExePath} && mono ${quotedOutExePath}`;
            } else {
                statusEl.textContent = 'No C# runner available';
                statusEl.className = 'status error';
                return;
            }
            break;
        case 'brainfuck':
            runCommand = `bf ${quotedFilePath}`;
            break;
        case 'magma':
            runCommand = `magma ${quotedFilePath}`;
            break;
        default:
            return;
    }

    runBtn.disabled = true;

    // Save file first if there are unsaved changes
    const newContent = getCurrentModifiedContent();
    const hasUnsavedChanges = newContent !== currentContent;

    if (hasUnsavedChanges) {
        statusEl.textContent = 'Saving before run...';
        statusEl.className = 'status';

        try {
            const response = await fetch('api/file', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': CSRF_TOKEN,
                },
                body: JSON.stringify({
                    path: FILE_PATH,
                    content: newContent,
                }),
            });

            const data = await response.json();

            if (!response.ok) {
                statusEl.textContent = data.error || 'Save failed';
                statusEl.className = 'status error';
                runBtn.disabled = false;
                return;
            }

            // Update state
            currentContent = newContent;
            isModified = false;
        } catch (err) {
            statusEl.textContent = `Error: ${err.message}`;
            statusEl.className = 'status error';
            runBtn.disabled = false;
            return;
        }
    }

    // Close any existing run terminal session before starting a new one
    const overlay = document.getElementById('run-terminal-overlay');
    const frame = document.getElementById('run-terminal-frame');
    const restoreBtn = document.getElementById('btn-run-terminal-restore');
    if (frame.src && frame.src !== window.location.href) {
        frame.src = 'about:blank';
    }
    restoreBtn.classList.add('hidden');

    // Open inline terminal modal with the run command
    const terminalUrl = `/terminal?cmd=${encodeURIComponent(runCommand)}`;
    frame.src = terminalUrl;
    overlay.classList.remove('hidden');

    runBtn.disabled = false;
    statusEl.textContent = 'Running...';
    statusEl.className = 'status';
}

function toggleTextboxMode() {
    if (isImageFile || !diffEditor) return;

    if (isTextboxMode) {
        exitTextboxMode();
    } else {
        enterTextboxMode();
    }

    // Update button state
    const textboxBtn = document.getElementById('btn-textbox');
    const textboxBtnText = textboxBtn.querySelector('.btn-text');
    if (textboxBtnText) {
        textboxBtnText.textContent = isTextboxMode ? 'Diff View' : 'Textbox';
    }
    textboxBtn.classList.toggle('active', isTextboxMode);
}

let unifiedDiffEditor = null;
let unifiedDiffOriginalModel = null;
let unifiedDiffModifiedModel = null;
let textboxUpdateLineNumbers = null; // Function to recalculate line numbers
let textboxLineHeights = []; // Cumulative heights for each line (for scroll sync with wrapping)
let textboxResizeObserver = null;
let unifiedDiffUpdateTimeout = null;
let pendingUnifiedDiffContent = null;
let textboxHighlightUpdateTimeout = null;
let pendingTextboxHighlightContent = null;
let textboxHighlightScrollTimeout = null;
const UNIFIED_DIFF_UPDATE_DELAY_MS = 600;
const TEXTBOX_HIGHLIGHT_UPDATE_DELAY_MS = 120;
const TEXTBOX_HIGHLIGHT_SCROLL_IDLE_DELAY_MS = 90;
const AUTO_REFRESH_LOG_SUFFIXES = ['.log', '.jsonl', '.err', '.out', '.trace', '.logfile', '.access'];
const ROTATED_LOG_SUFFIX_RE = /\.(log|jsonl|err|out|trace|logfile|access|txt)\.(gz|[0-9]+)$/i;

function isAutoRefreshLogFile(filePath) {
    if (!filePath) return false;

    const fileName = String(filePath).split('/').pop() || '';
    const loweredName = fileName.toLowerCase();
    if (!loweredName || loweredName === 'a.out') return false;
    if (ROTATED_LOG_SUFFIX_RE.test(loweredName)) return false;

    if (AUTO_REFRESH_LOG_SUFFIXES.some((suffix) => loweredName.endsWith(suffix))) {
        return true;
    }

    return loweredName.endsWith('.txt') && loweredName.includes('log');
}

function isElementNearBottom(view) {
    if (!view) return false;
    if (typeof view.getScrollTop === 'function') {
        const scrollTop = view.getScrollTop();
        const scrollHeight = view.getScrollHeight();
        const clientHeight = view.getLayoutInfo().height;
        return scrollTop + clientHeight >= scrollHeight - 10;
    }
    return view.scrollTop + view.clientHeight >= view.scrollHeight - 10;
}

function getTextareaContentWidth(textarea) {
    const computedStyle = window.getComputedStyle(textarea);
    const paddingLeft = parseFloat(computedStyle.paddingLeft) || 0;
    const paddingRight = parseFloat(computedStyle.paddingRight) || 0;
    return Math.max(0, textarea.clientWidth - paddingLeft - paddingRight);
}

function syncTextboxContentToModifiedModel(content) {
    const modifiedModel = diffEditor.getModifiedEditor().getModel();
    if (modifiedModel && modifiedModel.getValue() !== content) {
        modifiedModel.setValue(content);
    }
}

function getCurrentModifiedContent() {
    const textboxEditor = document.getElementById('textbox-editor');
    if (isTextboxMode && textboxEditor) {
        return textboxEditor.value;
    }

    const modifiedModel = diffEditor.getModifiedEditor().getModel();
    return modifiedModel ? modifiedModel.getValue() : '';
}

function setUnifiedDiffModifiedContent(content) {
    if (!unifiedDiffEditor || !unifiedDiffOriginalModel || !unifiedDiffModifiedModel) return;
    if (unifiedDiffModifiedModel.getValue() !== content) {
        unifiedDiffModifiedModel.setValue(content);
    }
}

function updateTextboxHighlightVisibility(highlightEl) {
    if (!highlightEl) return;
    const hasPendingUpdate = highlightEl.dataset.pendingHighlight === 'true';
    const isScrolling = highlightEl.dataset.scrolling === 'true';
    highlightEl.style.opacity = (hasPendingUpdate || isScrolling) ? '0' : '1';
}

function setTextboxHighlightPending(highlightEl, isPending) {
    if (!highlightEl) return;
    highlightEl.dataset.pendingHighlight = isPending ? 'true' : 'false';
    updateTextboxHighlightVisibility(highlightEl);
}

function setTextboxHighlightScrolling(highlightEl, isScrolling) {
    if (!highlightEl) return;
    highlightEl.dataset.scrolling = isScrolling ? 'true' : 'false';
    updateTextboxHighlightVisibility(highlightEl);
}

function isKeywordToken(tokenType) {
    return typeof tokenType === 'string' && tokenType.split('.').includes('keyword');
}

function renderHighlightedTextboxContent(highlightEl, content) {
    if (!highlightEl) return;
    const highlightContentEl = highlightEl.querySelector('.textbox-highlight-content');
    if (!highlightContentEl) return;

    let html;
    try {
        const tokenLines = monaco.editor.tokenize(content, fileLanguage);
        const lines = content.split('\n');

        html = lines.map((line, lineIndex) => {
            const tokens = tokenLines[lineIndex] || [];
            if (tokens.length === 0) {
                return escapeHtml(line);
            }

            let lineHtml = '';
            for (let i = 0; i < tokens.length; i++) {
                const start = tokens[i].offset;
                const end = i + 1 < tokens.length ? tokens[i + 1].offset : line.length;
                const segment = line.slice(start, end);
                if (!segment) continue;

                const escapedSegment = escapeHtml(segment);
                lineHtml += isKeywordToken(tokens[i].type)
                    ? `<span class="textbox-token-keyword">${escapedSegment}</span>`
                    : escapedSegment;
            }

            return lineHtml;
        }).join('\n');
    } catch (err) {
        html = escapeHtml(content);
    }

    if (content.endsWith('\n')) {
        html += '<span aria-hidden="true">\u200b</span>';
    }

    highlightContentEl.innerHTML = html;
    setTextboxHighlightPending(highlightEl, false);
}

function cancelPendingTextboxHighlightUpdate() {
    if (textboxHighlightUpdateTimeout !== null) {
        window.clearTimeout(textboxHighlightUpdateTimeout);
        textboxHighlightUpdateTimeout = null;
    }
    pendingTextboxHighlightContent = null;
}

function cancelPendingTextboxHighlightScroll() {
    if (textboxHighlightScrollTimeout !== null) {
        window.clearTimeout(textboxHighlightScrollTimeout);
        textboxHighlightScrollTimeout = null;
    }
}

function flushPendingTextboxHighlightUpdate(highlightEl) {
    if (!highlightEl || pendingTextboxHighlightContent === null) return;
    const content = pendingTextboxHighlightContent;
    pendingTextboxHighlightContent = null;
    if (textboxHighlightUpdateTimeout !== null) {
        window.clearTimeout(textboxHighlightUpdateTimeout);
        textboxHighlightUpdateTimeout = null;
    }
    renderHighlightedTextboxContent(highlightEl, content);
}

function scheduleTextboxHighlightUpdate(highlightEl, content) {
    if (!highlightEl) return;
    pendingTextboxHighlightContent = content;
    setTextboxHighlightPending(highlightEl, true);
    if (textboxHighlightUpdateTimeout !== null) {
        window.clearTimeout(textboxHighlightUpdateTimeout);
    }
    textboxHighlightUpdateTimeout = window.setTimeout(() => {
        textboxHighlightUpdateTimeout = null;
        flushPendingTextboxHighlightUpdate(highlightEl);
    }, TEXTBOX_HIGHLIGHT_UPDATE_DELAY_MS);
}

function syncTextboxHighlightScroll(textarea, highlightEl) {
    if (!textarea || !highlightEl) return;
    const highlightContentEl = highlightEl.querySelector('.textbox-highlight-content');
    if (!highlightContentEl) return;

    highlightContentEl.style.transform = `translate(${-textarea.scrollLeft}px, ${-textarea.scrollTop}px)`;
}

function scheduleTextboxHighlightScrollSync(textarea, highlightEl) {
    if (!textarea || !highlightEl) return;

    setTextboxHighlightScrolling(highlightEl, true);
    cancelPendingTextboxHighlightScroll();
    textboxHighlightScrollTimeout = window.setTimeout(() => {
        textboxHighlightScrollTimeout = null;
        syncTextboxHighlightScroll(textarea, highlightEl);
        setTextboxHighlightScrolling(highlightEl, false);
    }, TEXTBOX_HIGHLIGHT_SCROLL_IDLE_DELAY_MS);
}

function syncTextboxModeExternalContent(content, { followTail = false } = {}) {
    const textarea = document.getElementById('textbox-editor');
    if (!textarea) return;

    const highlightEl = document.getElementById('textbox-highlight');
    const lineNumsEl = document.getElementById('textbox-line-nums');

    textarea.value = content;

    if (textboxUpdateLineNumbers) {
        textboxUpdateLineNumbers(true);
    }

    cancelPendingTextboxHighlightUpdate();
    renderHighlightedTextboxContent(highlightEl, content);

    if (unifiedDiffEditor) {
        cancelPendingUnifiedDiffUpdate();
        setUnifiedDiffModifiedContent(content);
    }

    if (followTail) {
        textarea.scrollTop = textarea.scrollHeight;
    }

    if (lineNumsEl) {
        lineNumsEl.scrollTop = textarea.scrollTop;
    }
    syncTextboxHighlightScroll(textarea, highlightEl);

    if (followTail && unifiedDiffEditor) {
        unifiedDiffEditor.getModifiedEditor().revealLine(
            unifiedDiffModifiedModel ? unifiedDiffModifiedModel.getLineCount() : textarea.value.split('\n').length
        );
    }
}

function cancelPendingUnifiedDiffUpdate() {
    if (unifiedDiffUpdateTimeout !== null) {
        window.clearTimeout(unifiedDiffUpdateTimeout);
        unifiedDiffUpdateTimeout = null;
    }
    pendingUnifiedDiffContent = null;
}

function flushPendingUnifiedDiffUpdate() {
    if (pendingUnifiedDiffContent === null) return;
    const content = pendingUnifiedDiffContent;
    pendingUnifiedDiffContent = null;
    if (unifiedDiffUpdateTimeout !== null) {
        window.clearTimeout(unifiedDiffUpdateTimeout);
        unifiedDiffUpdateTimeout = null;
    }
    setUnifiedDiffModifiedContent(content);
}

function scheduleUnifiedDiffUpdate(content) {
    if (!unifiedDiffEditor) return;
    pendingUnifiedDiffContent = content;
    if (unifiedDiffUpdateTimeout !== null) {
        window.clearTimeout(unifiedDiffUpdateTimeout);
    }
    unifiedDiffUpdateTimeout = window.setTimeout(() => {
        unifiedDiffUpdateTimeout = null;
        flushPendingUnifiedDiffUpdate();
    }, UNIFIED_DIFF_UPDATE_DELAY_MS);
}

function disposeUnifiedDiffResources() {
    cancelPendingUnifiedDiffUpdate();
    cancelPendingTextboxHighlightUpdate();
    cancelPendingTextboxHighlightScroll();

    if (unifiedDiffEditor) {
        unifiedDiffEditor.dispose();
        unifiedDiffEditor = null;
    }

    if (unifiedDiffOriginalModel) {
        unifiedDiffOriginalModel.dispose();
        unifiedDiffOriginalModel = null;
    }

    if (unifiedDiffModifiedModel) {
        unifiedDiffModifiedModel.dispose();
        unifiedDiffModifiedModel = null;
    }
}

function enterTextboxMode() {
    const container = document.getElementById('editor-container');
    const modifiedEditor = diffEditor.getModifiedEditor();

    // Get current content from Monaco
    const modifiedContent = modifiedEditor.getModel().getValue();

    // Save scroll info before hiding the editor
    const visibleRanges = modifiedEditor.getVisibleRanges();
    const topLine = visibleRanges.length > 0 ? visibleRanges[0].startLineNumber : 1;
    savedScrollInfo = { lineNumber: topLine };

    // Hide Monaco diff editor
    container.style.display = 'none';

    // Check if wide screen (show split view) or narrow (just textbox)
    const isWideScreen = window.innerWidth > 768;

    // Create textbox mode container (hidden initially to prevent flash)
    const textboxContainer = document.createElement('div');
    textboxContainer.id = 'textbox-mode-container';
    textboxContainer.className = 'textbox-mode-container';
    textboxContainer.style.opacity = '0';

    if (isWideScreen) {
        // Left panel: Monaco unified diff (read-only)
        const leftPanel = document.createElement('div');
        leftPanel.className = 'textbox-panel';
        leftPanel.innerHTML = `
            <div class="textbox-content-wrapper">
                <div id="unified-diff-monaco" style="flex: 1; min-height: 0; background: transparent;"></div>
            </div>
        `;
        textboxContainer.appendChild(leftPanel);
    }

    // Right panel (or only panel on mobile): Editable textarea
    const rightPanel = document.createElement('div');
    rightPanel.className = 'textbox-panel';

    rightPanel.innerHTML = `
        <div class="textbox-content-wrapper">
            <div class="textbox-line-numbers" id="textbox-line-nums"></div>
            <div class="textbox-editor-stack">
                <div class="textbox-highlight-layer" id="textbox-highlight" aria-hidden="true">
                    <pre class="textbox-highlight-content"></pre>
                </div>
                <textarea class="textbox-textarea" id="textbox-editor" spellcheck="false"></textarea>
            </div>
            <pre class="textbox-mirror" id="textbox-mirror" aria-hidden="true"></pre>
        </div>
    `;
    textboxContainer.appendChild(rightPanel);

    container.parentNode.insertBefore(textboxContainer, container);

    // Set textarea value directly (not via innerHTML - that doesn't work reliably for textareas)
    const textarea = document.getElementById('textbox-editor');
    const highlightEl = document.getElementById('textbox-highlight');
    const lineNumsEl = document.getElementById('textbox-line-nums');
    const mirror = document.getElementById('textbox-mirror');
    textarea.value = modifiedContent;
    setTextboxHighlightPending(highlightEl, false);
    setTextboxHighlightScrolling(highlightEl, false);
    renderHighlightedTextboxContent(highlightEl, modifiedContent);

    // Apply current wrap setting to textarea
    if (wordWrapEnabled) {
        textarea.style.whiteSpace = 'pre-wrap';
        textarea.style.overflowX = 'hidden';
        if (highlightEl) {
            const highlightContentEl = highlightEl.querySelector('.textbox-highlight-content');
            if (highlightContentEl) {
                highlightContentEl.style.whiteSpace = 'pre-wrap';
            }
        }
    }

    // Set up line number measurement
    const baseLineHeight = 14 * 1.5; // font-size * line-height
    let lastUnwrappedLineCount = null;

    function updateLineNumbers(force = false) {
        const lines = textarea.value.split('\n');

        if (!wordWrapEnabled) {
            if (!force && lastUnwrappedLineCount === lines.length) {
                return;
            }

            lastUnwrappedLineCount = lines.length;
            let html = '';
            textboxLineHeights = [0];

            for (let i = 0; i < lines.length; i++) {
                textboxLineHeights.push((i + 1) * baseLineHeight);
                html += `<div style="height: ${baseLineHeight}px">${i + 1}</div>`;
            }

            lineNumsEl.innerHTML = html;
            return;
        }

        lastUnwrappedLineCount = null;

        // Match the textarea content box so wrap measurement accounts for padding.
        mirror.style.width = getTextareaContentWidth(textarea) + 'px';
        mirror.style.whiteSpace = wordWrapEnabled ? 'pre-wrap' : 'pre';

        let html = '';
        textboxLineHeights = [0]; // Reset cumulative heights, starting at 0
        let cumulativeHeight = 0;

        for (let i = 0; i < lines.length; i++) {
            // Measure this line's height by putting it in the mirror
            // Use non-breaking space for empty lines to get correct height
            mirror.textContent = lines[i] || '\u00A0';
            const lineHeight = mirror.offsetHeight;
            const numWrappedLines = Math.max(1, Math.round(lineHeight / baseLineHeight));
            const actualHeight = numWrappedLines * baseLineHeight;

            cumulativeHeight += actualHeight;
            textboxLineHeights.push(cumulativeHeight);

            // Create line number with appropriate height
            html += `<div style="height: ${actualHeight}px">${i + 1}</div>`;
        }

        lineNumsEl.innerHTML = html;
    }

    // Helper: find logical line number at a given scroll position
    function getLineAtScrollTop(scrollTop) {
        for (let i = 1; i < textboxLineHeights.length; i++) {
            if (scrollTop < textboxLineHeights[i]) {
                return i; // 1-based line number
            }
        }
        return Math.max(1, textboxLineHeights.length - 1);
    }

    // Helper: get scroll position for a logical line number
    function getScrollTopForLine(lineNumber) {
        if (lineNumber <= 1) return 0;
        if (lineNumber > textboxLineHeights.length - 1) {
            return textboxLineHeights[textboxLineHeights.length - 1] || 0;
        }
        return textboxLineHeights[lineNumber - 1] || 0;
    }

    // Store function globally for wrap toggle to use
    textboxUpdateLineNumbers = updateLineNumbers;

    // Initial line number generation (deferred to ensure DOM layout is complete)
    function initTextbox() {
        updateLineNumbers(true);

        // Restore scroll position (using cumulative heights for wrapped lines)
        if (savedScrollInfo && savedScrollInfo.lineNumber) {
            textarea.scrollTop = getScrollTopForLine(savedScrollInfo.lineNumber);
            lineNumsEl.scrollTop = textarea.scrollTop;
            syncTextboxHighlightScroll(textarea, highlightEl);
            if (unifiedDiffEditor) {
                const monacoEditor = unifiedDiffEditor.getModifiedEditor();
                const monacoLineHeight = monacoEditor.getOption(monaco.editor.EditorOption.lineHeight);
                monacoEditor.setScrollTop((savedScrollInfo.lineNumber - 1) * monacoLineHeight);
            }
        }

        // Reveal container now that layout and scroll are ready
        textboxContainer.style.opacity = '1';
    }

    // Double RAF needed on mobile for layout to settle; single RAF sufficient on wider screens
    if (isWideScreen) {
        requestAnimationFrame(initTextbox);
    } else {
        requestAnimationFrame(() => requestAnimationFrame(initTextbox));
    }

    // Update on resize (affects wrapping)
    textboxResizeObserver = new ResizeObserver(() => {
        if (wordWrapEnabled) updateLineNumbers();
    });
    textboxResizeObserver.observe(textarea);

    // Create Monaco unified diff viewer on wide screens
    if (isWideScreen) {
        const unifiedContainer = document.getElementById('unified-diff-monaco');
        unifiedDiffEditor = monaco.editor.createDiffEditor(unifiedContainer, {
            theme: 'cute-transparent',
            automaticLayout: true,
            renderSideBySide: false, // Unified/inline diff view
            originalEditable: false,
            readOnly: true,
            minimap: { enabled: false },
            scrollBeyondLastLine: false,
            fontSize: 14,
            lineNumbers: 'on',
            renderWhitespace: 'selection',
            wordWrap: wordWrapEnabled ? 'on' : 'off',
            scrollbar: { vertical: 'hidden', horizontal: 'hidden', verticalScrollbarSize: 0, horizontalScrollbarSize: 0 },
        });

        unifiedDiffOriginalModel = monaco.editor.createModel(originalContent, fileLanguage);
        unifiedDiffModifiedModel = monaco.editor.createModel(modifiedContent, fileLanguage);
        unifiedDiffEditor.setModel({
            original: unifiedDiffOriginalModel,
            modified: unifiedDiffModifiedModel,
        });
    }

    // Sync line numbers with textarea scroll
    let scrollSource = null; // Track which element initiated scroll

    textarea.addEventListener('scroll', () => {
        lineNumsEl.scrollTop = textarea.scrollTop;
        scheduleTextboxHighlightScrollSync(textarea, highlightEl);

        // Sync Monaco unified diff to same line
        if (unifiedDiffEditor && scrollSource !== 'monaco') {
            scrollSource = 'textarea';
            const monacoEditor = unifiedDiffEditor.getModifiedEditor();
            const topLine = getLineAtScrollTop(textarea.scrollTop);
            if (wordWrapEnabled) {
                // In wrap mode, use revealLineNearTop to handle Monaco's own wrapping
                // Add 7-line offset to compensate for revealLineNearTop's built-in padding
                monacoEditor.revealLineNearTop(topLine + 7);
            } else {
                // Without wrapping, pixel-based sync works fine
                const monacoLineHeight = monacoEditor.getOption(monaco.editor.EditorOption.lineHeight);
                monacoEditor.setScrollTop((topLine - 1) * monacoLineHeight);
            }
            requestAnimationFrame(() => { scrollSource = null; });
        }
    });

    // Sync textarea when scrolling Monaco unified diff
    if (unifiedDiffEditor) {
        unifiedDiffEditor.getModifiedEditor().onDidScrollChange((e) => {
            if (scrollSource !== 'textarea' && e.scrollTopChanged) {
                scrollSource = 'monaco';
                const monacoEditor = unifiedDiffEditor.getModifiedEditor();
                let topLine;
                if (wordWrapEnabled) {
                    // In wrap mode, use getVisibleRanges to get accurate top line
                    const visibleRanges = monacoEditor.getVisibleRanges();
                    topLine = visibleRanges.length > 0 ? visibleRanges[0].startLineNumber : 1;
                } else {
                    // Without wrapping, pixel-based calculation works fine
                    const monacoLineHeight = monacoEditor.getOption(monaco.editor.EditorOption.lineHeight);
                    topLine = Math.floor(e.scrollTop / monacoLineHeight) + 1;
                }
                textarea.scrollTop = getScrollTopForLine(topLine);
                lineNumsEl.scrollTop = textarea.scrollTop;
                scheduleTextboxHighlightScrollSync(textarea, highlightEl);
                requestAnimationFrame(() => { scrollSource = null; });
            }
        });
    }

    // Update line numbers on content change
    textarea.addEventListener('input', () => {
        // Recalculate line numbers (handles wrapping)
        updateLineNumbers();

        // Track modifications
        isModified = textarea.value !== currentContent;
        updateStatus();

        scheduleTextboxHighlightUpdate(highlightEl, textarea.value);

        // Update unified diff Monaco editor if present
        if (unifiedDiffEditor) {
            scheduleUnifiedDiffUpdate(textarea.value);
        }
    });

    isTextboxMode = true;
}

function exitTextboxMode() {
    const container = document.getElementById('editor-container');
    const textboxContainer = document.getElementById('textbox-mode-container');
    const textarea = document.getElementById('textbox-editor');

    if (!textboxContainer || !textarea) return;

    // Get content and scroll position from textarea (using cumulative heights)
    const newContent = textarea.value;
    // Find logical line number at current scroll position
    let lineNumber = 1;
    for (let i = 1; i < textboxLineHeights.length; i++) {
        if (textarea.scrollTop < textboxLineHeights[i]) {
            lineNumber = i;
            break;
        }
        lineNumber = i;
    }
    savedScrollInfo = { lineNumber };

    if (textboxResizeObserver) {
        textboxResizeObserver.disconnect();
        textboxResizeObserver = null;
    }

    disposeUnifiedDiffResources();

    // Clear line number update function and heights array
    textboxUpdateLineNumbers = null;
    textboxLineHeights = [];

    // Remove textbox container
    textboxContainer.remove();

    // Show Monaco editor
    container.style.display = '';

    // Update Monaco model with new content
    syncTextboxContentToModifiedModel(newContent);

    // Restore scroll position in Monaco
    if (savedScrollInfo && savedScrollInfo.lineNumber) {
        diffEditor.getModifiedEditor().revealLineNearTop(savedScrollInfo.lineNumber);
    }

    isTextboxMode = false;
}

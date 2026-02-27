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
            saveBtn.disabled = true;
            saveBtn.title = 'Image files are preview-only';
            wrapBtn.disabled = true;
            wrapBtn.title = 'Wrap is unavailable for image preview';
            aiReviewBtn.disabled = true;
            aiReviewBtn.title = 'AI review is unavailable for image preview';
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

        // Close AI review panel
        const closeReviewBtn = document.getElementById('btn-close-review');
        closeReviewBtn.addEventListener('click', (e) => {
            e.preventDefault();
            e.stopPropagation();
            document.getElementById('ai-review-panel').classList.add('hidden');
        });

        // Run button - show for runnable file types
        const runBtn = document.getElementById('btn-run');
        const runnableLanguages = ['python', 'javascript', 'shell', 'go', 'c', 'cpp', 'java'];
        if (runnableLanguages.includes(fileLanguage)) {
            runBtn.classList.remove('hidden');
            runBtn.addEventListener('click', runFile);
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

    const modifiedModel = diffEditor.getModifiedEditor().getModel();
    const newContent = modifiedModel.getValue();

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

    // Update button appearance
    const wrapBtn = document.getElementById('btn-wrap');
    wrapBtn.textContent = wordWrapEnabled ? 'Wrap: On' : 'Wrap: Off';
}

let currentReviewController = null;
const REVIEW_ID_STORAGE_KEY = `diff-editor-review-id:${FILE_PATH}`;
const REVIEW_ID_PATTERN = /^[A-Za-z0-9_-]{8,64}$/;
let currentReviewId = loadStoredReviewId();

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

    const panel = document.getElementById('ai-review-panel');
    const content = document.getElementById('ai-review-content');
    const modifiedContent = diffEditor.getModifiedEditor().getModel().getValue();

    if (!forceNew) {
        if (currentReviewController) {
            panel.classList.remove('hidden');
            return;
        }
        if (currentReviewId) {
            const existingResult = await streamExistingReview(currentReviewId);
            if (existingResult !== 'missing') {
                return;
            }
        }
        const latestReviewId = await fetchLatestAiReviewId(FILE_PATH);
        if (latestReviewId) {
            setCurrentReviewId(latestReviewId);
            const latestResult = await streamExistingReview(latestReviewId);
            if (latestResult !== 'missing') {
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

async function runFile() {
    const statusEl = document.getElementById('status');
    const runBtn = document.getElementById('btn-run');

    if (!diffEditor) return;

    // Build the run command based on file language
    const basename = FILE_PATH.split('/').pop().replace(/\.[^.]+$/, '');
    const outPath = `/tmp/run_${basename}`;

    let runCommand;
    switch (fileLanguage) {
        case 'python':
            runCommand = `python3 ${FILE_PATH}`;
            break;
        case 'javascript':
            runCommand = `node ${FILE_PATH}`;
            break;
        case 'shell':
            runCommand = `bash ${FILE_PATH}`;
            break;
        case 'go':
            runCommand = `go run ${FILE_PATH}`;
            break;
        case 'c':
            runCommand = `gcc ${FILE_PATH} -o ${outPath} && ${outPath}`;
            break;
        case 'cpp':
            runCommand = `g++ ${FILE_PATH} -o ${outPath} && ${outPath}`;
            break;
        case 'java':
            // Java 11+ single-file source execution
            runCommand = `java ${FILE_PATH}`;
            break;
        default:
            return;
    }

    runBtn.disabled = true;

    // Save file first if there are unsaved changes
    const modifiedModel = diffEditor.getModifiedEditor().getModel();
    const newContent = modifiedModel.getValue();
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

    // Open terminal with the run command
    const terminalUrl = `/terminal?cmd=${encodeURIComponent(runCommand)}`;
    window.open(terminalUrl, '_blank');

    runBtn.disabled = false;
    statusEl.textContent = 'Opened in terminal';
    statusEl.className = 'status';
}

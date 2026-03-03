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
            const textboxBtn = document.getElementById('btn-textbox');
            saveBtn.disabled = true;
            saveBtn.title = 'Image files are preview-only';
            wrapBtn.disabled = true;
            wrapBtn.title = 'Wrap is unavailable for image preview';
            aiReviewBtn.disabled = true;
            aiReviewBtn.title = 'AI review is unavailable for image preview';
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
        const runnableLanguages = ['python', 'javascript', 'shell', 'go', 'c', 'cpp', 'java', 'ruby', 'perl', 'rust', 'csharp', 'brainfuck'];
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
        case 'ruby':
            runCommand = `ruby ${FILE_PATH}`;
            break;
        case 'perl':
            runCommand = `perl ${FILE_PATH}`;
            break;
        case 'rust':
            runCommand = `if command -v rustc >/dev/null 2>&1; then rustc ${FILE_PATH} -o ${outPath} && ${outPath}; else echo 'Rust requires rustc and cargo. Install with: sudo apt update && sudo apt install rustc cargo'; exit 1; fi`;
            break;
        case 'csharp':
            runCommand = `if command -v mcs >/dev/null 2>&1 && command -v mono >/dev/null 2>&1; then mcs ${FILE_PATH} -out:${outPath}.exe && mono ${outPath}.exe; elif command -v csc >/dev/null 2>&1 && command -v mono >/dev/null 2>&1; then csc -nologo -out:${outPath}.exe ${FILE_PATH} && mono ${outPath}.exe; else echo 'C# requires Mono. Install with: sudo apt update && sudo apt install mono-devel'; exit 1; fi`;
            break;
        case 'brainfuck':
            runCommand = `bf ${FILE_PATH}`;
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

    // Open terminal with the run command
    const terminalUrl = `/terminal?cmd=${encodeURIComponent(runCommand)}`;
    window.open(terminalUrl, '_blank');

    runBtn.disabled = false;
    statusEl.textContent = 'Opened in terminal';
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

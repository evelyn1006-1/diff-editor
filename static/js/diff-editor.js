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
        closeReviewBtn.addEventListener('click', () => {
            document.getElementById('ai-review-panel').classList.add('hidden');
        });

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

async function requestAiReview() {
    if (!diffEditor) return;

    const panel = document.getElementById('ai-review-panel');
    const content = document.getElementById('ai-review-content');
    const btn = document.getElementById('btn-ai-review');

    const modifiedContent = diffEditor.getModifiedEditor().getModel().getValue();

    // Show panel with loading state
    panel.classList.remove('hidden');
    content.innerHTML = '<div class="loading">Analyzing changes...</div>';
    btn.disabled = true;

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
            }),
        });

        if (!response.ok) {
            const errorData = await response.json();
            content.innerHTML = `<div class="error">${errorData.error || 'Review failed'}</div>`;
            return;
        }

        // Stream the response
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let markdown = '';

        content.innerHTML = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;

            markdown += decoder.decode(value, { stream: true });
            content.innerHTML = renderMarkdown(markdown);
            content.scrollTop = content.scrollHeight;
        }

    } catch (err) {
        content.innerHTML = `<div class="error">Error: ${err.message}</div>`;
    } finally {
        btn.disabled = false;
    }
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
        // Bold
        .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
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

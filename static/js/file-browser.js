/**
 * File browser component for the diff editor
 */

let currentPath = '';

function initFileBrowser(defaultRoot) {
    currentPath = defaultRoot;

    document.getElementById('btn-up').addEventListener('click', goUp);
    document.getElementById('btn-new-file').addEventListener('click', createNewFile);
    document.getElementById('show-hidden').addEventListener('change', () => loadDirectory(currentPath));

    // Allow direct path editing
    const pathInput = document.getElementById('path-input');
    pathInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            loadDirectory(pathInput.value.trim() || '/');
        }
    });

    loadDirectory(currentPath);
}

async function loadDirectory(path) {
    const fileList = document.getElementById('file-list');
    const pathInput = document.getElementById('path-input');
    const showHidden = document.getElementById('show-hidden').checked;

    fileList.innerHTML = '<div class="loading">Loading...</div>';

    try {
        const response = await fetch(`api/browse?path=${encodeURIComponent(path)}&hidden=${showHidden}`);
        const data = await response.json();

        if (!response.ok) {
            fileList.innerHTML = `<div class="loading" style="color: var(--error)">${data.error || 'Failed to load directory'}</div>`;
            return;
        }

        currentPath = data.path;
        pathInput.value = data.path;

        if (data.items.length === 0) {
            fileList.innerHTML = '<div class="loading">Empty directory</div>';
            return;
        }

        fileList.innerHTML = data.items.map(item => createFileItem(item)).join('');

        // Add click handlers
        fileList.querySelectorAll('.file-item').forEach(el => {
            el.addEventListener('click', () => handleItemClick(el.dataset.path, el.dataset.isDir === 'true'));
        });

    } catch (err) {
        fileList.innerHTML = `<div class="loading" style="color: var(--error)">Error: ${err.message}</div>`;
    }
}

function createFileItem(item) {
    const icon = item.is_dir ? '<span class="icon folder">&#128193;</span>' : '<span class="icon">&#128196;</span>';

    let badges = '';
    if (!item.is_dir) {
        // Git status badges
        if (item.git_status === 'modified') {
            badges += '<span class="badge modified">modified</span>';
        } else if (item.git_status === 'new') {
            badges += '<span class="badge new">new</span>';
        } else if (item.git_status === 'deleted') {
            badges += '<span class="badge deleted">deleted</span>';
        } else if (item.is_git) {
            badges += '<span class="badge git">git</span>';
        }
        if (item.writable === false) {
            badges += '<span class="badge sudo">sudo</span>';
        }
    }

    return `
        <div class="file-item" data-path="${escapeHtml(item.path)}" data-is-dir="${item.is_dir}">
            ${icon}
            <span class="name">${escapeHtml(item.name)}</span>
            ${badges}
        </div>
    `;
}

function handleItemClick(path, isDir) {
    if (isDir) {
        loadDirectory(path);
    } else {
        // Open in diff editor
        window.location.href = `diff?file=${encodeURIComponent(path)}`;
    }
}

function goUp() {
    // Go to parent directory by removing last path component
    const parts = currentPath.split('/').filter(p => p);
    if (parts.length > 1) {
        parts.pop();
        loadDirectory('/' + parts.join('/'));
    } else if (parts.length === 1) {
        loadDirectory('/');
    }
}

async function createNewFile() {
    const name = window.prompt('New file name:', 'newfile.txt');
    if (name === null) return;

    const trimmedName = name.trim();
    if (!trimmedName) return;

    try {
        const response = await fetch('api/file/create', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': CSRF_TOKEN,
            },
            body: JSON.stringify({
                directory: currentPath,
                name: trimmedName,
            }),
        });

        const data = await response.json();
        if (!response.ok) {
            window.alert(data.error || 'Failed to create file');
            return;
        }

        // Open the new file directly in the diff editor.
        window.location.href = `diff?file=${encodeURIComponent(data.path)}`;
    } catch (err) {
        window.alert(`Error: ${err.message}`);
    }
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

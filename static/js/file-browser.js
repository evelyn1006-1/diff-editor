/**
 * File browser component for the diff editor
 */

let currentPath = '';

function initFileBrowser(defaultRoot) {
    currentPath = defaultRoot;

    document.getElementById('btn-up').addEventListener('click', goUp);
    document.getElementById('show-hidden').addEventListener('change', () => loadDirectory(currentPath));

    // "New" dropdown
    const btnNew = document.getElementById('btn-new');
    const dropdown = document.getElementById('new-dropdown');
    btnNew.addEventListener('click', (e) => {
        e.stopPropagation();
        dropdown.classList.toggle('open');
    });
    document.addEventListener('click', () => dropdown.classList.remove('open'));
    dropdown.addEventListener('click', (e) => e.stopPropagation());

    document.getElementById('btn-new-file').addEventListener('click', () => { dropdown.classList.remove('open'); createNewFile(); });
    document.getElementById('btn-new-dir').addEventListener('click', () => { dropdown.classList.remove('open'); createNewDir(); });
    document.getElementById('upload-input').addEventListener('change', (e) => { dropdown.classList.remove('open'); uploadFiles(e.target.files); e.target.value = ''; });

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

        // Download buttons
        fileList.querySelectorAll('.btn-download').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                downloadFile(btn.dataset.path);
            });
        });

        // Delete buttons (files)
        fileList.querySelectorAll('.btn-delete').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                deleteFile(btn.dataset.path, btn.dataset.name);
            });
        });

        // Rename buttons
        fileList.querySelectorAll('.btn-rename').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                renameItem(btn.dataset.path, btn.dataset.name);
            });
        });

        // Download directory buttons
        fileList.querySelectorAll('.btn-download-dir').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                downloadDir(btn.dataset.path);
            });
        });

        // Delete directory buttons
        fileList.querySelectorAll('.btn-delete-dir').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                deleteDirectory(btn.dataset.path, btn.dataset.name);
            });
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

    let actions = '';
    if (item.is_dir) {
        actions = `<span class="file-actions">
            <button class="btn-action btn-rename" title="Rename" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998;</button>
            <button class="btn-action btn-download-dir" title="Download as zip" data-path="${escapeHtml(item.path)}">&#11015;</button>
            <button class="btn-action btn-delete-dir" title="Delete directory" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10005;</button>
        </span>`;
    } else {
        actions = `<span class="file-actions">
            <button class="btn-action btn-rename" title="Rename" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998;</button>
            <button class="btn-action btn-download" title="Download" data-path="${escapeHtml(item.path)}">&#11015;</button>
            <button class="btn-action btn-delete" title="Delete" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10005;</button>
        </span>`;
    }

    return `
        <div class="file-item" data-path="${escapeHtml(item.path)}" data-is-dir="${item.is_dir}">
            ${icon}
            <span class="name">${escapeHtml(item.name)}</span>
            ${badges}
            ${actions}
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

function downloadFile(path) {
    const a = document.createElement('a');
    a.href = `api/file/download?path=${encodeURIComponent(path)}`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

function downloadDir(path) {
    const a = document.createElement('a');
    a.href = `api/dir/download?path=${encodeURIComponent(path)}`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

async function deleteFile(path, name) {
    if (!window.confirm(`Delete "${name}"?\n\nThis action cannot be undone.`)) {
        return;
    }

    try {
        const response = await fetch('api/file/delete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': CSRF_TOKEN,
            },
            body: JSON.stringify({ path }),
        });

        const data = await response.json();
        if (!response.ok) {
            window.alert(data.error || 'Failed to delete file');
            return;
        }

        loadDirectory(currentPath);
    } catch (err) {
        window.alert(`Error: ${err.message}`);
    }
}

async function renameItem(path, oldName) {
    const newName = window.prompt('Rename to:', oldName);
    if (newName === null) return;

    const trimmed = newName.trim();
    if (!trimmed || trimmed === oldName) return;

    try {
        const response = await fetch('api/rename', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': CSRF_TOKEN,
            },
            body: JSON.stringify({ path, new_name: trimmed }),
        });

        const data = await response.json();
        if (!response.ok) {
            window.alert(data.error || 'Failed to rename');
            return;
        }

        loadDirectory(currentPath);
    } catch (err) {
        window.alert(`Error: ${err.message}`);
    }
}

async function deleteDirectory(path, name) {
    // First fetch a preview of what's inside
    try {
        const response = await fetch(`api/dir/preview?path=${encodeURIComponent(path)}`);
        const data = await response.json();

        if (!response.ok) {
            window.alert(data.error || 'Failed to preview directory');
            return;
        }

        // Build the preview modal
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';

        const allEntries = [...data.dirs, ...data.files];
        const maxShow = 50;
        const truncated = allEntries.length > maxShow;
        const shown = allEntries.slice(0, maxShow);

        let listHtml = shown.map(e => `<div class="preview-entry">${escapeHtml(e)}</div>`).join('');
        if (truncated) {
            listHtml += `<div class="preview-entry preview-more">... and ${allEntries.length - maxShow} more</div>`;
        }
        if (allEntries.length === 0) {
            listHtml = '<div class="preview-entry preview-more">Directory is empty</div>';
        }

        // Safe = nested inside a top-level home dir (4+ segments), or anywhere in /tmp
        // e.g. /home/user/repo/sub is safe, but /home/user/repo is not
        const parts = path.split('/').filter(p => p);
        const isSafePath = (path.startsWith('/home/') && parts.length > 3) ||
                           (path.startsWith('/tmp/') && path !== '/tmp');
        const needsConfirmPath = !isSafePath;

        let confirmHtml = '';
        if (needsConfirmPath) {
            confirmHtml = `
                <div class="confirm-path" style="display:none">
                    <div class="modal-summary" style="margin-top:0.75rem">This is a top-level or system directory. Type the full path to confirm:</div>
                    <input type="text" class="confirm-path-input" autocomplete="off">
                    <div class="confirm-path-error" style="display:none">Path does not match.</div>
                </div>
            `;
        }

        overlay.innerHTML = `
            <div class="modal-dialog">
                <div class="modal-title">Delete "${escapeHtml(name)}"?</div>
                <div class="modal-summary">${data.total_files} file${data.total_files !== 1 ? 's' : ''} and ${data.total_dirs} subdirector${data.total_dirs !== 1 ? 'ies' : 'y'} will be permanently deleted.</div>
                <div class="preview-list">${listHtml}</div>
                ${confirmHtml}
                <div class="modal-buttons">
                    <button class="btn-modal btn-modal-cancel">Cancel</button>
                    <button class="btn-modal btn-modal-delete">Delete</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        // Set placeholder safely via DOM to avoid HTML attribute escaping issues
        const confirmInput = overlay.querySelector('.confirm-path-input');
        if (confirmInput) confirmInput.placeholder = path;

        // Wait for user choice
        const result = await new Promise(resolve => {
            const btnDelete = overlay.querySelector('.btn-modal-delete');
            const confirmSection = overlay.querySelector('.confirm-path');

            overlay.querySelector('.btn-modal-cancel').addEventListener('click', () => resolve(false));
            overlay.addEventListener('click', (e) => { if (e.target === overlay) resolve(false); });

            if (needsConfirmPath) {
                const input = overlay.querySelector('.confirm-path-input');
                const error = overlay.querySelector('.confirm-path-error');

                btnDelete.addEventListener('click', () => {
                    if (confirmSection.style.display === 'none') {
                        // First click: expand the confirmation section
                        confirmSection.style.display = 'block';
                        input.focus();
                        return;
                    }
                    // Second click: validate the typed path
                    if (input.value.trim() === path) {
                        resolve(true);
                    } else {
                        error.style.display = 'block';
                    }
                });

                input.addEventListener('keydown', (e) => {
                    if (e.key === 'Enter') btnDelete.click();
                });
                input.addEventListener('input', () => { error.style.display = 'none'; });
            } else {
                btnDelete.addEventListener('click', () => resolve(true));
            }
        });

        overlay.remove();

        if (!result) return;

        // Extra confirmation for directories outside /home and /tmp
        // Proceed with deletion
        const delResponse = await fetch('api/dir/delete', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': CSRF_TOKEN,
            },
            body: JSON.stringify({ path }),
        });

        const delData = await delResponse.json();
        if (!delResponse.ok) {
            window.alert(delData.error || 'Failed to delete directory');
            return;
        }

        loadDirectory(currentPath);
    } catch (err) {
        window.alert(`Error: ${err.message}`);
    }
}

async function createNewDir() {
    const name = window.prompt('New directory name:');
    if (name === null) return;

    const trimmed = name.trim();
    if (!trimmed) return;

    try {
        const response = await fetch('api/dir/create', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': CSRF_TOKEN,
            },
            body: JSON.stringify({
                directory: currentPath,
                name: trimmed,
            }),
        });

        const data = await response.json();
        if (!response.ok) {
            window.alert(data.error || 'Failed to create directory');
            return;
        }

        loadDirectory(currentPath);
    } catch (err) {
        window.alert(`Error: ${err.message}`);
    }
}

function uploadFiles(files) {
    if (!files || files.length === 0) return;

    const fileNames = Array.from(files).map(f => f.name);

    const formData = new FormData();
    formData.append('directory', currentPath);
    for (const file of files) {
        formData.append('files', file);
    }

    // Build upload modal
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-dialog">
            <div class="modal-title">Uploading ${files.length} file${files.length !== 1 ? 's' : ''}</div>
            <div class="upload-file-list">${fileNames.map(n => `<div class="preview-entry">${escapeHtml(n)}</div>`).join('')}</div>
            <div class="upload-progress-wrap">
                <div class="upload-progress-bar"></div>
            </div>
            <div class="upload-status">0%</div>
            <div class="modal-buttons">
                <button class="btn-modal btn-modal-cancel upload-cancel-btn">Cancel</button>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    const bar = overlay.querySelector('.upload-progress-bar');
    const status = overlay.querySelector('.upload-status');
    const cancelBtn = overlay.querySelector('.upload-cancel-btn');

    const xhr = new XMLHttpRequest();

    cancelBtn.addEventListener('click', () => {
        xhr.abort();
        overlay.remove();
    });

    xhr.upload.addEventListener('progress', (e) => {
        if (e.lengthComputable) {
            const pct = Math.round((e.loaded / e.total) * 100);
            bar.style.width = pct + '%';
            status.textContent = pct < 100 ? `${pct}%` : 'Processing...';
        }
    });

    xhr.addEventListener('load', () => {
        overlay.remove();
        if (xhr.status === 413) {
            window.alert('File too large to upload through the browser. Use SFTP instead.');
            return;
        }
        try {
            const data = JSON.parse(xhr.responseText);
            if (xhr.status >= 400) {
                window.alert(data.error || 'Failed to upload');
            } else if (data.skipped && data.skipped.length > 0) {
                window.alert(data.message);
            }
        } catch {
            if (xhr.status >= 400) window.alert('Upload failed');
        }
        loadDirectory(currentPath);
    });

    xhr.addEventListener('error', () => {
        overlay.remove();
        window.alert('Upload failed — network error');
    });

    xhr.addEventListener('abort', () => {
        // already handled by cancel button
    });

    xhr.open('POST', 'api/file/upload');
    xhr.setRequestHeader('X-CSRF-Token', CSRF_TOKEN);
    xhr.send(formData);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

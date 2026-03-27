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
    document.addEventListener('click', () => {
        dropdown.classList.remove('open');
        document.querySelectorAll('.actions-dropdown.open').forEach(d => d.classList.remove('open'));
    });
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

        // Copy file buttons
        fileList.querySelectorAll('.btn-copy-file').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                copyFile(btn.dataset.path, btn.dataset.name);
            });
        });

        // Copy directory buttons
        fileList.querySelectorAll('.btn-copy-dir').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                copyDir(btn.dataset.path, btn.dataset.name);
            });
        });

        // Close mobile dropdown when any action inside it is tapped
        fileList.querySelectorAll('.actions-dropdown').forEach(dd => {
            dd.addEventListener('click', (e) => e.stopPropagation());
        });
        fileList.querySelectorAll('.actions-dropdown .dropdown-item').forEach(item => {
            item.addEventListener('click', () => item.closest('.actions-dropdown')?.classList.remove('open'));
        });

        // Mobile actions dropdown toggle
        fileList.querySelectorAll('.btn-actions-toggle').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const dropdown = btn.nextElementSibling;
                // Close any other open dropdowns first
                fileList.querySelectorAll('.actions-dropdown.open').forEach(d => {
                    if (d !== dropdown) d.classList.remove('open');
                });
                // Flip upward if button is near the bottom of the viewport
                const rect = btn.getBoundingClientRect();
                dropdown.classList.toggle('flip-up', rect.bottom + 160 > window.innerHeight);
                dropdown.classList.toggle('open');
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

    let actionButtons, dropdownItems;
    if (item.is_dir) {
        actionButtons = `
            <button class="btn-action btn-copy-dir" title="Copy" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10697;</button>
            <button class="btn-action btn-rename" title="Rename" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998;</button>
            <button class="btn-action btn-download-dir" title="Download as zip" data-path="${escapeHtml(item.path)}">&#11015;</button>
            <button class="btn-action btn-delete-dir" title="Delete directory" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10005;</button>`;
        dropdownItems = `
            <button class="dropdown-item btn-copy-dir" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10697; Copy</button>
            <button class="dropdown-item btn-rename" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998; Rename</button>
            <button class="dropdown-item btn-download-dir" data-path="${escapeHtml(item.path)}">&#11015; Download</button>
            <button class="dropdown-item btn-delete-dir" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10005; Delete</button>`;
    } else {
        actionButtons = `
            <button class="btn-action btn-copy-file" title="Copy" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10697;</button>
            <button class="btn-action btn-rename" title="Rename" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998;</button>
            <button class="btn-action btn-download" title="Download" data-path="${escapeHtml(item.path)}">&#11015;</button>
            <button class="btn-action btn-delete" title="Delete" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10005;</button>`;
        dropdownItems = `
            <button class="dropdown-item btn-copy-file" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10697; Copy</button>
            <button class="dropdown-item btn-rename" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998; Rename</button>
            <button class="dropdown-item btn-download" data-path="${escapeHtml(item.path)}">&#11015; Download</button>
            <button class="dropdown-item btn-delete" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10005; Delete</button>`;
    }

    const actions = `<span class="file-actions">
        ${actionButtons}
        <span class="actions-dropdown-wrap">
            <button class="btn-action btn-actions-toggle">&#8943;</button>
            <div class="actions-dropdown">${dropdownItems}</div>
        </span>
    </span>`;

    const symlinkTarget = item.is_symlink
        ? `<span class="symlink-target"> → ${escapeHtml(item.symlink_target)}</span>`
        : '';

    return `
        <div class="file-item" data-path="${escapeHtml(item.path)}" data-is-dir="${item.is_dir}">
            ${icon}
            <span class="name">${escapeHtml(item.name)}</span>${symlinkTarget}
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

function renameItem(path, name) {
    const isDir = path.endsWith('/') || document.querySelector(`.file-item[data-path="${CSS.escape(path)}"]`)?.dataset.isDir === 'true';
    showFileModal({ path, name, isDir, mode: 'move' });
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

function copyDir(path, name) {
    showFileModal({ path, name, isDir: true, mode: 'copy' });
}

function copyFile(path, name) {
    showFileModal({ path, name, isDir: false, mode: 'copy' });
}

function showFileModal({ path, name, isDir, mode }) {
    const isCopy = mode === 'copy';
    const verb = isCopy ? 'Copy' : 'Rename / Move';
    const verbLower = isCopy ? 'copy' : 'move';
    const verbIng = isCopy ? 'Copying' : 'Renaming';

    let endpoint;
    if (isCopy) {
        endpoint = isDir ? 'api/dir/copy' : 'api/file/copy';
    } else {
        endpoint = 'api/rename';
    }

    const dotIdx = name.lastIndexOf('.');
    const defaultName = isCopy
        ? ((!isDir && dotIdx > 0) ? `${name.slice(0, dotIdx)} (2)${name.slice(dotIdx)}` : `${name} (2)`)
        : name;

    const ACTIVATABLE_DIRS = {
        '/etc/systemd/system': 'Enable & start service',
        '/etc/nginx/sites-available': 'Enable site & reload nginx',
    };

    const shortcuts = [
        { label: 'Executables', dir: '/usr/local/bin', filename: name.replace(/\.[^.]+$/, '') },
        { label: 'systemd', dir: '/etc/systemd/system', filename: name.endsWith('.service') ? name : `${name}.service` },
        { label: 'nginx', dir: '/etc/nginx/sites-available', filename: name },
    ];

    const symlinkOption = isCopy
        ? '<label class="copy-option"><input type="checkbox" class="chk-symlink"> Symlink instead of copy</label>'
        : '';

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-dialog">
            <div class="modal-title copy-modal-title"><span class="modal-verb">${verb}</span> ${isDir ? 'directory' : 'file'}</div>
            <div class="modal-summary"><span class="modal-verb-ing">${verbIng}</span> "${escapeHtml(name)}"</div>
            ${isDir ? '' : `<div class="copy-shortcuts">
                ${shortcuts.map((s, i) => `<button class="btn-shortcut" data-idx="${i}">${escapeHtml(s.label)}</button>`).join('')}
            </div>`}
            <label class="copy-label">Destination</label>
            <input type="text" class="copy-input copy-dir-input" autocomplete="off">
            <label class="copy-label">Name</label>
            <input type="text" class="copy-input copy-name-input" autocomplete="off">
            <div class="copy-options">
                ${symlinkOption}
                <label class="copy-option"><input type="checkbox" class="chk-overwrite"> Replace if already exists</label>
                <label class="copy-option"><input type="checkbox" class="chk-create-dirs"> Create directory if it doesn't exist</label>
                <label class="copy-option copy-option-activate" style="display:none"><input type="checkbox" class="chk-activate"> <span class="activate-label"></span></label>
            </div>
            <div class="copy-error" style="display:none"></div>
            <div class="modal-buttons">
                <button class="btn-modal btn-modal-cancel">Cancel</button>
                <button class="btn-modal btn-modal-copy">${verb}</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);

    const dirInput = overlay.querySelector('.copy-dir-input');
    const nameInput = overlay.querySelector('.copy-name-input');
    const errorEl = overlay.querySelector('.copy-error');
    const actionBtn = overlay.querySelector('.btn-modal-copy');
    const chkSymlink = overlay.querySelector('.chk-symlink');
    const chkOverwrite = overlay.querySelector('.chk-overwrite');
    const chkCreateDirs = overlay.querySelector('.chk-create-dirs');
    const chkActivate = overlay.querySelector('.chk-activate');
    const activateRow = overlay.querySelector('.copy-option-activate');
    const activateLabel = overlay.querySelector('.activate-label');

    dirInput.value = currentPath;
    nameInput.value = defaultName;

    function updateActivateVisibility() {
        const dir = dirInput.value.trim().replace(/\/+$/, '');
        if (!isDir && ACTIVATABLE_DIRS[dir]) {
            activateLabel.textContent = ACTIVATABLE_DIRS[dir];
            activateRow.style.display = '';
        } else {
            activateRow.style.display = 'none';
            chkActivate.checked = false;
        }
    }

    const verbSpans = overlay.querySelectorAll('.modal-verb');
    const verbIngSpans = overlay.querySelectorAll('.modal-verb-ing');

    function updateActionLabel() {
        if (chkSymlink && chkSymlink.checked) {
            actionBtn.textContent = 'Symlink';
            verbSpans.forEach(s => s.textContent = 'Symlink');
            verbIngSpans.forEach(s => s.textContent = 'Symlinking');
        } else if (!isCopy) {
            const isMove = dirInput.value.trim().replace(/\/+$/, '') !== currentPath.replace(/\/+$/, '');
            const v = isMove ? 'Move' : 'Rename';
            const vIng = isMove ? 'Moving' : 'Renaming';
            actionBtn.textContent = v;
            verbSpans.forEach(s => s.textContent = v);
            verbIngSpans.forEach(s => s.textContent = vIng);
        } else {
            actionBtn.textContent = verb;
            verbSpans.forEach(s => s.textContent = verb);
            verbIngSpans.forEach(s => s.textContent = verbIng);
        }
    }

    // Shortcut buttons
    overlay.querySelectorAll('.btn-shortcut').forEach(btn => {
        btn.addEventListener('click', () => {
            const s = shortcuts[btn.dataset.idx];
            dirInput.value = s.dir;
            nameInput.value = s.filename;
            updateActivateVisibility();
            updateActionLabel();
            nameInput.focus();
        });
    });

    dirInput.addEventListener('input', () => {
        errorEl.style.display = 'none';
        updateActivateVisibility();
        updateActionLabel();
    });

    if (chkSymlink) chkSymlink.addEventListener('change', updateActionLabel);

    function showError(msg) {
        errorEl.textContent = msg;
        errorEl.style.display = 'block';
    }

    let submitting = false;

    function close() {
        if (!submitting) overlay.remove();
    }

    async function submit() {
        if (submitting) return;
        const trimmedName = nameInput.value.trim();
        const trimmedDir = dirInput.value.trim();
        errorEl.style.display = 'none';

        if (!trimmedName) { showError('Name cannot be empty.'); return; }
        if (!trimmedDir) { showError('Destination cannot be empty.'); return; }
        const normDir = trimmedDir.replace(/\/+/g, '/').replace(/\/\.$/, '').replace(/\/+$/, '') || '/';
        if (normDir === (currentPath.replace(/\/+$/, '') || '/') && trimmedName === name) {
            showError(isCopy
                ? `Cannot ${verbLower} ${isDir ? 'a directory' : 'a file'} onto itself.`
                : 'Nothing to change.');
            return;
        }

        const isSymlink = chkSymlink && chkSymlink.checked;
        submitting = true;
        actionBtn.disabled = true;
        const currentVerbIng = verbIngSpans[0]?.textContent || verbIng;
        actionBtn.textContent = isSymlink ? 'Linking...' : `${currentVerbIng}...`;

        try {
            const body = {
                path,
                new_name: trimmedName,
                directory: trimmedDir,
                overwrite: chkOverwrite.checked,
                create_dirs: chkCreateDirs.checked,
                activate: chkActivate.checked,
            };
            if (isCopy) body.symlink = isSymlink;

            const response = await fetch(endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': CSRF_TOKEN,
                },
                body: JSON.stringify(body),
            });

            const data = await response.json();
            if (!response.ok) {
                showError(data.error || `${verb} failed.`);
                submitting = false;
                actionBtn.disabled = false;
                updateActionLabel();
                return;
            }

            submitting = false;
            close();
            if (data.activate_message) {
                window.alert(data.activate_message);
            }
            loadDirectory(currentPath);
        } catch (err) {
            showError(`Error: ${err.message}`);
            submitting = false;
            actionBtn.disabled = false;
            updateActionLabel();
        }
    }

    overlay.querySelector('.btn-modal-cancel').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
    actionBtn.addEventListener('click', submit);
    nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    dirInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); nameInput.focus(); } });
    nameInput.addEventListener('input', () => { errorEl.style.display = 'none'; });

    updateActivateVisibility();
    updateActionLabel();
    setTimeout(() => nameInput.focus(), 50);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

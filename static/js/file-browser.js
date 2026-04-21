/**
 * File browser component for the diff editor
 */

let currentPath = '';
let selectedPaths = new Set();
let lastClickedIndex = -1;
let selectMode = false;
let recycleBinRoot = '/var/tmp/RECYCLE_BIN'; // must match RECYCLE_BIN in file_ops.py
let currentInRecycleBin = false;
let currentAtRecycleBinRoot = false;

const ACTIVATABLE_DIRS = {
    '/etc/systemd/system': 'Enable & start service',
    '/etc/nginx/sites-available': 'Enable site & reload nginx',
};

const BATCH_SHORTCUTS = [
    { label: 'Executables', dir: '/usr/local/bin' },
    { label: 'systemd', dir: '/etc/systemd/system' },
    { label: 'nginx', dir: '/etc/nginx/sites-available' },
];

const COMPILABLE_SUFFIXES = new Map([
    ['.c', 'c'],
    ['.cc', 'cpp'],
    ['.cpp', 'cpp'],
    ['.cxx', 'cpp'],
    ['.c++', 'cpp'],
    ['.go', 'go'],
    ['.java', 'java'],
    ['.rs', 'rust'],
    ['.cs', 'csharp'],
]);

function normalizeDirectoryPath(path) {
    return path.trim().replace(/\/+/g, '/').replace(/\/\.$/, '').replace(/\/+$/, '') || '/';
}

const BACKDROP_CLOSE_SAFETY_ZONE_PX = 16;

function getBackdropDialog(overlay) {
    return overlay.querySelector('.modal-dialog, [role="dialog"]') || overlay.firstElementChild;
}

function isEventInSafetyZone(event, dialog, safetyZonePx) {
    if (!dialog || typeof event.clientX !== 'number' || typeof event.clientY !== 'number') {
        return false;
    }

    const rect = dialog.getBoundingClientRect();
    return event.clientX >= rect.left - safetyZonePx
        && event.clientX <= rect.right + safetyZonePx
        && event.clientY >= rect.top - safetyZonePx
        && event.clientY <= rect.bottom + safetyZonePx;
}

function bindBackdropClose(overlay, callback, { safetyZonePx = BACKDROP_CLOSE_SAFETY_ZONE_PX } = {}) {
    let downOnBackdrop = false;
    let downInSafetyZone = false;

    overlay.addEventListener('mousedown', (e) => {
        const dialog = getBackdropDialog(overlay);
        downInSafetyZone = isEventInSafetyZone(e, dialog, safetyZonePx);
        downOnBackdrop = e.target === overlay && !downInSafetyZone;
    });

    overlay.addEventListener('click', (e) => {
        const dialog = getBackdropDialog(overlay);
        const clickInSafetyZone = isEventInSafetyZone(e, dialog, safetyZonePx);
        if (e.target === overlay && downOnBackdrop && !downInSafetyZone && !clickInSafetyZone) {
            callback();
        }
        downOnBackdrop = false;
        downInSafetyZone = false;
    });
}

function getParentDirectory(path) {
    const normalized = normalizeDirectoryPath(String(path || ''));
    if (normalized === '/') {
        return '/';
    }
    const parts = normalized.split('/').filter(Boolean);
    return parts.length > 1 ? `/${parts.slice(0, -1).join('/')}` : '/';
}

function isPathInRecycleBin(path) {
    const normalized = normalizeDirectoryPath(String(path || ''));
    return normalized === recycleBinRoot || normalized.startsWith(`${recycleBinRoot}/`);
}

function updateRecycleBinButton() {
    const button = document.getElementById('btn-empty-recycle-bin');
    if (!button) {
        return;
    }
    button.classList.toggle('hidden', !currentAtRecycleBinRoot);
}

function setRecycleBinState(data = {}) {
    recycleBinRoot = normalizeDirectoryPath(data.recycle_bin_root || recycleBinRoot);
    currentInRecycleBin = Boolean(data.is_in_recycle_bin);
    currentAtRecycleBinRoot = Boolean(data.is_recycle_bin_root);
    updateRecycleBinButton();
}

function getDeleteIntent(path) {
    const permanent = isPathInRecycleBin(path);
    return {
        permanent,
        buttonLabel: permanent ? 'Delete Permanently' : 'Delete',
        filePrompt(name) {
            return permanent
                ? `Permanently delete "${name}"?\n\nThis cannot be undone.`
                : `Delete "${name}"?\n\nThis will move it to the recycle bin.`;
        },
        directorySummary(totalFiles, totalDirs) {
            return permanent
                ? `${totalFiles} file${totalFiles !== 1 ? 's' : ''} and ${totalDirs} subdirector${totalDirs !== 1 ? 'ies' : 'y'} will be permanently deleted.`
                : `${totalFiles} file${totalFiles !== 1 ? 's' : ''} and ${totalDirs} subdirector${totalDirs !== 1 ? 'ies' : 'y'} will be moved to the recycle bin.`;
        },
        batchSummary(summaryParts) {
            return summaryParts.join(', ') + (permanent
                ? ' will be permanently deleted.'
                : ' will be moved to the recycle bin.');
        },
    };
}

function getFileShortcuts(name) {
    return [
        { label: 'Executables', dir: '/usr/local/bin', filename: name.replace(/\.[^.]+$/, '') },
        { label: 'systemd', dir: '/etc/systemd/system', filename: name.endsWith('.service') ? name : `${name}.service` },
        { label: 'nginx', dir: '/etc/nginx/sites-available', filename: name },
    ];
}

function getActivationLabel(dir, { allowActivation = true } = {}) {
    if (!allowActivation) {
        return '';
    }
    return ACTIVATABLE_DIRS[normalizeDirectoryPath(dir)] || '';
}

function getSelectionItems(paths) {
    return paths.map((path) => {
        const itemEl = document.querySelector(`.file-item[data-path="${CSS.escape(path)}"]`);
        return {
            path,
            name: itemEl?.dataset.name || path.split('/').pop(),
            isDir: itemEl?.dataset.isDir === 'true',
        };
    });
}

function renderShortcutButtons(shortcuts) {
    if (!shortcuts.length) {
        return '';
    }

    return `<div class="copy-shortcuts">
        ${shortcuts.map((s, i) => `<button class="btn-shortcut" data-idx="${i}">${escapeHtml(s.label)}</button>`).join('')}
    </div>`;
}

function getSymlinkOptionHtml(isCopy) {
    if (!isCopy) {
        return '';
    }

    return '<label class="copy-option"><input type="checkbox" class="chk-symlink"> Symlink instead of copy</label>';
}

function createCopyModal({ titleHtml, summaryHtml = '', shortcuts = [], fieldsHtml, symlinkOptionHtml = '', actionLabel }) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-dialog">
            <div class="modal-title copy-modal-title">${titleHtml}</div>
            ${summaryHtml ? `<div class="modal-summary">${summaryHtml}</div>` : ''}
            ${renderShortcutButtons(shortcuts)}
            ${fieldsHtml}
            <div class="copy-options">
                ${symlinkOptionHtml}
                <label class="copy-option"><input type="checkbox" class="chk-overwrite"> Replace if already exists</label>
                <label class="copy-option"><input type="checkbox" class="chk-create-dirs"> Create directory if it doesn't exist</label>
                <label class="copy-option copy-option-activate" style="display:none"><input type="checkbox" class="chk-activate"> <span class="activate-label"></span></label>
            </div>
            <div class="copy-error" style="display:none"></div>
            <div class="modal-buttons">
                <button class="btn-modal btn-modal-cancel">Cancel</button>
                <button class="btn-modal btn-modal-copy">${actionLabel}</button>
            </div>
        </div>
    `;

    document.body.appendChild(overlay);
    return overlay;
}

function getCopyModalControls(overlay) {
    return {
        dirInput: overlay.querySelector('.copy-dir-input'),
        errorEl: overlay.querySelector('.copy-error'),
        actionBtn: overlay.querySelector('.btn-modal-copy'),
        chkSymlink: overlay.querySelector('.chk-symlink'),
        chkOverwrite: overlay.querySelector('.chk-overwrite'),
        chkCreateDirs: overlay.querySelector('.chk-create-dirs'),
        chkActivate: overlay.querySelector('.chk-activate'),
        activateRow: overlay.querySelector('.copy-option-activate'),
        activateLabel: overlay.querySelector('.activate-label'),
    };
}

function createActivationVisibilityUpdater({ dirInput, activateRow, activateLabel, chkActivate, allowActivation }) {
    return function updateActivateVisibility() {
        const activationLabel = getActivationLabel(dirInput.value, { allowActivation });
        if (activationLabel) {
            activateLabel.textContent = activationLabel;
            activateRow.style.display = '';
        } else {
            activateRow.style.display = 'none';
            chkActivate.checked = false;
        }
    };
}

function hideModalError(errorEl) {
    errorEl.style.display = 'none';
}

function showModalError(errorEl, content) {
    if (Array.isArray(content)) {
        errorEl.innerHTML = content.map(escapeHtml).join('<br>');
    } else {
        errorEl.textContent = content;
    }
    errorEl.style.display = 'block';
}

function bindShortcutButtons(overlay, shortcuts, onSelect) {
    overlay.querySelectorAll('.btn-shortcut').forEach(btn => {
        btn.addEventListener('click', () => onSelect(shortcuts[btn.dataset.idx]));
    });
}

function getCompilableLanguageHint(name) {
    const lowerName = String(name || '').toLowerCase();
    for (const [suffix, language] of COMPILABLE_SUFFIXES.entries()) {
        if (lowerName.endsWith(suffix)) {
            return language;
        }
    }
    return null;
}

function isCompilableFileName(name) {
    return Boolean(getCompilableLanguageHint(name));
}

function buildOperationRequest({ mode, path, name, isDir, directory, overwrite, createDirs, activate, isSymlink }) {
    const isCopy = mode === 'copy';
    const body = {
        path,
        new_name: name,
        directory,
        overwrite,
        create_dirs: createDirs,
        activate,
    };

    if (isCopy) {
        body.symlink = isSymlink;
    }

    return {
        endpoint: isCopy ? (isDir ? 'api/dir/copy' : 'api/file/copy') : 'api/rename',
        body,
    };
}

async function postJson(endpoint, body) {
    const response = await fetch(endpoint, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-CSRF-Token': CSRF_TOKEN,
        },
        body: JSON.stringify(body),
    });

    return {
        response,
        data: await response.json(),
    };
}

async function fetchCompileInfo(path) {
    const response = await fetch(`api/file/compile-info?path=${encodeURIComponent(path)}`);
    const data = await response.json();

    if (!response.ok) {
        throw new Error(data.error || 'Failed to load compile options');
    }

    return data;
}

function initFileBrowser(defaultRoot) {
    currentPath = defaultRoot;

    document.getElementById('btn-up').addEventListener('click', goUp);
    document.getElementById('show-hidden').addEventListener('change', () => loadDirectory(currentPath));
    document.getElementById('btn-empty-recycle-bin')?.addEventListener('click', emptyRecycleBin);

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

    clearSelection();
    exitSelectMode();
    fileList.innerHTML = '<div class="loading">Loading...</div>';

    try {
        const response = await fetch(`api/browse?path=${encodeURIComponent(path)}&hidden=${showHidden}`);
        const data = await response.json();

        if (!response.ok) {
            setRecycleBinState();
            fileList.innerHTML = `<div class="loading" style="color: var(--error)">${data.error || 'Failed to load directory'}</div>`;
            return;
        }

        setRecycleBinState(data);
        currentPath = data.path;
        pathInput.value = data.path;

        if (data.items.length === 0) {
            fileList.innerHTML = '<div class="loading">Empty directory</div>';
            return;
        }

        fileList.innerHTML = data.items.map(item => createFileItem(item)).join('');

        // Click handlers with selection support
        const allItems = [...fileList.querySelectorAll('.file-item')];
        selectedPaths.clear();
        lastClickedIndex = -1;

        allItems.forEach((el, idx) => {
            // Click handler
            el.addEventListener('click', (e) => {
                if (e.target.closest('.file-actions')) return;

                if (selectMode) {
                    e.preventDefault();
                    if (e.shiftKey && lastClickedIndex >= 0) {
                        // Range selection in select mode
                        const from = Math.min(lastClickedIndex, idx);
                        const to = Math.max(lastClickedIndex, idx);
                        selectedPaths.clear();
                        allItems.forEach(item => item.classList.remove('selected'));
                        for (let i = from; i <= to; i++) {
                            allItems[i].classList.add('selected');
                            selectedPaths.add(allItems[i].dataset.path);
                        }
                    } else {
                        toggleSelect(el);
                        lastClickedIndex = idx;
                    }
                    updateSelectBar();
                    if (selectedPaths.size === 0) exitSelectMode();
                } else if (e.ctrlKey || e.metaKey) {
                    e.preventDefault();
                    if (!selectMode) enterSelectMode();
                    toggleSelect(el);
                    lastClickedIndex = idx;
                    updateSelectBar();
                } else if (selectedPaths.size > 0) {
                    clearSelection();
                    exitSelectMode();
                    handleItemClick(el.dataset.path, el.dataset.isDir === 'true');
                } else {
                    handleItemClick(el.dataset.path, el.dataset.isDir === 'true');
                }
            });

            // Long-press to enter select mode (mobile)
            let longPressTimer = null;

            el.addEventListener('touchstart', (e) => {
                if (e.target.closest('.file-actions')) return;
                longPressTimer = setTimeout(() => {
                    longPressTimer = null;
                    if (!selectMode) enterSelectMode();
                    toggleSelect(el);
                    lastClickedIndex = idx;
                    updateSelectBar();
                    // Prevent the subsequent click/tap
                    el.dataset.longPressed = 'true';
                    // Haptic feedback if available
                    if (navigator.vibrate) navigator.vibrate(30);
                }, 500);
            }, { passive: true });

            el.addEventListener('touchmove', () => {
                if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
            }, { passive: true });

            el.addEventListener('touchend', () => {
                if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
            });

            el.addEventListener('click', (e) => {
                // Suppress the click that follows a long-press
                if (el.dataset.longPressed === 'true') {
                    e.preventDefault();
                    e.stopImmediatePropagation();
                    delete el.dataset.longPressed;
                }
            }, true); // capture phase to fire before the main click handler
        });

        // Drag and drop
        setupDragAndDrop(allItems);

        // Download buttons
        fileList.querySelectorAll('.btn-download').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (selectMode && selectedPaths.has(btn.dataset.path)) {
                    batchDownload();
                } else {
                    downloadFile(btn.dataset.path);
                }
            });
        });

        // Delete buttons (files)
        fileList.querySelectorAll('.btn-delete').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (selectMode && selectedPaths.has(btn.dataset.path)) {
                    batchDelete();
                } else {
                    deleteFile(btn.dataset.path, btn.dataset.name);
                }
            });
        });

        // Rename buttons
        fileList.querySelectorAll('.btn-rename').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (selectMode && selectedPaths.has(btn.dataset.path)) {
                    showBatchFileModal({ paths: [...selectedPaths], mode: 'move' });
                } else {
                    renameItem(btn.dataset.path, btn.dataset.name);
                }
            });
        });

        // Download directory buttons
        fileList.querySelectorAll('.btn-download-dir').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (selectMode && selectedPaths.has(btn.dataset.path)) {
                    batchDownload();
                } else {
                    downloadDir(btn.dataset.path);
                }
            });
        });

        // Delete directory buttons
        fileList.querySelectorAll('.btn-delete-dir').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (selectMode && selectedPaths.has(btn.dataset.path)) {
                    batchDelete();
                } else {
                    deleteDirectory(btn.dataset.path, btn.dataset.name);
                }
            });
        });

        // Copy file buttons
        fileList.querySelectorAll('.btn-copy-file').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (selectMode && selectedPaths.has(btn.dataset.path)) {
                    showBatchFileModal({ paths: [...selectedPaths], mode: 'copy' });
                } else {
                    copyFile(btn.dataset.path, btn.dataset.name);
                }
            });
        });

        // Copy directory buttons
        fileList.querySelectorAll('.btn-copy-dir').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (selectMode && selectedPaths.has(btn.dataset.path)) {
                    showBatchFileModal({ paths: [...selectedPaths], mode: 'copy' });
                } else {
                    copyDir(btn.dataset.path, btn.dataset.name);
                }
            });
        });

        fileList.querySelectorAll('.btn-compile').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                await compileFile(btn.dataset.path, btn.dataset.name);
            });
        });

        fileList.querySelectorAll('.btn-info').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                showInfoModal(btn.dataset.path);
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
        setRecycleBinState();
        fileList.innerHTML = `<div class="loading" style="color: var(--error)">Error: ${err.message}</div>`;
    }
}

async function emptyRecycleBin() {
    if (!currentAtRecycleBinRoot) {
        return;
    }

    if (!window.confirm('Permanently empty the recycle bin?\n\nThis cannot be undone.')) {
        return;
    }

    try {
        const response = await fetch('api/recycle-bin/empty', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRF-Token': CSRF_TOKEN,
            },
            body: JSON.stringify({}),
        });
        const data = await response.json();
        if (!response.ok) {
            const details = Array.isArray(data.details) && data.details.length > 0
                ? `\n\n${data.details.join('\n')}`
                : '';
            window.alert((data.error || 'Failed to empty the recycle bin.') + details);
            return;
        }

        if (data.message) {
            window.alert(data.message);
        }
        loadDirectory(currentPath);
    } catch (err) {
        window.alert(`Error: ${err.message}`);
    }
}

function createFileItem(item) {
    const icon = item.is_dir ? '<span class="icon folder">&#128193;</span>' : '<span class="icon">&#128196;</span>';
    const renameLabel = 'Rename/move';
    const isRecycleBinRootItem = item.is_dir && normalizeDirectoryPath(item.path) === recycleBinRoot;
    const compileItem = !item.is_dir && isCompilableFileName(item.name)
        ? `<button class="dropdown-item btn-compile" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9881; Compile</button>`
        : '';

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
        const deleteButton = isRecycleBinRootItem
            ? ''
            : `<button class="btn-action btn-delete-dir" title="Delete directory" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10005;</button>`;
        const deleteDropdownItem = isRecycleBinRootItem
            ? ''
            : `<button class="dropdown-item btn-delete-dir" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10005; Delete</button>`;
        actionButtons = `
            <button class="btn-action btn-info" title="Info" data-path="${escapeHtml(item.path)}">&#9432;</button>
            <button class="btn-action btn-copy-dir" title="Copy" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10697;</button>
            <button class="btn-action btn-rename" title="${renameLabel}" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998;</button>
            <button class="btn-action btn-download-dir" title="Download as zip" data-path="${escapeHtml(item.path)}">&#11015;</button>
            ${deleteButton}`;
        dropdownItems = `
            <button class="dropdown-item btn-info" data-path="${escapeHtml(item.path)}">&#9432; Info</button>
            <button class="dropdown-item btn-copy-dir" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10697; Copy</button>
            <button class="dropdown-item btn-rename" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998; ${renameLabel}</button>
            <button class="dropdown-item btn-download-dir" data-path="${escapeHtml(item.path)}">&#11015; Download</button>
            ${deleteDropdownItem}`;
    } else {
        actionButtons = `
            <button class="btn-action btn-info" title="Info" data-path="${escapeHtml(item.path)}">&#9432;</button>
            <button class="btn-action btn-copy-file" title="Copy" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10697;</button>
            <button class="btn-action btn-rename" title="${renameLabel}" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998;</button>
            <button class="btn-action btn-download" title="Download" data-path="${escapeHtml(item.path)}">&#11015;</button>
            <button class="btn-action btn-delete" title="Delete" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10005;</button>`;
        dropdownItems = `
            <button class="dropdown-item btn-info" data-path="${escapeHtml(item.path)}">&#9432; Info</button>
            <button class="dropdown-item btn-copy-file" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#10697; Copy</button>
            <button class="dropdown-item btn-rename" data-path="${escapeHtml(item.path)}" data-name="${escapeHtml(item.name)}">&#9998; ${renameLabel}</button>
            ${compileItem}
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
    const trashAttrs = item.trash_original_path
        ? ` data-trash-original-path="${escapeHtml(item.trash_original_path)}" data-trash-original-name="${escapeHtml(item.trash_original_name || '')}"`
        : '';

    return `
        <div class="file-item" data-path="${escapeHtml(item.path)}" data-is-dir="${item.is_dir}" data-name="${escapeHtml(item.name)}"${trashAttrs} draggable="true">
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
    } else if (path.toLowerCase().endsWith('.zip')) {
        showZipModal(path);
    } else {
        // Open in diff editor
        window.location.href = `diff?file=${encodeURIComponent(path)}`;
    }
}

function showZipModal(zipPath) {
    const name = zipPath.split('/').pop();
    const stem = name.replace(/\.zip$/i, '');

    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-dialog">
            <div class="modal-title copy-modal-title">Extract zip</div>
            <div class="modal-summary">${escapeHtml(name)} <span class="zip-info" style="opacity:0.6"></span></div>
            <div class="zip-actions">
                <button class="btn-zip" data-mode="directory">Extract to <strong>${escapeHtml(stem)}/</strong></button>
                <button class="btn-zip" data-mode="here">Extract to current directory</button>
                <button class="btn-zip btn-zip-open">Open anyway</button>
                <button class="btn-zip btn-zip-cancel">Cancel</button>
            </div>
            <div class="zip-status" style="display:none"></div>
        </div>
    `;

    document.body.appendChild(overlay);

    // Fetch zip info (size + file count)
    const infoEl = overlay.querySelector('.zip-info');
    fetch(`api/file/zip-info?path=${encodeURIComponent(zipPath)}`)
        .then(r => r.ok ? r.json() : null)
        .then(data => {
            if (!data || !infoEl) return;
            const size = data.size < 1024 ? `${data.size} B`
                : data.size < 1048576 ? `${(data.size / 1024).toFixed(1)} KB`
                : `${(data.size / 1048576).toFixed(1)} MB`;
            infoEl.textContent = `(${size}, ${data.file_count} file${data.file_count !== 1 ? 's' : ''})`;
        })
        .catch(() => {});

    const statusEl = overlay.querySelector('.zip-status');
    const buttons = overlay.querySelectorAll('.btn-zip');

    function disableAll() {
        buttons.forEach(b => b.disabled = true);
    }

    function close() {
        overlay.remove();
    }

    // Extract buttons
    overlay.querySelectorAll('.btn-zip[data-mode]').forEach(btn => {
        btn.addEventListener('click', async () => {
            disableAll();
            statusEl.textContent = 'Extracting...';
            statusEl.style.display = '';
            statusEl.style.color = '';

            try {
                const response = await fetch('api/file/extract', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'X-CSRF-Token': CSRF_TOKEN,
                    },
                    body: JSON.stringify({ path: zipPath, mode: btn.dataset.mode }),
                });

                const data = await response.json();
                if (!response.ok) {
                    statusEl.textContent = data.error || 'Extract failed.';
                    statusEl.style.color = 'var(--error)';
                    buttons.forEach(b => b.disabled = false);
                    return;
                }

                close();
                if (data.warning) {
                    window.alert(data.warning);
                }
                loadDirectory(currentPath);
            } catch (err) {
                statusEl.textContent = `Error: ${err.message}`;
                statusEl.style.color = 'var(--error)';
                buttons.forEach(b => b.disabled = false);
            }
        });
    });

    // Open anyway
    overlay.querySelector('.btn-zip-open').addEventListener('click', () => {
        close();
        window.location.href = `diff?file=${encodeURIComponent(zipPath)}`;
    });

    // Cancel
    overlay.querySelector('.btn-zip-cancel').addEventListener('click', close);
    bindBackdropClose(overlay, close);
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

function batchDownload() {
    const params = new URLSearchParams();
    for (const p of selectedPaths) {
        params.append('path', p);
    }
    const a = document.createElement('a');
    a.href = `api/batch/download?${params}`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
}

async function deleteFile(path, name) {
    const intent = getDeleteIntent(path);
    if (!window.confirm(intent.filePrompt(name))) {
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
    const itemEl = document.querySelector(`.file-item[data-path="${CSS.escape(path)}"]`);
    const isDir = path.endsWith('/') || itemEl?.dataset.isDir === 'true';
    showFileModal({
        path,
        name,
        isDir,
        mode: 'move',
        trashOriginalPath: itemEl?.dataset.trashOriginalPath || '',
        trashOriginalName: itemEl?.dataset.trashOriginalName || '',
    });
}

async function deleteDirectory(path, name) {
    const intent = getDeleteIntent(path);
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

        overlay.innerHTML = `
            <div class="modal-dialog">
                <div class="modal-title">Delete "${escapeHtml(name)}"?</div>
                <div class="modal-summary">${intent.directorySummary(data.total_files, data.total_dirs)}</div>
                <div class="preview-list">${listHtml}</div>
                <div class="modal-buttons">
                    <button class="btn-modal btn-modal-cancel">Cancel</button>
                    <button class="btn-modal btn-modal-delete">${intent.buttonLabel}</button>
                </div>
            </div>
        `;

        document.body.appendChild(overlay);

        // Wait for user choice
        const result = await new Promise(resolve => {
            overlay.querySelector('.btn-modal-delete').addEventListener('click', () => resolve(true));
            overlay.querySelector('.btn-modal-cancel').addEventListener('click', () => resolve(false));
            bindBackdropClose(overlay, () => resolve(false));
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

async function compileFile(path, name) {
    let compileInfo;
    try {
        compileInfo = await fetchCompileInfo(path);
    } catch (err) {
        window.alert(`Error: ${err.message}`);
        return;
    }

    const supportsOptimization = Boolean(compileInfo.supports_optimization);
    const supportsWarnings = Boolean(compileInfo.supports_warnings);
    const optionHtml = `
        ${supportsOptimization ? `<label class="copy-option"><input type="checkbox" class="chk-optimize"> ${escapeHtml(compileInfo.optimization_label || 'Optimize')}</label>` : ''}
        ${supportsWarnings ? `<label class="copy-option"><input type="checkbox" class="chk-warnings" checked> ${escapeHtml(compileInfo.warning_label || 'Warnings')}</label>` : ''}
    `;
    const summaryHtml = [
        `<span class="modal-verb-ing">Compiling</span> "${escapeHtml(name)}"`,
        compileInfo.artifact_note ? escapeHtml(compileInfo.artifact_note) : '',
    ].filter(Boolean).join('<br>');

    const overlay = createCopyModal({
        titleHtml: `<span class="modal-verb">Compile</span> ${escapeHtml(compileInfo.label || 'program')}`,
        summaryHtml,
        fieldsHtml: `
            <label class="copy-label">Output directory</label>
            <input type="text" class="copy-input copy-dir-input" autocomplete="off">
            <label class="copy-label">Output name</label>
            <input type="text" class="copy-input copy-name-input" autocomplete="off">
        `,
        symlinkOptionHtml: optionHtml,
        actionLabel: 'Compile',
    });

    const {
        dirInput,
        errorEl,
        actionBtn,
        chkOverwrite,
        chkCreateDirs,
    } = getCopyModalControls(overlay);
    const nameInput = overlay.querySelector('.copy-name-input');
    const chkOptimize = overlay.querySelector('.chk-optimize');
    const chkWarnings = overlay.querySelector('.chk-warnings');

    dirInput.value = compileInfo.default_directory || currentPath;
    nameInput.value = compileInfo.default_name || name.replace(/\.[^.]+$/, '');

    let submitting = false;

    function close() {
        if (!submitting) overlay.remove();
    }

    function resetActionButton() {
        actionBtn.disabled = false;
        actionBtn.textContent = 'Compile';
    }

    if (!compileInfo.available) {
        const installSuffix = compileInfo.install_command ? ` Install with: ${compileInfo.install_command}` : '';
        showModalError(errorEl, `${compileInfo.error || 'Compiler unavailable.'}${installSuffix}`);
        actionBtn.disabled = true;
    }

    async function submit() {
        if (submitting || actionBtn.disabled) return;

        const trimmedDir = dirInput.value.trim();
        const trimmedName = nameInput.value.trim();
        hideModalError(errorEl);

        if (!trimmedDir) {
            showModalError(errorEl, 'Output directory cannot be empty.');
            return;
        }
        if (!trimmedName) {
            showModalError(errorEl, 'Output name cannot be empty.');
            return;
        }

        submitting = true;
        actionBtn.disabled = true;
        actionBtn.textContent = 'Compiling...';

        try {
            const { response, data } = await postJson('api/file/compile', {
                path,
                directory: trimmedDir,
                name: trimmedName,
                optimize: Boolean(chkOptimize?.checked),
                warnings: Boolean(chkWarnings?.checked),
                overwrite: chkOverwrite.checked,
                create_dirs: chkCreateDirs.checked,
            });
            if (!response.ok) {
                const errorLines = String(data.error || 'Compile failed.').split('\n');
                showModalError(errorEl, errorLines);
                submitting = false;
                resetActionButton();
                return;
            }

            submitting = false;
            close();
            loadDirectory(currentPath);

            const successLines = [
                data.message || 'Compilation finished.',
                `Output: ${data.output_path}`,
            ];
            if (data.artifact_note) {
                successLines.push(data.artifact_note);
            }
            if (data.compiler_output) {
                successLines.push('', data.compiler_output);
            }
            window.alert(successLines.join('\n'));
        } catch (err) {
            showModalError(errorEl, `Error: ${err.message}`);
            submitting = false;
            resetActionButton();
        }
    }

    overlay.querySelector('.btn-modal-cancel').addEventListener('click', close);
    bindBackdropClose(overlay, close);
    actionBtn.addEventListener('click', submit);
    dirInput.addEventListener('input', () => {
        if (compileInfo.available) hideModalError(errorEl);
    });
    nameInput.addEventListener('input', () => {
        if (compileInfo.available) hideModalError(errorEl);
    });
    dirInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            nameInput.focus();
        }
    });
    nameInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            submit();
        }
    });

    setTimeout(() => nameInput.focus(), 50);
}

function showFileModal({ path, name, isDir, mode, trashOriginalPath = '', trashOriginalName = '' }) {
    const isCopy = mode === 'copy';
    const verb = isCopy ? 'Copy' : 'Rename / Move';
    const verbLower = isCopy ? 'copy' : 'move';
    const verbIng = isCopy ? 'Copying' : 'Renaming';

    const dotIdx = name.lastIndexOf('.');
    const defaultName = isCopy
        ? ((!isDir && dotIdx > 0) ? `${name.slice(0, dotIdx)} (2)${name.slice(dotIdx)}` : `${name} (2)`)
        : name;

    const shortcuts = isDir ? [] : getFileShortcuts(name);
    if (!isCopy && trashOriginalPath) {
        shortcuts.unshift({
            label: 'Original path',
            dir: getParentDirectory(trashOriginalPath),
            filename: trashOriginalName || trashOriginalPath.split('/').pop() || name,
        });
    }
    const overlay = createCopyModal({
        titleHtml: `<span class="modal-verb">${verb}</span> ${isDir ? 'directory' : 'file'}`,
        summaryHtml: `<span class="modal-verb-ing">${verbIng}</span> "${escapeHtml(name)}"`,
        shortcuts,
        fieldsHtml: `
            <label class="copy-label">Destination</label>
            <input type="text" class="copy-input copy-dir-input" autocomplete="off">
            <label class="copy-label">Name</label>
            <input type="text" class="copy-input copy-name-input" autocomplete="off">
        `,
        symlinkOptionHtml: getSymlinkOptionHtml(isCopy),
        actionLabel: verb,
    });

    const {
        dirInput,
        errorEl,
        actionBtn,
        chkSymlink,
        chkOverwrite,
        chkCreateDirs,
        chkActivate,
        activateRow,
        activateLabel,
    } = getCopyModalControls(overlay);
    const nameInput = overlay.querySelector('.copy-name-input');

    dirInput.value = currentPath;
    nameInput.value = defaultName;

    const updateActivateVisibility = createActivationVisibilityUpdater({
        dirInput,
        activateRow,
        activateLabel,
        chkActivate,
        allowActivation: !isDir,
    });

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

    bindShortcutButtons(overlay, shortcuts, (shortcut) => {
        dirInput.value = shortcut.dir;
        nameInput.value = shortcut.filename;
        updateActivateVisibility();
        updateActionLabel();
        nameInput.focus();
    });

    dirInput.addEventListener('input', () => {
        hideModalError(errorEl);
        updateActivateVisibility();
        updateActionLabel();
    });

    if (chkSymlink) chkSymlink.addEventListener('change', updateActionLabel);

    let submitting = false;

    function close() {
        if (!submitting) overlay.remove();
    }

    async function submit() {
        if (submitting) return;
        const trimmedName = nameInput.value.trim();
        const trimmedDir = dirInput.value.trim();
        hideModalError(errorEl);

        if (!trimmedName) { showModalError(errorEl, 'Name cannot be empty.'); return; }
        if (!trimmedDir) { showModalError(errorEl, 'Destination cannot be empty.'); return; }
        const normDir = normalizeDirectoryPath(trimmedDir);
        if (normDir === normalizeDirectoryPath(currentPath) && trimmedName === name) {
            showModalError(errorEl, isCopy
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
            const { endpoint, body } = buildOperationRequest({
                mode,
                path,
                name: trimmedName,
                isDir,
                directory: trimmedDir,
                overwrite: chkOverwrite.checked,
                createDirs: chkCreateDirs.checked,
                activate: chkActivate.checked,
                isSymlink,
            });
            const { response, data } = await postJson(endpoint, body);
            if (!response.ok) {
                showModalError(errorEl, data.error || `${verb} failed.`);
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
            showModalError(errorEl, `Error: ${err.message}`);
            submitting = false;
            actionBtn.disabled = false;
            updateActionLabel();
        }
    }

    overlay.querySelector('.btn-modal-cancel').addEventListener('click', close);
    bindBackdropClose(overlay, close);
    actionBtn.addEventListener('click', submit);
    nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
    dirInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); nameInput.focus(); } });
    nameInput.addEventListener('input', () => { hideModalError(errorEl); });

    updateActivateVisibility();
    updateActionLabel();
    setTimeout(() => nameInput.focus(), 50);
}

function showBatchFileModal({ paths, mode }) {
    const isCopy = mode === 'copy';
    const verb = isCopy ? 'Copy' : 'Move';
    const verbIng = isCopy ? 'Copying' : 'Moving';
    const n = paths.length;
    const items = getSelectionItems(paths);
    const hasDirectories = items.some((item) => item.isDir);
    const shortcuts = hasDirectories ? [] : BATCH_SHORTCUTS;

    const names = items.map((item) => item.name);
    const maxShow = 4;
    const namePreview = names.slice(0, maxShow).join('\n')
        + (names.length > maxShow ? `\n… (${n} total)` : `\n(${n} total)`);

    const overlay = createCopyModal({
        titleHtml: `${verb} ${n} item${n !== 1 ? 's' : ''}`,
        shortcuts,
        fieldsHtml: `
            <label class="copy-label">Destination</label>
            <input type="text" class="copy-input copy-dir-input" autocomplete="off">
            <label class="copy-label">Names</label>
            <textarea class="copy-input copy-names-batch" rows="${Math.min(n, maxShow) + 1}" disabled></textarea>
        `,
        symlinkOptionHtml: getSymlinkOptionHtml(isCopy),
        actionLabel: verb,
    });

    const {
        dirInput,
        errorEl,
        actionBtn,
        chkSymlink,
        chkOverwrite,
        chkCreateDirs,
        chkActivate,
        activateRow,
        activateLabel,
    } = getCopyModalControls(overlay);
    const namesTextarea = overlay.querySelector('.copy-names-batch');

    dirInput.value = currentPath;
    namesTextarea.value = namePreview;

    const updateActivateVisibility = createActivationVisibilityUpdater({
        dirInput,
        activateRow,
        activateLabel,
        chkActivate,
        allowActivation: !hasDirectories,
    });

    bindShortcutButtons(overlay, shortcuts, (shortcut) => {
        dirInput.value = shortcut.dir;
        updateActivateVisibility();
        dirInput.focus();
    });

    dirInput.addEventListener('input', () => {
        hideModalError(errorEl);
        updateActivateVisibility();
    });

    if (chkSymlink) {
        chkSymlink.addEventListener('change', () => {
            actionBtn.textContent = chkSymlink.checked ? 'Symlink' : verb;
        });
    }

    let submitting = false;

    function close() {
        if (!submitting) overlay.remove();
    }

    async function submit() {
        if (submitting) return;
        const trimmedDir = dirInput.value.trim();
        hideModalError(errorEl);

        if (!trimmedDir) { showModalError(errorEl, ['Destination cannot be empty.']); return; }

        const isSymlink = chkSymlink && chkSymlink.checked;
        submitting = true;
        actionBtn.disabled = true;
        actionBtn.textContent = isSymlink ? 'Linking...' : `${verbIng}...`;

        const errors = [];
        const activationErrors = [];
        const activationMessages = [];
        for (const item of items) {
            const { path, name, isDir } = item;

            const { endpoint, body } = buildOperationRequest({
                mode,
                path,
                name,
                isDir,
                directory: trimmedDir,
                overwrite: chkOverwrite.checked,
                createDirs: chkCreateDirs.checked,
                activate: !hasDirectories && chkActivate.checked,
                isSymlink,
            });

            try {
                const { response, data } = await postJson(endpoint, body);
                if (!response.ok) {
                    errors.push(`${name}: ${data.error}`);
                    continue;
                }

                if (data.activate_message) {
                    const message = `${name}: ${data.activate_message}`;
                    if (data.activate_error) {
                        activationErrors.push(message);
                    } else {
                        activationMessages.push(message);
                    }
                }
            } catch (err) {
                errors.push(`${name}: ${err.message}`);
            }
        }

        submitting = false;

        if (errors.length > 0) {
            const lines = [`${errors.length} item${errors.length !== 1 ? 's' : ''} failed:`, ...errors];
            if (activationErrors.length > 0) {
                lines.push('', 'Activation issues:', ...activationErrors);
            }
            showModalError(errorEl, lines);
            actionBtn.disabled = false;
            actionBtn.textContent = isSymlink ? 'Symlink' : verb;
            return;
        }

        close();
        clearSelection();
        exitSelectMode();
        loadDirectory(currentPath);

        if (activationErrors.length > 0 || activationMessages.length > 0) {
            const notices = [];
            if (activationErrors.length > 0) {
                notices.push(`Activation issues:\n\n${activationErrors.join('\n\n')}`);
            }
            if (activationMessages.length > 0) {
                notices.push(`Activation results:\n\n${activationMessages.join('\n\n')}`);
            }
            window.alert(notices.join('\n\n'));
        }
    }

    overlay.querySelector('.btn-modal-cancel').addEventListener('click', close);
    bindBackdropClose(overlay, close);
    actionBtn.addEventListener('click', submit);
    dirInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });

    updateActivateVisibility();
    setTimeout(() => dirInput.focus(), 50);
}

function appendInfoRow(tbody, label, valueHtml) {
    tbody.insertAdjacentHTML('beforeend', `
        <tr><td class="info-label">${escapeHtml(label)}</td><td class="info-value">${valueHtml}</td></tr>
    `);
}

function formatDuration(seconds) {
    if (seconds == null || !Number.isFinite(seconds)) return null;
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const sec = Math.floor(seconds % 60);
    return h > 0
        ? `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
        : `${m}:${String(sec).padStart(2, '0')}`;
}

function formatBitrate(bitRate) {
    if (bitRate == null || !Number.isFinite(bitRate)) return null;
    if (bitRate >= 1_000_000_000) {
        return `${(bitRate / 1_000_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })} Gbps`;
    }
    if (bitRate >= 1_000_000) {
        return `${(bitRate / 1_000_000).toLocaleString(undefined, { maximumFractionDigits: 2 })} Mbps`;
    }
    if (bitRate >= 1_000) {
        return `${(bitRate / 1_000).toLocaleString(undefined, { maximumFractionDigits: 1 })} kbps`;
    }
    return `${bitRate.toLocaleString()} bps`;
}

function formatSampleRate(sampleRate) {
    if (sampleRate == null || !Number.isFinite(sampleRate)) return null;
    if (sampleRate >= 1000) {
        return `${(sampleRate / 1000).toLocaleString(undefined, { maximumFractionDigits: 2 })} kHz`;
    }
    return `${sampleRate.toLocaleString()} Hz`;
}

function formatFrameRate(frameRate) {
    if (frameRate == null || !Number.isFinite(frameRate)) return null;
    return `${frameRate.toLocaleString(undefined, { maximumFractionDigits: 2 })} fps`;
}

function formatChannels(channels, layout) {
    if (layout && channels != null) {
        return `<span class="info-mono">${escapeHtml(layout)}</span> <span class="info-secondary">(${channels} ch)</span>`;
    }
    if (layout) {
        return `<span class="info-mono">${escapeHtml(layout)}</span>`;
    }
    if (channels != null) {
        return `${channels.toLocaleString()} ch`;
    }
    return null;
}

function formatPdfPageSize(widthPt, heightPt) {
    if (widthPt == null || heightPt == null) return null;
    const widthIn = widthPt / 72;
    const heightIn = heightPt / 72;
    return `${widthIn.toLocaleString(undefined, { maximumFractionDigits: 2 })} x ${heightIn.toLocaleString(undefined, { maximumFractionDigits: 2 })} in`;
}

async function showInfoModal(path) {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-dialog">
            <div class="modal-title info-modal-title">&#9432; Info</div>
            <div class="info-loading">Loading…</div>
            <div class="modal-buttons">
                <button class="btn-modal btn-modal-cancel">Close</button>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    const dialog = overlay.querySelector('.modal-dialog');

    function close() { overlay.remove(); }
    function bindClose() {
        overlay.querySelector('.btn-modal-cancel')?.addEventListener('click', close);
    }
    bindBackdropClose(overlay, close);
    bindClose();

    // Phase 1: fetch basic stat info (fast)
    let data;
    try {
        const response = await fetch(`api/file/info?path=${encodeURIComponent(path)}`);
        data = await response.json();
        if (!response.ok) {
            overlay.querySelector('.info-loading').textContent = data.error || 'Failed to load info';
            return;
        }
    } catch (err) {
        overlay.querySelector('.info-loading').textContent = `Error: ${err.message}`;
        return;
    }

    // Build basic rows
    const rows = [];

    rows.push(['Path', `<span class="info-mono">${escapeHtml(data.path)}</span>`]);

    if (data.is_symlink) {
        rows.push(['Symlink →', `<span class="info-mono">${escapeHtml(data.symlink_target)}</span>`]);
    }

    if (data.is_dir) {
        rows.push(['Size', '<span class="info-placeholder">calculating…</span>', 'info-size-value']);
    } else {
        rows.push(['Size', `${escapeHtml(data.size_human)} <span class="info-secondary">(${data.size.toLocaleString()} B)</span>`]);
    }

    if (data.mime_type) {
        rows.push(['Type', `<span class="info-mono">${escapeHtml(data.mime_type)}</span>`]);
    }

    rows.push([
        'Permissions',
        `<span class="info-mono">${escapeHtml(data.permissions)}</span> <span class="info-secondary">${escapeHtml(data.permissions_octal)}</span>`,
    ]);
    rows.push(['Owner', `${escapeHtml(data.owner)} <span class="info-secondary">:</span> ${escapeHtml(data.group)}`]);
    rows.push(['Modified', escapeHtml(data.modified)]);

    // Render modal with basic info + loading placeholder for extended
    dialog.innerHTML = `
        <div class="modal-title info-modal-title">&#9432; ${escapeHtml(data.name)}</div>
        <div class="info-table-wrap">
            <table class="info-table">
                <tbody id="info-tbody">
                    ${rows.map(([label, value, id]) => `
                        <tr>
                            <td class="info-label">${escapeHtml(label)}</td>
                            <td class="info-value"${id ? ` id="${id}"` : ''}>${value}</td>
                        </tr>
                    `).join('')}
                    <tr class="info-extended-row">
                        <td colspan="2" class="info-loading">Loading details…</td>
                    </tr>
                </tbody>
            </table>
        </div>
        <div class="modal-buttons">
            <button class="btn-modal btn-modal-cancel">Close</button>
        </div>
    `;
    bindClose();

    // Phase 2: fetch extended info (may be slow)
    try {
        const response = await fetch(`api/file/info/extended?path=${encodeURIComponent(path)}`);
        const ext = await response.json();

        const placeholder = dialog.querySelector('.info-extended-row');
        if (placeholder) placeholder.remove();

        const tbody = dialog.querySelector('#info-tbody');

        if (!response.ok) {
            tbody.insertAdjacentHTML('beforeend', `
                <tr><td colspan="2" class="info-loading" style="color: var(--error)">${escapeHtml(ext.error || 'Failed to load details')}</td></tr>
            `);
            return;
        }

        // Update dir size
        if (data.is_dir && ext.size_recursive != null) {
            const sizeCell = dialog.querySelector('#info-size-value');
            if (sizeCell) {
                sizeCell.innerHTML = `${escapeHtml(ext.size_recursive_human)} <span class="info-secondary">· ${ext.file_count.toLocaleString()} file${ext.file_count !== 1 ? 's' : ''}, ${ext.dir_count.toLocaleString()} dir${ext.dir_count !== 1 ? 's' : ''}</span>`;
            }
        }

        // File extended details
        if (ext.line_count != null) {
            appendInfoRow(tbody, 'Lines', ext.line_count.toLocaleString());
        }

        if (ext.image_info) {
            if (ext.image_info.format) {
                appendInfoRow(tbody, 'Format', `<span class="info-mono">${escapeHtml(ext.image_info.format)}</span>`);
            }
            appendInfoRow(tbody, 'Resolution', `${ext.image_info.width} x ${ext.image_info.height}`);
            appendInfoRow(tbody, 'Mode', `<span class="info-mono">${escapeHtml(ext.image_info.mode)}</span>`);
            appendInfoRow(tbody, 'Transparency', ext.image_info.has_alpha ? 'Yes' : 'No');
            if (ext.image_info.orientation) {
                appendInfoRow(tbody, 'Orientation', escapeHtml(ext.image_info.orientation));
            }
            if (ext.image_info.frame_count > 1) {
                appendInfoRow(tbody, 'Frames', ext.image_info.frame_count.toLocaleString());
            }
        }

        const pdfInfo = ext.pdf_info;
        if (pdfInfo) {
            appendInfoRow(tbody, 'Encrypted', pdfInfo.encrypted ? 'Yes' : 'No');
            if (pdfInfo.version) {
                appendInfoRow(tbody, 'Version', `<span class="info-mono">PDF ${escapeHtml(pdfInfo.version)}</span>`);
            }
            if (ext.pdf_pages != null) {
                appendInfoRow(tbody, 'Pages', ext.pdf_pages.toLocaleString());
            }
            const pageSize = formatPdfPageSize(pdfInfo.page_width_pt, pdfInfo.page_height_pt);
            if (pageSize) {
                appendInfoRow(tbody, 'First page', pageSize);
            }
            if (pdfInfo.title) {
                appendInfoRow(tbody, 'Title', escapeHtml(pdfInfo.title));
            }
            if (pdfInfo.author) {
                appendInfoRow(tbody, 'Author', escapeHtml(pdfInfo.author));
            }
        } else if (ext.pdf_pages != null) {
            appendInfoRow(tbody, 'Pages', ext.pdf_pages.toLocaleString());
        }

        const mediaInfo = ext.media_info || ext.video_info;
        if (mediaInfo) {
            if (mediaInfo.container) {
                appendInfoRow(tbody, 'Container', `<span class="info-mono">${escapeHtml(mediaInfo.container)}</span>`);
            }
            const bitrate = formatBitrate(mediaInfo.bit_rate);
            if (bitrate) {
                appendInfoRow(tbody, 'Bitrate', bitrate);
            }
            if (mediaInfo.width && mediaInfo.height) {
                appendInfoRow(tbody, 'Resolution', `${mediaInfo.width} x ${mediaInfo.height}`);
            }
            const frameRate = formatFrameRate(mediaInfo.frame_rate);
            if (frameRate) {
                appendInfoRow(tbody, 'Frame rate', frameRate);
            }
            const duration = formatDuration(mediaInfo.duration);
            if (duration) {
                appendInfoRow(tbody, 'Duration', duration);
            }
            const codecs = [mediaInfo.video_codec, mediaInfo.audio_codec].filter(Boolean).map(escapeHtml);
            if (codecs.length > 0) {
                appendInfoRow(tbody, 'Codec', `<span class="info-mono">${codecs.join(' / ')}</span>`);
            }
            const sampleRate = formatSampleRate(mediaInfo.sample_rate);
            if (sampleRate) {
                appendInfoRow(tbody, 'Sample rate', sampleRate);
            }
            const channels = formatChannels(mediaInfo.channels, mediaInfo.channel_layout);
            if (channels) {
                appendInfoRow(tbody, 'Channels', channels);
            }
            if (!mediaInfo.is_audio_only && mediaInfo.audio_tracks > 0) {
                appendInfoRow(tbody, 'Audio tracks', mediaInfo.audio_tracks.toLocaleString());
            }
            if (!mediaInfo.is_audio_only && mediaInfo.subtitle_tracks > 0) {
                appendInfoRow(tbody, 'Subtitle tracks', mediaInfo.subtitle_tracks.toLocaleString());
            }
        }

        // Show error from extended endpoint (e.g. dir traversal failure)
        if (ext.error) {
            tbody.insertAdjacentHTML('beforeend', `
                <tr><td colspan="2" class="info-loading" style="color: var(--error)">${escapeHtml(ext.error)}</td></tr>
            `);
        }
    } catch (err) {
        const placeholder = dialog.querySelector('.info-extended-row');
        if (placeholder) {
            placeholder.innerHTML = `<td colspan="2" class="info-loading" style="color: var(--error)">Failed to load details</td>`;
        }
    }
}

// --- Select mode (mobile long-press) ---

function enterSelectMode() {
    selectMode = true;
    document.querySelector('.file-list')?.classList.add('select-mode');
    // Create selection bar if it doesn't exist
    if (!document.getElementById('select-bar')) {
        const bar = document.createElement('div');
        bar.id = 'select-bar';
        bar.innerHTML = `
            <span class="select-bar-count">0 selected</span>
            <button class="select-bar-cancel">Cancel</button>
        `;
        bar.querySelector('.select-bar-cancel').addEventListener('click', () => {
            clearSelection();
            exitSelectMode();
        });
        document.querySelector('.browser-container')?.prepend(bar);
    }
    updateSelectBar();
}

function exitSelectMode() {
    selectMode = false;
    document.querySelector('.file-list')?.classList.remove('select-mode');
    document.getElementById('select-bar')?.remove();
}

function updateSelectBar() {
    const countEl = document.querySelector('.select-bar-count');
    if (countEl) {
        const n = selectedPaths.size;
        countEl.textContent = `${n} selected`;
    }
}

// --- Selection helpers ---

function toggleSelect(el) {
    const path = el.dataset.path;
    if (selectedPaths.has(path)) {
        selectedPaths.delete(path);
        el.classList.remove('selected');
    } else {
        selectedPaths.add(path);
        el.classList.add('selected');
    }
}

function clearSelection() {
    selectedPaths.clear();
    document.querySelectorAll('.file-item.selected').forEach(el => el.classList.remove('selected'));
    lastClickedIndex = -1;
}

// --- Drag and drop ---

function setupDragAndDrop(allItems) {
    let dragGhost = null;

    allItems.forEach(el => {
        el.addEventListener('dragstart', (e) => {
            const path = el.dataset.path;

            // If dragging an unselected item, select only it
            if (!selectedPaths.has(path)) {
                clearSelection();
                selectedPaths.add(path);
                el.classList.add('selected');
            }

            // Build the list of items being dragged
            const dragPaths = [...selectedPaths];
            e.dataTransfer.setData('application/x-file-paths', JSON.stringify(dragPaths));
            e.dataTransfer.effectAllowed = 'move';

            // Custom drag ghost
            dragGhost = document.createElement('div');
            dragGhost.className = 'drag-ghost';
            const names = dragPaths.map(p => {
                const item = document.querySelector(`.file-item[data-path="${CSS.escape(p)}"]`);
                return item ? item.dataset.name : p.split('/').pop();
            });
            if (names.length === 1) {
                dragGhost.textContent = names[0];
            } else {
                dragGhost.textContent = `${names[0]} + ${names.length - 1} more`;
            }
            document.body.appendChild(dragGhost);
            e.dataTransfer.setDragImage(dragGhost, 0, 0);

            el.classList.add('dragging');
            requestAnimationFrame(() => {
                selectedPaths.forEach(p => {
                    const item = document.querySelector(`.file-item[data-path="${CSS.escape(p)}"]`);
                    if (item) item.classList.add('dragging');
                });
            });
        });

        el.addEventListener('dragend', () => {
            document.querySelectorAll('.file-item.dragging').forEach(item => item.classList.remove('dragging'));
            document.querySelectorAll('.file-item.drop-target').forEach(item => item.classList.remove('drop-target'));
            document.getElementById('btn-up')?.classList.remove('drop-target');
            if (dragGhost) { dragGhost.remove(); dragGhost = null; }
        });

        // Drop target handling (only for directories)
        if (el.dataset.isDir === 'true') {
            el.addEventListener('dragover', (e) => {
                // Don't allow dropping onto a selected (dragged) item
                if (selectedPaths.has(el.dataset.path)) return;
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                el.classList.add('drop-target');
            });

            el.addEventListener('dragleave', () => {
                el.classList.remove('drop-target');
            });

            el.addEventListener('drop', (e) => {
                e.preventDefault();
                el.classList.remove('drop-target');
                const paths = JSON.parse(e.dataTransfer.getData('application/x-file-paths') || '[]');
                if (paths.length > 0) {
                    moveItems(paths, el.dataset.path);
                }
            });
        }
    });

    // ../  button as drop target (move to parent)
    const btnUp = document.getElementById('btn-up');
    if (btnUp) {
        btnUp.addEventListener('dragover', (e) => {
            const parts = currentPath.split('/').filter(p => p);
            if (parts.length < 1) return; // already at root
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            btnUp.classList.add('drop-target');
        });
        btnUp.addEventListener('dragleave', () => btnUp.classList.remove('drop-target'));
        btnUp.addEventListener('drop', (e) => {
            e.preventDefault();
            btnUp.classList.remove('drop-target');
            const parts = currentPath.split('/').filter(p => p);
            if (parts.length < 1) return;
            const parentPath = parts.length > 1 ? '/' + parts.slice(0, -1).join('/') : '/';
            const paths = JSON.parse(e.dataTransfer.getData('application/x-file-paths') || '[]');
            if (paths.length > 0) {
                moveItems(paths, parentPath);
            }
        });
    }
}

let _moveInFlight = false;
async function moveItems(paths, destDir) {
    if (_moveInFlight) return;
    _moveInFlight = true;

    let errors = [];
    for (const srcPath of paths) {
        const name = srcPath.split('/').pop();
        try {
            const response = await fetch('api/rename', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': CSRF_TOKEN,
                },
                body: JSON.stringify({
                    path: srcPath,
                    new_name: name,
                    directory: destDir,
                }),
            });
            const data = await response.json();
            if (!response.ok) {
                errors.push(`${name}: ${data.error}`);
            }
        } catch (err) {
            errors.push(`${name}: ${err.message}`);
        }
    }

    if (errors.length > 0) {
        window.alert(`Some items could not be moved:\n\n${errors.join('\n')}`);
    }

    clearSelection();
    exitSelectMode();
    _moveInFlight = false;
    loadDirectory(currentPath);
}

async function batchDelete() {
    const paths = [...selectedPaths];
    const items = paths.map(p => {
        const el = document.querySelector(`.file-item[data-path="${CSS.escape(p)}"]`);
        return { path: p, name: p.split('/').pop(), isDir: el?.dataset.isDir === 'true' };
    });
    const intent = getDeleteIntent(items[0]?.path || currentPath);

    const files = items.filter(i => !i.isDir);
    const dirs = items.filter(i => i.isDir);

    // Fetch previews for all directories in parallel
    const dirPreviews = await Promise.all(dirs.map(async (d) => {
        try {
            const resp = await fetch(`api/dir/preview?path=${encodeURIComponent(d.path)}`);
            if (resp.ok) return { ...d, preview: await resp.json() };
        } catch {}
        return { ...d, preview: null };
    }));

    // Build preview list: selected items first, then directory contents
    let totalFiles = files.length;
    let totalDirs = 0;
    let entries = [];

    // Selected files
    files.forEach(f => entries.push(f.name));
    // Selected directories + their contents
    dirPreviews.forEach(d => {
        entries.push(d.name + '/');
        if (d.preview) {
            totalFiles += d.preview.total_files;
            totalDirs += d.preview.total_dirs;
            [...d.preview.dirs, ...d.preview.files].forEach(e => entries.push('  ' + d.name + '/' + e));
        }
    });

    const maxShow = 50;
    const truncated = entries.length > maxShow;
    const shown = entries.slice(0, maxShow);

    let listHtml = shown.map(e => `<div class="preview-entry">${escapeHtml(e)}</div>`).join('');
    if (truncated) {
        listHtml += `<div class="preview-entry preview-more">... and ${entries.length - maxShow} more</div>`;
    }

    const summaryParts = [];
    if (totalFiles > 0) summaryParts.push(`${totalFiles} file${totalFiles !== 1 ? 's' : ''}`);
    if (totalDirs > 0) summaryParts.push(`${totalDirs} subdirector${totalDirs !== 1 ? 'ies' : 'y'}`);
    if (dirs.length > 0) summaryParts.push(`${dirs.length} director${dirs.length !== 1 ? 'ies' : 'y'}`);
    const summary = intent.batchSummary(summaryParts);

    // Show modal
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
        <div class="modal-dialog">
            <div class="modal-title">Delete ${items.length} item${items.length !== 1 ? 's' : ''}?</div>
            <div class="modal-summary">${summary}</div>
            <div class="preview-list">${listHtml}</div>
            <div class="modal-buttons">
                <button class="btn-modal btn-modal-cancel">Cancel</button>
                <button class="btn-modal btn-modal-delete">${intent.buttonLabel}</button>
            </div>
        </div>
    `;
    document.body.appendChild(overlay);

    const confirmed = await new Promise(resolve => {
        overlay.querySelector('.btn-modal-delete').addEventListener('click', () => resolve(true));
        overlay.querySelector('.btn-modal-cancel').addEventListener('click', () => resolve(false));
        bindBackdropClose(overlay, () => resolve(false));
    });
    overlay.remove();
    if (!confirmed) return;

    // Perform deletions
    let errors = [];
    for (const item of items) {
        const endpoint = item.isDir ? 'api/dir/delete' : 'api/file/delete';
        try {
            const response = await fetch(endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRF-Token': CSRF_TOKEN,
                },
                body: JSON.stringify({ path: item.path }),
            });
            const data = await response.json();
            if (!response.ok) {
                errors.push(`${item.name}: ${data.error}`);
            }
        } catch (err) {
            errors.push(`${item.name}: ${err.message}`);
        }
    }

    if (errors.length > 0) {
        window.alert(`Some items could not be deleted:\n\n${errors.join('\n')}`);
    }

    clearSelection();
    exitSelectMode();
    loadDirectory(currentPath);
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML.replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

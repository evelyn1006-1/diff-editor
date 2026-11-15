/* Browser-only recovery. File writes remain explicit editor actions. */
(() => {
    let database;
    function openDatabase() {
        if (!database) database = new Promise((resolve, reject) => {
            const request = indexedDB.open('diff-editor-recovery', 1);
            request.onupgradeneeded = () => {
                const store = request.result.createObjectStore('drafts', { keyPath: 'id' });
                store.createIndex('file', 'file');
            };
            request.onsuccess = () => {
                request.result.onversionchange = () => request.result.close();
                resolve(request.result);
            };
            request.onerror = () => reject(request.error);
            request.onblocked = () => reject(new Error('Browser draft storage is blocked'));
        });
        return database;
    }
    async function transaction(mode, action) {
        const db = await openDatabase();
        return new Promise((resolve, reject) => {
            const tx = db.transaction('drafts', mode);
            const request = action(tx.objectStore('drafts'));
            tx.oncomplete = () => resolve(request.result);
            tx.onerror = tx.onabort = () => reject(tx.error || request.error || new Error('Draft storage failed'));
        });
    }
    const identifier = () => globalThis.crypto?.randomUUID?.()
        || `draft-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

    window.createDraftRecovery = options => {
        let id = identifier();
        const file = JSON.stringify([options.kind, options.path]);
        let pending;
        let writing = null;
        let timer = null;
        let lastSnapshot = -Infinity;
        let disposed = false;
        let records = [];
        let failure = '';
        let stored = false;
        let storedAt = null;
        let recovering = false;
        let explicitlyShown = false;
        const panel = document.createElement('section');
        panel.className = 'draft-recovery hidden';
        panel.setAttribute('aria-label', 'Browser draft recovery');
        const notice = document.createElement('p');
        notice.tabIndex = -1;
        notice.setAttribute('aria-live', 'polite');
        const storageStatus = document.createElement('small');
        const rows = document.createElement('div');
        panel.append(notice, storageStatus, rows);
        options.container.prepend(panel);
        const previewHost = document.createElement('div');
        panel.after(previewHost);
        const viewer = createRecoveryPreview(previewHost, {
            beforeRestore: async () => !disposed && !options.busy?.()
                && (!options.dirty() || await flush()),
            onRestored: async (result, selected) => {
                if (disposed) return;
                const current = options.snapshot();
                const matchesEditor = current.content === selected.content && Boolean(current.is_binary) === Boolean(selected.is_binary);
                if (matchesEditor) {
                    options.saved?.(selected, result);
                    await flush();
                }
                notice.textContent = matchesEditor ? 'Version restored to file. The editor already matches it.'
                    : 'Version restored to file. Other editor edits were kept; open the restored file to load it.';
            },
        });

        async function previewVersion(record, restoreImmediately = false) {
            try {
                const path = options.resolvePath ? await options.resolvePath() : options.kind === 'terminal' ? null : options.path;
                if (!path || disposed) return;
                await viewer.open({path, source: 'draft', kind: options.kind,
                    content: record.content, is_binary: Boolean(record.is_binary)}, restoreImmediately);
            } catch (exception) { error(exception); }
        }

        function error(exception) {
            failure = `Browser draft recovery unavailable: ${exception.message || exception}. Use Save or download your draft.`;
            render();
        }
        function render() {
            if (disposed) return;
            panel.classList.toggle('hidden', !explicitlyShown && !failure && !records.length && !stored);
            notice.textContent = failure || (records.length
                ? 'Browser recovery drafts are available. Restoring changes the editor only; use Save to update the file.'
                : stored ? `Recovery draft stored in this browser at ${storedAt.toLocaleTimeString()}. Save is still required to update the file.`
                    : 'No browser recovery drafts are available for this file.');
            storageStatus.hidden = !stored || !records.length;
            storageStatus.textContent = stored ? `Recovery draft stored in this browser at ${storedAt?.toLocaleTimeString()}.` : '';
            rows.replaceChildren();
            for (const record of records) {
                const row = document.createElement('div');
                row.className = 'draft-recovery-row';
                const label = document.createElement('span');
                const preview = record.summary || record.content.slice(0, 150).replace(/\s+/g, ' ').slice(0, 80);
                label.textContent = `Draft from ${new Date(record.updated).toLocaleString()} — ${preview}`;
                const compare = document.createElement('button');
                compare.textContent = 'Preview / compare';
                compare.onclick = () => { void previewVersion(record); };
                const restoreFile = document.createElement('button');
                restoreFile.textContent = 'Restore to file';
                restoreFile.onclick = () => { void previewVersion(record, true); };
                row.append(label, compare, restoreFile);
                const currentSnapshot = record.id === id ? options.snapshot() : null;
                if (currentSnapshot && record.content === currentSnapshot.content
                    && Boolean(record.is_binary) === Boolean(currentSnapshot.is_binary)) {
                    label.textContent = `Current draft is already open in the editor — ${preview}`;
                    rows.appendChild(row);
                    continue;
                }
                const restore = document.createElement('button');
                restore.textContent = 'Restore draft';
                restore.addEventListener('click', async () => {
                    if (recovering) return;
                    const current = options.snapshot();
                    if (record.content === current.content && Boolean(record.is_binary) === Boolean(current.is_binary)) {
                        notice.textContent = 'That draft already matches the editor.';
                        return;
                    }
                    if (options.busy?.()) {
                        notice.textContent = 'Finish the current save or execution before restoring a draft.';
                        return;
                    }
                    if (options.dirty() && !confirm('Replace the current edits with this draft? The current edits will also remain in browser recovery.')) return;
                    recovering = true;
                    restore.disabled = true;
                    try {
                        // Preserve the current buffer under its own ID before
                        // starting a restored buffer. Other tabs keep their IDs.
                        const before = options.snapshot();
                        if (!await flush() || disposed || options.busy?.()) return;
                        const after = options.snapshot();
                        if (before.content !== after.content || before.revision !== after.revision
                            || before.is_binary !== after.is_binary) {
                            notice.textContent = 'The editor changed while preparing recovery. Try Restore draft again.';
                            return;
                        }
                        const changedOnDisk = record.revision !== options.snapshot().revision;
                        id = identifier();
                        options.restore(record, changedOnDisk);
                        records = records.filter(item => item.id !== record.id);
                        await flush();
                        render();
                    } catch (exception) { error(exception); }
                    finally { recovering = false; restore.disabled = false; }
                });
                const discard = document.createElement('button');
                discard.textContent = 'Discard draft';
                discard.addEventListener('click', async () => {
                    if (recovering) return;
                    if (!confirm('Permanently discard this browser recovery draft?')) return;
                    try {
                        await transaction('readwrite', store => store.delete(record.id));
                        records = records.filter(item => item.id !== record.id);
                        render();
                    } catch (exception) { error(exception); }
                });
                row.append(restore, discard);
                rows.appendChild(row);
            }
        }
        async function discover(showEmpty = false) {
            try {
                const current = options.snapshot();
                const found = await transaction('readonly', store => store.index('file').getAll(file));
                if (disposed) return;
                records = found.filter(record => (showEmpty || record.id !== id) && typeof record.content === 'string'
                    && (showEmpty || record.content !== current.content || Boolean(record.is_binary) !== Boolean(current.is_binary)))
                    .sort((a, b) => b.updated - a.updated);
                render();
                if (showEmpty && !records.length && !failure) {
                    panel.classList.remove('hidden');
                    notice.textContent = 'No earlier browser drafts are available for this file.';
                }
            } catch (exception) { error(exception); }
        }
        async function drain() {
            let success = true;
            try {
                while (pending !== undefined) {
                    const next = pending;
                    pending = undefined;
                    try {
                        await transaction('readwrite', store => next.record ? store.put(next.record) : store.delete(next.id));
                        if (!disposed) {
                            failure = '';
                            stored = next.record !== null;
                            storedAt = new Date();
                            render();
                        }
                    } catch (exception) { success = false; error(exception); }
                }
                return success;
            } finally {
                // Clear before settling so a later flush always starts a new batch.
                writing = null;
            }
        }
        function flush(clear = false) {
            clearTimeout(timer);
            timer = null;
            if (disposed && !clear) return writing || Promise.resolve();
            try {
                pending = { id, record: !clear && options.dirty()
                    ? { ...options.snapshot(), id, file, updated: Date.now() } : null };
            } catch (exception) { error(exception); return Promise.resolve(false); }
            // Coalesced snapshots share the active drain's result, including failures.
            if (!writing) writing = Promise.resolve().then(drain);
            return writing;
        }
        function changed() {
            if (disposed) return;
            const delay = Math.max(0, 150 - (performance.now() - lastSnapshot));
            if (delay === 0) {
                lastSnapshot = performance.now();
                void flush();
            } else if (timer === null) {
                timer = setTimeout(() => { lastSnapshot = performance.now(); void flush(); }, delay);
            }
        }
        const hidden = () => { if (document.visibilityState === 'hidden') void flush(); };
        const leaving = () => { void flush(); };
        document.addEventListener('visibilitychange', hidden);
        window.addEventListener('pagehide', leaving);
        void discover();
        return {
            changed, flush,
            async show() {
                explicitlyShown = true;
                panel.classList.remove('hidden');
                notice.textContent = 'Looking for browser recovery drafts…';
                await flush();
                await discover(true);
                if (!disposed) {
                    panel.scrollIntoView({block: 'nearest'});
                    notice.focus({preventScroll: true});
                }
            },
            dispose(discard = false) {
                if (discard) void flush(true);
                else void flush();
                disposed = true;
                clearTimeout(timer);
                document.removeEventListener('visibilitychange', hidden);
                window.removeEventListener('pagehide', leaving);
                viewer.destroy();
                previewHost.remove();
                panel.remove();
            },
        };
    };

    window.bindSaveMenu = (button, options) => {
        if (button._saveMenu) { button._saveMenu.options = options; return; }
        const group = document.createElement('div');
        group.className = 'save-split-button';
        button.before(group);
        group.appendChild(button);
        const toggle = document.createElement('button');
        toggle.type = 'button';
        toggle.textContent = '▾';
        toggle.className = button.className + ' save-menu-toggle';
        toggle.setAttribute('aria-label', 'Save options');
        toggle.setAttribute('aria-expanded', 'false');
        const menu = document.createElement('div');
        menu.className = 'save-options hidden';
        menu.id = button.id + '-options';
        toggle.setAttribute('aria-controls', menu.id);
        menu.setAttribute('aria-label', 'Save options menu');
        const state = { options };
        button._saveMenu = state;
        const close = () => { menu.classList.add('hidden'); toggle.setAttribute('aria-expanded', 'false'); };
        const recover = document.createElement('button');
        recover.textContent = 'Recover browser drafts';
        recover.onclick = () => { close(); void state.options.recovery()?.show(); };
        const download = document.createElement('button');
        download.textContent = 'Download current draft';
        download.onclick = () => {
            close();
            const snapshot = state.options.snapshot();
            const binary = Boolean(snapshot.is_binary);
            const name = state.options.path().split('/').pop() || 'untitled';
            const link = document.createElement('a');
            link.download = binary ? name + '.hex-draft.txt' : name.endsWith('.ipynb')
                ? name.slice(0, -6) + '.draft.ipynb' : name + '.draft.txt';
            link.href = URL.createObjectURL(new Blob([snapshot.content], {type: 'text/plain;charset=utf-8'}));
            document.body.appendChild(link);
            link.click();
            link.remove();
            setTimeout(() => URL.revokeObjectURL(link.href), 10000);
        };
        const backups = document.createElement('a');
        backups.textContent = 'Backups';
        backups.target = '_blank';
        backups.rel = 'noopener';
        backups.onclick = close;
        menu.append(backups, recover, download);
        group.append(toggle, menu);
        toggle.onclick = () => {
            const url = new URL(button.dataset.backupsUrl, location.href);
            url.searchParams.set('path', state.options.path());
            backups.href = url.href;
            menu.classList.toggle('hidden');
            toggle.setAttribute('aria-expanded', String(!menu.classList.contains('hidden')));
            menu.style.left = '';
            menu.style.right = '0';
            if (!menu.classList.contains('hidden') && menu.getBoundingClientRect().left < 8) {
                menu.style.right = 'auto';
                menu.style.left = `${8 - group.getBoundingClientRect().left}px`;
            }
        };
        document.addEventListener('click', event => { if (!group.contains(event.target)) close(); });
        group.addEventListener('keydown', event => {
            if (event.key === 'Escape') { close(); toggle.focus(); event.stopPropagation(); }
        });
    };
})();

/* One comparison and guarded file-restore view for saved backups and browser drafts. */
(() => {
    async function request(action, payload, signal) {
        const response = await fetch(RECOVERY_URLS[action], {
            method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF_TOKEN},
            body: JSON.stringify(payload), signal,
        });
        const data = await response.json();
        if (!response.ok) {
            const error = new Error(data.error || `Recovery request failed (${response.status})`);
            error.status = response.status;
            throw error;
        }
        return data;
    }

    window.createRecoveryPreview = (container, options = {}) => {
        const section = document.createElement('section');
        section.className = 'recovery-preview hidden';
        section.setAttribute('aria-label', 'Recovery preview and comparison');
        section.tabIndex = -1;
        const heading = document.createElement('h2');
        heading.textContent = 'Preview and compare';
        const status = document.createElement('p');
        status.setAttribute('aria-live', 'polite');
        const metadata = document.createElement('p');
        const controls = document.createElement('div');
        controls.className = 'recovery-preview-controls';
        const previewButton = document.createElement('button');
        previewButton.textContent = 'Preview version';
        const compareButton = document.createElement('button');
        compareButton.textContent = 'Compare with file';
        const refreshButton = document.createElement('button');
        refreshButton.textContent = 'Refresh comparison';
        const restoreButton = document.createElement('button');
        restoreButton.textContent = 'Restore to file';
        const closeButton = document.createElement('button');
        closeButton.textContent = 'Close preview';
        const content = document.createElement('pre');
        content.className = 'recovery-preview-content';
        content.setAttribute('aria-label', 'Selected version content');
        const openFile = document.createElement('a');
        openFile.textContent = 'Open restored file';
        openFile.target = '_blank';
        openFile.rel = 'noopener';
        openFile.className = 'hidden';
        controls.append(previewButton, compareButton, refreshButton, restoreButton, closeButton);
        section.append(heading, status, metadata, controls, content, openFile);
        container.appendChild(section);
        let source = null;
        let state = null;
        let mode = 'compare';
        let restoring = false;
        let disposed = false;
        let controller = null;
        let generation = 0;
        let opener = null;

        function render() {
            if (!state || disposed) return;
            metadata.textContent = `${state.path} — current file: ${state.exists ? state.current_size + ' bytes' : 'missing'}; selected version: ${state.candidate_size} bytes.`
                + (state.target !== state.path ? ` Resolved target: ${state.target}.` : '')
                + (state.is_binary ? ' Binary data is shown as hex.' : '')
                + (state.truncated ? ' Preview/comparison is truncated; restoration uses the complete version.' : '');
            previewButton.setAttribute('aria-pressed', String(mode === 'preview'));
            compareButton.setAttribute('aria-pressed', String(mode === 'compare'));
            content.textContent = mode === 'preview' ? state.candidate : state.diff || (state.identical
                ? 'The selected version matches the current file.'
                : state.truncated ? 'The complete versions differ; differences are outside this limited preview.'
                    : state.exists ? 'No textual differences.' : 'The target file is missing. Restoring will create it.');
            restoreButton.disabled = restoring;
        }
        async function load() {
            controller?.abort();
            controller = new AbortController();
            const token = ++generation;
            state = null;
            restoreButton.disabled = true;
            openFile.classList.add('hidden');
            status.textContent = 'Loading recovery preview…';
            content.textContent = '';
            metadata.textContent = '';
            try {
                const data = await request('preview', source, controller.signal);
                if (disposed || token !== generation) return false;
                state = data;
                status.textContent = state.identical ? 'This version is already on disk.' : 'Review the selected version before restoring it.';
                render();
                return true;
            } catch (error) {
                if (!disposed && token === generation && error.name !== 'AbortError') status.textContent = `Preview failed: ${error.message}`;
                return false;
            }
        }
        async function restore() {
            if (!state || restoring || disposed) return;
            if (!confirm(`${state.exists ? 'Replace the contents of' : 'Create'} ${state.path} with this version?`
                + (state.exists ? ' The current disk contents will be backed up first.'
                    : ' The new file will have owner-only permissions (0600).'))) return;
            restoring = true;
            restoreButton.disabled = true;
            refreshButton.disabled = true;
            closeButton.disabled = true;
            try {
                if (options.beforeRestore && !await options.beforeRestore()) {
                    status.textContent = 'Restore stopped. Save or preserve the current editor edits first.';
                    return;
                }
                if (disposed) return;
                status.textContent = 'Restoring version to file…';
                const result = await request('restore', {...source, path: state.path, revision: state.revision,
                    target: state.target, exists: state.exists, candidate_revision: state.candidate_revision});
                if (disposed) return;
                state = {...state, exists: true, revision: result.revision, current: state.candidate,
                    current_size: state.candidate_size, diff: '', identical: true};
                openFile.href = result.editor_url;
                openFile.classList.remove('hidden');
                status.textContent = result.created ? 'Version restored to file. The new file has owner-only permissions (0600).'
                    : 'Version restored to file. The previous disk contents remain available in Backups.';
                try { await options.onRestored?.(result, source); }
                catch { status.textContent += ' Refresh the backup list to see its latest entries.'; }
            } catch (error) {
                status.textContent = error.status
                    ? `Restore failed: ${error.message} Refresh the comparison before trying again.`
                    : 'The restore response was lost. Check the file and refresh the comparison before trying again.';
                state = null;
            } finally {
                restoring = false;
                refreshButton.disabled = false;
                closeButton.disabled = false;
                restoreButton.disabled = !state;
                if (state) render();
            }
        }
        previewButton.onclick = () => { mode = 'preview'; render(); };
        compareButton.onclick = () => { mode = 'compare'; render(); };
        refreshButton.onclick = () => { if (!restoring) void load(); };
        restoreButton.onclick = () => { void restore(); };
        function close() {
            if (restoring) return;
            ++generation;
            controller?.abort();
            section.classList.add('hidden');
            if (opener?.isConnected) opener.focus();
        }
        closeButton.onclick = close;
        section.addEventListener('keydown', event => {
            event.stopPropagation();
            if (event.key === 'Escape') { event.preventDefault(); close(); }
        });
        return {
            async open(payload, restoreImmediately = false) {
                if (restoring || disposed) return;
                opener = document.activeElement;
                source = {...payload};
                section.classList.remove('hidden');
                section.scrollIntoView({block: 'nearest'});
                section.focus({preventScroll: true});
                if (await load() && restoreImmediately) await restore();
            },
            destroy() { disposed = true; ++generation; controller?.abort(); section.remove(); },
        };
    };
})();

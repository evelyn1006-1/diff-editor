(() => {
    const versions = document.getElementById('backup-versions');
    const viewer = createRecoveryPreview(document.getElementById('backup-preview'), {
        onRestored: async () => {
            const response = await fetch(location.href, {cache: 'no-store'});
            if (!response.ok) throw new Error('Could not refresh backup list');
            const documentCopy = new DOMParser().parseFromString(await response.text(), 'text/html');
            versions.replaceChildren(...documentCopy.getElementById('backup-versions').childNodes);
        },
    });
    versions.addEventListener('click', event => {
        const button = event.target.closest('button[data-backup-id]');
        if (!button) return;
        void viewer.open({path: FILE_PATH, source: 'backup', backup_id: button.dataset.backupId},
            button.dataset.action === 'restore');
    });
})();

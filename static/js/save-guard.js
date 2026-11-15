/* Shared navigation protection for editors with explicit saves. */
(() => {
    const checks = [];
    window.guardUnsavedChanges = check => checks.push(check);
    window.addEventListener('beforeunload', event => {
        if (checks.some(check => check())) {
            event.preventDefault();
            event.returnValue = '';
        }
    });
})();

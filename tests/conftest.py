"""Keep save recovery artifacts isolated from the editor's live backups."""

import pytest


@pytest.fixture
def browser_engine(request):
    return getattr(request, "param", "chromium")


@pytest.fixture(autouse=True)
def isolated_file_backups(tmp_path, monkeypatch):
    monkeypatch.setenv("FILE_BACKUP_ROOT", str(tmp_path / "save-backups"))


@pytest.fixture
def offline_monaco():
    """Supply model interfaces while exercising real editor handlers offline."""
    return """
        window.require = (dependencies, callback) => callback();
        window.require.config = () => {};
        window.monaco = { editor: {
            defineTheme() {},
            EditorOption: { lineHeight: 1 },
            tokenize: content => content.split('\\n').map(() => []),
            createModel(value) {
                const listeners = [];
                return {
                    getValue: () => value,
                    getLineCount: () => value.split('\\n').length,
                    setValue(next) { value = next; listeners.forEach(listener => listener()); },
                    onDidChangeContent(listener) { listeners.push(listener); },
                    dispose() {},
                };
            },
            createDiffEditor() {
                let models;
                const editor = which => ({
                    getModel: () => models[which],
                    getScrollTop: () => 0,
                    getScrollHeight: () => 100,
                    getLayoutInfo: () => ({height: 100}),
                    getVisibleRanges: () => [{startLineNumber: 1}],
                    getOption: () => 20,
                    onDidScrollChange() {},
                    updateOptions() {},
                    revealLine() {},
                    revealLineNearTop() {},
                });
                return {
                    setModel(next) { models = next; },
                    getModel: () => models,
                    getModifiedEditor: () => editor('modified'),
                    getOriginalEditor: () => editor('original'),
                    dispose() {},
                };
            },
        } };
    """

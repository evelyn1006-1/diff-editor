/**
 * Mobile-friendly terminal interface with SocketIO.
 */

let socket = null;
let currentCwd = '~';

// Command history for textbox-based navigation
const commandHistory = [];
let historyIndex = -1;
let savedInput = '';  // Saves current input when navigating history

// ANSI/terminal escape sequence patterns
const ANSI_PATTERNS = [
    /\x1b\[[0-9;?]*[a-zA-Z]/g,           // CSI sequences (colors, cursor, modes)
    /\x1b\][^\x07]*\x07/g,                // OSC sequences (window title, etc.) ending with BEL
    /\x1b\][^\x1b]*\x1b\\/g,              // OSC sequences ending with ST
    /\x1b[PX^_][^\x1b]*\x1b\\/g,          // DCS, SOS, PM, APC sequences
    /\x1b[\(\)][AB012]/g,                 // Character set selection
    /\x1b[=>]/g,                          // Keypad modes
];

// Connect to terminal namespace
function connect() {
    const statusEl = document.getElementById('status');
    statusEl.textContent = 'Connecting...';
    statusEl.className = 'status';

    // Determine socket.io path based on current URL
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '/terminal';
    const socketPath = basePath + '/socket.io';

    if (typeof io === 'undefined') {
        statusEl.textContent = 'Error: socket.io not loaded';
        statusEl.className = 'status error';
        return;
    }

    socket = io('/terminal', {
        path: socketPath,
        transports: ['websocket', 'polling'],
        reconnection: true,
        reconnectionAttempts: 5,
        reconnectionDelay: 1000,
    });

    socket.on('connect', () => {
        statusEl.textContent = 'Connected';
        statusEl.className = 'status connected';
    });

    socket.on('connected', (data) => {
        if (data.cwd) {
            currentCwd = data.cwd;
        }
        appendOutput(`Connected to terminal at ${currentCwd}\n`, 'info');
    });

    socket.on('output', (data) => {
        appendOutput(data.data);
    });

    socket.on('error', (data) => {
        appendOutput(`Error: ${data.message}\n`, 'error');
    });

    socket.on('session_ended', (data) => {
        statusEl.textContent = 'Session ended';
        statusEl.className = 'status error';
        appendOutput(`\n${data.message}\n`, 'info');
    });

    socket.on('editor_redirect', (data) => {
        handleEditorRedirect(data.url);
    });

    socket.on('disconnect', () => {
        statusEl.textContent = 'Disconnected';
        statusEl.className = 'status error';
    });

    socket.on('connect_error', () => {
        statusEl.textContent = 'Connection failed';
        statusEl.className = 'status error';
    });
}

function appendOutput(text, className = '') {
    const outputEl = document.getElementById('terminal-output');

    // Convert ANSI to simple text (strip colors for now, could enhance later)
    const cleanText = stripAnsi(text);

    const span = document.createElement('span');
    if (className) {
        span.className = className;
    }
    span.textContent = cleanText;
    outputEl.appendChild(span);

    // Auto-scroll to bottom
    outputEl.scrollTop = outputEl.scrollHeight;
}

function stripAnsi(text) {
    let result = text;
    for (const pattern of ANSI_PATTERNS) {
        result = result.replace(pattern, '');
    }
    return result;
}

function sendInput(text) {
    if (!socket || !socket.connected) {
        appendOutput('Not connected to terminal\n', 'error');
        return;
    }

    socket.emit('input', { data: text });
}

function sendSignal(signal) {
    if (!socket || !socket.connected) {
        return;
    }

    socket.emit('signal', { signal: signal });
}

function handleEditorRedirect(url) {
    if (!url) return;
    window.open(url, '_blank', 'noopener,noreferrer');
}

function clearOutput() {
    const outputEl = document.getElementById('terminal-output');
    outputEl.innerHTML = '';
}

async function openTabCompletion() {
    const inputEl = document.getElementById('terminal-input');
    const text = inputEl.value;
    const cursorPos = inputEl.selectionStart;

    // Get the text before cursor and find the current "word" being typed
    const beforeCursor = text.substring(0, cursorPos);
    const afterCursor = text.substring(cursorPos);

    // Parse the command line into parts
    const trimmedBeforeCursor = beforeCursor.trim();
    const parts = trimmedBeforeCursor ? trimmedBeforeCursor.split(/\s+/) : [];
    const lastSpaceIdx = beforeCursor.lastIndexOf(' ');
    const currentWord = beforeCursor.substring(lastSpaceIdx + 1);
    const beforeWord = beforeCursor.substring(0, lastSpaceIdx + 1);

    // Commands that always expect paths
    const pathCommands = ['cd', 'nano', 'vim', 'vi', 'nvim', 'emacs', 'pico', 'edit', 'cat', 'less', 'more', 'head', 'tail'];
    // Commands with special argument completion
    const argCommands = ['systemctl', 'git', 'apt', 'apt-get', 'ssh'];

    // Determine the base command (skip sudo)
    let cmdIndex = 0;
    if (parts[0] === 'sudo' && parts.length > 1) cmdIndex = 1;
    const baseCommand = parts[cmdIndex] || '';

    // Calculate argument index (0 = first arg after command)
    const endsWithWhitespace = beforeCursor.length > 0 && /\s$/.test(beforeCursor);
    let argIndex = parts.length - cmdIndex - 2;
    if (endsWithWhitespace) argIndex += 1;
    argIndex = Math.max(0, argIndex);

    // Determine completion type
    let compType = 'path';
    let url = '';
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    const isFirstWord = beforeWord.trim() === '' || beforeWord.trim() === 'sudo';
    const looksLikePath = currentWord.includes('/') || currentWord.startsWith('~') || currentWord.startsWith('.');
    const endsWithSlash = currentWord.endsWith('/');

    if (isFirstWord && !looksLikePath) {
        // First word: command completion
        compType = 'command';
        url = `${basePath}/terminal/complete?type=command&prefix=${encodeURIComponent(currentWord)}`;
    } else if (baseCommand === 'cd') {
        // cd: directories only
        compType = 'path';
        url = `${basePath}/terminal/complete?type=path&prefix=${encodeURIComponent(currentWord)}&dirs_only=true`;
    } else if (pathCommands.includes(baseCommand) || looksLikePath || endsWithSlash) {
        // Path commands or path-like input: path completion
        compType = 'path';
        url = `${basePath}/terminal/complete?type=path&prefix=${encodeURIComponent(currentWord)}`;
    } else if (argCommands.includes(baseCommand)) {
        // Special argument completion
        compType = 'argument';
        url = `${basePath}/terminal/complete?type=argument&command=${encodeURIComponent(baseCommand)}&prefix=${encodeURIComponent(currentWord)}&arg_index=${argIndex}`;

        // For git, also pass subcommand context
        if (baseCommand === 'git' && argIndex > 0 && parts.length > cmdIndex + 1) {
            const gitSubcmd = parts[cmdIndex + 1];
            url += `&subcommand=${encodeURIComponent(gitSubcmd)}`;
        }
    } else {
        // Default to path completion
        url = `${basePath}/terminal/complete?type=path&prefix=${encodeURIComponent(currentWord)}`;
    }

    if (socket && socket.id) {
        url += `&session_id=${encodeURIComponent(socket.id)}`;
    }

    // Fetch completions from backend
    let completions = [];
    try {
        const resp = await fetch(url);
        if (resp.ok) {
            completions = await resp.json();
        }
    } catch (e) {
        console.error('Completion fetch failed:', e);
        return;
    }

    if (completions.length === 0) return;

    // If single match, auto-complete directly
    if (completions.length === 1) {
        inputEl.value = beforeWord + completions[0] + afterCursor;
        inputEl.selectionStart = inputEl.selectionEnd = beforeWord.length + completions[0].length;
        return;
    }

    // Multiple matches: show popup
    const overlay = document.createElement('div');
    overlay.className = 'history-search-overlay';
    overlay.innerHTML = `
        <div class="history-search-popup">
            <div class="history-search-header">
                <span style="padding: 0.5rem; color: var(--text-secondary);">Tab completion (${compType})</span>
                <button class="history-search-close" title="Close (Esc)">&times;</button>
            </div>
            <ul class="history-search-list"></ul>
        </div>
    `;
    document.body.appendChild(overlay);

    const listEl = overlay.querySelector('.history-search-list');
    const closeBtn = overlay.querySelector('.history-search-close');
    let selectedIndex = 0;

    function renderList() {
        listEl.innerHTML = '';
        completions.forEach((item, idx) => {
            const li = document.createElement('li');
            li.className = 'history-search-item' + (idx === selectedIndex ? ' selected' : '');
            li.textContent = item;
            li.addEventListener('click', () => selectCompletion(item));
            listEl.appendChild(li);
        });
    }

    function selectCompletion(item) {
        inputEl.value = beforeWord + item + afterCursor;
        inputEl.selectionStart = inputEl.selectionEnd = beforeWord.length + item.length;
        closePopup();
    }

    function closePopup() {
        overlay.remove();
        inputEl.focus();
    }

    function updateSelection(delta) {
        selectedIndex = Math.max(0, Math.min(completions.length - 1, selectedIndex + delta));
        renderList();
        // Scroll selected into view
        const selected = listEl.querySelector('.selected');
        if (selected) selected.scrollIntoView({ block: 'nearest' });
    }

    renderList();

    // Keyboard navigation
    function handleKeydown(e) {
        if (e.key === 'Escape') {
            e.preventDefault();
            closePopup();
        } else if (e.key === 'ArrowDown' || (e.ctrlKey && e.key.toLowerCase() === 'n')) {
            e.preventDefault();
            updateSelection(1);
        } else if (e.key === 'ArrowUp' || (e.ctrlKey && e.key.toLowerCase() === 'p')) {
            e.preventDefault();
            updateSelection(-1);
        } else if (e.key === 'Enter' || e.key === 'Tab') {
            e.preventDefault();
            selectCompletion(completions[selectedIndex]);
        }
    }

    document.addEventListener('keydown', handleKeydown);
    closeBtn.addEventListener('click', closePopup);
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closePopup();
    });

    // Clean up event listener when popup closes
    const origClose = closePopup;
    closePopup = function() {
        document.removeEventListener('keydown', handleKeydown);
        origClose();
    };
}

function openHistorySearch() {
    if (commandHistory.length === 0) return;

    // Create popup overlay
    const overlay = document.createElement('div');
    overlay.className = 'history-search-overlay';
    overlay.innerHTML = `
        <div class="history-search-popup">
            <div class="history-search-header">
                <input type="text" class="history-search-input" placeholder="Search history..." autofocus>
                <button class="history-search-close" title="Close (Esc)">&times;</button>
            </div>
            <ul class="history-search-list"></ul>
        </div>
    `;
    document.body.appendChild(overlay);

    const searchInput = overlay.querySelector('.history-search-input');
    const listEl = overlay.querySelector('.history-search-list');
    const closeBtn = overlay.querySelector('.history-search-close');
    let selectedIndex = 0;

    function renderList(filter = '') {
        const filtered = commandHistory
            .map((cmd, i) => ({ cmd, i }))
            .filter(({ cmd }) => cmd.toLowerCase().includes(filter.toLowerCase()))
            .reverse();  // Most recent first

        listEl.innerHTML = '';
        if (filtered.length === 0) {
            listEl.innerHTML = '<li class="history-search-empty">No matches</li>';
            return [];
        }

        filtered.forEach(({ cmd }, idx) => {
            const li = document.createElement('li');
            li.className = 'history-search-item' + (idx === selectedIndex ? ' selected' : '');
            li.textContent = cmd;
            li.addEventListener('click', () => selectCommand(cmd));
            listEl.appendChild(li);
        });

        return filtered;
    }

    function selectCommand(cmd) {
        const inputEl = document.getElementById('terminal-input');
        inputEl.value = cmd;
        closePopup();
        inputEl.focus();
    }

    function closePopup() {
        overlay.remove();
        document.getElementById('terminal-input').focus();
    }

    function updateSelection(filtered, delta) {
        if (filtered.length === 0) return;
        selectedIndex = Math.max(0, Math.min(filtered.length - 1, selectedIndex + delta));
        renderList(searchInput.value);
    }

    let currentFiltered = renderList();

    searchInput.addEventListener('input', () => {
        selectedIndex = 0;
        currentFiltered = renderList(searchInput.value);
    });

    searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            e.preventDefault();
            closePopup();
        } else if (e.key === 'ArrowDown' || (e.ctrlKey && e.key.toLowerCase() === 'n')) {
            e.preventDefault();
            updateSelection(currentFiltered, 1);
        } else if (e.key === 'ArrowUp' || (e.ctrlKey && e.key.toLowerCase() === 'p')) {
            e.preventDefault();
            updateSelection(currentFiltered, -1);
        } else if (e.key === 'Enter') {
            e.preventDefault();
            if (currentFiltered.length > 0) {
                selectCommand(currentFiltered[selectedIndex].cmd);
            }
        }
    });

    closeBtn.addEventListener('click', closePopup);
    overlay.addEventListener('click', (e) => {
        if (e.target === overlay) closePopup();
    });

    searchInput.focus();
}

function setTerminalRaised(raised, buttonEl) {
    document.body.classList.toggle('terminal-raised', raised);

    if (buttonEl) {
        buttonEl.textContent = raised ? 'Lower Panel' : 'Raise Panel';
        buttonEl.setAttribute('aria-pressed', raised ? 'true' : 'false');
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    const inputEl = document.getElementById('terminal-input');
    const sendBtn = document.getElementById('btn-send');
    const clearBtn = document.getElementById('btn-clear');
    const raiseBtn = document.getElementById('btn-raise-terminal');

    // Connect to terminal
    connect();

    // Restore panel position preference
    let isRaised = false;
    try {
        isRaised = localStorage.getItem('terminal_panel_raised') === '1';
    } catch (_err) {
        isRaised = false;
    }
    setTerminalRaised(isRaised, raiseBtn);

    // Send button
    sendBtn.addEventListener('click', () => {
        const text = inputEl.value;
        if (text) {
            // Add to history (avoid duplicates of last command)
            if (commandHistory.length === 0 || commandHistory[commandHistory.length - 1] !== text) {
                commandHistory.push(text);
            }
            historyIndex = -1;
            savedInput = '';

            sendInput(text + '\n');
            inputEl.value = '';
        }
        inputEl.focus();
    });

    // Helper to navigate command history
    function navigateHistory(direction) {
        if (commandHistory.length === 0) return;

        if (direction === 'up') {
            if (historyIndex === -1) {
                // Starting to navigate: save current input
                savedInput = inputEl.value;
                historyIndex = commandHistory.length - 1;
            } else if (historyIndex > 0) {
                historyIndex--;
            }
        } else if (direction === 'down') {
            if (historyIndex === -1) return;  // Not navigating
            if (historyIndex < commandHistory.length - 1) {
                historyIndex++;
            } else {
                // Back to current input
                historyIndex = -1;
                inputEl.value = savedInput;
                return;
            }
        }

        inputEl.value = commandHistory[historyIndex];
    }

    // Keyboard handling: Enter to send, arrows for history, Ctrl combos proxied to terminal
    inputEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendBtn.click();
            return;
        }

        // Tab key for completion
        if (e.key === 'Tab' && !e.ctrlKey && !e.altKey && !e.metaKey) {
            e.preventDefault();
            openTabCompletion();
            return;
        }

        // Arrow keys for history navigation (without modifiers)
        if (!e.ctrlKey && !e.altKey && !e.metaKey) {
            if (e.key === 'ArrowUp') {
                e.preventDefault();
                navigateHistory('up');
                return;
            }
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                navigateHistory('down');
                return;
            }
        }

        // Proxy Ctrl combinations to the terminal
        if (e.ctrlKey && !e.altKey && !e.metaKey) {
            const key = e.key.toLowerCase();

            // Local textbox actions (not sent to terminal)
            if (key === 'l') {
                e.preventDefault();
                clearOutput();
                sendInput('\x0c');  // Clear output + fresh prompt
                return;
            }
            if (key === 'p') {
                e.preventDefault();
                navigateHistory('up');
                return;
            }
            if (key === 'n') {
                e.preventDefault();
                navigateHistory('down');
                return;
            }
            if (key === 'u') {
                // Clear text before cursor
                e.preventDefault();
                const pos = inputEl.selectionStart;
                inputEl.value = inputEl.value.substring(pos);
                inputEl.selectionStart = inputEl.selectionEnd = 0;
                return;
            }
            if (key === 'k') {
                // Clear text after cursor
                e.preventDefault();
                const pos = inputEl.selectionStart;
                inputEl.value = inputEl.value.substring(0, pos);
                return;
            }
            if (key === 'w') {
                // Delete word before cursor
                e.preventDefault();
                const pos = inputEl.selectionStart;
                const before = inputEl.value.substring(0, pos);
                const after = inputEl.value.substring(pos);
                // Find word boundary (skip trailing spaces, then delete word)
                const trimmed = before.replace(/\s+$/, '');
                const lastSpace = trimmed.lastIndexOf(' ');
                const newBefore = lastSpace === -1 ? '' : trimmed.substring(0, lastSpace + 1);
                inputEl.value = newBefore + after;
                inputEl.selectionStart = inputEl.selectionEnd = newBefore.length;
                return;
            }
            if (key === 'r') {
                // Open history search popup
                e.preventDefault();
                openHistorySearch();
                return;
            }

            // Map of Ctrl combinations to signals
            const ctrlSignals = {
                'c': 'SIGINT',   // Ctrl+C - interrupt
                'd': 'EOF',      // Ctrl+D - end of input
                'z': 'SIGTSTP',  // Ctrl+Z - suspend
                '\\': 'SIGQUIT', // Ctrl+\ - quit (core dump)
            };

            if (ctrlSignals[key]) {
                e.preventDefault();
                sendSignal(ctrlSignals[key]);
                return;
            }

            // Let browser handle Ctrl+A (select all / start of line) and Ctrl+E (end of line)
        }
    });

    // Auto-resize textarea
    inputEl.addEventListener('input', () => {
        inputEl.style.height = 'auto';
        inputEl.style.height = Math.min(inputEl.scrollHeight, 150) + 'px';
    });

    // Clear button - clears output and sends Ctrl+L for fresh prompt
    clearBtn.addEventListener('click', () => {
        clearOutput();
        sendInput('\x0c');  // Ctrl+L to clear terminal and get fresh prompt
    });

    // Raise/lower panel button (useful on mobile browser UI overlap)
    if (raiseBtn) {
        raiseBtn.addEventListener('click', () => {
            const next = !document.body.classList.contains('terminal-raised');
            setTerminalRaised(next, raiseBtn);
            try {
                localStorage.setItem('terminal_panel_raised', next ? '1' : '0');
            } catch (_err) {
                // Ignore storage failures (e.g. private browsing restrictions).
            }
            inputEl.focus();
        });
    }

    // Special key buttons
    document.querySelectorAll('.key-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const signal = btn.dataset.signal;
            const key = btn.dataset.key;
            const history = btn.dataset.history;
            const action = btn.dataset.action;

            if (signal) {
                sendSignal(signal);
            } else if (history) {
                navigateHistory(history);
            } else if (action === 'delete-word') {
                // Ctrl+W behavior: delete word before cursor
                const pos = inputEl.selectionStart;
                const before = inputEl.value.substring(0, pos);
                const after = inputEl.value.substring(pos);
                const trimmed = before.replace(/\s+$/, '');
                const lastSpace = trimmed.lastIndexOf(' ');
                const newBefore = lastSpace === -1 ? '' : trimmed.substring(0, lastSpace + 1);
                inputEl.value = newBefore + after;
                inputEl.selectionStart = inputEl.selectionEnd = newBefore.length;
            } else if (action === 'search') {
                openHistorySearch();
            } else if (action === 'complete') {
                openTabCompletion();
            } else if (key) {
                sendInput(key);
            }

            inputEl.focus();
        });
    });

    // Focus input on page load
    inputEl.focus();
});

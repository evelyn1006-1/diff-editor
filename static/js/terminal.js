/**
 * Mobile-friendly terminal interface with SocketIO.
 */

let socket = null;
let currentCwd = '~';
let terminalToken = null;  // Secret token for authenticated terminal requests

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
        if (data.token) {
            terminalToken = data.token;
        }
        appendOutput(`Connected to terminal at ${currentCwd}\n`, 'info');

        // Auto-execute command if provided via URL param
        if (window.AUTO_CMD) {
            const cmd = window.AUTO_CMD;
            window.AUTO_CMD = '';  // Clear to prevent re-execution on reconnect
            // Small delay to let shell initialize
            setTimeout(() => {
                if (commandHistory.length === 0 || commandHistory[commandHistory.length - 1] !== cmd) {
                    commandHistory.push(cmd);
                }
                sendInput(cmd + '\n');
            }, 100);
        }
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

    socket.on('task_manager_popup', () => {
        openTaskManager();
    });

    socket.on('pager_popup', (data) => {
        openPagerModal(data.title, data.content);
    });

    socket.on('editor_modal', (data) => {
        openEditorModal(data.title, data.content);
    });

    socket.on('disconnect', () => {
        statusEl.textContent = 'Disconnected';
        statusEl.className = 'status error';
        // Clear token to prevent stale token usage during reconnect window
        terminalToken = null;
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

function parseShellWords(beforeCursor) {
    const tokens = [];
    let tokenStart = -1;
    let raw = '';
    let value = '';
    let quote = null;  // "'" or '"'
    let escaped = false;

    function pushToken(endIdx) {
        if (tokenStart === -1) return;
        tokens.push({ raw: raw, value: value, start: tokenStart, end: endIdx });
        tokenStart = -1;
        raw = '';
        value = '';
    }

    for (let i = 0; i < beforeCursor.length; i++) {
        const ch = beforeCursor[i];

        if (tokenStart === -1) {
            if (/\s/.test(ch)) continue;
            tokenStart = i;
        }

        if (quote === null && !escaped && /\s/.test(ch)) {
            pushToken(i);
            continue;
        }

        raw += ch;

        if (quote === '\'') {
            if (ch === '\'') {
                quote = null;
            } else {
                value += ch;
            }
            continue;
        }

        if (quote === '"') {
            if (escaped) {
                value += ch;
                escaped = false;
            } else if (ch === '\\') {
                escaped = true;
            } else if (ch === '"') {
                quote = null;
            } else {
                value += ch;
            }
            continue;
        }

        if (escaped) {
            value += ch;
            escaped = false;
        } else if (ch === '\\') {
            escaped = true;
        } else if (ch === '\'') {
            quote = '\'';
        } else if (ch === '"') {
            quote = '"';
        } else {
            value += ch;
        }
    }

    if (tokenStart !== -1) {
        if (escaped) value += '\\';
        pushToken(beforeCursor.length);
    }

    const endedWithWhitespace = beforeCursor.length > 0 && /\s/.test(beforeCursor[beforeCursor.length - 1]);
    const current = (!endedWithWhitespace && tokens.length > 0)
        ? tokens[tokens.length - 1]
        : { raw: '', value: '', start: beforeCursor.length, end: beforeCursor.length };

    return { tokens, current, endedWithWhitespace };
}

function resolveCommandContext(tokens, endedWithWhitespace) {
    const longNeedsArg = new Set(['user', 'group', 'host', 'prompt', 'chdir', 'chroot', 'other-user']);
    const shortNeedsArg = new Set(['u', 'g', 'h', 'p', 'C', 'T', 'R', 'r', 't']);

    let commandLookupIndex = 0;
    let commandIndex = -1;
    let baseCommand = '';

    if (tokens.length > 0) {
        if (tokens[0].value !== 'sudo') {
            commandLookupIndex = 0;
            commandIndex = 0;
            baseCommand = tokens[0].value;
        } else {
            let i = 1;
            let expectValue = false;

            while (i < tokens.length) {
                const tok = tokens[i].value;
                if (expectValue) {
                    expectValue = false;
                    i++;
                    continue;
                }

                if (tok === '--') {
                    i++;
                    break;
                }

                if (!tok.startsWith('-') || tok === '-') break;

                if (tok.startsWith('--')) {
                    const eqIdx = tok.indexOf('=');
                    const name = eqIdx === -1 ? tok.slice(2) : tok.slice(2, eqIdx);
                    if (eqIdx === -1 && longNeedsArg.has(name)) {
                        expectValue = true;
                    }
                    i++;
                    continue;
                }

                const flags = tok.slice(1);
                for (let j = 0; j < flags.length; j++) {
                    const f = flags[j];
                    if (shortNeedsArg.has(f)) {
                        if (j === flags.length - 1) expectValue = true;
                        break;
                    }
                }
                i++;
            }

            commandLookupIndex = i;
            const pointsToCurrentOptionToken =
                !endedWithWhitespace &&
                i === tokens.length - 1 &&
                tokens[i].value.startsWith('-');
            if (i < tokens.length && !pointsToCurrentOptionToken) {
                commandIndex = i;
                baseCommand = tokens[i].value;
            }
        }
    }

    const currentTokenIndex = endedWithWhitespace ? tokens.length : Math.max(0, tokens.length - 1);
    const completingCommand = (commandIndex === -1)
        ? currentTokenIndex === commandLookupIndex
        : (currentTokenIndex === commandIndex && !endedWithWhitespace);

    let argIndex = 0;
    if (commandIndex >= 0 && !completingCommand) {
        const firstArgIdx = commandIndex + 1;
        if (endedWithWhitespace) {
            argIndex = Math.max(0, tokens.length - firstArgIdx);
        } else {
            argIndex = Math.max(0, (tokens.length - 1) - firstArgIdx);
        }
    }

    let subcommand = '';
    if (commandIndex >= 0 && tokens.length > commandIndex + 1) {
        subcommand = tokens[commandIndex + 1].value;
    }

    return {
        baseCommand: baseCommand,
        commandIndex: commandIndex,
        commandLookupIndex: commandLookupIndex,
        completingCommand: completingCommand,
        argIndex: argIndex,
        subcommand: subcommand,
    };
}

function longestCommonPrefix(items) {
    if (!items || items.length === 0) return '';
    let prefix = items[0];
    for (let i = 1; i < items.length; i++) {
        const item = items[i];
        let j = 0;
        while (j < prefix.length && j < item.length && prefix[j] === item[j]) j++;
        prefix = prefix.slice(0, j);
        if (!prefix) break;
    }
    return prefix;
}

function getUnclosedQuote(rawToken) {
    let quote = null;
    let escaped = false;

    for (let i = 0; i < rawToken.length; i++) {
        const ch = rawToken[i];
        if (quote === '\'') {
            if (ch === '\'') quote = null;
            continue;
        }
        if (quote === '"') {
            if (escaped) {
                escaped = false;
            } else if (ch === '\\') {
                escaped = true;
            } else if (ch === '"') {
                quote = null;
            }
            continue;
        }

        if (escaped) {
            escaped = false;
        } else if (ch === '\\') {
            escaped = true;
        } else if (ch === '\'' || ch === '"') {
            quote = ch;
        }
    }

    return quote;
}

async function openTabCompletion() {
    const inputEl = document.getElementById('terminal-input');
    const text = inputEl.value;
    const cursorPos = inputEl.selectionStart;

    // Get the text before cursor and find the current "word" being typed
    const beforeCursor = text.substring(0, cursorPos);
    const afterCursor = text.substring(cursorPos);

    // Parse shell words with quote and escape awareness.
    const parsed = parseShellWords(beforeCursor);
    const parts = parsed.tokens;
    const rawWords = parts.map(part => part.raw);
    const completionWordIndex = parsed.endedWithWhitespace ? parts.length : Math.max(0, parts.length - 1);
    if (parsed.endedWithWhitespace) {
        rawWords.push('');
    } else if (rawWords.length === 0) {
        rawWords.push('');
    }
    const currentWord = parsed.current.value;
    const currentRawWord = parsed.current.raw;
    let replaceStart = parsed.current.start;
    const unclosedQuote = getUnclosedQuote(currentRawWord);
    if (
        unclosedQuote &&
        currentRawWord.startsWith(unclosedQuote) &&
        currentRawWord.indexOf(unclosedQuote, 1) === -1
    ) {
        // Preserve an opening quote already typed by user.
        replaceStart += 1;
    }
    const beforeWord = beforeCursor.substring(0, replaceStart);
    const context = resolveCommandContext(parts, parsed.endedWithWhitespace);

    // Commands that always expect paths
    const pathCommands = ['cd', 'nano', 'vim', 'vi', 'nvim', 'emacs', 'pico', 'edit', 'cat', 'less', 'more', 'head', 'tail'];
    // Commands with special argument completion
    const argCommands = ['systemctl', 'git', 'apt', 'apt-get', 'ssh', 'pip', 'pip3', 'npm'];

    const baseCommand = context.baseCommand;
    const argIndex = context.argIndex;

    // Determine completion type
    let compType = 'path';
    let url = '';
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    const looksLikePath =
        currentRawWord.includes('/') ||
        currentWord.includes('/') ||
        currentRawWord.startsWith('~') ||
        currentWord.startsWith('~') ||
        currentRawWord.startsWith('.') ||
        currentWord.startsWith('.');
    const endsWithSlash = currentRawWord.endsWith('/') || currentWord.endsWith('/');
    const inSudoOptionContext =
        parts.length > 0 &&
        parts[0].value === 'sudo' &&
        !context.baseCommand &&
        currentRawWord.startsWith('-');

    if (inSudoOptionContext) {
        compType = 'argument';
        url = `${basePath}/terminal/complete?type=argument&command=sudo&prefix=${encodeURIComponent(currentWord)}&arg_index=${argIndex}`;
    } else if (context.completingCommand && !looksLikePath) {
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

        // Pass subcommand context for commands whose completion depends on it.
        if (argIndex > 0 && context.subcommand) {
            url += `&subcommand=${encodeURIComponent(context.subcommand)}`;
        }
    } else {
        // Default to path completion
        url = `${basePath}/terminal/complete?type=path&prefix=${encodeURIComponent(currentWord)}`;
    }

    if (socket && socket.id) {
        url += `&session_id=${encodeURIComponent(socket.id)}`;
    }
    url += `&base_command=${encodeURIComponent(baseCommand || '')}`;
    url += `&line=${encodeURIComponent(text)}`;
    url += `&cursor=${encodeURIComponent(String(cursorPos))}`;
    url += `&raw_words=${encodeURIComponent(JSON.stringify(rawWords))}`;
    url += `&cword=${encodeURIComponent(String(completionWordIndex))}`;

    // Status indicator helpers
    const statusEl = document.getElementById('completion-status');
    function showStatus(msg, type = '') {
        statusEl.textContent = msg;
        statusEl.className = 'completion-status' + (type ? ` ${type}` : '');
    }
    function hideStatus() {
        statusEl.className = 'completion-status hidden';
        statusEl.textContent = '';
    }

    // Fetch completions from backend
    showStatus('Loading...', 'loading');
    let completions = [];
    try {
        const resp = await fetch(url);
        if (resp.ok) {
            completions = await resp.json();
            hideStatus();
        } else {
            showStatus('Failed', 'error');
            setTimeout(hideStatus, 1500);
            return;
        }
    } catch (e) {
        console.error('Completion fetch failed:', e);
        showStatus('Failed', 'error');
        setTimeout(hideStatus, 1500);
        return;
    }

    if (completions.length === 0) {
        hideStatus();
        return;
    }

    function selectAndApply(item, addSpace = true) {
        const needsSpace =
            addSpace &&
            !unclosedQuote &&
            !item.endsWith('/') &&
            !item.endsWith(' ') &&
            (afterCursor.length === 0 || /^\s/.test(afterCursor));
        const suffix = needsSpace ? ' ' : '';
        inputEl.value = beforeWord + item + suffix + afterCursor;
        const caretPos = beforeWord.length + item.length + suffix.length;
        inputEl.selectionStart = inputEl.selectionEnd = caretPos;
    }

    // If single match, auto-complete directly
    if (completions.length === 1) {
        selectAndApply(completions[0], true);
        return;
    }

    // If multiple matches share a longer common prefix, expand first.
    const sharedPrefix = longestCommonPrefix(completions);
    if (sharedPrefix && sharedPrefix.length > currentWord.length) {
        selectAndApply(sharedPrefix, false);
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
        selectAndApply(item, true);
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

// ─────────────────────────────────────────────────────────────────────────────
// Task Manager
// ─────────────────────────────────────────────────────────────────────────────

let taskManagerRefreshInterval = null;
let taskManagerProcesses = [];
let taskManagerCurrentUser = null;            // Current user running the server
let taskManagerSortColumn = 'cpu';
let taskManagerSortAsc = false;
let taskManagerFilter = '';
let taskManagerExpandedCommands = new Set();  // PIDs with expanded command text
let taskManagerExpandedDetails = new Set();   // PIDs with expanded details panel
let taskManagerTreeMode = false;
let taskManagerStatsExpanded = false;
let taskManagerProcessDetails = new Map();    // PID -> {ports, connections, loading}
let taskManagerFailedServices = [];           // [{unit, short_status, reason_preview, ...}]
let taskManagerDismissedFailedServices = new Map(); // unit -> stateKey, hidden until state changes
let taskManagerStoppedServices = new Map();   // unit -> {unit, command, label}
let taskManagerOpenMenuPid = null;            // PID with open signal dropdown
let taskManagerOpenServiceMenuPid = null;     // PID with open service dropdown
let sudoConfirmCallback = null;               // Callback for sudo confirmation
let forceKillCallback = null;                 // Callback for force kill confirmation

function openTaskManager() {
    const overlay = document.getElementById('task-manager-overlay');
    if (!overlay) return;

    overlay.classList.remove('hidden');

    // Set up event listeners
    const closeBtn = document.getElementById('tm-close');
    const filterInput = document.getElementById('tm-filter');
    const sortSelect = document.getElementById('tm-sort');
    const refreshBtn = document.getElementById('tm-refresh');
    const expandStatsBtn = document.getElementById('tm-expand-stats');
    const treeToggleBtn = document.getElementById('tm-tree-toggle');

    closeBtn.onclick = closeTaskManager;
    filterInput.oninput = (e) => {
        taskManagerFilter = e.target.value.toLowerCase();
        renderFailedServices();
        renderStoppedServices();
        renderTaskManagerTable();
    };
    sortSelect.onchange = (e) => {
        taskManagerSortColumn = e.target.value;
        renderTaskManagerTable();
    };
    refreshBtn.onclick = () => refreshTaskManager({ includeFailed: true });

    // Expand/collapse stats
    expandStatsBtn.onclick = () => {
        taskManagerStatsExpanded = !taskManagerStatsExpanded;
        document.getElementById('tm-stats-detail').classList.toggle('hidden', !taskManagerStatsExpanded);
        expandStatsBtn.classList.toggle('expanded', taskManagerStatsExpanded);
    };

    // Tree view toggle
    treeToggleBtn.onclick = () => {
        taskManagerTreeMode = !taskManagerTreeMode;
        treeToggleBtn.classList.toggle('active', taskManagerTreeMode);
        renderTaskManagerTable();
    };

    // Close on Escape key
    document.addEventListener('keydown', handleTaskManagerKeydown);

    // Set up sortable headers
    document.querySelectorAll('.task-manager-table th[data-sort]').forEach(th => {
        th.onclick = () => {
            const col = th.dataset.sort;
            if (taskManagerSortColumn === col) {
                taskManagerSortAsc = !taskManagerSortAsc;
            } else {
                taskManagerSortColumn = col;
                taskManagerSortAsc = false;
            }
            sortSelect.value = col;
            renderTaskManagerTable();
        };
    });

    // Fetch initial data and start auto-refresh
    renderFailedServices();
    renderStoppedServices();
    refreshTaskManager({ includeFailed: true });
    taskManagerRefreshInterval = setInterval(fetchTaskManagerData, 2000);
}

function closeTaskManager() {
    const overlay = document.getElementById('task-manager-overlay');
    if (overlay) {
        overlay.classList.add('hidden');
    }

    // Clean up
    if (taskManagerRefreshInterval) {
        clearInterval(taskManagerRefreshInterval);
        taskManagerRefreshInterval = null;
    }
    document.removeEventListener('keydown', handleTaskManagerKeydown);
    taskManagerExpandedCommands.clear();
    taskManagerExpandedDetails.clear();
    taskManagerProcessDetails.clear();
    taskManagerOpenMenuPid = null;
    taskManagerOpenServiceMenuPid = null;
    taskManagerTreeMode = false;
    taskManagerStatsExpanded = false;
    hideServiceFailurePopup();

    // Reset UI state
    document.getElementById('tm-stats-detail')?.classList.add('hidden');
    document.getElementById('tm-expand-stats')?.classList.remove('expanded');
    document.getElementById('tm-tree-toggle')?.classList.remove('active');

    // Return focus to terminal input
    document.getElementById('terminal-input')?.focus();
}

function handleTaskManagerKeydown(e) {
    if (e.key === 'Escape') {
        e.preventDefault();
        closeTaskManager();
    }
}

// ── Pager Modal ────────────────────────────────────────────────────────────

let pagerSearchMatches = [];
let pagerSearchIndex = 0;

function openPagerModal(title, content) {
    const overlay = document.getElementById('pager-overlay');
    const titleEl = document.getElementById('pager-title');
    const contentEl = document.getElementById('pager-content');
    const searchEl = document.getElementById('pager-search');
    const countEl = document.getElementById('pager-search-count');
    const closeBtn = document.getElementById('pager-close');
    if (!overlay) return;

    titleEl.textContent = title;
    contentEl.textContent = content;
    pagerSearchMatches = [];
    pagerSearchIndex = 0;
    searchEl.value = '';
    countEl.textContent = '';

    overlay.classList.remove('hidden');
    searchEl.focus();

    closeBtn.onclick = closePagerModal;
    searchEl.oninput = () => pagerSearch(content);
    searchEl.onkeydown = (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            pagerSearchStep(e.shiftKey ? -1 : 1);
        }
    };
    document.addEventListener('keydown', handlePagerKeydown);
}

function closePagerModal() {
    const overlay = document.getElementById('pager-overlay');
    if (overlay) overlay.classList.add('hidden');
    document.removeEventListener('keydown', handlePagerKeydown);
    pagerSearchMatches = [];
    document.getElementById('terminal-input')?.focus();
}

function handlePagerKeydown(e) {
    if (e.key === 'Escape') {
        e.preventDefault();
        closePagerModal();
    }
}

function escapeHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function pagerSearch(rawContent) {
    const contentEl = document.getElementById('pager-content');
    const countEl = document.getElementById('pager-search-count');
    const query = document.getElementById('pager-search').value;
    pagerSearchMatches = [];
    pagerSearchIndex = 0;

    if (!query) {
        contentEl.textContent = rawContent;
        countEl.textContent = '';
        return;
    }

    // Run regex on raw content so <, >, & in the visible text are matched
    // correctly. Then build HTML by escaping segments around each match.
    const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(escaped, 'gi');
    let html = '';
    let lastIndex = 0;
    let matchIndex = 0;
    let match;
    while ((match = re.exec(rawContent)) !== null) {
        html += escapeHtml(rawContent.slice(lastIndex, match.index));
        const cls = matchIndex === 0 ? ' class="pager-mark-current"' : '';
        html += `<mark${cls}>${escapeHtml(match[0])}</mark>`;
        lastIndex = match.index + match[0].length;
        matchIndex++;
    }
    html += escapeHtml(rawContent.slice(lastIndex));
    contentEl.innerHTML = html;

    pagerSearchMatches = Array.from(contentEl.querySelectorAll('mark'));
    countEl.textContent = pagerSearchMatches.length ? `1 / ${pagerSearchMatches.length}` : '0';
    if (pagerSearchMatches.length) {
        pagerSearchMatches[0].scrollIntoView({ block: 'center' });
    }
}

function pagerSearchStep(delta) {
    if (!pagerSearchMatches.length) return;
    pagerSearchMatches[pagerSearchIndex].classList.remove('pager-mark-current');
    pagerSearchIndex = (pagerSearchIndex + delta + pagerSearchMatches.length) % pagerSearchMatches.length;
    const current = pagerSearchMatches[pagerSearchIndex];
    current.classList.add('pager-mark-current');
    current.scrollIntoView({ block: 'center' });
    document.getElementById('pager-search-count').textContent =
        `${pagerSearchIndex + 1} / ${pagerSearchMatches.length}`;
}

// ── Editor Modal ───────────────────────────────────────────────────────────

const EDITOR_DIFF_CELL_LIMIT = 250000;
const EDITOR_DIFF_CONTEXT_LINES = 2;

let editorOriginalContent = '';
let editorSearchMatches = [];
let editorSearchIndex = -1;
let editorPanelMode = 'hidden';
let editorCaseSensitive = false;
let editorWholeWord = false;
let editorRegexMode = false;
let editorSearchError = '';
let editorDiffMode = false;
let editorDiffStats = { added: 0, removed: 0 };
let editorModalInitialized = false;

function setEditorStatus(message, isError = false) {
    const hint = document.getElementById('editor-hint');
    if (!hint) return;
    hint.textContent = message;
    hint.classList.toggle('editor-hint-error', isError);
}

function setEditorBusy(isBusy) {
    const textarea = document.getElementById('editor-content');
    const saveBtn = document.getElementById('editor-save');
    const cancelBtn = document.getElementById('editor-cancel');
    const closeBtn = document.getElementById('editor-close');
    const controlIds = [
        'editor-search-toggle',
        'editor-find-toggle',
        'editor-replace-toggle',
        'editor-diff-toggle',
        'editor-search-close',
        'editor-find-prev',
        'editor-find-next',
        'editor-case-toggle',
        'editor-word-toggle',
        'editor-regex-toggle',
        'editor-replace-one',
        'editor-replace-all',
        'editor-find-input',
        'editor-replace-input',
    ];
    if (textarea) textarea.readOnly = isBusy;
    if (saveBtn) saveBtn.disabled = isBusy;
    if (cancelBtn) cancelBtn.disabled = isBusy;
    if (closeBtn) closeBtn.disabled = isBusy;
    controlIds.forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = isBusy;
    });
}

function getEditorTextarea() {
    return document.getElementById('editor-content');
}

function getEditorFindQuery() {
    return document.getElementById('editor-find-input')?.value || '';
}

function getEditorReplaceValue() {
    return document.getElementById('editor-replace-input')?.value || '';
}

function setEditorSummary(message = '') {
    const summary = document.getElementById('editor-search-summary');
    if (summary) summary.textContent = message;
}

function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function normalizeEditorDiffText(text) {
    return (text || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
}

function isEditorWordChar(char) {
    return !!char && /\w/.test(char);
}

function isEditorWholeWordMatch(content, start, end) {
    return !isEditorWordChar(content[start - 1]) && !isEditorWordChar(content[end]);
}

function buildEditorSearchRegExp(query, { global = true, sticky = false } = {}) {
    if (!query) return { regex: null, error: '' };

    const source = editorRegexMode ? query : escapeRegExp(query);
    const flags = `${global ? 'g' : ''}${editorCaseSensitive ? '' : 'i'}${sticky ? 'y' : ''}`;

    try {
        return { regex: new RegExp(source, flags), error: '' };
    } catch (error) {
        return { regex: null, error: error.message || 'Invalid regular expression' };
    }
}

function collectEditorMatches(content, query) {
    const { regex, error } = buildEditorSearchRegExp(query);
    if (!regex) {
        return { matches: [], error };
    }

    const matches = [];
    let match;

    while ((match = regex.exec(content)) !== null) {
        const text = match[0] || '';
        if (!text) {
            regex.lastIndex += 1;
            continue;
        }

        const start = match.index;
        const end = start + text.length;
        if (editorWholeWord && !isEditorWholeWordMatch(content, start, end)) {
            continue;
        }

        matches.push({
            start,
            end,
            text,
        });
    }

    return { matches, error: '' };
}

function resolveEditorReplacement(content, match, replacement) {
    if (!editorRegexMode) {
        return {
            nextContent: content.slice(0, match.start) + replacement + content.slice(match.end),
            cursor: match.start + replacement.length,
        };
    }

    const query = getEditorFindQuery();
    const { regex } = buildEditorSearchRegExp(query, { global: false, sticky: true });
    if (!regex) {
        return {
            nextContent: content.slice(0, match.start) + replacement + content.slice(match.end),
            cursor: match.start + replacement.length,
        };
    }

    regex.lastIndex = match.start;
    const nextContent = content.replace(regex, replacement);
    const replacedLength = nextContent.length - content.length + (match.end - match.start);
    return {
        nextContent,
        cursor: match.start + replacedLength,
    };
}

function updateEditorToolbarState() {
    const searchPanel = document.getElementById('editor-search-panel');
    const replaceGroup = document.getElementById('editor-replace-group');
    const replaceOneBtn = document.getElementById('editor-replace-one');
    const replaceAllBtn = document.getElementById('editor-replace-all');
    const searchToggleBtn = document.getElementById('editor-search-toggle');
    const findBtn = document.getElementById('editor-find-toggle');
    const replaceBtn = document.getElementById('editor-replace-toggle');
    const diffBtn = document.getElementById('editor-diff-toggle');
    const caseBtn = document.getElementById('editor-case-toggle');
    const wordBtn = document.getElementById('editor-word-toggle');
    const regexBtn = document.getElementById('editor-regex-toggle');
    const textarea = getEditorTextarea();
    const diffView = document.getElementById('editor-diff-view');

    if (searchPanel) searchPanel.classList.toggle('hidden', editorPanelMode === 'hidden');
    if (replaceGroup) replaceGroup.classList.toggle('hidden', editorPanelMode !== 'replace');
    if (replaceOneBtn) replaceOneBtn.classList.toggle('hidden', editorPanelMode !== 'replace');
    if (replaceAllBtn) replaceAllBtn.classList.toggle('hidden', editorPanelMode !== 'replace');
    if (searchToggleBtn) {
        searchToggleBtn.classList.toggle('active', editorPanelMode !== 'hidden');
        searchToggleBtn.textContent = editorPanelMode === 'hidden' ? 'Find / Replace' : 'Hide Search';
    }

    if (findBtn) findBtn.classList.toggle('active', editorPanelMode === 'find');
    if (replaceBtn) replaceBtn.classList.toggle('active', editorPanelMode === 'replace');
    if (caseBtn) caseBtn.classList.toggle('active', editorCaseSensitive);
    if (wordBtn) wordBtn.classList.toggle('active', editorWholeWord);
    if (regexBtn) regexBtn.classList.toggle('active', editorRegexMode);
    if (diffBtn) {
        diffBtn.classList.toggle('active', editorDiffMode);
        diffBtn.textContent = editorDiffMode ? 'Back to Edit' : 'View Changes';
    }

    if (textarea) textarea.classList.toggle('hidden', editorDiffMode);
    if (diffView) diffView.classList.toggle('hidden', !editorDiffMode);
}

function updateEditorSummary() {
    if (editorDiffMode) {
        if (editorDiffStats.added === 0 && editorDiffStats.removed === 0) {
            setEditorSummary('No changes yet');
            return;
        }
        const parts = [];
        if (editorDiffStats.added > 0) {
            parts.push(`${editorDiffStats.added} addition${editorDiffStats.added === 1 ? '' : 's'}`);
        }
        if (editorDiffStats.removed > 0) {
            parts.push(`${editorDiffStats.removed} deletion${editorDiffStats.removed === 1 ? '' : 's'}`);
        }
        setEditorSummary(parts.join(' · '));
        return;
    }

    const query = getEditorFindQuery();
    if (editorPanelMode === 'hidden') {
        setEditorSummary('');
    } else if (editorSearchError) {
        setEditorSummary('Invalid regex');
    } else if (!query) {
        setEditorSummary(editorPanelMode === 'replace' ? 'Find text to replace' : 'Type to search');
    } else if (editorSearchMatches.length === 0) {
        setEditorSummary('No matches');
    } else {
        setEditorSummary(`${editorSearchIndex + 1} of ${editorSearchMatches.length}`);
    }
}

function focusEditorMatch(index, { focus = true } = {}) {
    const textarea = getEditorTextarea();
    if (!textarea || editorSearchMatches.length === 0) return false;

    editorSearchIndex = (index + editorSearchMatches.length) % editorSearchMatches.length;
    const match = editorSearchMatches[editorSearchIndex];
    if (!match) return false;

    if (focus) textarea.focus();
    textarea.setSelectionRange(match.start, match.end);
    updateEditorSummary();
    return true;
}

function syncEditorSelectionToSearch() {
    const textarea = getEditorTextarea();
    if (!textarea || editorSearchMatches.length === 0) return;
    const idx = editorSearchMatches.findIndex(
        match => match.start === textarea.selectionStart && match.end === textarea.selectionEnd
    );
    if (idx !== -1) {
        editorSearchIndex = idx;
        updateEditorSummary();
    }
}

function refreshEditorSearch({ revealCurrent = false } = {}) {
    const textarea = getEditorTextarea();
    if (!textarea) return;

    const query = getEditorFindQuery();
    if (!query) {
        editorSearchMatches = [];
        editorSearchIndex = -1;
        editorSearchError = '';
        updateEditorSummary();
        return;
    }

    const previousMatch = editorSearchMatches[editorSearchIndex] || null;
    const { matches, error } = collectEditorMatches(textarea.value, query);
    editorSearchMatches = matches;
    editorSearchError = error;

    if (editorSearchError) {
        editorSearchIndex = -1;
        updateEditorSummary();
        return;
    }

    if (editorSearchMatches.length === 0) {
        editorSearchIndex = -1;
        updateEditorSummary();
        return;
    }

    let nextIndex = editorSearchMatches.findIndex(
        match => match.start === textarea.selectionStart && match.end === textarea.selectionEnd
    );

    if (nextIndex === -1 && previousMatch) {
        nextIndex = editorSearchMatches.findIndex(
            match => match.start === previousMatch.start && match.end === previousMatch.end
        );
    }

    if (nextIndex === -1) {
        nextIndex = editorSearchMatches.findIndex(match => match.start >= textarea.selectionStart);
    }

    if (nextIndex === -1) {
        nextIndex = 0;
    }

    editorSearchIndex = nextIndex;

    if (revealCurrent && !editorDiffMode) {
        focusEditorMatch(editorSearchIndex, { focus: false });
    } else {
        updateEditorSummary();
    }
}

function setEditorPanelMode(mode) {
    editorPanelMode = mode;
    if (mode !== 'hidden' && editorDiffMode) {
        setEditorDiffMode(false);
    }
    updateEditorToolbarState();
    refreshEditorSearch();

    const target = mode === 'replace'
        ? document.getElementById('editor-replace-input')
        : document.getElementById('editor-find-input');
    if (mode !== 'hidden' && target) {
        target.focus();
        target.select?.();
    }
}

function stepEditorSearch(delta) {
    if (editorDiffMode) {
        setEditorDiffMode(false);
    }

    refreshEditorSearch();
    if (editorSearchError || editorSearchMatches.length === 0) return;

    const baseIndex = editorSearchIndex === -1 ? (delta < 0 ? editorSearchMatches.length - 1 : 0) : editorSearchIndex;
    focusEditorMatch(baseIndex + delta);
}

function replaceCurrentEditorMatch() {
    const textarea = getEditorTextarea();
    const query = getEditorFindQuery();
    if (!textarea || !query) return;

    if (editorDiffMode) {
        setEditorDiffMode(false);
    }

    refreshEditorSearch();
    if (editorSearchError || editorSearchMatches.length === 0) return;

    const match = editorSearchMatches[editorSearchIndex === -1 ? 0 : editorSearchIndex];
    if (!match) return;

    const replacement = getEditorReplaceValue();
    const { nextContent, cursor } = resolveEditorReplacement(textarea.value, match, replacement);
    textarea.value = nextContent;
    textarea.focus();
    textarea.setSelectionRange(cursor, cursor);
    editorSearchIndex = -1;
    setEditorStatus('Ctrl+Enter to save · Esc to cancel');
    refreshEditorSearch({ revealCurrent: true });
}

function replaceAllEditorMatches() {
    const textarea = getEditorTextarea();
    const query = getEditorFindQuery();
    if (!textarea || !query) return;

    refreshEditorSearch();
    if (editorSearchError || editorSearchMatches.length === 0) return;

    const replacement = getEditorReplaceValue();
    const matchCount = editorSearchMatches.length;
    let nextContent = textarea.value;

    for (let index = editorSearchMatches.length - 1; index >= 0; index -= 1) {
        const match = editorSearchMatches[index];
        nextContent = resolveEditorReplacement(nextContent, match, replacement).nextContent;
    }

    textarea.value = nextContent;
    textarea.focus();
    textarea.setSelectionRange(0, 0);
    setEditorStatus(
        `Replaced ${matchCount} match${matchCount === 1 ? '' : 'es'} · Ctrl+Enter to save · Esc to cancel`
    );
    refreshEditorSearch();
}

function buildEditorFallbackDiff(originalLines, modifiedLines) {
    const lines = [];
    let added = 0;
    let removed = 0;
    let start = 0;
    while (
        start < originalLines.length &&
        start < modifiedLines.length &&
        originalLines[start] === modifiedLines[start]
    ) {
        lines.push({ type: 'context', text: originalLines[start] });
        start += 1;
    }

    let originalEnd = originalLines.length - 1;
    let modifiedEnd = modifiedLines.length - 1;
    while (
        originalEnd >= start &&
        modifiedEnd >= start &&
        originalLines[originalEnd] === modifiedLines[modifiedEnd]
    ) {
        originalEnd -= 1;
        modifiedEnd -= 1;
    }

    const changedOriginal = originalLines.slice(start, originalEnd + 1);
    const changedModified = modifiedLines.slice(start, modifiedEnd + 1);
    const buildLineIndexMap = (entries) => {
        const indexMap = new Map();
        entries.forEach((entry, index) => {
            const positions = indexMap.get(entry);
            if (positions) {
                positions.push(index);
            } else {
                indexMap.set(entry, [index]);
            }
        });
        return indexMap;
    };
    const findNextLineIndex = (positions, fromIndex) => {
        if (!positions) return -1;
        let left = 0;
        let right = positions.length;
        while (left < right) {
            const mid = (left + right) >> 1;
            if (positions[mid] < fromIndex) {
                left = mid + 1;
            } else {
                right = mid;
            }
        }
        return left < positions.length ? positions[left] : -1;
    };
    const originalIndexMap = buildLineIndexMap(changedOriginal);
    const modifiedIndexMap = buildLineIndexMap(changedModified);
    let originalIndex = 0;
    let modifiedIndex = 0;

    while (originalIndex < changedOriginal.length && modifiedIndex < changedModified.length) {
        const before = changedOriginal[originalIndex];
        const after = changedModified[modifiedIndex];
        if (before === after) {
            lines.push({ type: 'context', text: before });
            originalIndex += 1;
            modifiedIndex += 1;
            continue;
        }

        const nextOriginalIndex = findNextLineIndex(originalIndexMap.get(after), originalIndex + 1);
        const nextModifiedIndex = findNextLineIndex(modifiedIndexMap.get(before), modifiedIndex + 1);

        if (nextOriginalIndex === -1 && nextModifiedIndex === -1) {
            removed += 1;
            added += 1;
            lines.push({ type: 'removed', text: before });
            lines.push({ type: 'added', text: after });
            originalIndex += 1;
            modifiedIndex += 1;
            continue;
        }

        const originalOffset = nextOriginalIndex === -1 ? Infinity : nextOriginalIndex - originalIndex;
        const modifiedOffset = nextModifiedIndex === -1 ? Infinity : nextModifiedIndex - modifiedIndex;

        if (originalOffset <= modifiedOffset) {
            while (originalIndex < nextOriginalIndex) {
                removed += 1;
                lines.push({ type: 'removed', text: changedOriginal[originalIndex] });
                originalIndex += 1;
            }
            continue;
        }

        while (modifiedIndex < nextModifiedIndex) {
            added += 1;
            lines.push({ type: 'added', text: changedModified[modifiedIndex] });
            modifiedIndex += 1;
        }
    }

    while (originalIndex < changedOriginal.length) {
        removed += 1;
        lines.push({ type: 'removed', text: changedOriginal[originalIndex] });
        originalIndex += 1;
    }

    while (modifiedIndex < changedModified.length) {
        added += 1;
        lines.push({ type: 'added', text: changedModified[modifiedIndex] });
        modifiedIndex += 1;
    }

    for (let i = originalEnd + 1; i < originalLines.length; i++) {
        lines.push({ type: 'context', text: originalLines[i] });
    }

    return { lines, added, removed };
}

function buildEditorDiff(originalText, modifiedText) {
    const originalLines = normalizeEditorDiffText(originalText).split('\n');
    const modifiedLines = normalizeEditorDiffText(modifiedText).split('\n');

    if (originalLines.length * modifiedLines.length > EDITOR_DIFF_CELL_LIMIT) {
        return buildEditorFallbackDiff(originalLines, modifiedLines);
    }

    const dp = Array.from(
        { length: originalLines.length + 1 },
        () => new Array(modifiedLines.length + 1).fill(0)
    );

    for (let i = originalLines.length - 1; i >= 0; i--) {
        for (let j = modifiedLines.length - 1; j >= 0; j--) {
            if (originalLines[i] === modifiedLines[j]) {
                dp[i][j] = dp[i + 1][j + 1] + 1;
            } else {
                dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
            }
        }
    }

    const lines = [];
    let added = 0;
    let removed = 0;
    let i = 0;
    let j = 0;

    while (i < originalLines.length && j < modifiedLines.length) {
        if (originalLines[i] === modifiedLines[j]) {
            lines.push({ type: 'context', text: originalLines[i] });
            i += 1;
            j += 1;
        } else if (dp[i + 1][j] >= dp[i][j + 1]) {
            removed += 1;
            lines.push({ type: 'removed', text: originalLines[i] });
            i += 1;
        } else {
            added += 1;
            lines.push({ type: 'added', text: modifiedLines[j] });
            j += 1;
        }
    }

    while (i < originalLines.length) {
        removed += 1;
        lines.push({ type: 'removed', text: originalLines[i] });
        i += 1;
    }

    while (j < modifiedLines.length) {
        added += 1;
        lines.push({ type: 'added', text: modifiedLines[j] });
        j += 1;
    }

    return { lines, added, removed };
}

function compressEditorDiffLines(lines) {
    const changedIndices = [];
    lines.forEach((line, index) => {
        if (line.type !== 'context') {
            changedIndices.push(index);
        }
    });

    if (changedIndices.length === 0) {
        return [];
    }

    const keep = new Array(lines.length).fill(false);
    changedIndices.forEach(index => {
        const start = Math.max(0, index - EDITOR_DIFF_CONTEXT_LINES);
        const end = Math.min(lines.length - 1, index + EDITOR_DIFF_CONTEXT_LINES);
        for (let pos = start; pos <= end; pos++) {
            keep[pos] = true;
        }
    });

    const compressed = [];
    let index = 0;
    while (index < lines.length) {
        if (keep[index]) {
            compressed.push(lines[index]);
            index += 1;
            continue;
        }

        const start = index;
        while (index < lines.length && !keep[index]) {
            index += 1;
        }
        const skipped = index - start;
        if (skipped > 0) {
            compressed.push({
                type: 'separator',
                text: `${skipped} unchanged line${skipped === 1 ? '' : 's'}`,
            });
        }
    }

    return compressed;
}

function renderEditorDiffView() {
    const diffView = document.getElementById('editor-diff-view');
    const textarea = getEditorTextarea();
    if (!diffView || !textarea) return;

    const diff = buildEditorDiff(editorOriginalContent, textarea.value);
    editorDiffStats = { added: diff.added, removed: diff.removed };

    if (diff.added === 0 && diff.removed === 0) {
        diffView.innerHTML = '<div class="editor-diff-empty">No changes yet.</div>';
        updateEditorSummary();
        return;
    }

    const renderedLines = compressEditorDiffLines(diff.lines);
    diffView.innerHTML = renderedLines.map(line => {
        if (line.type === 'separator') {
            return `
                <div class="editor-diff-line editor-diff-line-separator">
                    <span class="editor-diff-marker">…</span>
                    <span class="editor-diff-code">${escapeHtml(line.text)}</span>
                </div>
            `;
        }

        const marker = line.type === 'added' ? '+' : line.type === 'removed' ? '-' : ' ';
        return `
            <div class="editor-diff-line editor-diff-line-${line.type}">
                <span class="editor-diff-marker">${marker}</span>
                <span class="editor-diff-code">${escapeHtml(line.text)}</span>
            </div>
        `;
    }).join('');

    updateEditorSummary();
}

function setEditorDiffMode(enabled) {
    editorDiffMode = enabled;
    if (enabled) {
        editorPanelMode = 'hidden';
        renderEditorDiffView();
    }
    updateEditorToolbarState();
    updateEditorSummary();
    if (!enabled) {
        getEditorTextarea()?.focus();
    }
}

function initializeEditorModal() {
    if (editorModalInitialized) return;
    editorModalInitialized = true;

    const textarea = getEditorTextarea();
    const findInput = document.getElementById('editor-find-input');
    const replaceInput = document.getElementById('editor-replace-input');

    textarea?.addEventListener('input', () => {
        setEditorStatus('Ctrl+Enter to save · Esc to cancel');
        refreshEditorSearch();
        if (editorDiffMode) {
            renderEditorDiffView();
        }
    });
    textarea?.addEventListener('click', syncEditorSelectionToSearch);
    textarea?.addEventListener('keyup', syncEditorSelectionToSearch);

    findInput?.addEventListener('input', () => refreshEditorSearch({ revealCurrent: true }));
    replaceInput?.addEventListener('input', () => updateEditorSummary());

    findInput?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            stepEditorSearch(e.shiftKey ? -1 : 1);
        }
    });
    replaceInput?.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
            e.preventDefault();
            replaceCurrentEditorMatch();
        }
    });

    document.getElementById('editor-find-toggle')?.addEventListener('click', () => {
        setEditorPanelMode(editorPanelMode === 'find' ? 'hidden' : 'find');
    });
    document.getElementById('editor-replace-toggle')?.addEventListener('click', () => {
        setEditorPanelMode(editorPanelMode === 'replace' ? 'hidden' : 'replace');
    });
    document.getElementById('editor-search-toggle')?.addEventListener('click', () => {
        setEditorPanelMode(editorPanelMode === 'hidden' ? 'find' : 'hidden');
    });
    document.getElementById('editor-diff-toggle')?.addEventListener('click', () => {
        setEditorDiffMode(!editorDiffMode);
    });
    document.getElementById('editor-search-close')?.addEventListener('click', () => {
        setEditorPanelMode('hidden');
        getEditorTextarea()?.focus();
    });
    document.getElementById('editor-case-toggle')?.addEventListener('click', () => {
        editorCaseSensitive = !editorCaseSensitive;
        refreshEditorSearch({ revealCurrent: true });
        updateEditorToolbarState();
        updateEditorSummary();
    });
    document.getElementById('editor-word-toggle')?.addEventListener('click', () => {
        editorWholeWord = !editorWholeWord;
        refreshEditorSearch({ revealCurrent: true });
        updateEditorToolbarState();
        updateEditorSummary();
    });
    document.getElementById('editor-regex-toggle')?.addEventListener('click', () => {
        editorRegexMode = !editorRegexMode;
        refreshEditorSearch({ revealCurrent: true });
        updateEditorToolbarState();
        updateEditorSummary();
    });
    document.getElementById('editor-find-prev')?.addEventListener('click', () => stepEditorSearch(-1));
    document.getElementById('editor-find-next')?.addEventListener('click', () => stepEditorSearch(1));
    document.getElementById('editor-replace-one')?.addEventListener('click', replaceCurrentEditorMatch);
    document.getElementById('editor-replace-all')?.addEventListener('click', replaceAllEditorMatches);
}

function openEditorModal(title, content) {
    initializeEditorModal();

    const overlay = document.getElementById('editor-overlay');
    if (!overlay) return;

    document.getElementById('editor-title').textContent = title;
    const textarea = getEditorTextarea();
    const findInput = document.getElementById('editor-find-input');
    const replaceInput = document.getElementById('editor-replace-input');
    const diffView = document.getElementById('editor-diff-view');

    editorOriginalContent = content;
    editorSearchMatches = [];
    editorSearchIndex = -1;
    editorPanelMode = 'hidden';
    editorCaseSensitive = false;
    editorWholeWord = false;
    editorRegexMode = false;
    editorSearchError = '';
    editorDiffMode = false;
    editorDiffStats = { added: 0, removed: 0 };

    textarea.value = content;
    if (findInput) findInput.value = '';
    if (replaceInput) replaceInput.value = '';
    if (diffView) diffView.innerHTML = '<div class="editor-diff-empty">No changes yet.</div>';

    setEditorStatus('Ctrl+Enter to save · Esc to cancel');
    setEditorBusy(false);
    updateEditorToolbarState();
    updateEditorSummary();
    overlay.classList.remove('hidden');
    textarea.focus();
    textarea.setSelectionRange(0, 0);
    textarea.scrollTop = 0;

    document.getElementById('editor-save').onclick = () => submitEditorModal(true);
    document.getElementById('editor-cancel').onclick = () => submitEditorModal(false);
    document.getElementById('editor-close').onclick = () => submitEditorModal(false);
    document.addEventListener('keydown', handleEditorKeydown);
}

function closeEditorModal() {
    const overlay = document.getElementById('editor-overlay');
    if (overlay) overlay.classList.add('hidden');
    editorPanelMode = 'hidden';
    editorWholeWord = false;
    editorRegexMode = false;
    editorDiffMode = false;
    editorSearchMatches = [];
    editorSearchIndex = -1;
    editorSearchError = '';
    editorDiffStats = { added: 0, removed: 0 };
    updateEditorToolbarState();
    updateEditorSummary();
    setEditorBusy(false);
    document.removeEventListener('keydown', handleEditorKeydown);
    document.getElementById('terminal-input')?.focus();
}

function handleEditorKeydown(e) {
    if (e.key === 'Escape') {
        if (editorDiffMode) {
            e.preventDefault();
            setEditorDiffMode(false);
            return;
        }
        if (editorPanelMode !== 'hidden') {
            e.preventDefault();
            setEditorPanelMode('hidden');
            getEditorTextarea()?.focus();
            return;
        }
        e.preventDefault();
        submitEditorModal(false);
    } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        submitEditorModal(true);
    }
}

async function submitEditorModal(saved) {
    if (!socket || !socket.id || !terminalToken) {
        setEditorStatus('Connection lost. Reconnect before trying again.', true);
        return;
    }

    const content = getEditorTextarea().value;
    setEditorBusy(true);
    setEditorStatus(saved ? 'Saving...' : 'Cancelling...');

    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';
    const body = new URLSearchParams({
        session_id: socket.id,
        saved: saved ? 'true' : 'false',
        content: saved ? content : '',
    });

    try {
        const resp = await fetch(`${basePath}/terminal/editor_response`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded',
                'X-Terminal-Token': terminalToken,
            },
            body: body.toString(),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || !data.ok) {
            setEditorBusy(false);
            setEditorStatus(data.error || 'Could not send the editor response. Try again.', true);
            return;
        }
    } catch (_err) {
        setEditorBusy(false);
        setEditorStatus('Could not reach the server. Try again.', true);
        return;
    }

    closeEditorModal();
}

async function refreshTaskManager(options = {}) {
    const { includeFailed = false } = options;
    if (includeFailed) {
        await Promise.all([fetchTaskManagerData(), fetchFailedServices()]);
    } else {
        await fetchTaskManagerData();
    }
}

async function fetchTaskManagerData() {
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    try {
        const resp = await fetch(`${basePath}/terminal/processes`);
        if (!resp.ok) {
            console.error('Failed to fetch processes:', resp.status);
            return;
        }

        const data = await resp.json();

        // Update main stats with bars
        const cpuPercent = data.cpu_percent || 0;
        document.getElementById('tm-cpu').textContent = `${cpuPercent}%`;
        document.getElementById('tm-cpu-bar').style.width = `${cpuPercent}%`;

        const mem = data.memory;
        document.getElementById('tm-memory').textContent = `${mem.percent}%`;
        document.getElementById('tm-mem-bar').style.width = `${mem.percent}%`;

        // Update expanded stats if visible
        if (taskManagerStatsExpanded) {
            updateExpandedStats(data);
        }

        // Store current user and processes, then render
        taskManagerCurrentUser = data.current_user || null;
        taskManagerProcesses = data.processes || [];
        syncStoppedServicesWithProcesses();
        renderStoppedServices();
        renderTaskManagerTable();

        // Refresh details for expanded rows
        taskManagerExpandedDetails.forEach(pid => {
            fetchProcessDetails(pid, true);
        });
    } catch (e) {
        console.error('Failed to fetch task manager data:', e);
    }
}

function updateExpandedStats(data) {
    // System info
    document.getElementById('tm-uptime').textContent = data.uptime?.formatted || '-';
    const load = data.load;
    if (load) {
        document.getElementById('tm-load').textContent = `${load['1min']} / ${load['5min']} / ${load['15min']}`;
    }
    const counts = data.process_counts;
    if (counts) {
        document.getElementById('tm-proc-counts').textContent =
            `${counts.total} (${counts.running}R ${counts.sleeping}S ${counts.zombie}Z)`;
    }

    // CPU breakdown
    const cpu = data.cpu;
    if (cpu?.breakdown) {
        document.getElementById('tm-cpu-user').textContent = `${cpu.breakdown.user}%`;
        document.getElementById('tm-cpu-system').textContent = `${cpu.breakdown.system}%`;
        document.getElementById('tm-cpu-iowait').textContent = `${cpu.breakdown.iowait}%`;
    }

    // Memory breakdown
    const mem = data.memory;
    if (mem) {
        document.getElementById('tm-mem-used').textContent = `${mem.used_gb} / ${mem.total_gb} GB`;
        document.getElementById('tm-mem-buffers').textContent = `${mem.buffers_mb} MB`;
        document.getElementById('tm-mem-cached').textContent = `${mem.cached_mb} MB`;
        document.getElementById('tm-swap').textContent =
            `${mem.swap_used_gb} / ${mem.swap_total_gb} GB`;
        document.getElementById('tm-swap-bar').style.width = `${mem.swap_percent}%`;
    }

    // Per-core CPU
    const coresEl = document.getElementById('tm-cores');
    if (cpu?.cores && coresEl) {
        coresEl.innerHTML = cpu.cores.map(core => `
            <div class="tm-core-row">
                <span>${core.name.replace('cpu', 'Core ')}</span>
                <div class="tm-bar">
                    <div class="tm-bar-fill core" style="width: ${core.percent}%"></div>
                </div>
                <span class="tm-stat-value">${core.percent}%</span>
            </div>
        `).join('');
    }
}

function tokenizeCommand(command) {
    return command.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];
}

function shouldCollapsePathToken(token) {
    if (!token.includes('/')) return false;
    if (token.startsWith('[') && token.endsWith(']')) return false;

    return /^(?:\/|\.\/|\.\.\/|~\/)/.test(token) || token.indexOf('/') !== token.lastIndexOf('/');
}

function reduceCommandToken(token) {
    const unquoted = token.replace(/^['"]|['"]$/g, '');
    // Preserve option keys: --config=/etc/app.yml → --config=app.yml
    const eqIdx = unquoted.indexOf('=');
    if (eqIdx !== -1) {
        const key = unquoted.slice(0, eqIdx + 1);
        const val = unquoted.slice(eqIdx + 1);
        const basename = shouldCollapsePathToken(val) ? val.split('/').pop() : val;
        return key + (basename || val);
    }
    const basename = shouldCollapsePathToken(unquoted) ? unquoted.split('/').pop() : unquoted;
    return basename || unquoted;
}

function formatCommandLead(command) {
    if (!command) return 'unknown';

    const tokens = tokenizeCommand(command);
    if (tokens.length === 0) return 'unknown';

    const executable = reduceCommandToken(tokens[0]);
    const firstArg = tokens[1] ? reduceCommandToken(tokens[1]) : null;
    return firstArg ? `${executable} ${firstArg}` : executable;
}

function formatCollapsedCommand(command) {
    if (!command) return '';

    const tokens = tokenizeCommand(command);
    if (tokens.length === 0) return command;

    return tokens.map(reduceCommandToken).join(' ');
}

function getStoppedServiceLabel(command) {
    return formatCommandLead(command);
}

function _failedServiceStateKey(service) {
    return `${service.active_state || ''}/${service.result || ''}/${service.short_status || ''}`;
}

function syncDismissedFailedServices() {
    const currentByUnit = new Map(
        taskManagerFailedServices.filter(s => s.unit).map(s => [s.unit, s])
    );
    taskManagerDismissedFailedServices.forEach((stateKey, unit) => {
        const current = currentByUnit.get(unit);
        if (!current) {
            taskManagerDismissedFailedServices.delete(unit);
        } else if (_failedServiceStateKey(current) !== stateKey) {
            taskManagerDismissedFailedServices.delete(unit);
        }
    });
}

function dismissFailedService(unit) {
    const service = taskManagerFailedServices.find(s => s.unit === unit);
    const key = service ? _failedServiceStateKey(service) : '';
    taskManagerDismissedFailedServices.set(unit, key);
    renderFailedServices();
}

async function fetchFailedServices() {
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    if (!socket || !socket.id || !terminalToken) {
        taskManagerFailedServices = [];
        renderFailedServices();
        return [];
    }

    try {
        const resp = await fetch(`${basePath}/terminal/services/failed`, {
            headers: {
                'X-Terminal-Session': socket.id,
                'X-Terminal-Token': terminalToken,
            },
        });
        if (!resp.ok) {
            console.error('Failed to fetch failed services:', resp.status);
            return [];
        }

        const data = await resp.json();
        taskManagerFailedServices = Array.isArray(data.failed_services) ? data.failed_services : [];
        syncDismissedFailedServices();
        renderFailedServices();
        return taskManagerFailedServices;
    } catch (e) {
        console.error('Failed to fetch failed services:', e);
        return [];
    }
}

async function fetchServiceFailureDetails(unit) {
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    if (!socket || !socket.id || !terminalToken) {
        throw new Error('No active terminal session');
    }

    const resp = await fetch(`${basePath}/terminal/service/failure?unit=${encodeURIComponent(unit)}`, {
        headers: {
            'X-Terminal-Session': socket.id,
            'X-Terminal-Token': terminalToken,
        },
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
        throw new Error(data.error || 'Failed to fetch failure details');
    }
    return data;
}

function showServiceFailurePopup(title, details = {}) {
    const overlay = document.getElementById('service-failure-overlay');
    if (!overlay) return;

    const unit = details.unit || 'Unknown service';
    const state = details.active_state || 'unknown';
    const result = details.result || '';
    const exitStatus = details.exit_status || '';
    const mainPid = details.main_pid || '';
    const started = details.started || '';
    const finished = details.finished || '';
    const restartCount = details.restart_count || '0';
    const logExcerpt = details.log_excerpt || '';

    document.getElementById('service-failure-title').textContent = title || 'Service Needs Attention';
    document.getElementById('service-failure-unit').textContent = unit;
    document.getElementById('service-failure-state').textContent = state;
    document.getElementById('service-failure-result').textContent = result || 'none';

    // Conditionally show exit status row
    const exitRow = document.getElementById('service-failure-exit-row');
    const exitEl = document.getElementById('service-failure-exit');
    if (exitStatus && exitStatus !== '0') {
        exitEl.textContent = exitStatus;
        exitRow.classList.remove('hidden');
    } else {
        exitRow.classList.add('hidden');
    }

    // Conditionally show PID row
    const pidRow = document.getElementById('service-failure-pid-row');
    const pidEl = document.getElementById('service-failure-pid');
    if (mainPid && mainPid !== '0') {
        pidEl.textContent = mainPid;
        pidRow.classList.remove('hidden');
    } else {
        pidRow.classList.add('hidden');
    }

    // Conditionally show started row
    const startedRow = document.getElementById('service-failure-started-row');
    const startedEl = document.getElementById('service-failure-started');
    if (started) {
        startedEl.textContent = started;
        startedRow.classList.remove('hidden');
    } else {
        startedRow.classList.add('hidden');
    }

    // Conditionally show finished row
    const finishedRow = document.getElementById('service-failure-finished-row');
    const finishedEl = document.getElementById('service-failure-finished');
    if (finished) {
        finishedEl.textContent = finished;
        finishedRow.classList.remove('hidden');
    } else {
        finishedRow.classList.add('hidden');
    }

    // Conditionally show restart count row
    const restartsRow = document.getElementById('service-failure-restarts-row');
    const restartsEl = document.getElementById('service-failure-restarts');
    if (restartCount && restartCount !== '0') {
        restartsEl.textContent = restartCount;
        restartsRow.classList.remove('hidden');
    } else {
        restartsRow.classList.add('hidden');
    }

    const logEl = document.getElementById('service-failure-log');
    if (logExcerpt) {
        logEl.textContent = logExcerpt;
        logEl.classList.remove('hidden');
    } else {
        logEl.textContent = '';
        logEl.classList.add('hidden');
    }

    // Reset extra logs container
    const extraLogsEl = document.getElementById('service-failure-extra-logs');
    if (extraLogsEl) {
        extraLogsEl.innerHTML = '';
        extraLogsEl.classList.add('hidden');
    }

    // Store current unit for "Find more logs" button
    overlay.dataset.currentUnit = unit;

    // Show/hide and wire "Edit service config" button
    const editConfigBtn = document.getElementById('service-failure-edit-config');
    if (editConfigBtn) {
        const fragmentPath = details.fragment_path || '';
        if (fragmentPath) {
            const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';
            editConfigBtn.onclick = () => handleEditorRedirect(`${basePath}/diff?file=${encodeURIComponent(fragmentPath)}`);
            editConfigBtn.classList.remove('hidden');
        } else {
            editConfigBtn.classList.add('hidden');
        }
    }

    overlay.classList.remove('hidden');
}

async function findMoreServiceLogs() {
    const overlay = document.getElementById('service-failure-overlay');
    const unit = overlay?.dataset.currentUnit;
    if (!unit) return;

    const extraLogsEl = document.getElementById('service-failure-extra-logs');
    const findLogsBtn = document.getElementById('service-failure-find-logs');
    if (!extraLogsEl) return;

    // Show loading state
    findLogsBtn.disabled = true;
    findLogsBtn.textContent = 'Searching...';
    extraLogsEl.innerHTML = '<p class="service-log-searching">Scanning for log files...</p>';
    extraLogsEl.classList.remove('hidden');

    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    try {
        const resp = await fetch(`${basePath}/terminal/service/logs?unit=${encodeURIComponent(unit)}`, {
            headers: {
                'X-Terminal-Session': socket?.id || '',
                'X-Terminal-Token': terminalToken || '',
            },
        });
        const data = await resp.json();

        if (!resp.ok) {
            extraLogsEl.innerHTML = `<p class="service-log-error">Error: ${escapeHtml(data.error || 'Failed to search')}</p>`;
            return;
        }

        if (!data.log_files || data.log_files.length === 0) {
            extraLogsEl.innerHTML = `<p class="service-log-none">No log files found in ${escapeHtml(data.working_directory || 'working directory')}</p>`;
            return;
        }

        // Show list of found log files
        let html = `<p class="service-log-found">Found ${data.log_files.length} log file(s) in ${escapeHtml(data.working_directory)}:</p>`;
        html += '<div class="service-log-list">';
        for (const file of data.log_files) {
            const relPath = file.path.replace(data.working_directory + '/', '');
            html += `<button class="service-log-file-btn" data-path="${escapeHtml(file.path)}" data-unit="${escapeHtml(unit)}">${escapeHtml(relPath)}</button>`;
        }
        html += '</div>';
        html += '<pre id="service-failure-extra-log-content" class="service-failure-log hidden"></pre>';
        extraLogsEl.innerHTML = html;

        // Add click handlers
        extraLogsEl.querySelectorAll('.service-log-file-btn').forEach(btn => {
            btn.onclick = () => loadLogFileContent(btn.dataset.unit, btn.dataset.path);
        });

    } catch (e) {
        extraLogsEl.innerHTML = `<p class="service-log-error">Error: ${escapeHtml(e.message)}</p>`;
    } finally {
        findLogsBtn.disabled = false;
        findLogsBtn.textContent = 'Find more logs';
    }
}

async function loadLogFileContent(unit, filePath) {
    const extraLogContent = document.getElementById('service-failure-extra-log-content');
    if (!extraLogContent) return;

    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    try {
        extraLogContent.textContent = 'Loading...';
        extraLogContent.classList.remove('hidden');

        const resp = await fetch(`${basePath}/terminal/service/logs?unit=${encodeURIComponent(unit)}&file=${encodeURIComponent(filePath)}`, {
            headers: {
                'X-Terminal-Session': socket?.id || '',
                'X-Terminal-Token': terminalToken || '',
            },
        });
        const data = await resp.json();

        if (!resp.ok) {
            extraLogContent.textContent = `Error: ${data.error || 'Failed to read file'}`;
            return;
        }

        extraLogContent.textContent = data.content || '(empty)';
    } catch (e) {
        extraLogContent.textContent = `Error: ${e.message}`;
    }
}

function hideServiceFailurePopup() {
    const overlay = document.getElementById('service-failure-overlay');
    if (overlay) {
        overlay.classList.add('hidden');
        delete overlay.dataset.currentUnit;
    }
}

async function inspectFailedService(unit) {
    const fallback = taskManagerFailedServices.find(service => service.unit === unit) || { unit };
    try {
        const details = await fetchServiceFailureDetails(unit);
        showServiceFailurePopup('Service Needs Attention', details);
    } catch (e) {
        showServiceFailurePopup('Service Needs Attention', fallback, e.message);
    }
}

function syncStoppedServicesWithProcesses() {
    const activeUnits = new Set(
        taskManagerProcesses
            .map(p => p.systemd_unit)
            .filter(Boolean)
    );

    activeUnits.forEach(unit => {
        taskManagerStoppedServices.delete(unit);
    });
}

function rememberStoppedService(unit, command) {
    const current = taskManagerStoppedServices.get(unit);
    const process = taskManagerProcesses.find(p => p.systemd_unit === unit);
    const nextCommand = command || process?.command || current?.command || unit;

    taskManagerStoppedServices.set(unit, {
        unit,
        command: nextCommand,
        label: getStoppedServiceLabel(nextCommand),
    });
    renderStoppedServices();
}

function removeStoppedService(unit) {
    if (taskManagerStoppedServices.delete(unit)) {
        renderStoppedServices();
    }
}

function renderStoppedServices() {
    const container = document.getElementById('tm-stopped-services');
    if (!container) return;

    const services = Array.from(taskManagerStoppedServices.values())
        .filter(service => {
            if (!taskManagerFilter) return true;
            const haystack = `${service.label} ${service.command} ${service.unit}`.toLowerCase();
            return haystack.includes(taskManagerFilter);
        })
        .sort((a, b) => a.label.localeCompare(b.label) || a.unit.localeCompare(b.unit));

    if (services.length === 0) {
        container.innerHTML = '';
        container.classList.add('hidden');
        return;
    }

    container.classList.remove('hidden');
    container.innerHTML = `
        <div class="tm-stopped-header">
            <span>Stopped Services</span>
            <span class="tm-stopped-caption">Saved from this task manager session</span>
        </div>
        <div class="tm-stopped-list">
            ${services.map(service => `
                <div class="tm-stopped-item">
                    <div class="tm-stopped-copy">
                        <div class="tm-stopped-command" title="${escapeHtml(service.command)}">${escapeHtml(service.label)}</div>
                        <div class="tm-stopped-service">Service <span class="tm-unit">${escapeHtml(service.unit)}</span></div>
                    </div>
                    <div class="tm-stopped-actions">
                        <button class="tm-stopped-btn tm-stopped-start" data-unit="${escapeHtml(service.unit)}" title="Start service">Start</button>
                        <button class="tm-stopped-btn tm-stopped-remove" data-unit="${escapeHtml(service.unit)}" title="Dismiss entry">Dismiss</button>
                    </div>
                </div>
            `).join('')}
        </div>
    `;

    container.querySelectorAll('.tm-stopped-start').forEach(btn => {
        btn.onclick = (e) => {
            e.stopPropagation();
            controlService(btn.dataset.unit, 'start', {
                includeFailedRefresh: true,
                daemonReload: true,
            });
        };
    });

    container.querySelectorAll('.tm-stopped-remove').forEach(btn => {
        btn.onclick = (e) => {
            e.stopPropagation();
            removeStoppedService(btn.dataset.unit);
        };
    });
}

function renderFailedServices() {
    const container = document.getElementById('tm-failed-services');
    if (!container) return;

    const services = taskManagerFailedServices
        .filter(service => service.unit && !taskManagerDismissedFailedServices.has(service.unit))
        .filter(service => {
            if (!taskManagerFilter) return true;
            const haystack = `${service.unit} ${service.short_status || ''} ${service.reason_preview || ''} ${service.description || ''}`.toLowerCase();
            return haystack.includes(taskManagerFilter);
        })
        .sort((a, b) => a.unit.localeCompare(b.unit));

    if (services.length === 0) {
        container.innerHTML = '';
        container.classList.add('hidden');
        return;
    }

    container.classList.remove('hidden');
    container.innerHTML = `
        <div class="tm-failed-header">
            <div>
                <div class="tm-failed-title">Failed Services</div>
                <div class="tm-failed-caption">Systemd reported these units as failed when the task manager opened or last refreshed.</div>
            </div>
            <div class="tm-failed-badge">Needs attention</div>
        </div>
        <div class="tm-failed-list">
            ${services.map(service => `
                <div class="tm-failed-item">
                    <div class="tm-failed-copy" data-unit="${escapeHtml(service.unit)}" title="Show failure details and recent logs">
                        <div class="tm-failed-topline">
                            <span class="tm-failed-unit">${escapeHtml(service.unit)}</span>
                            <span class="tm-failed-status">${escapeHtml(service.short_status || 'failed')}</span>
                        </div>
                        <div class="tm-failed-reason">${escapeHtml(service.reason_preview || service.description || 'Open to inspect recent failure output')}</div>
                    </div>
                    <div class="tm-failed-actions">
                        <button class="tm-failed-btn tm-failed-restart" data-unit="${escapeHtml(service.unit)}" title="Restart service">Restart</button>
                        <button class="tm-failed-btn tm-failed-remove" data-unit="${escapeHtml(service.unit)}" title="Dismiss this entry until the failure state changes">Dismiss</button>
                    </div>
                </div>
            `).join('')}
        </div>
    `;

    container.querySelectorAll('.tm-failed-copy').forEach(copy => {
        copy.onclick = async (e) => {
            e.stopPropagation();
            await inspectFailedService(copy.dataset.unit);
        };
    });

    container.querySelectorAll('.tm-failed-restart').forEach(btn => {
        btn.onclick = (e) => {
            e.stopPropagation();
            taskManagerDismissedFailedServices.delete(btn.dataset.unit);
            controlService(btn.dataset.unit, 'restart', {
                showFailurePopupOnError: true,
                includeFailedRefresh: true,
                daemonReload: true,
            });
        };
    });

    container.querySelectorAll('.tm-failed-remove').forEach(btn => {
        btn.onclick = (e) => {
            e.stopPropagation();
            dismissFailedService(btn.dataset.unit);
        };
    });
}

// Compare two processes by the current sort column and direction
function compareProcessesBySort(a, b) {
    let valA, valB;
    switch (taskManagerSortColumn) {
        case 'pid':
            valA = a.pid;
            valB = b.pid;
            break;
        case 'user':
            valA = a.user.toLowerCase();
            valB = b.user.toLowerCase();
            break;
        case 'state':
            // Concern-based ordering (descending = most concerning first)
            // D(isk wait) > Z(ombie) > X(dead) > T(stopped) > Other > R(unning) > S(leeping) > I(dle)
            // Higher rank = sorted first in descending mode
            const stateDescRank = { D: 7, Z: 6, X: 5, T: 4, R: 2, S: 1, I: 0 };
            // Active-based ordering (ascending = most active first)
            // R(unning) > D(isk wait) > S(leeping) > I(dle) > T(stopped) > Z(ombie) > X(dead) > Other
            // Lower rank = sorted first in ascending mode
            const stateAscRank = { R: 0, D: 1, S: 2, I: 3, T: 4, Z: 5, X: 6 };
            const ranks = taskManagerSortAsc ? stateAscRank : stateDescRank;
            const fallback = taskManagerSortAsc ? 7 : 3;
            valA = ranks[a.state] ?? fallback;
            valB = ranks[b.state] ?? fallback;
            break;
        case 'cpu':
            valA = a.cpu;
            valB = b.cpu;
            break;
        case 'mem':
            valA = a.mem;
            valB = b.mem;
            break;
        case 'name':
            valA = a.command.toLowerCase();
            valB = b.command.toLowerCase();
            break;
        default:
            valA = a.cpu;
            valB = b.cpu;
    }

    if (valA < valB) return taskManagerSortAsc ? -1 : 1;
    if (valA > valB) return taskManagerSortAsc ? 1 : -1;

    // Tiebreaker: when sorting by CPU (or default), use the stickier sort_cpu
    // EMA to keep recently-active processes above truly-idle ones within the
    // same displayed CPU value.
    if (taskManagerSortColumn === 'cpu' || !taskManagerSortColumn) {
        const sa = a.sort_cpu || 0, sb = b.sort_cpu || 0;
        if (sa !== sb) return taskManagerSortAsc ? sa - sb : sb - sa;
    }
    return a.pid - b.pid;
}

function renderTaskManagerTable() {
    const tbody = document.getElementById('tm-process-list');
    if (!tbody) return;

    // Build tree structure or filter/sort flat list
    let displayList;
    if (taskManagerTreeMode) {
        // In tree mode, build tree with optional filter
        displayList = buildProcessTree(taskManagerProcesses, taskManagerFilter);
    } else {
        // Flat view with filter
        let filtered = taskManagerProcesses;
        if (taskManagerFilter) {
            filtered = filtered.filter(p =>
                p.command.toLowerCase().includes(taskManagerFilter) ||
                p.user.toLowerCase().includes(taskManagerFilter) ||
                String(p.pid).includes(taskManagerFilter) ||
                (p.state && p.state.toLowerCase().includes(taskManagerFilter)) ||
                (p.systemd_unit && p.systemd_unit.toLowerCase().includes(taskManagerFilter))
            );
        }
        // Sort processes for flat view
        filtered.sort(compareProcessesBySort);
        displayList = filtered.map(p => ({ ...p, depth: 0 }));
    }

    // Update header sort indicators
    document.querySelectorAll('.task-manager-table th[data-sort]').forEach(th => {
        th.classList.remove('sorted', 'asc');
        if (th.dataset.sort === taskManagerSortColumn) {
            th.classList.add('sorted');
            if (taskManagerSortAsc) th.classList.add('asc');
        }
    });

    // Render rows
    if (displayList.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" class="tm-empty">No processes found</td></tr>';
        return;
    }

    tbody.innerHTML = displayList.map(p => {
        const indent = taskManagerTreeMode ? '<span class="tm-tree-indent"></span>'.repeat(p.depth) : '';
        const branch = taskManagerTreeMode && p.depth > 0 ? '<span class="tm-tree-branch">└─</span>' : '';
        const childClass = taskManagerTreeMode && p.depth > 0 ? ' tm-tree-child' : '';
        const matchClass = p.isMatch === false ? ' tm-tree-ancestor' : '';
        const rootClass = p.user === 'root' ? ' tm-root-process' : '';
        const details = taskManagerProcessDetails.get(p.pid);
        const detailsHtml = details ? formatProcessDetails(details) : '<div class="tm-details">Click ▶ to load details</div>';
        const cmdExpanded = taskManagerExpandedCommands.has(p.pid);
        const detailsExpanded = taskManagerExpandedDetails.has(p.pid);
        const displayCommand = cmdExpanded ? p.command : formatCollapsedCommand(p.command);
        const systemdUnit = p.systemd_unit || null;

        // Service control button (shown only for systemd-managed processes)
        const svcDefault = p.has_reload ? 'reload' : 'restart';
        const svcLabel = p.has_reload ? 'Reload' : 'Restart';
        const serviceBtn = systemdUnit ? `
                <div class="tm-split-btn tm-service-btn" data-pid="${p.pid}" data-unit="${escapeHtml(systemdUnit)}" data-default-action="${svcDefault}">
                    <button class="tm-split-main tm-svc-main" title="${svcLabel} service">${svcLabel}</button>
                    <button class="tm-split-arrow" title="Service actions">&#9662;</button>
                    <div class="tm-split-menu hidden">
                        <div class="tm-menu-header">Service</div>
                        <div class="tm-split-option" data-action="restart">Restart</div>
                        <div class="tm-split-option" data-action="stop">Stop</div>
                        <div class="tm-split-option" data-action="reload">Reload</div>
                        <div class="tm-menu-header">Start at Boot</div>
                        <div class="tm-split-option" data-action="enable">Enable</div>
                        <div class="tm-split-option" data-action="disable">Disable</div>
                    </div>
                </div>
        ` : '';

        return `
        <tr data-pid="${p.pid}" class="${childClass}${matchClass}${rootClass}">
            <td>${p.pid}</td>
            <td>${escapeHtml(p.user)}</td>
            <td class="tm-state tm-state-${p.state || 'S'}" title="${{'R':'Running','S':'Sleeping','D':'Disk Wait','Z':'Zombie','T':'Stopped','I':'Idle'}[p.state] || p.state || ''}">${p.state || ''}</td>
            <td>${p.cpu.toFixed(1)}</td>
            <td>${p.mem.toFixed(1)}</td>
            <td class="tm-command${cmdExpanded ? ' cmd-expanded' : ''}${detailsExpanded ? ' details-expanded' : ''}${systemdUnit ? ' tm-has-service' : ''}">
                <div class="tm-command-wrapper">
                    <button class="tm-expand-details-btn" title="Show process details (ports, I/O, memory)">&#9654;</button>
                    <div class="tm-command-content">
                        <span class="tm-command-text" title="${escapeHtml(p.command)}">${indent}${branch}${escapeHtml(displayCommand)}</span>
                        <div class="tm-details-section">
                            ${detailsHtml}
                        </div>
                    </div>
                </div>
            </td>
            <td class="tm-actions">
                <div class="tm-split-btn" data-pid="${p.pid}">
                    <button class="tm-split-main" title="Send TERM">TERM</button>
                    <button class="tm-split-arrow" title="Choose signal">&#9662;</button>
                    <div class="tm-split-menu hidden">
                        <div class="tm-menu-header">Signal</div>
                        <div class="tm-split-option" data-signal="TERM">TERM <span class="tm-sig-desc">Graceful exit</span></div>
                        <div class="tm-split-option" data-signal="HUP">HUP <span class="tm-sig-desc">Reload/restart</span></div>
                        <div class="tm-split-option" data-signal="INT">INT <span class="tm-sig-desc">Interrupt</span></div>
                        <div class="tm-split-option" data-signal="KILL">KILL <span class="tm-sig-desc">Force kill</span></div>
                        <div class="tm-split-option" data-signal="STOP">STOP <span class="tm-sig-desc">Pause</span></div>
                        <div class="tm-split-option" data-signal="CONT">CONT <span class="tm-sig-desc">Resume</span></div>
                    </div>
                </div>
                ${serviceBtn}
            </td>
        </tr>
    `;
    }).join('');

    // Attach split button handlers
    tbody.querySelectorAll('.tm-split-btn').forEach(container => {
        const pid = parseInt(container.dataset.pid);
        const process = taskManagerProcesses.find(p => p.pid === pid);
        const processOwner = process ? process.user : null;
        const mainBtn = container.querySelector('.tm-split-main');
        const arrowBtn = container.querySelector('.tm-split-arrow');
        const menu = container.querySelector('.tm-split-menu');

        // Restore open menu state after refresh
        if (taskManagerOpenMenuPid === pid) {
            menu.classList.remove('hidden');
        }

        // Main button always sends TERM
        mainBtn.onclick = (e) => {
            e.stopPropagation();
            requestKillProcess(pid, 'TERM', processOwner);
        };

        // Arrow button toggles menu
        arrowBtn.onclick = (e) => {
            e.stopPropagation();
            // Close other menus first
            document.querySelectorAll('.tm-split-menu').forEach(m => {
                if (m !== menu) m.classList.add('hidden');
            });
            const nowHidden = menu.classList.toggle('hidden');
            taskManagerOpenMenuPid = nowHidden ? null : pid;
        };

        // Menu option selection - send signal immediately
        menu.querySelectorAll('.tm-split-option').forEach(opt => {
            opt.onclick = (e) => {
                e.stopPropagation();
                const signal = opt.dataset.signal;
                menu.classList.add('hidden');
                taskManagerOpenMenuPid = null;
                requestKillProcess(pid, signal, processOwner);
            };
        });
    });

    // Attach service control button handlers
    tbody.querySelectorAll('.tm-service-btn').forEach(container => {
        const pid = parseInt(container.dataset.pid);
        const unit = container.dataset.unit;
        const process = taskManagerProcesses.find(p => p.pid === pid);
        const mainBtn = container.querySelector('.tm-svc-main');
        const arrowBtn = container.querySelector('.tm-split-arrow');
        const menu = container.querySelector('.tm-split-menu');

        // Restore open menu state after refresh
        if (taskManagerOpenServiceMenuPid === pid) {
            menu.classList.remove('hidden');
        }

        // Main button sends the default action (reload if supported, else restart)
        const defaultAction = container.dataset.defaultAction || 'restart';
        mainBtn.onclick = (e) => {
            e.stopPropagation();
            controlService(unit, defaultAction, {
                command: process?.command,
                includeFailedRefresh: true,
                showFailurePopupOnError: true,
            });
        };

        // Arrow button toggles menu
        arrowBtn.onclick = (e) => {
            e.stopPropagation();
            document.querySelectorAll('.tm-split-menu').forEach(m => {
                if (m !== menu) m.classList.add('hidden');
            });
            const nowHidden = menu.classList.toggle('hidden');
            taskManagerOpenServiceMenuPid = nowHidden ? null : pid;
            taskManagerOpenMenuPid = null;
        };

        // Menu option selection
        menu.querySelectorAll('.tm-split-option').forEach(opt => {
            opt.onclick = (e) => {
                e.stopPropagation();
                const action = opt.dataset.action;
                menu.classList.add('hidden');
                taskManagerOpenServiceMenuPid = null;
                controlService(unit, action, {
                    command: process?.command,
                    includeFailedRefresh: !['enable', 'disable'].includes(action),
                    showFailurePopupOnError: action === 'restart',
                });
            };
        });
    });

    // Close menus when clicking elsewhere
    document.addEventListener('click', () => {
        document.querySelectorAll('.tm-split-menu').forEach(m => m.classList.add('hidden'));
        taskManagerOpenMenuPid = null;
        taskManagerOpenServiceMenuPid = null;
    }, { once: true });

    // Attach command text click handlers (click text to expand/collapse command)
    tbody.querySelectorAll('.tm-command-text').forEach(span => {
        const row = span.closest('tr');
        const pid = parseInt(row.dataset.pid);

        span.onclick = (e) => {
            e.stopPropagation();
            const wasExpanded = taskManagerExpandedCommands.has(pid);
            if (!wasExpanded) {
                taskManagerExpandedCommands.add(pid);
            } else {
                taskManagerExpandedCommands.delete(pid);
            }
            renderTaskManagerTable();
        };
    });

    // Attach details expand button handlers (▶ button expands details)
    tbody.querySelectorAll('.tm-expand-details-btn').forEach(btn => {
        const row = btn.closest('tr');
        const pid = parseInt(row.dataset.pid);
        const cell = btn.closest('.tm-command');

        btn.onclick = (e) => {
            e.stopPropagation();
            const wasExpanded = cell.classList.contains('details-expanded');
            cell.classList.toggle('details-expanded');
            if (!wasExpanded) {
                taskManagerExpandedDetails.add(pid);
                // Fetch details if not already loaded
                if (!taskManagerProcessDetails.has(pid)) {
                    fetchProcessDetails(pid);
                }
            } else {
                taskManagerExpandedDetails.delete(pid);
            }
        };
    });
}

function buildProcessTree(processes, filter = '') {
    // Build a map of pid -> process
    const byPid = new Map();
    processes.forEach(p => byPid.set(p.pid, { ...p, children: [], depth: 0, isMatch: true }));

    // If filtering, find matching processes and their ancestors
    let matchingPids = new Set();
    if (filter) {
        const lowerFilter = filter.toLowerCase();
        // First pass: find direct matches
        byPid.forEach((p, pid) => {
            if (p.command.toLowerCase().includes(lowerFilter) ||
                p.user.toLowerCase().includes(lowerFilter) ||
                String(pid).includes(lowerFilter) ||
                (p.state && p.state.toLowerCase().includes(lowerFilter)) ||
                (p.systemd_unit && p.systemd_unit.toLowerCase().includes(lowerFilter))) {
                matchingPids.add(pid);
                p.isMatch = true;
            } else {
                p.isMatch = false;
            }
        });

        // Second pass: add all ancestors of matching processes
        const ancestorsToAdd = new Set();
        matchingPids.forEach(pid => {
            let current = byPid.get(pid);
            while (current) {
                const parent = byPid.get(current.ppid);
                if (parent && parent.pid !== current.pid && !matchingPids.has(parent.pid)) {
                    ancestorsToAdd.add(parent.pid);
                }
                current = parent;
            }
        });

        // Mark ancestors (they're visible but not the actual matches)
        ancestorsToAdd.forEach(pid => {
            const p = byPid.get(pid);
            if (p) p.isMatch = false;  // ancestor, not a direct match
        });

        // Combine matches and ancestors
        const visiblePids = new Set([...matchingPids, ...ancestorsToAdd]);

        // Remove processes that aren't visible
        byPid.forEach((_, pid) => {
            if (!visiblePids.has(pid)) {
                byPid.delete(pid);
            }
        });
    }

    // Build tree structure
    const roots = [];
    byPid.forEach(p => {
        const parent = byPid.get(p.ppid);
        if (parent && parent.pid !== p.pid) {
            parent.children.push(p);
        } else {
            roots.push(p);
        }
    });

    // Sort roots by selected column, but prioritize matches when filtering
    roots.sort((a, b) => {
        if (filter) {
            // Matches first
            if (a.isMatch && !b.isMatch) return -1;
            if (!a.isMatch && b.isMatch) return 1;
        }
        return compareProcessesBySort(a, b);
    });

    // Flatten tree to list with depth info
    const result = [];
    function flatten(node, depth) {
        node.depth = depth;
        result.push(node);
        // Sort children by selected column, matches first when filtering
        node.children.sort((a, b) => {
            if (filter) {
                if (a.isMatch && !b.isMatch) return -1;
                if (!a.isMatch && b.isMatch) return 1;
            }
            return compareProcessesBySort(a, b);
        });
        node.children.forEach(child => flatten(child, depth + 1));
    }
    roots.forEach(root => flatten(root, 0));

    return result;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function fetchProcessDetails(pid, isRefresh = false) {
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    // Mark as loading only on first fetch
    if (!isRefresh) {
        taskManagerProcessDetails.set(pid, { loading: true });
        renderTaskManagerTable();
    }

    try {
        const resp = await fetch(`${basePath}/terminal/process/${pid}/details`);
        if (!resp.ok) {
            taskManagerProcessDetails.set(pid, { loading: false, error: 'Failed to fetch' });
            renderTaskManagerTable();
            return;
        }

        const data = await resp.json();
        const needsRateRefresh = data.io?.note === 'Rate available on next refresh';

        taskManagerProcessDetails.set(pid, {
            loading: false,
            ports: data.ports || [],
            connections: data.connections || [],
            io: data.io || null,
            fds: data.fds || null,
            cwd: data.cwd || null,
            threads: data.threads || null,
            memory: data.memory || null,
            systemd: data.systemd || null,
            start_time: data.start_time || null,
            cpu_time: data.cpu_time || null,
        });
        renderTaskManagerTable();

        // If we need a second fetch to get I/O rate, schedule it
        if (needsRateRefresh && taskManagerExpandedDetails.has(pid)) {
            setTimeout(() => {
                if (taskManagerExpandedDetails.has(pid)) {
                    fetchProcessDetails(pid, true);
                }
            }, 1000);
        }
    } catch (e) {
        console.error('Failed to fetch process details:', e);
        taskManagerProcessDetails.set(pid, { loading: false, error: e.message });
        renderTaskManagerTable();
    }
}

function formatProcessDetails(details) {
    if (details.loading) {
        return '<div class="tm-details tm-details-loading">Loading...</div>';
    }
    if (details.error) {
        return `<div class="tm-details tm-details-error">${escapeHtml(details.error)}</div>`;
    }

    const parts = [];

    // Start time and CPU time
    if (details.start_time || details.cpu_time) {
        const items = [];
        if (details.start_time) {
            const d = new Date(details.start_time * 1000);
            const now = new Date();
            const diffMs = now - d;
            const diffMins = Math.floor(diffMs / 60000);
            const diffHrs = Math.floor(diffMs / 3600000);
            const diffDays = Math.floor(diffMs / 86400000);
            const age = diffDays > 0 ? `${diffDays}d ago` : diffHrs > 0 ? `${diffHrs}h ago` : `${diffMins}m ago`;
            const timeStr = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
            items.push(`<span class="tm-detail-label">Started:</span> ${timeStr} (${age})`);
        }
        if (details.cpu_time) {
            items.push(`<span class="tm-detail-label">CPU Time:</span> ${details.cpu_time}`);
        }
        parts.push(`<div class="tm-detail-row">${items.join(' &nbsp;|&nbsp; ')}</div>`);
    }

    // Working directory
    if (details.cwd) {
        parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">CWD:</span> <span class="tm-cwd">${escapeHtml(details.cwd)}</span></div>`);
    }

    // Threads
    if (details.threads && details.threads > 1) {
        parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">Threads:</span> ${details.threads}</div>`);
    }

    // Listening ports
    if (details.ports && details.ports.length > 0) {
        const portList = details.ports.map(p => {
            const addr = p.local_addr === '*' || p.local_addr === '0.0.0.0' || p.local_addr === '::'
                ? `*:${p.local_port}`
                : `${p.local_addr}:${p.local_port}`;
            return `<span class="tm-port">${escapeHtml(addr)}</span>`;
        }).join(' ');
        parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">Ports:</span> ${portList}</div>`);
    }

    // Active connections (show count + sample)
    if (details.connections && details.connections.length > 0) {
        const connCount = details.connections.length;
        const sample = details.connections.slice(0, 3).map(c =>
            `${c.remote_addr}:${c.remote_port}`
        ).join(', ');
        const more = connCount > 3 ? ` (+${connCount - 3} more)` : '';
        parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">Connections:</span> ${escapeHtml(sample)}${more}</div>`);
    }

    // I/O stats
    if (details.io) {
        const io = details.io;
        if (io.read_rate && io.write_rate) {
            parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">I/O Rate:</span> ↓${io.read_rate}/s ↑${io.write_rate}/s</div>`);
            if (io.disk_read_rate !== '0.0B' || io.disk_write_rate !== '0.0B') {
                parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">Disk I/O:</span> ↓${io.disk_read_rate}/s ↑${io.disk_write_rate}/s</div>`);
            }
        } else {
            parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">I/O Total:</span> ↓${io.total_read} ↑${io.total_write}</div>`);
        }
    }

    // Memory breakdown
    if (details.memory) {
        const mem = details.memory;
        parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">Memory:</span> RSS ${mem.rss} | Private ${mem.private} | Shared ${mem.shared}${mem.swap !== '0.0B' ? ` | Swap ${mem.swap}` : ''}</div>`);
    }

    // File descriptors
    if (details.fds) {
        const fdInfo = `${details.fds.count} open`;
        parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">FDs:</span> ${fdInfo}</div>`);
    }

    // Systemd unit info
    if (details.systemd && details.systemd.unit) {
        const enabledBadge = details.systemd.enabled
            ? '<span class="tm-badge tm-badge-enabled">enabled</span>'
            : '<span class="tm-badge tm-badge-disabled">disabled</span>';
        parts.push(`<div class="tm-detail-row"><span class="tm-detail-label">Systemd:</span> <span class="tm-unit">${escapeHtml(details.systemd.unit)}</span> ${enabledBadge}</div>`);
    }

    if (parts.length === 0) {
        parts.push('<div class="tm-detail-row tm-detail-none">No details available</div>');
    }

    return `<div class="tm-details">${parts.join('')}</div>`;
}

async function killProcess(pid, signal, useSudo = false) {
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    // Require active terminal session with valid token
    if (!socket || !socket.id || !terminalToken) {
        appendOutput('Error: No active terminal session\n', 'error');
        return;
    }

    try {
        const resp = await fetch(`${basePath}/terminal/process/kill`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Terminal-Session': socket.id,
                'X-Terminal-Token': terminalToken,
            },
            body: JSON.stringify({ pid, signal, use_sudo: useSudo }),
        });

        const data = await resp.json();

        if (resp.ok && data.success) {
            appendOutput(`${data.message}\n`, 'info');
            // Refresh the process list
            setTimeout(fetchTaskManagerData, 500);
        } else {
            appendOutput(`Error: ${data.error || 'Failed to kill process'}\n`, 'error');
        }
    } catch (e) {
        console.error('Failed to kill process:', e);
        appendOutput(`Error: ${e.message}\n`, 'error');
    }
}

async function controlService(unit, action, options = {}) {
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    // Require active terminal session with valid token
    if (!socket || !socket.id || !terminalToken) {
        appendOutput('Error: No active terminal session\n', 'error');
        return;
    }

    appendOutput(`Service ${unit}: ${action}...\n`, 'info');

    try {
        const resp = await fetch(`${basePath}/terminal/service/control`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Terminal-Session': socket.id,
                'X-Terminal-Token': terminalToken,
            },
            body: JSON.stringify({ unit, action, daemon_reload: options.daemonReload || false }),
        });

        const data = await resp.json();
        const shouldRefreshFailed = options.includeFailedRefresh || !['enable', 'disable'].includes(action);

        if (resp.ok && data.success) {
            if (!['enable', 'disable'].includes(action)) {
                taskManagerDismissedFailedServices.delete(unit);
            }
            if (action === 'stop') {
                rememberStoppedService(unit, options.command);
            }
            appendOutput(`${data.message}\n`, 'info');
            // Refresh process data quickly, but only refresh failed-unit discovery on explicit actions.
            setTimeout(() => {
                fetchTaskManagerData();
                if (shouldRefreshFailed) {
                    fetchFailedServices();
                }
            }, 1000);
        } else {
            appendOutput(`Error: ${data.error || 'Failed to control service'}\n`, 'error');
            if (options.showFailurePopupOnError && action === 'restart') {
                showServiceFailurePopup('Restart Failed', data.service || { unit, reason_preview: data.error }, data.error || 'Failed to restart service');
            }
            if (shouldRefreshFailed) {
                fetchFailedServices();
            }
        }
    } catch (e) {
        console.error('Failed to control service:', e);
        appendOutput(`Error: ${e.message}\n`, 'error');
        if (options.showFailurePopupOnError && action === 'restart') {
            showServiceFailurePopup('Restart Failed', { unit, reason_preview: e.message }, e.message);
        }
    }
}

function showForceKillWarning(pid, signal, processOwner) {
    const overlay = document.getElementById('forcekill-overlay');
    document.getElementById('forcekill-pid').textContent = pid;

    // After force kill confirmation, check if sudo is needed
    // Root can kill any process without sudo
    forceKillCallback = () => {
        const needsSudo = taskManagerCurrentUser
            && taskManagerCurrentUser !== 'root'
            && processOwner !== taskManagerCurrentUser;
        if (needsSudo) {
            showSudoConfirm(pid, signal, processOwner);
        } else {
            killProcess(pid, signal, false);
        }
    };
    overlay.classList.remove('hidden');
}

function hideForceKillWarning() {
    const overlay = document.getElementById('forcekill-overlay');
    overlay.classList.add('hidden');
    forceKillCallback = null;
}

function showSudoConfirm(pid, signal, processOwner) {
    const overlay = document.getElementById('sudo-confirm-overlay');
    document.getElementById('sudo-confirm-pid').textContent = pid;
    document.getElementById('sudo-confirm-owner').textContent = processOwner;
    document.getElementById('sudo-confirm-signal').textContent = 'SIG' + signal;

    sudoConfirmCallback = () => killProcess(pid, signal, true);
    overlay.classList.remove('hidden');
}

function hideSudoConfirm() {
    const overlay = document.getElementById('sudo-confirm-overlay');
    overlay.classList.add('hidden');
    sudoConfirmCallback = null;
}

function requestKillProcess(pid, signal, processOwner) {
    // For SIGKILL, show force kill warning first
    if (signal === 'KILL') {
        showForceKillWarning(pid, signal, processOwner);
        return;
    }

    // For other signals, check if sudo is needed
    // Root can kill any process without sudo
    const needsSudo = taskManagerCurrentUser
        && taskManagerCurrentUser !== 'root'
        && processOwner !== taskManagerCurrentUser;
    if (needsSudo) {
        showSudoConfirm(pid, signal, processOwner);
    } else {
        killProcess(pid, signal, false);
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

    // Set up force kill warning popup handlers
    const forceKillCancelBtn = document.getElementById('forcekill-cancel');
    const forceKillYesBtn = document.getElementById('forcekill-yes');
    if (forceKillCancelBtn) {
        forceKillCancelBtn.onclick = hideForceKillWarning;
    }
    if (forceKillYesBtn) {
        forceKillYesBtn.onclick = () => {
            const callback = forceKillCallback;
            hideForceKillWarning();
            if (callback) {
                callback();
            }
        };
    }

    // Set up sudo confirmation popup handlers
    const sudoCancelBtn = document.getElementById('sudo-confirm-cancel');
    const sudoYesBtn = document.getElementById('sudo-confirm-yes');
    const serviceFailureOverlay = document.getElementById('service-failure-overlay');
    const serviceFailureCloseBtn = document.getElementById('service-failure-close');
    if (sudoCancelBtn) {
        sudoCancelBtn.onclick = hideSudoConfirm;
    }
    if (sudoYesBtn) {
        sudoYesBtn.onclick = () => {
            if (sudoConfirmCallback) {
                sudoConfirmCallback();
            }
            hideSudoConfirm();
        };
    }
    if (serviceFailureCloseBtn) {
        serviceFailureCloseBtn.onclick = hideServiceFailurePopup;
    }
    const serviceFailureFindLogsBtn = document.getElementById('service-failure-find-logs');
    if (serviceFailureFindLogsBtn) {
        serviceFailureFindLogsBtn.onclick = findMoreServiceLogs;
    }
    if (serviceFailureOverlay) {
        serviceFailureOverlay.onclick = (e) => {
            if (e.target === serviceFailureOverlay) {
                hideServiceFailurePopup();
            }
        };
    }

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

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

    socket.on('task_manager_popup', () => {
        openTaskManager();
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
let taskManagerSortColumn = 'cpu';
let taskManagerSortAsc = false;
let taskManagerFilter = '';
let taskManagerExpandedPids = new Set();

function openTaskManager() {
    const overlay = document.getElementById('task-manager-overlay');
    if (!overlay) return;

    overlay.classList.remove('hidden');

    // Set up event listeners
    const closeBtn = document.getElementById('tm-close');
    const filterInput = document.getElementById('tm-filter');
    const sortSelect = document.getElementById('tm-sort');
    const refreshBtn = document.getElementById('tm-refresh');

    closeBtn.onclick = closeTaskManager;
    filterInput.oninput = (e) => {
        taskManagerFilter = e.target.value.toLowerCase();
        renderTaskManagerTable();
    };
    sortSelect.onchange = (e) => {
        taskManagerSortColumn = e.target.value;
        renderTaskManagerTable();
    };
    refreshBtn.onclick = fetchTaskManagerData;

    // Close on overlay click
    overlay.onclick = (e) => {
        if (e.target === overlay) closeTaskManager();
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
    fetchTaskManagerData();
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
    taskManagerExpandedPids.clear();

    // Return focus to terminal input
    document.getElementById('terminal-input')?.focus();
}

function handleTaskManagerKeydown(e) {
    if (e.key === 'Escape') {
        e.preventDefault();
        closeTaskManager();
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

        // Update stats display
        document.getElementById('tm-cpu').textContent = `${data.cpu_percent}%`;
        const mem = data.memory;
        document.getElementById('tm-memory').textContent =
            `${mem.used_gb} / ${mem.total_gb} GB (${mem.percent}%)`;

        // Store processes and render
        taskManagerProcesses = data.processes || [];
        renderTaskManagerTable();
    } catch (e) {
        console.error('Failed to fetch task manager data:', e);
    }
}

function renderTaskManagerTable() {
    const tbody = document.getElementById('tm-process-list');
    if (!tbody) return;

    // Filter processes
    let filtered = taskManagerProcesses;
    if (taskManagerFilter) {
        filtered = filtered.filter(p =>
            p.command.toLowerCase().includes(taskManagerFilter) ||
            p.user.toLowerCase().includes(taskManagerFilter) ||
            String(p.pid).includes(taskManagerFilter)
        );
    }

    // Sort processes
    filtered.sort((a, b) => {
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
        return 0;
    });

    // Update header sort indicators
    document.querySelectorAll('.task-manager-table th[data-sort]').forEach(th => {
        th.classList.remove('sorted', 'asc');
        if (th.dataset.sort === taskManagerSortColumn) {
            th.classList.add('sorted');
            if (taskManagerSortAsc) th.classList.add('asc');
        }
    });

    // Render rows
    if (filtered.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="tm-empty">No processes found</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map(p => `
        <tr data-pid="${p.pid}">
            <td>${p.pid}</td>
            <td>${escapeHtml(p.user)}</td>
            <td>${p.cpu.toFixed(1)}</td>
            <td>${p.mem.toFixed(1)}</td>
            <td class="tm-command">
                <div class="tm-command-wrapper">
                    <button class="tm-expand-btn" title="Expand/collapse">&#9654;</button>
                    <span class="tm-command-text" title="${escapeHtml(p.command)}">${escapeHtml(p.command)}</span>
                </div>
            </td>
            <td class="tm-actions">
                <button class="tm-kill-btn term" data-pid="${p.pid}" data-signal="TERM" title="SIGTERM">Term</button>
                <button class="tm-kill-btn kill" data-pid="${p.pid}" data-signal="KILL" title="SIGKILL">Kill</button>
            </td>
        </tr>
    `).join('');

    // Attach kill button handlers
    tbody.querySelectorAll('.tm-kill-btn').forEach(btn => {
        btn.onclick = () => killProcess(parseInt(btn.dataset.pid), btn.dataset.signal);
    });

    // Attach expand button handlers and restore expanded state
    tbody.querySelectorAll('.tm-expand-btn').forEach(btn => {
        const row = btn.closest('tr');
        const pid = parseInt(row.dataset.pid);
        const cell = btn.closest('.tm-command');

        // Restore expanded state
        if (taskManagerExpandedPids.has(pid)) {
            cell.classList.add('expanded');
        }

        btn.onclick = (e) => {
            e.stopPropagation();
            cell.classList.toggle('expanded');
            if (cell.classList.contains('expanded')) {
                taskManagerExpandedPids.add(pid);
            } else {
                taskManagerExpandedPids.delete(pid);
            }
        };
    });
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

async function killProcess(pid, signal) {
    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '';

    try {
        const resp = await fetch(`${basePath}/terminal/process/kill`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pid, signal }),
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

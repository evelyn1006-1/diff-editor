(() => {
    const statusEl = document.getElementById('xterm-status');
    const hostEl = document.getElementById('xterm-root');

    if (typeof io === 'undefined' || typeof Terminal === 'undefined' || !window.FitAddon) {
        statusEl.textContent = 'Error: terminal assets failed to load';
        statusEl.className = 'error';
        return;
    }

    const term = new Terminal({
        cursorBlink: true,
        convertEol: true,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
        fontSize: 14,
        theme: {
            background: '#111318',
            foreground: '#d7dce8',
        },
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(hostEl);
    fitAddon.fit();

    const basePath = window.location.pathname.startsWith('/diff/') ? '/diff' : '/terminal';
    const socketOptions = {
        path: `${basePath}/socket.io`,
        transports: ['websocket', 'polling'],
        reconnection: true,
        reconnectionAttempts: 5,
        reconnectionDelay: 1000,
    };

    const query = {};
    if (window.XTERM_CWD) {
        query.cwd = window.XTERM_CWD;
    }
    if (window.XTERM_ROOT) {
        query.root = '1';
    }
    if (Object.keys(query).length > 0) {
        socketOptions.query = query;
    }

    const socket = io('/terminal', socketOptions);

    socket.on('connect', () => {
        statusEl.textContent = 'Connected';
        statusEl.className = 'connected';
    });

    socket.on('connected', (data) => {
        if (data?.cwd) {
            term.writeln(`Connected to terminal at ${data.cwd}`);
        }

        if (window.XTERM_AUTO_CMD) {
            const cmd = window.XTERM_AUTO_CMD;
            window.XTERM_AUTO_CMD = '';
            setTimeout(() => socket.emit('input', { data: `${cmd}\n` }), 100);
        }
    });

    socket.on('output', (data) => {
        if (data?.data) {
            term.write(data.data);
        }
    });

    socket.on('error', (data) => {
        const message = data?.message || 'Unknown error';
        term.writeln(`\r\nError: ${message}`);
    });

    socket.on('session_ended', (data) => {
        statusEl.textContent = 'Session ended';
        statusEl.className = 'error';
        term.writeln(`\r\n${data?.message || 'Session ended'}`);
    });

    socket.on('disconnect', () => {
        statusEl.textContent = 'Disconnected';
        statusEl.className = 'error';
    });

    socket.on('connect_error', () => {
        statusEl.textContent = 'Connection failed';
        statusEl.className = 'error';
    });

    term.onData((data) => {
        socket.emit('input', { data });
    });

    const resize = () => {
        fitAddon.fit();
        socket.emit('resize', {
            rows: term.rows,
            cols: term.cols,
        });
    };

    window.addEventListener('resize', resize);
    resize();

    window.addEventListener('beforeunload', () => {
        socket.disconnect();
    });
})();

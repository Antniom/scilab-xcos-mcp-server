import React, { useState, useEffect } from 'react';
import { render, Box, Text } from 'ink';
import Spinner from 'ink-spinner';
import { existsSync, readFileSync, mkdirSync, readdirSync, unlinkSync, createWriteStream } from 'fs';
import { resolve, join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { spawn } from 'child_process';
import sharp from 'sharp';

const h = React.createElement;
const __dir = dirname(fileURLToPath(import.meta.url));
const SERVER_DIR = resolve(__dir, '..');

function findScilabBinary(root) {
  const candidates = [
    resolve(root, 'bin', 'WScilex-cli.exe'),
    resolve(root, 'bin', 'scilab-cli.exe'),
    resolve(root, 'bin', 'scilab-cli'),
    resolve(root, 'bin', 'scilab.bat'),
  ];
  return candidates.find(existsSync) || candidates[0];
}

const ORANGE = '#D97757';
const GRAY   = '#666666';
const GREEN  = '#80c87a';
const RED    = '#e06c75';
const DIM    = '#444444';
const WHITE  = '#e8e8e8';
const HR     = '─'.repeat(44);

// ── Render a PNG file to a colored ANSI string using ▀▄ half-blocks ──────────
async function pngToAnsi(filepath, termWidth = 18) {
  const buf = readFileSync(filepath);
  const meta = await sharp(buf).metadata();
  const H = Math.round(termWidth * (meta.height / meta.width) * 0.5);
  const { data } = await sharp(buf)
    .resize(termWidth, H * 2, { kernel: 'nearest' })
    .ensureAlpha()
    .raw()
    .toBuffer({ resolveWithObject: true });

  let out = '';
  for (let row = 0; row < H; row++) {
    for (let col = 0; col < termWidth; col++) {
      const ti = (row * 2 * termWidth + col) * 4;
      const bi = ((row * 2 + 1) * termWidth + col) * 4;
      const [tr,tg,tb,ta] = [data[ti],data[ti+1],data[ti+2],data[ti+3]];
      const [br,bg,bb,ba] = [data[bi],data[bi+1],data[bi+2],data[bi+3]];
      const tT = ta < 10, bT = ba < 10;
      if (tT && bT)  { out += ' '; continue; }
      if (tT)        { out += `\x1b[38;2;${br};${bg};${bb}m▄\x1b[0m`; continue; }
      if (bT)        { out += `\x1b[38;2;${tr};${tg};${tb}m▀\x1b[0m`; continue; }
      out += `\x1b[38;2;${tr};${tg};${tb}m\x1b[48;2;${br};${bg};${bb}m▀\x1b[0m`;
    }
    out += '\n';
  }
  return out;
}

// ── Pre-render all frames ─────────────────────────────────────────────────────
async function loadFrames() {
  const names = {
    idle:    ['idle_0', 'idle_1'],
    working: ['work_0', 'work_1', 'work_2', 'work_3'],
    done:    ['done'],
    error:   ['error'],
  };
  const result = {};
  for (const [state, keys] of Object.entries(names)) {
    result[state] = [];
    for (const k of keys) {
      const path = join(__dir, `mascot_${k}.png`);
      result[state].push(await pngToAnsi(path, 18));
    }
  }
  return result;
}

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

// ── Mascot component — renders pre-computed ANSI string ───────────────────────
function Mascot({ frames, state, phase }) {
  const [fi, setFi] = useState(0);
  const stateFrames = frames?.[state] || [];
  const fps = state === 'working' ? 300 : state === 'idle' ? 1600 : 99999;

  useEffect(() => {
    setFi(0);
    if (!stateFrames.length || stateFrames.length <= 1) return;
    const id = setInterval(() => setFi(i => (i + 1) % stateFrames.length), fps);
    return () => clearInterval(id);
  }, [state, frames]);

  const QUIPS = {
    idle:    ['Ready.'],
    working: ['Reading config...', 'Checking binary...', 'Booting Scilab...', 'Handing off...'],
    done:    ['All done!'],
    error:   ['Uh oh.'],
  };
  const quip = (QUIPS[state] || QUIPS.idle)[Math.min(phase, (QUIPS[state]||QUIPS.idle).length - 1)];
  const nameColor = state === 'error' ? RED : state === 'done' ? GREEN : ORANGE;

  // Split ANSI frame string into lines for Ink rendering
  const lines = stateFrames[fi]?.split('\n').filter((_, i, a) => i < a.length - 1) || [];

  return h(Box, { flexDirection: 'row', marginTop: 1, marginBottom: 1, alignItems: 'center' },
    h(Box, { flexDirection: 'column' },
      ...lines.map((line, i) =>
        // Use raw ANSI via stdout trick — write pre-colored string directly
        h(Text, { key: String(i) }, line)
      )
    ),
    h(Box, { flexDirection: 'column', paddingLeft: 2, justifyContent: 'center' },
      h(Text, { color: nameColor, bold: true }, 'Gearsworth'),
      h(Text, { color: GRAY }, 'Scilab Daemon'),
      h(Text, null, ''),
      h(Text, { color: WHITE }, '\u201c' + quip + '\u201d'),
    )
  );
}

// ── Main App ──────────────────────────────────────────────────────────────────
function App({ frames }) {
  const [phase,  setPhase]  = useState(0);
  const [mstate, setMstate] = useState('idle');
  const [error,  setError]  = useState(null);
  const [root,   setRoot]   = useState('');
  const [bin,    setBin]    = useState('');
  const [done,   setDone]   = useState(false);
  const [logPath, setLogPath] = useState('');

  useEffect(() => {
    async function boot() {
      await delay(800);
      setMstate('working');

      setPhase(0); await delay(350);
      const pathFile = resolve(SERVER_DIR, '.scilab_path');
      if (!existsSync(pathFile)) {
        setMstate('error'); setError('.scilab_path not found — run init.bat first.'); return;
      }
      const rawRoot = readFileSync(pathFile, 'utf8').trim();
      const r = resolve(SERVER_DIR, rawRoot);
      setRoot(r);

      setPhase(1); await delay(350);
      const b = findScilabBinary(r);
      setBin(b);
      if (!existsSync(b)) {
        setMstate('error'); setError('Binary not found:\n  ' + b); return;
      }

      setPhase(2); await delay(350);
      setPhase(3); await delay(300);

      // ── Log file setup ──────────────────────────────────────────────────────
      const logsDir = resolve(SERVER_DIR, 'logs');
      if (!existsSync(logsDir)) mkdirSync(logsDir);

      // Rotate: keep only last 3 logs
      const existing = readdirSync(logsDir)
        .filter(f => f.startsWith('scilab_') && f.endsWith('.log'))
        .sort();
      while (existing.length >= 3) {
        unlinkSync(join(logsDir, existing.shift()));
      }

      const stamp = new Date().toISOString().replace(/[T:]/g, '-').replace(/\..+/, '');
      const lp = join(logsDir, `scilab_${stamp}.log`);
      setLogPath(lp);
      const logStream = createWriteStream(lp, { flags: 'a' });
      logStream.write(`=== Scilab session started ${new Date().toISOString()} ===\n`);

      // ── Spawn Scilab, tee stdout+stderr to log file ─────────────────────────
      const startPollScript = resolve(SERVER_DIR, 'start_poll.sce');
      const useShell = b.toLowerCase().endsWith('.bat');
      const child = spawn(b, ['-f', startPollScript], { shell: useShell, cwd: SERVER_DIR });

      child.stdout.on('data', chunk => { process.stdout.write(chunk); logStream.write(chunk); });
      child.stderr.on('data', chunk => { process.stderr.write(chunk); logStream.write(chunk); });

      child.on('exit', code => {
        logStream.write(`\n=== Scilab exited with code ${code} ===\n`);
        logStream.end();
        if (code !== 0) { setMstate('error'); setError('Scilab exited with code ' + code); }
        else            { setMstate('done');  setDone(true); }
      });
    }
    boot();
  }, []);

  const phaseLabels = [
    'Reading configuration...',
    'Locating Scilab binary...',
    'Initialising polling loop...',
    'Handing off to Scilab...',
  ];

  return h(Box, { flexDirection: 'column', paddingLeft: 4, paddingTop: 1 },
    h(Text, { color: DIM }, HR),
    h(Box, null,
      h(Text, { color: ORANGE, bold: true }, 'Scilab Xcos '),
      h(Text, { color: WHITE }, 'Polling Daemon'),
      h(Text, { color: GRAY }, '  •  xcosai v1.0'),
    ),
    h(Text, { color: DIM }, HR),

    h(Mascot, { frames, state: mstate, phase }),

    h(Text, { color: DIM }, HR),
    h(Text, null, ''),
    h(Box, null,
      h(Text, { color: GRAY }, 'Scilab root   '),
      h(Text, { color: root ? WHITE : DIM }, root || '…'),
    ),
    h(Box, null,
      h(Text, { color: GRAY }, 'Binary        '),
      h(Text, { color: bin  ? WHITE : DIM }, bin  || '…'),
    ),
    h(Box, null,
      h(Text, { color: GRAY }, 'Log           '),
      h(Text, { color: logPath ? '#888' : DIM }, logPath || '…'),
    ),
    h(Text, null, ''),
    h(Text, { color: DIM }, HR),
    h(Text, null, ''),

    error
      ? h(Box, { flexDirection: 'column' },
          h(Text, { color: RED  }, '[!] ' + error),
          h(Text, null, ''),
          h(Text, { color: GRAY }, 'Press Ctrl+C to exit.'),
        )
      : done
        ? h(Text, { color: GREEN }, '[OK] Scilab exited cleanly.')
        : h(Box, null,
            h(Spinner, { type: 'dots' }),
            h(Text, { color: WHITE }, '  ' + phaseLabels[Math.min(phase, phaseLabels.length - 1)]),
          ),
    h(Text, null, ''),
  );
}

// ── Entry point — pre-render frames then mount ────────────────────────────────
async function main() {
  const frames = await loadFrames();
  process.stdout.write('\x1b[2J\x1b[H\x1b[40m');
  const { waitUntilExit } = render(h(App, { frames }), { exitOnCtrlC: true });
  await waitUntilExit();
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});

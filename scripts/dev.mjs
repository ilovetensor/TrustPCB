import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import net from 'node:net';
import { existsSync } from 'node:fs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
if (existsSync(path.join(root, '.env'))) process.loadEnvFile(path.join(root, '.env'));

const BACKEND_PORT = 8008;
const WEB_PORT = 3000;
const hasWeb = existsSync(path.join(root, 'web', 'package.json'));
// Optional local-only extras (git-ignored): extra packages for a development runner.
const localRequirements = path.join(root, 'server', 'requirements.local.txt');

async function available(port) {
  await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', () => reject(new Error(`Port ${port} is occupied. Stop that process before starting TrustPCB.`)));
    server.listen(port, '127.0.0.1', () => server.close(resolve));
  });
}
try {
  await Promise.all([available(BACKEND_PORT), ...(hasWeb ? [available(WEB_PORT)] : [])]);
} catch (error) {
  console.error(error.message);
  process.exit(1);
}

const env = {
  ...process.env,
  TRUSTPCB_TOKEN: randomBytes(32).toString('hex'),
  TRUSTPCB_BACKEND_URL: `http://127.0.0.1:${BACKEND_PORT}`,
};
const children = [];
let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of children) {
    if (child.pid) {
      try { process.kill(-child.pid, 'SIGTERM'); } catch { /* already exited */ }
    }
  }
  const force = setTimeout(() => {
    for (const child of children) {
      if (child.pid) { try { process.kill(-child.pid, 'SIGKILL'); } catch {} }
    }
    process.exit(code);
  }, 4000);
  force.unref();
  process.exitCode = code;
}
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => stop());
function launch(command, args, cwd) {
  const child = spawn(command, args, { cwd, env, stdio: 'inherit', detached: true });
  children.push(child);
  child.on('error', error => { console.error(error.message); stop(1); });
  child.on('exit', code => { if (!stopping) stop(code ?? 1); });
}

const uvArgs = ['run', '--project', 'server'];
if (existsSync(localRequirements)) uvArgs.push('--with-requirements', 'server/requirements.local.txt');
uvArgs.push('uvicorn', 'trustpcb.api:app', '--app-dir', 'server', '--host', '127.0.0.1', '--port', String(BACKEND_PORT), '--timeout-graceful-shutdown', '3');
launch('uv', uvArgs, root);
if (hasWeb) launch('npm', ['run', 'dev'], path.join(root, 'web'));
else console.log('web/package.json not found: starting the backend only.');
console.log(`\nTrustPCB backend → http://127.0.0.1:${BACKEND_PORT}${hasWeb ? `\nTrustPCB → http://127.0.0.1:${WEB_PORT}` : ''}\n`);

if (hasWeb && process.env.TRUSTPCB_NO_OPEN !== '1') {
  void (async () => {
    for (let attempt = 0; attempt < 40 && !stopping; attempt++) {
      try {
        const response = await fetch(`http://127.0.0.1:${WEB_PORT}`);
        if (response.ok) {
          if (process.platform === 'darwin') spawn('open', [`http://127.0.0.1:${WEB_PORT}`], { stdio: 'ignore' });
          return;
        }
      } catch {}
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  })();
}

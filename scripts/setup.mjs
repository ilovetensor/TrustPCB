import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { existsSync } from 'node:fs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const run = (command, args, cwd) => new Promise((resolve, reject) => {
  const child = spawn(command, args, { cwd, stdio: 'inherit', env: process.env });
  child.on('error', reject);
  child.on('exit', (code) => code === 0 ? resolve() : reject(new Error(`${command} exited ${code}`)));
});
const jobs = [run('uv', ['sync', '--python', '3.12'], path.join(root, 'server'))];
if (existsSync(path.join(root, 'web', 'package.json'))) {
  jobs.push(run('npm', ['install', '--no-audit', '--no-fund'], path.join(root, 'web')));
}
await Promise.all(jobs);
console.log('\nTrustPCB is ready. Run npm run dev.');

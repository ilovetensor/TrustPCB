// Copies the shared assets (logos, demo video, installers) from the main repo into public/ before a build,
// so the landing page has no second copy of them in git.
import { copyFileSync, mkdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '..');
const files = {
  'web/public/brand/trustpcb-logo.svg': 'trustpcb-logo.svg',
  'web/public/brand/meta-logo.svg': 'meta-logo.svg',
  'docs/media/demo.mp4': 'demo.mp4',
  'docs/media/demo-poster.jpg': 'demo-poster.jpg',
  'install.sh': 'install.sh',
  'install.ps1': 'install.ps1',
};
mkdirSync(path.join(here, 'public'), { recursive: true });
for (const [from, to] of Object.entries(files)) copyFileSync(path.join(repo, from), path.join(here, 'public', to));

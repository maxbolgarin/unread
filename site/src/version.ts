import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// The released version, read from the package at build time so the site
// can't drift from it. Builds run from site/ (npm run build), so the
// package root is one level up. Server-only: page frontmatter imports
// this, never client scripts.
const pyproject = readFileSync(resolve(process.cwd(), '../pyproject.toml'), 'utf8');

export const VERSION = /^version\s*=\s*"([^"]+)"/m.exec(pyproject)?.[1] ?? '';

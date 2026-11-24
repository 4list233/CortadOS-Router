// Builds every committed image under assets/ from sprites.mjs, deterministically.
//   node integrations/office-claw3d/build-assets.mjs --write   regenerate assets/
//   node integrations/office-claw3d/build-assets.mjs --check   exit 1 if assets/ is stale
// The preview renders fixtures/sample-office.json (invented metadata, no endpoint)
// through the real adapter mapping, so it shows exactly the sprites the gateway sends.
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isMainModule, mapOffice } from './adapter.mjs';
import { Canvas, FAMILIES, OUTFITS, SIZE, TILE_NAMES, drawText, spritePixels, spritePng, textWidth,
  tilePixels, tilePng } from './sprites.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
export const PREVIEW_SCALE = 3;
const INK = '#3e2723';
const PANEL = '#1b232a';
const STATUS_PIP = { working: '#34d399', idle: '#9ca3af', done: '#60a5fa', error: '#ef4444' };

export function sampleSnapshot() {
  return JSON.parse(readFileSync(join(HERE, 'fixtures', 'sample-office.json'), 'utf8'));
}

function tileArea(c, name, x, y, w, h) {
  const t = tilePixels(name);
  for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) c.set(x + i, y + j, t[j % SIZE][i % SIZE]);
}

function person(c, agent, x, y) {
  // Desk behind the person: a wooden top, a monitor whose screen shows the status colour.
  c.rect(x - 3, y + 3, 22, 7, '#8d6e63'); c.rect(x - 3, y + 3, 22, 1, '#a1887f');
  c.rect(x + 12, y - 1, 7, 5, '#263238'); c.rect(x + 13, y, 5, 3, STATUS_PIP[agent.office.status]);
  c.blit(spritePixels(agent.office.characterFamily, agent.office.outfit), x, y);
  const lines = [agent.office.characterFamily, ...(agent.office.outfit === 'suit' ? ['lead'] : [])];
  lines.forEach((label, i) => {
    const lx = x + 8 - Math.floor(textWidth(label) / 2), ly = y + SIZE + 2 + i * 7;
    c.rect(lx - 1, ly - 1, textWidth(label) + 2, 7, '#efe6d6');   // a light plate: readable on wood and carpet
    drawText(c, label, lx, ly, INK);
  });
}

function floorPanel(c, x, y, w, h, title, agents) {
  tileArea(c, 'wall', x, y, w, SIZE);
  tileArea(c, 'floor-wood', x, y + SIZE, w, h - SIZE);
  drawText(c, title, x + 3, y + 3, INK);
  const lead = agents.find(a => a.role === 'orchestrator');
  if (lead) {
    tileArea(c, 'floor-carpet', x + 2, y + SIZE + 2, 34, h - SIZE - 4);
    person(c, lead, x + 11, y + SIZE + 8);
  }
  agents.filter(a => a.role !== 'orchestrator').forEach((a, i) => {
    const col = i % 3, row = Math.floor(i / 3);
    person(c, a, x + 46 + col * 38, y + SIZE + 8 + row * 34);
  });
}

/** The preview as an unscaled canvas (1 logical pixel per art pixel). */
export function renderPreview(view = sampleSnapshot()) {
  const agents = mapOffice(view);
  const c = new Canvas(256, 170, PANEL);
  drawText(c, 'CORTADOS OFFICE - SAMPLE FIXTURE - NOT LIVE DATA', 4, 3, '#f5f5f4');
  const [a, b] = view.floors;
  const on = floor => agents.filter(x => x.office.floor === floor.session);
  floorPanel(c, 4, 12, 160, 100, `${a.project} on ${a.device}`.toUpperCase(), on(a));
  floorPanel(c, 168, 12, 84, 58, `${b.project} on ${b.device}`.toUpperCase(), on(b));
  // Lobby: untagged routed work, no orchestrator.
  tileArea(c, 'floor-carpet', 168, 72, 84, 40);
  drawText(c, 'LOBBY', 171, 75, '#f5f5f4');
  agents.filter(x => x.office.floor === 'lobby').forEach((x, i) => person(c, x, 190 + i * 38, 86));
  // Legend: every family, worker and suit.
  drawText(c, 'SPRITES: WORKER AND SUIT (ORCHESTRATOR)', 4, 117, '#f5f5f4');
  FAMILIES.forEach((family, i) => {
    const x = 6 + i * 42;
    c.rect(x - 2, 125, 38, 20, '#c8a97e');
    OUTFITS.forEach((outfit, j) => c.blit(spritePixels(family, outfit), x + j * 18, 127));
    drawText(c, family, x + 17 - Math.floor(textWidth(family) / 2), 149, '#f5f5f4');
  });
  drawText(c, 'STATUS SCREEN:', 4, 160, '#f5f5f4');
  let x = 64;
  for (const [status, colour] of Object.entries(STATUS_PIP)) {
    c.rect(x, 160, 5, 5, colour);
    x = drawText(c, status, x + 7, 160, '#f5f5f4') + 6;
  }
  return c;
}

/** Every generated file: relative path -> Buffer. */
export function buildAssets() {
  const files = new Map();
  const sheet = new Canvas(SIZE * FAMILIES.length, SIZE * OUTFITS.length);
  const frames = {};
  FAMILIES.forEach((family, i) => OUTFITS.forEach((outfit, j) => {
    const name = family + (outfit === 'suit' ? '-suit' : '');
    files.set(`assets/sprites/${name}.png`, spritePng(family, outfit));
    sheet.blit(spritePixels(family, outfit), i * SIZE, j * SIZE);
    frames[name] = { x: i * SIZE, y: j * SIZE, w: SIZE, h: SIZE };
  }));
  files.set('assets/sprites/sheet.png', sheet.png());
  files.set('assets/sprites/sheet.json', Buffer.from(JSON.stringify(
    { image: 'sheet.png', size: SIZE, frames }, null, 2) + '\n'));
  for (const name of TILE_NAMES) files.set(`assets/tiles/${name}.png`, tilePng(name));
  files.set('assets/preview.png', renderPreview().scaled(PREVIEW_SCALE).png());
  return files;
}

// Real-path comparison: see isMainModule in adapter.mjs (the deploy starts the bridge via a symlink).
if (isMainModule(import.meta.url)) {
  const files = buildAssets();
  if (process.argv.includes('--write')) {
    for (const [rel, buf] of files) {
      mkdirSync(dirname(join(HERE, rel)), { recursive: true });
      writeFileSync(join(HERE, rel), buf);
    }
    process.stdout.write(`wrote ${files.size} files\n`);
  } else if (process.argv.includes('--check')) {
    const stale = [...files].filter(([rel, buf]) => {
      try { return !readFileSync(join(HERE, rel)).equals(buf); } catch { return true; }
    }).map(([rel]) => rel);
    process.stdout.write(stale.length ? 'stale: ' + stale.join(', ') + '\n' : 'assets up to date\n');
    process.exitCode = stale.length ? 1 : 0;
  } else {
    process.stderr.write('usage: build-assets.mjs --write | --check\n');
    process.exitCode = 2;
  }
}

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { inflateSync } from 'node:zlib';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { FAMILIES, OUTFITS, SIZE, TILE_NAMES, avatarDataUrl, avatarProfile, spritePixels, spritePng,
  tilePixels } from './sprites.mjs';
import { buildAssets } from './build-assets.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));

function decodePng(buf) {
  assert.deepEqual([...buf.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  let off = 8, width, height; const idat = [];
  while (off < buf.length) {
    const len = buf.readUInt32BE(off), type = buf.toString('ascii', off + 4, off + 8);
    const body = buf.subarray(off + 8, off + 8 + len);
    if (type === 'IHDR') { width = body.readUInt32BE(0); height = body.readUInt32BE(4); assert.equal(body[8], 8); assert.equal(body[9], 6); }
    if (type === 'IDAT') idat.push(body);
    off += 12 + len;
  }
  const raw = inflateSync(Buffer.concat(idat));
  assert.equal(raw.length, (width * 4 + 1) * height);
  const px = (x, y) => { const i = y * (width * 4 + 1) + 1 + x * 4; return [...raw.subarray(i, i + 4)]; };
  return { width, height, px };
}
const rgba = c => c === null ? [0, 0, 0, 0] : [1, 3, 5].map(i => parseInt(c.slice(i, i + 2), 16)).concat(255);

test('every family has a 16x16 worker and suit sprite, and the suit differs', () => {
  for (const family of FAMILIES) {
    const worker = spritePixels(family, 'worker'), suit = spritePixels(family, 'suit');
    for (const grid of [worker, suit]) {
      assert.equal(grid.length, SIZE);
      for (const row of grid) {
        assert.equal(row.length, SIZE);
        for (const c of row) assert.ok(c === null || /^#[0-9a-f]{6}$/.test(c), family);
      }
    }
    assert.ok(worker.flat().filter(Boolean).length > 80, family + ' has a body');
    assert.notDeepEqual(worker, suit, family);
  }
  assert.throws(() => spritePixels('acme'));
  assert.throws(() => spritePixels('claude', 'cape'));
});
test('families are visually distinct', () => {
  const seen = new Set(FAMILIES.map(f => JSON.stringify(spritePixels(f))));
  assert.equal(seen.size, FAMILIES.length);
});
test('PNG encoder round-trips pixels exactly (sprite and 8x avatar)', () => {
  const grid = spritePixels('ollama', 'suit');
  const one = decodePng(spritePng('ollama', 'suit'));
  assert.deepEqual([one.width, one.height], [SIZE, SIZE]);
  for (let y = 0; y < SIZE; y++) for (let x = 0; x < SIZE; x++) assert.deepEqual(one.px(x, y), rgba(grid[y][x]));
  const url = avatarDataUrl('ollama', 'suit');
  const big = decodePng(Buffer.from(url.split(',')[1], 'base64'));
  assert.deepEqual([big.width, big.height], [128, 128]);
  assert.deepEqual(big.px(8 * 5 + 7, 8 * 4 + 7), rgba(grid[4][5]));
  assert.ok(url.length < 4096, 'avatar data URL stays small: ' + url.length);
  assert.equal(avatarDataUrl('ollama', 'suit'), url);
});
test('tiles are 16x16 and fully opaque', () => {
  for (const name of TILE_NAMES) {
    const t = tilePixels(name);
    assert.equal(t.length, SIZE);
    for (const row of t) { assert.equal(row.length, SIZE); for (const c of row) assert.match(c, /^#[0-9a-f]{6}$/); }
  }
});
// Claw3D upstream 51337f7 src/lib/avatars/profile.ts option lists: values outside them
// are silently replaced by normalizeAgentAvatarProfile, so the hint must use them.
const CLAW3D = {
  hairStyle: ['short', 'parted', 'spiky', 'bun'], topStyle: ['tee', 'hoodie', 'jacket'],
  bottomStyle: ['pants', 'shorts', 'cuffed'], hat: ['none', 'cap', 'beanie'],
  hair: ['#151515', '#3e2723', '#6b4f3a', '#7b341e', '#d6b56c', '#7c3aed', '#0891b2', '#db2777'],
  clothing: ['#2d3748', '#7090ff', '#34d399', '#f59e0b', '#f43f5e', '#8b5cf6', '#f5f5f4', '#64748b'],
  shoes: ['#1a1a1a', '#1e3a8a', '#7c4a2d', '#e5e7eb'],
};
test('3D avatar profiles use only Claw3D option values and never set skin tone', () => {
  for (const family of FAMILIES) for (const outfit of OUTFITS) {
    const p = avatarProfile(family, outfit, 'seed-1');
    assert.equal(p.version, 1); assert.equal(p.seed, 'seed-1');
    assert.equal(p.body, undefined);
    assert.ok(CLAW3D.hairStyle.includes(p.hair.style)); assert.ok(CLAW3D.hair.includes(p.hair.color));
    assert.ok(CLAW3D.topStyle.includes(p.clothing.topStyle)); assert.ok(CLAW3D.clothing.includes(p.clothing.topColor));
    assert.ok(CLAW3D.bottomStyle.includes(p.clothing.bottomStyle)); assert.ok(CLAW3D.clothing.includes(p.clothing.bottomColor));
    assert.ok(CLAW3D.shoes.includes(p.clothing.shoesColor)); assert.ok(CLAW3D.hat.includes(p.accessories.hatStyle));
    assert.equal(p.clothing.topStyle, outfit === 'suit' ? 'jacket' : 'hoodie');
  }
});
test('committed assets are exactly what build-assets.mjs generates', () => {
  const files = buildAssets();
  for (const [rel, buf] of files) assert.ok(readFileSync(join(HERE, rel)).equals(buf), rel + ' is stale: run build-assets.mjs --write');
  const committed = ['sprites', 'tiles'].flatMap(d => readdirSync(join(HERE, 'assets', d)).map(f => `assets/${d}/${f}`));
  for (const rel of committed) assert.ok(files.has(rel), rel + ' is not generated');
  const preview = decodePng(readFileSync(join(HERE, 'assets', 'preview.png')));
  assert.deepEqual([preview.width, preview.height], [768, 510]);
});
test('NOTICE states the art is original and names its licence', () => {
  const notice = readFileSync(join(HERE, 'assets', 'NOTICE.md'), 'utf8');
  assert.match(notice, /original/i);
  assert.match(notice, /licen[cs]e/i);
  for (const dir of ['sprites', 'tiles']) assert.ok(notice.includes(`assets/${dir}/`), dir);
  assert.ok(notice.includes('preview.png'));
});

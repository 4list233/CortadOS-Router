// Original pixel art for the CortadOS Office (issue #77). Every sprite below is drawn
// here, pixel by pixel, for this repository; none is copied or traced from a company
// logo or mascot. They only EVOKE a provider through colour and a motif (an orange
// bean, a terminal robot, a dark visor robot, a llama, a circuit-chip creature).
// See assets/NOTICE.md. Node 22+ stdlib only; output is byte-for-byte deterministic.
import { deflateSync } from 'node:zlib';

export const FAMILIES = ['claude', 'codex', 'grok', 'ollama', 'local', 'unknown'];
export const OUTFITS = ['worker', 'suit'];
export const SIZE = 16;
// Body rows the orchestrator's suit is drawn over (same for every family).
const SUIT_ROWS = [10, 11, 12, 13];
const JACKET = '#262b3a';
const SHIRT = '#f4f4f0';

// '.' is transparent; every other letter is looked up in the sprite's palette.
const ART = {
  // A round orange bean with a cream highlight and rosy cheeks.
  claude: {
    tie: '#8a3b12',
    palette: { K: '#4a2511', O: '#e8833a', L: '#f6b27a', E: '#2b1608', P: '#f2a07b' },
    rows: [
      '................',
      '.....KKKKKK.....',
      '...KKOOOOOOKK...',
      '..KOOLLOOOOOOK..',
      '..KOLLOOOOOOOK..',
      '.KOOOOOOOOOOOOK.',
      '.KOOOEOOOOEOOOK.',
      '.KOOOEOOOOEOOOK.',
      '.KOPPOOOOOOPPOK.',
      '.KOOOOOKKOOOOOK.',
      '.KOOOOOOOOOOOOK.',
      '.KOOOOOOOOOOOOK.',
      '..KOOOOOOOOOOK..',
      '...KKOOOOOOKK...',
      '....KOK..KOK....',
      '....KKK..KKK....',
    ],
  },
  // A white terminal robot: a ">_" prompt on its screen face, a cyan antenna bulb.
  codex: {
    tie: '#3fd1e8',
    palette: { K: '#2a3140', W: '#eef1f5', G: '#b9c2cf', S: '#10223a', C: '#3fd1e8', Y: '#7fe6f5' },
    rows: [
      '.......YY.......',
      '.......KK.......',
      '...KKKKKKKKKK...',
      '..KWWWWWWWWWWK..',
      '..KWSSSSSSSSGK..',
      '..KWSCSSSSSSGK..',
      '..KWSSCSSSSSGK..',
      '..KWSCSSCCSSGK..',
      '..KGGGGGGGGGGK..',
      '...KKKKKKKKKK...',
      '.KKGGWWWWWWGGKK.',
      'KGKGWWWWWWWWGKGK',
      'KGKGWWCCCCWWGKGK',
      '.K.KGGGGGGGGK.K.',
      '....KGK..KGK....',
      '....KKK..KKK....',
    ],
  },
  // A charcoal robot with a bright visor slit and one red sensor.
  grok: {
    tie: '#ff4a3d',
    palette: { K: '#0b0c0f', M: '#4a4f5a', D: '#24272e', V: '#f4f4f4', R: '#ff4a3d' },
    rows: [
      '................',
      '....KKKKKKKK....',
      '...KMMMMMMMMK...',
      '..KMDDDDDDDDMK..',
      '..KDDDDDDDDDDK..',
      '..KDVVVVVVVVDK..',
      '..KDDDDDDDDDDK..',
      '..KDDDDDDRDDDK..',
      '...KDDDDDDDDK...',
      '....KKKKKKKK....',
      '.KKMMDDDDDDMMKK.',
      'KMKDDDDRRDDDDKMK',
      'KMKDDDDDDDDDDKMK',
      '.K.KDDDDDDDDK.K.',
      '....KDK..KDK....',
      '....KKK..KKK....',
    ],
  },
  // A woolly cream llama: tall ears, long neck, pink nose.
  ollama: {
    tie: '#2f9e8f',
    palette: { K: '#3a3024', W: '#f5efe2', S: '#d8ccb4', E: '#1d1812', N: '#e6a6a0' },
    rows: [
      '....KK....KK....',
      '....KWK..KWK....',
      '....KWWKKWWK....',
      '...KWWWWWWWWK...',
      '...KWEWWWWEWK...',
      '...KWWWWWWWWK...',
      '....KWWSSWWK....',
      '....KWSNNSWK....',
      '.....KWWWWK.....',
      '.....KWWWWK.....',
      '..KKKWWWWWWKKK..',
      '.KWWWWWWWWWWWWK.',
      '.KWSWWWWWWWWSWK.',
      '.KKWWWWWWWWWWKK.',
      '..KSK......KSK..',
      '..KKK......KKK..',
    ],
  },
  // A green circuit-chip creature with gold pins: a model running on our own hardware.
  local: {
    tie: '#e0b443',
    palette: { K: '#133824', G: '#2f8f5b', L: '#5cc985', P: '#e0b443', W: '#ffffff', E: '#102018' },
    rows: [
      '.....P.P.P.P....',
      '................',
      '...KKKKKKKKKKK..',
      '..PKLLLLLLLLLKP.',
      '...KLGGGGGGGGK..',
      '..PKLGWEGGWEGKP.',
      '...KLGGGGGGGGK..',
      '..PKLGGLLLGGGKP.',
      '...KLGGGGGGGGK..',
      '...KKKKKKKKKKK..',
      '..KKLLLLLLLLKK..',
      '.KLKGGGGGGGGKLK.',
      '.KLKGGPGGPGGKLK.',
      '...KGGGGGGGGK...',
      '....KGK..KGK....',
      '....KKK..KKK....',
    ],
  },
  // A grey hooded visitor with a question mark: provider not known, never guessed.
  unknown: {
    tie: '#5b616b',
    palette: { K: '#2b2f36', G: '#8d939c', W: '#f2f2f2' },
    rows: [
      '................',
      '.....KKKKKK.....',
      '....KGGGGGGK....',
      '...KGGGGGGGGK...',
      '...KGKKKKKKGK...',
      '...KGKWWWKKGK...',
      '...KGKKKWKKGK...',
      '...KGKKWKKKGK...',
      '...KGKKKKKKGK...',
      '...KGKKWKKKGK...',
      '..KGGGGGGGGGGK..',
      '.KGGGGGGGGGGGGK.',
      '.KGGGGGGGGGGGGK.',
      '..KGGGGGGGGGGK..',
      '....KGK..KGK....',
      '....KKK..KKK....',
    ],
  },
};

// Floor art in the palette of Claw3D's own office floor (warm wood, #c8a97e) so the
// sprites sit on the same kind of floor as the 3D office. The tiles are drawn here.
const TILES = {
  'floor-wood': {
    palette: { B: '#c8a97e', H: '#d6ba92', S: '#a88a60', G: '#b39467' },
    rows: [
      'HHHHHHHHHHHSHHHH',
      'BBBGBBBBBBBSBBBB',
      'BBBBBBBBGBBSBBBB',
      'SSSSSSSSSSSSSSSS',
      'HHHHSHHHHHHHHHHH',
      'BBBBSBBBBBBGBBBB',
      'BGBBSBBBBBBBBBBB',
      'SSSSSSSSSSSSSSSS',
      'HHHHHHHHHHHHHHSH',
      'BBBBBBGBBBBBBBSB',
      'BBBBBBBBBBBBBBSB',
      'SSSSSSSSSSSSSSSS',
      'HHHHHHHSHHHHHHHH',
      'BBGBBBBSBBBBBBBB',
      'BBBBBBBSBBBBGBBB',
      'SSSSSSSSSSSSSSSS',
    ],
  },
  'floor-carpet': {
    palette: { A: '#4b5d73', C: '#55697f', D: '#435366' },
    rows: Array.from({ length: SIZE }, (_, y) => Array.from({ length: SIZE }, (_, x) =>
      (x === 0 || y === 0) ? 'D' : ((x >> 2) + (y >> 2)) % 2 ? 'C' : 'A').join('')),
  },
  wall: {
    palette: { W: '#efe6d6', V: '#e3d8c4', B: '#5d4037', T: '#7a5646' },
    rows: [
      ...Array.from({ length: 12 }, (_, y) => Array.from({ length: SIZE }, (_, x) =>
        x === 15 && y % 4 !== 3 ? 'V' : 'W').join('')),
      'TTTTTTTTTTTTTTTT', 'BBBBBBBBBBBBBBBB', 'BBBBBBBBBBBBBBBB', 'TTTTTTTTTTTTTTTT',
    ],
  },
};
export const TILE_NAMES = Object.keys(TILES);

const hex = c => [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16), 255];

/** A sprite as SIZE x SIZE rows of '#rrggbb' or null (transparent). */
export function spritePixels(family, outfit = 'worker') {
  const art = ART[family];
  if (!art || !OUTFITS.includes(outfit)) throw new Error('unknown sprite');
  const grid = art.rows.map(row => [...row].map(ch => ch === '.' ? null : art.palette[ch]));
  if (outfit === 'suit') {
    const outline = art.palette.K;
    for (const y of SUIT_ROWS) {
      for (let x = 0; x < SIZE; x++) {
        if (grid[y][x] === null || grid[y][x] === outline) continue;
        // Shirt V at the collar, a tie in the family colour below it, jacket elsewhere.
        const centre = x === 7 || x === 8;
        if (y === SUIT_ROWS[0] && (centre || x === 6 || x === 9)) grid[y][x] = SHIRT;
        else if (centre && y < SUIT_ROWS.at(-1)) grid[y][x] = art.tie;
        else grid[y][x] = JACKET;
      }
    }
  }
  return grid;
}

export function tilePixels(name) {
  const tile = TILES[name];
  if (!tile) throw new Error('unknown tile');
  return tile.rows.map(row => [...row].map(ch => tile.palette[ch]));
}

// ---- a minimal RGBA canvas and PNG encoder (deterministic, stdlib only) ----
export class Canvas {
  constructor(width, height, background = null) {
    this.width = width; this.height = height;
    this.data = new Uint8Array(width * height * 4);
    if (background) this.rect(0, 0, width, height, background);
  }
  set(x, y, colour) {
    if (colour === null || x < 0 || y < 0 || x >= this.width || y >= this.height) return;
    this.data.set(hex(colour), (y * this.width + x) * 4);
  }
  rect(x, y, w, h, colour) {
    for (let j = y; j < y + h; j++) for (let i = x; i < x + w; i++) this.set(i, j, colour);
  }
  blit(pixels, x, y) {
    pixels.forEach((row, j) => row.forEach((c, i) => this.set(x + i, y + j, c)));
  }
  scaled(factor) {
    const out = new Canvas(this.width * factor, this.height * factor);
    for (let y = 0; y < out.height; y++) {
      for (let x = 0; x < out.width; x++) {
        const from = ((Math.floor(y / factor) * this.width) + Math.floor(x / factor)) * 4;
        out.data.set(this.data.subarray(from, from + 4), (y * out.width + x) * 4);
      }
    }
    return out;
  }
  png() { return encodePng(this.width, this.height, this.data); }
}

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  return c >>> 0;
});
function crc32(buf) {
  let c = 0xffffffff;
  for (const b of buf) c = CRC_TABLE[(c ^ b) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}
function chunk(type, body) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(body.length, 0);
  head.write(type, 4, 'ascii');
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), body])), 0);
  return Buffer.concat([head, body, crc]);
}
export function encodePng(width, height, rgba) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0); ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 6; // 8-bit RGBA; compression, filter, interlace all 0
  const raw = Buffer.alloc((width * 4 + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (width * 4 + 1)] = 0;
    Buffer.from(rgba.buffer, rgba.byteOffset + y * width * 4, width * 4).copy(raw, y * (width * 4 + 1) + 1);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr), chunk('IDAT', deflateSync(raw, { level: 9 })), chunk('IEND', Buffer.alloc(0)),
  ]);
}

export function spritePng(family, outfit = 'worker', scale = 1) {
  const c = new Canvas(SIZE, SIZE);
  c.blit(spritePixels(family, outfit), 0, 0);
  return (scale === 1 ? c : c.scaled(scale)).png();
}
export function tilePng(name, scale = 1) {
  const c = new Canvas(SIZE, SIZE);
  c.blit(tilePixels(name), 0, 0);
  return (scale === 1 ? c : c.scaled(scale)).png();
}

// Claw3D's 2D panels draw identity.avatarUrl into a ~112 px box with browser smoothing,
// so the gateway sends the sprite pre-scaled 8x (128 px): blocks stay crisp. Cached:
// the image depends only on (family, outfit), never on per-agent metadata.
const AVATAR_SCALE = 8;
const avatarCache = new Map();
export function avatarDataUrl(family, outfit) {
  const k = family + '/' + outfit;
  if (!avatarCache.has(k)) {
    avatarCache.set(k, 'data:image/png;base64,' + spritePng(family, outfit, AVATAR_SCALE).toString('base64'));
  }
  return avatarCache.get(k);
}

// Claw3D's 3D office draws people as voxel figures from an AgentAvatarProfile
// (src/lib/avatars/profile.ts, upstream 51337f7). Colours are taken from Claw3D's own
// option lists so normalizeAgentAvatarProfile keeps them. Skin tone is omitted on
// purpose (Claw3D derives it from the seed): a vendor is never shown as a skin colour.
// The suit is Claw3D's own "jacket" top with pants, in the original Claw3D style.
const PROFILE_LOOK = {
  claude: { top: '#f59e0b', hair: '#7b341e', hat: 'none', headset: false, glasses: false },
  codex: { top: '#7090ff', hair: '#151515', hat: 'none', headset: true, glasses: true },
  grok: { top: '#2d3748', hair: '#151515', hat: 'beanie', headset: true, glasses: false },
  ollama: { top: '#f5f5f4', hair: '#d6b56c', hat: 'beanie', headset: false, glasses: false },
  local: { top: '#34d399', hair: '#3e2723', hat: 'cap', headset: false, glasses: false },
  unknown: { top: '#64748b', hair: '#6b4f3a', hat: 'none', headset: false, glasses: false },
};
// Office round 2 (owner 2026-11-27): a room's people wear its colour as their trousers (adapter.mjs roomColor), so a
// glance ties a person to their room. Only a colour of ROOM_TROUSERS is used, else the outfit's default: it is Claw3D's
// own clothing list (profile.ts AGENT_AVATAR_CLOTHING_COLOR_OPTIONS), the only values normalizeAgentAvatarProfile keeps.
export const ROOM_TROUSERS = Object.freeze(['#2d3748', '#7090ff', '#34d399', '#f59e0b', '#f43f5e', '#8b5cf6', '#f5f5f4',
  '#64748b']);
export function avatarProfile(family, outfit, seed, { roomColor = null } = {}) {
  const look = PROFILE_LOOK[family];
  if (!look || !OUTFITS.includes(outfit)) throw new Error('unknown sprite');
  const suit = outfit === 'suit';
  const trousers = ROOM_TROUSERS.includes(roomColor) ? roomColor : null;
  return {
    version: 1, seed,
    hair: { style: suit ? 'parted' : 'short', color: look.hair },
    clothing: {
      topStyle: suit ? 'jacket' : 'hoodie', topColor: look.top,
      bottomStyle: 'pants', bottomColor: trousers ?? (suit ? '#2d3748' : '#64748b'),
      shoesColor: suit ? '#1a1a1a' : '#7c4a2d',
    },
    accessories: { glasses: look.glasses, headset: look.headset, hatStyle: suit ? 'none' : look.hat, backpack: false },
  };
}

// ---- a 3x5 pixel font for preview labels (drawn here) ----
const FONT = {
  A: '.X.X.XXXXX.XX.X', B: 'XX.X.XXX.X.XXX.', C: '.XXX..X..X...XX', D: 'XX.X.XX.XX.XXX.',
  E: 'XXXX..XX.X..XXX', F: 'XXXX..XX.X..X..', G: '.XXX..X.XX.X.XX', H: 'X.XX.XXXXX.XX.X',
  I: 'XXX.X..X..X.XXX', J: '..X..X..XX.X.X.', K: 'X.XX.XXX.X.XX.X', L: 'X..X..X..X..XXX',
  M: 'X.XXXXXXXX.XX.X', N: 'XX.X.XX.XX.XX.X', O: '.X.X.XX.XX.X.X.', P: 'XX.X.XXX.X..X..',
  Q: '.X.X.XX.XXX..XX', R: 'XX.X.XXX.X.XX.X', S: '.XXX...X...XXX.', T: 'XXX.X..X..X..X.',
  U: 'X.XX.XX.XX.XXXX', V: 'X.XX.XX.XX.X.X.', W: 'X.XX.XXXXXXXX.X', X: 'X.XX.X.X.X.XX.X',
  Y: 'X.XX.X.X..X..X.', Z: 'XXX..X.X.X..XXX', 0: 'XXXX.XX.XX.XXXX', 1: '.X.XX..X..X.XXX',
  2: 'XX...X.X.X..XXX', 3: 'XX...X.X...XXX.', 4: 'X.XX.XXXX..X..X', 5: 'XXXX..XX...XXX.',
  6: '.XXX..XXXX.XXXX', 7: 'XXX..X.X..X..X.', 8: 'XXXX.XXXXX.XXXX', 9: 'XXXX.XXXX..XXX.',
  ' ': '...............', '-': '......XXX......', '.': '.............X.', ':': '....X.....X....',
  '(': '.X.X..X..X...X.', ')': '.X...X..X..X.X.', '/': '..X..X.X.X..X..', '#': 'X.XXXXX.XXXXX.X',
};
export function drawText(canvas, text, x, y, colour) {
  let cx = x;
  for (const ch of text.toUpperCase()) {
    const glyph = FONT[ch] ?? FONT[' '];
    for (let i = 0; i < 15; i++) if (glyph[i] === 'X') canvas.set(cx + (i % 3), y + Math.floor(i / 3), colour);
    cx += 4;
  }
  return cx;
}
export const textWidth = text => text.length * 4 - 1;

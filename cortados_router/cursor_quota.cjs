// PURPOSE: Load only the captured Cursor credential/config/Dashboard owners.
// CONTRACT: RT-QUOTA-SOURCES-006; no main, refresh, settings writes or quota policy.
// DEPENDS: Python lifecycle bounds; exact reviewed bundled Node/native manifest.
'use strict';
process.on('uncaughtException', () => { process.stderr.write('Native quota source unavailable\n'); process.exit(1); });
const fs = require('fs'), path = require('path'), Module = require('module'), crypto = require('crypto');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = path.dirname(process.execPath), index = path.join(root, 'index.js');
let mutation = false;
const refuse = () => { mutation = true; throw Error('Read-only source refused'); };
const nativeFS = {...fs, promises: {...fs.promises}};
const originals = {existsSync: fs.existsSync.bind(fs), statSync: fs.statSync.bind(fs)};
const ancestors = new Set();
for (let folder = process.cwd();;) {
  ancestors.add(path.resolve(folder, '.cursor/cli.json'));
  const parent = path.dirname(folder); if (parent === folder) break; folder = parent;
}
nativeFS.existsSync = file => {
  if (typeof file === 'string' && file.endsWith('/.cursor/cli.json') && !ancestors.has(path.resolve(file))) refuse();
  return originals.existsSync(file);
};
for (const key of ['writeFile','appendFile','rename','unlink','rm','rmdir','copyFile','truncate','chmod','chown','symlink','link','write']) {
  if (typeof nativeFS[key] === 'function') nativeFS[key] = refuse;
  if (typeof nativeFS[key + 'Sync'] === 'function') nativeFS[key + 'Sync'] = refuse;
  if (typeof nativeFS.promises[key] === 'function') nativeFS.promises[key] = async () => refuse();
}
nativeFS.createWriteStream = refuse;
const oldOpen = fs.open.bind(fs), oldOpenSync = fs.openSync.bind(fs), oldPromiseOpen = fs.promises.open.bind(fs.promises);
const reading = flags => flags === 'r' || flags === 0;
nativeFS.open = (file, flags, ...args) => reading(flags) ? oldOpen(file, flags, ...args) : refuse();
nativeFS.openSync = (file, flags, ...args) => reading(flags) ? oldOpenSync(file, flags, ...args) : refuse();
nativeFS.promises.open = async (file, flags, ...args) => reading(flags) ? oldPromiseOpen(file, flags, ...args) : refuse();
const existingDirectory = file => originals.existsSync(file) && originals.statSync(file).isDirectory();
nativeFS.mkdirSync = file => { if (!existingDirectory(file)) refuse(); };
nativeFS.mkdir = (file, options, callback) => {
  if (!existingDirectory(file)) return refuse();
  (typeof options === 'function' ? options : callback)?.(null);
};
nativeFS.promises.mkdir = async file => { if (!existingDirectory(file)) refuse(); };
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
for (const [relative, expected] of Object.entries(input.manifest)) {
  if (digest(fs.readFileSync(path.join(root, relative))) !== expected) throw Error('Native profile unavailable');
}
const load = Module._load;
Module._load = function(id, parent, main) {
  if (parent?.filename?.startsWith(root + path.sep)) {
    if (id === 'fs' || id === 'node:fs') return nativeFS;
    if (id === 'fs/promises' || id === 'node:fs/promises') return nativeFS.promises;
  }
  const resolved = Module._resolveFilename(id, parent);
  if (parent?.filename?.startsWith(root + path.sep) && path.isAbsolute(resolved) && !resolved.startsWith(root + path.sep)) throw Error('Native dependency unavailable');
  if (typeof resolved === 'string' && resolved.startsWith(root + path.sep)) {
    const relative = path.relative(root, resolved);
    if (!Object.hasOwn(input.manifest, relative) || digest(fs.readFileSync(resolved)) !== input.manifest[relative]) throw Error('Native dependency unavailable');
  }
  return load.apply(this, arguments);
};
(async () => {
  const source = fs.readFileSync(index, 'utf8');
  const needle = 'var __webpack_exports__=__webpack_require__("./src/main.tsx")';
  if (source.split(needle).length !== 2) throw Error('Native entrypoint unavailable');
  const module = new Module(index, exports);
  module.filename = index; module.paths = Module._nodeModulePaths(root);
  module._compile(source.replace(needle, 'module.exports=__webpack_require__'), index);
  const native = module.exports;
  if (native('./src/utils/credential-store.ts').SI() !== 'file') throw Error('Native source unavailable');
  const credentials = native('../cli-credentials/dist/index.js').jo({domain: 'cursor', store: 'file'});
  const expectedAuth = path.resolve(process.env.HOME, '.cursor/auth.json');
  if (path.resolve(credentials.authFilePath) !== expectedAuth) throw Error('Native auth authority unavailable');
  const all = await credentials.getAllCredentials();
  if (all.apiKey) throw Error('Native subscription unavailable');
  const token = await credentials.getAccessToken();
  if (!token || native('./src/auth-refresh.ts').nH(token)) throw Error('Current credential unavailable');
  const claim = JSON.parse(Buffer.from(token.split('.')[1], 'base64url').toString('utf8'));
  if (typeof claim.exp !== 'number' || !Number.isFinite(claim.exp)) throw Error('Native expiry unavailable');
  const configOwner = native('../cursor-config/dist/file-based-config-provider.js').F;
  const config = await configOwner.loadFromDefaults('cursor', {onError: () => { throw Error('Native config unavailable'); }});
  const expectedSettings = path.resolve(process.env.CURSOR_CONFIG_DIR || (process.env.XDG_CONFIG_HOME ? path.join(process.env.XDG_CONFIG_HOME, 'cursor') : path.join(process.env.HOME, '.cursor')), 'cli-config.json');
  if (mutation || path.resolve(config.getConfigFilePath()) !== expectedSettings || config.projectConfigFilePaths.length) throw Error('Native config authority unavailable');
  const urls = native('./src/utils/service-urls.ts');
  const endpoint = urls.ju[urls.Uz].apiUrl;
  if (endpoint !== 'https://api2.cursor.sh') throw Error('Native endpoint unavailable');
  const readOnly = {getAccessToken: async () => token, getApiKey: async () => undefined, setAuthentication: refuse};
  const dashboard = native('./src/dashboard-client.ts').m({credentialManager: readOnly, endpoint,
      configProvider: {get: () => config.get(), transform: refuse}});
  const raw = await dashboard.getCurrentPeriodUsage({});
  if (mutation) throw Error('Read-only source refused');
  const payload = raw.toJson ? raw.toJson() : raw;
  // Emit the closed quota projection only; never account, token or billing payloads.
  const safe = {billingCycleStart: payload.billingCycleStart, billingCycleEnd: payload.billingCycleEnd,
      planUsage: Object.fromEntries(['totalPercentUsed','autoPercentUsed','apiPercentUsed'].map(k => [k,payload.planUsage?.[k]]))};
  process.stdout.write(JSON.stringify({payload: safe, valid_until_epoch: claim.exp - 300}) + '\n');
})().then(() => process.exit(0)).catch(() => { process.stderr.write('Native quota source unavailable\n'); process.exit(1); });

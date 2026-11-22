// deploy-air.sh tests. deploy-air.sh itself is never run here: its pure checks live in
// deploy-lib.sh, sourced by bash with a FAKE dscl / dseditgroup first on PATH, and the
// script's own text is checked for the order "every check, then the first change".
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, readFile, rm, chmod } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const LIB = join(HERE, 'deploy-lib.sh');
const ROLE = { name: '_cortados-office', uid: '480', home: '/opt/cortados-office/home/bridge' };
const GOOD = { UniqueID: '480', PrimaryGroupID: '20', NFSHomeDirectory: ROLE.home, UserShell: '/usr/bin/false',
  Password: '*', IsHidden: '1', RealName: 'CortadOS Office role user' };

const FAKE_DSCL = `#!/bin/bash
# fake dscl: ". -list /Users UniqueID" and ". -read /Users/NAME [KEY]" over $FAKE/users/NAME files
shift
case "$1" in
  -list) for f in "$FAKE"/users/*; do [ -e "$f" ] || continue
           echo "$(basename "$f") $(awk -F': ' '$1 == "UniqueID" { print $2 }' "$f")"; done ;;
  -read) f="$FAKE/users/\${2#/Users/}"
         [ -f "$f" ] || { echo "DS Error: -14136 (eDSRecordNotFound)" >&2; exit 56; }
         if [ -n "$3" ]; then grep "^$3:" "$f" || { echo "No such key: $3" >&2; exit 181; }; else cat "$f"; fi ;;
  *) echo "fake dscl: unexpected $*" >&2; exit 99 ;;
esac
`;
const FAKE_DSEDITGROUP = `#!/bin/bash
# fake dseditgroup: "-o checkmember -m NAME admin" -> 0 when NAME is listed in $FAKE/admins
[ "$1 $2 $3" = "-o checkmember -m" ] || { echo "fake dseditgroup: unexpected $*" >&2; exit 99; }
grep -qx "$4" "$FAKE/admins" 2>/dev/null
`;

async function fakeHost(t, users, admins = []) {
  const dir = await mkdtemp(join(tmpdir(), 'office-deploy-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  await mkdir(join(dir, 'bin')); await mkdir(join(dir, 'users'));
  await writeFile(join(dir, 'bin', 'dscl'), FAKE_DSCL); await chmod(join(dir, 'bin', 'dscl'), 0o755);
  await writeFile(join(dir, 'bin', 'dseditgroup'), FAKE_DSEDITGROUP); await chmod(join(dir, 'bin', 'dseditgroup'), 0o755);
  for (const [name, attrs] of Object.entries(users)) {
    await writeFile(join(dir, 'users', name), Object.entries(attrs).map(([k, v]) => `${k}: ${v}`).join('\n') + '\n');
  }
  await writeFile(join(dir, 'admins'), admins.join('\n') + '\n');
  return dir;
}
function checkRole(dir, { name, uid, home } = ROLE) {
  const r = spawnSync('bash', ['-c', '. "$LIB"; check_role "$@"', 'check', name, uid, home], {
    env: { PATH: `${join(dir, 'bin')}:/usr/bin:/bin`, FAKE: dir, LIB, ROLE_GID: '20', ROLE_SHELL: '/usr/bin/false' },
    encoding: 'utf8' });
  return { code: r.status, out: r.stdout.trim(), err: r.stderr.trim() };
}

test('role check: a free name and UID is created; an exact role account is reused', async t => {
  assert.deepEqual(checkRole(await fakeHost(t, {})), { code: 0, out: 'create', err: '' });
  assert.deepEqual(checkRole(await fakeHost(t, { [ROLE.name]: GOOD })), { code: 0, out: 'reuse', err: '' });
});

test('role check: two accounts sharing the UID abort (the round-1 finding)', async t => {
  const r = checkRole(await fakeHost(t, { [ROLE.name]: GOOD, '_cortados-claw3d': { ...GOOD, NFSHomeDirectory: '/x' } }));
  assert.equal(r.code, 1);
  assert.match(r.err, /UID 480 is held by '_cortados-claw3d,_cortados-office'/);
  assert.equal(r.out, '');
});

test('role check: the UID held by another name, or the name on another UID, aborts', async t => {
  let r = checkRole(await fakeHost(t, { someone: { ...GOOD } }));
  assert.equal(r.code, 1); assert.match(r.err, /UID 480 is taken by 'someone'/);
  // the old preflight accepted either role name for either UID: the studio account on 480 is refused
  r = checkRole(await fakeHost(t, { '_cortados-claw3d': { ...GOOD } }));
  assert.equal(r.code, 1); assert.match(r.err, /taken by '_cortados-claw3d'/);
  r = checkRole(await fakeHost(t, { [ROLE.name]: { ...GOOD, UniqueID: '481' } }));
  assert.equal(r.code, 1); assert.match(r.err, /held by 'nobody'/);
});

test('role check: a reused account must have the home, disabled login and shell we create', async t => {
  const cases = [
    [{ NFSHomeDirectory: '/Users/_cortados-office' }, /home is not/],
    [{ UserShell: '/bin/zsh' }, /shell is not \/usr\/bin\/false/],
    [{ Password: '********' }, /password is not '\*'/],
    [{ AuthenticationAuthority: ';ShadowHash;HASHLIST:<SALTED-SHA512-PBKDF2>' }, /AuthenticationAuthority/],
    [{ IsHidden: '0' }, /not hidden/],
    [{ PrimaryGroupID: '80' }, /primary group is not 20/],
  ];
  for (const [patch, why] of cases) {
    const r = checkRole(await fakeHost(t, { [ROLE.name]: { ...GOOD, ...patch } }));
    assert.equal(r.code, 1, JSON.stringify(patch));
    assert.match(r.err, why);
  }
  const { UserShell, ...noShell } = GOOD;
  assert.match(checkRole(await fakeHost(t, { [ROLE.name]: noShell })).err, /shell is not/);
  const admin = checkRole(await fakeHost(t, { [ROLE.name]: GOOD }, [ROLE.name]));
  assert.equal(admin.code, 1); assert.match(admin.err, /is an admin/);
});

function portState(json, port = '8446', target = 'http://127.0.0.1:3000') {
  const r = spawnSync('bash', ['-c', '. "$LIB"; serve_port_state "$@"', 'ps', port, target],
    { env: { PATH: '/usr/bin:/bin', LIB }, input: typeof json === 'string' ? json : JSON.stringify(json), encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  return r.stdout.trim();
}
// The Air's serve config as README.md records it (2026-11-13): 443 funnel /mcp, 8444 serve -> provider.
const AIR = { TCP: { 443: { HTTPS: true }, 8444: { HTTPS: true } },
  Web: { 'air.example.ts.net:443': { Handlers: { '/mcp': { Proxy: 'http://127.0.0.1:8434/mcp' } } },
    'air.example.ts.net:8444': { Handlers: { '/': { Proxy: 'http://127.0.0.1:8432' } } } },
  AllowFunnel: { 'air.example.ts.net:443': true } };

test('tailnet port check: free next to the Air\'s serves; theirs are never ours', () => {
  assert.equal(portState(AIR), 'free');
  assert.equal(portState({}), 'free');
  assert.equal(portState(AIR, '8444'), 'other');                     // the provider's serve
  assert.equal(portState(AIR, '443'), 'funnel');                     // public: refused, never reused
  assert.equal(portState('not json'), 'other');
});

test('tailnet port check: our exact tailnet-only proxy is recognised; anything near it is not', () => {
  const ours = structuredClone(AIR);
  ours.TCP[8446] = { HTTPS: true };
  ours.Web['air.example.ts.net:8446'] = { Handlers: { '/': { Proxy: 'http://127.0.0.1:3000' } } };
  assert.equal(portState(ours), 'ours');
  const funneled = structuredClone(ours);
  funneled.AllowFunnel['air.example.ts.net:8446'] = true;
  assert.equal(portState(funneled), 'funnel');
  const elsewhere = structuredClone(ours);
  elsewhere.Web['air.example.ts.net:8446'].Handlers['/'].Proxy = 'http://127.0.0.1:18789';   // the bridge: never
  assert.equal(portState(elsewhere), 'other');
  const extra = structuredClone(ours);
  extra.Web['air.example.ts.net:8446'].Handlers['/admin'] = { Proxy: 'http://127.0.0.1:9999' };
  assert.equal(portState(extra), 'other');
  const plain = structuredClone(ours);
  plain.TCP[8446] = { HTTP: true };
  assert.equal(portState(plain), 'other');
});

test('deploy-air.sh: every check comes before the first change, and the order is stage -> switch -> restart', async () => {
  const lines = (await readFile(join(HERE, 'deploy-air.sh'), 'utf8')).split('\n');
  const at = re => { const i = lines.findIndex(l => re.test(l)); assert.ok(i >= 0, String(re)); return i; };
  const installStart = at(/^# install \/ upgrade$/);
  const plan = at(/echo "  plan: /);
  const firstChange = lines.findIndex((l, i) => i > installStart && /^\s*(run|as_studio|create_user|switch_link|restart|write_root_file|new_secret) /.test(l));
  assert.ok(firstChange > plan, `first change at line ${firstChange + 1} precedes the plan at ${plan + 1}`);
  // between the install banner and the plan: reads and dies only
  for (let i = installStart; i < plan; i++) {
    const code = lines[i].replace(/^\s*#.*/, '').replace(/"(?:[^"\\]|\\.)*"/g, '""');   // messages are not commands
    assert.ok(!/(^|[\s;&|(])(run|git clone|npm|sudo (install|mv|rm|ln|dscl|launchctl|tee|chown|chmod))\b/.test(code),
      `a change in the preflight: ${lines[i]}`);
  }
  const build = at(/run build && touch \.cortados-build-ok/);
  const switchAdapter = at(/^switch_link "\$ADAPTER_DIR" "\$ROOT\/adapter"/);
  const switchStudio = at(/^switch_link "\$BUILD" "\$ROOT\/claw3d\/current"/);
  const restartBridge = at(/^restart "\$BRIDGE_LABEL"/);
  assert.ok(build < switchAdapter && switchAdapter < switchStudio && switchStudio < restartBridge);
  // no clone into a fixed, possibly existing directory: only into the per-commit build dir, after clearing a failed one
  const clones = lines.filter(l => /git clone/.test(l));
  assert.deepEqual(clones.map(l => l.trim()), ['as_studio git clone --no-checkout "$CLAW3D_REPO" "$BUILD"']);
  // a loaded daemon is booted out before it is bootstrapped again
  assert.ok(at(/run sudo launchctl bootout "system\/\$1"/) < at(/run sudo launchctl bootstrap system "\$p"/));
});

test('deploy-air.sh: the tailnet step serves HTTPS to loopback Studio only and never turns funnel on', async () => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const cmds = text.split('\n').filter(l => /"\$TS" (serve|funnel)/.test(l) && !/status/.test(l));
  for (const l of cmds) {
    if (/funnel/.test(l)) assert.match(l, /funnel --https="\$TAILNET_PORT" off/, l);
    else assert.match(l, /serve (--bg --https="\$TAILNET_PORT" "http:\/\/127\.0\.0\.1:\$STUDIO_PORT"|--https="\$TAILNET_PORT" off)/, l);
  }
  assert.ok(cmds.some(l => /serve --bg --https=/.test(l)));
  assert.match(text, /^TAILNET_PORT=8446$/m);
  assert.ok(!/^TAILNET_PORT=(443|8444)$/m.test(text));
  assert.match(text, /export HOST=127\.0\.0\.1 PORT=\$STUDIO_PORT/);           // Studio itself stays on loopback
  assert.match(text, /export CORTADOS_OFFICE_BRIDGE_HOST=127\.0\.0\.1/);       // and so does the bridge
  assert.match(text, /STUDIO_ACCESS_TOKEN="\\\$\(cat "\$ACCESS_TOKEN"\)"/);   // the gate is on before any publish
});

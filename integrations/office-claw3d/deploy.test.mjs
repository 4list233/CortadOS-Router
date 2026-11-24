// deploy-air.sh tests. deploy-air.sh itself is never run here: its pure checks live in
// deploy-lib.sh, sourced by bash with a FAKE dscl / dseditgroup first on PATH, and the
// script's own text is checked for the order "every check, then the first change".
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { mkdtemp, mkdir, writeFile, readFile, rm, chmod } from 'node:fs/promises';
import { existsSync, readFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join, resolve as resolvePath } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = fileURLToPath(new URL('.', import.meta.url));
const LIB = join(HERE, 'deploy-lib.sh');
const ROLE = { name: '_cortados-office', uid: '480', home: '/opt/cortados-office/home/bridge' };
const REALNAME = 'CortadOS Office role user';
// Keys as real dscl names them (captured on the Air, 2026-11-23): IsHidden is a native attribute.
const GOOD = { UniqueID: '480', PrimaryGroupID: '20', NFSHomeDirectory: ROLE.home, UserShell: '/usr/bin/false',
  Password: '*', IsHidden: '1', RealName: REALNAME };
const NATIVE = new Set(['IsHidden']);
// The record exactly as `dscl -plist . -read /Users/_cortados-office` printed it on the Air after the
// stopped --apply of 2026-11-23 (every attribute set, no AuthenticationAuthority).
const HALF_CREATED = join(HERE, 'fixtures', 'dscl-half-created-cortados-office.plist');

// The fake behaves as the real dscl does (macOS 26.6.2): a missing record is DS Error -14136, exit 56;
// `-plist . -read /Users/U KEY...` prints a plist dict holding only the keys present (`<dict/>` when
// none), exit 0; the text form prints "No such key: KEY" on stderr and STILL exits 0 (the bug of #77).
// Users are plist records in $FAKE/users/NAME, keys prefixed as dscl prefixes them.
const FAKE_DSCL = `#!/usr/bin/env python3
import os, plistlib, sys
fake = os.environ["FAKE"]
argv = sys.argv[1:]
plist = argv[:1] == ["-plist"]
if plist:
    argv = argv[1:]
def load(name):
    try:
        with open(os.path.join(fake, "users", name), "rb") as f:
            return plistlib.load(f)
    except FileNotFoundError:
        sys.stderr.write("<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)\\n"); sys.exit(56)
def short(k):
    return k.split(":", 1)[1] if k.startswith("dsAttrType") else k
def override(name):
    # $FAKE/NAME, when present, holds "STATUS|STDOUT|STDERR" (a literal backslash-n is a newline) and replaces the answer
    path = os.path.join(fake, name)
    if os.path.exists(path):
        st, out, err = open(path).read().rstrip("\\n").split("|", 2)
        sys.stdout.write(out.replace("\\\\n", "\\n")); sys.stderr.write(err.replace("\\\\n", "\\n")); sys.exit(int(st))
if argv[:4] == [".", "-list", "/Users", "UniqueID"] and not plist:
    override("list-override")
    # a real directory always holds the system accounts (captured: root 0, daemon 1, the _ accounts)
    print("daemon                   1")
    print("root                     0")
    for n in sorted(os.listdir(os.path.join(fake, "users"))):
        for k, v in load(n).items():
            if short(k) == "UniqueID":
                print(n, v[0])
elif argv[:2] == [".", "-read"] and len(argv) >= 3 and argv[2].startswith("/Users/"):
    if os.path.exists(os.path.join(fake, "fail-plist")) and plist and len(argv) > 3:
        sys.stderr.write("<dscl_cmd> DS Error: -14987 (eDSPermissionError)\\n"); sys.exit(40)
    if not plist:
        override("read-override")
    rec, keys = load(argv[2][len("/Users/"):]), argv[3:]
    got = {k: v for k, v in rec.items() if not keys or short(k) in keys or k in keys}
    if plist:
        sys.stdout.buffer.write(plistlib.dumps(got))
    else:
        for k, v in got.items():
            print("%s: %s" % (k, " ".join(v)))
        for k in keys:
            if not any(short(g) == k or g == k for g in got):
                sys.stderr.write("No such key: %s\\n" % k)
else:
    sys.stderr.write("fake dscl: unexpected %s\\n" % sys.argv[1:]); sys.exit(99)
`;
const FAKE_DSEDITGROUP = `#!/bin/bash
# fake dseditgroup, answering as the real one does (captured on the Air, macOS 26):
# "-o checkmember -m NAME admin" -> exit 0 "yes NAME is a member of admin" when NAME is listed in
# $FAKE/admins, else exit 67 "no NAME is NOT a member of admin". $FAKE/dsedit-override, when present,
# holds "STATUS|STDOUT|STDERR" and replaces the answer (failures, unexpected output).
[ "$1 $2 $3" = "-o checkmember -m" ] || { echo "fake dseditgroup: unexpected $*" >&2; exit 99; }
if [ -f "$FAKE/dsedit-override" ]; then
  IFS='|' read -r st out err < "$FAKE/dsedit-override"
  [ -n "$out" ] && echo "$out"; [ -n "$err" ] && echo "$err" >&2; exit "$st"
fi
if grep -qx "$4" "$FAKE/admins" 2>/dev/null; then echo "yes $4 is a member of $5"; exit 0; fi
echo "no $4 is NOT a member of $5"; exit 67
`;

const xml = v => String(v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
// attrs: short name -> value or [values]; a key already holding a dsAttrType prefix is kept as given
function plistRecord(attrs) {
  const body = Object.entries(attrs).map(([k, v]) => {
    const key = k.startsWith('dsAttrType') ? k : `dsAttrType${NATIVE.has(k) ? 'Native' : 'Standard'}:${k}`;
    const vals = (Array.isArray(v) ? v : [v]).map(x => `\t\t<string>${xml(x)}</string>`).join('\n');
    return `\t<key>${xml(key)}</key>\n\t<array>\n${vals}\n\t</array>`;
  }).join('\n');
  return `<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" ` +
    `"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n${body}\n</dict>\n</plist>\n`;
}

async function fakeHost(t, users, admins = [], { failPlist = false, list, read } = {}) {
  const dir = await mkdtemp(join(tmpdir(), 'office-deploy-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  await mkdir(join(dir, 'bin')); await mkdir(join(dir, 'users'));
  await writeFile(join(dir, 'bin', 'dscl'), FAKE_DSCL); await chmod(join(dir, 'bin', 'dscl'), 0o755);
  await writeFile(join(dir, 'bin', 'dseditgroup'), FAKE_DSEDITGROUP); await chmod(join(dir, 'bin', 'dseditgroup'), 0o755);
  for (const [name, attrs] of Object.entries(users)) {
    await writeFile(join(dir, 'users', name), typeof attrs === 'string' ? attrs : plistRecord(attrs));
  }
  await writeFile(join(dir, 'admins'), admins.join('\n') + '\n');
  if (failPlist) await writeFile(join(dir, 'fail-plist'), '');
  if (list !== undefined) await writeFile(join(dir, 'list-override'), list + '\n');
  if (read !== undefined) await writeFile(join(dir, 'read-override'), read + '\n');
  return dir;
}
function checkRole(dir, { name, uid, home } = ROLE) {
  const r = spawnSync('bash', ['-c', '. "$LIB"; check_role "$@"', 'check', name, uid, home], {
    env: { PATH: `${join(dir, 'bin')}:/usr/bin:/bin`, FAKE: dir, LIB, ROLE_GID: '20', ROLE_SHELL: '/usr/bin/false',
      ROLE_REALNAME: REALNAME },
    encoding: 'utf8' });
  return { code: r.status, out: r.stdout.trim(), err: r.stderr.trim() };
}
function dscl(dir, ...args) {
  const r = spawnSync(join(dir, 'bin', 'dscl'), args, { env: { PATH: '/usr/bin:/bin', FAKE: dir }, encoding: 'utf8' });
  return { code: r.status, out: r.stdout, err: r.stderr };
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
    [{ RealName: 'Someone' }, /RealName is not/],
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

test('role check: "not an admin" only on dseditgroup\'s exact confirmed negative (#77 fix round 1)', async t => {
  const N = ROLE.name;
  const run = async override => {
    const dir = await fakeHost(t, { [N]: GOOD });
    if (override !== undefined) await writeFile(join(dir, 'dsedit-override'), override + '\n');
    return checkRole(dir);
  };
  // captured on the Air: exit 67, stdout "no NAME is NOT a member of admin", stderr empty
  assert.deepEqual(await run(), { code: 0, out: 'reuse', err: '' });
  assert.deepEqual(await run(`67|no ${N} is NOT a member of admin|`), { code: 0, out: 'reuse', err: '' });
  // direct member (captured: exit 0 "yes NAME is a member of admin")
  let r = await run(`0|yes ${N} is a member of admin|`);
  assert.equal(r.code, 1); assert.equal(r.out, ''); assert.match(r.err, /is an admin/);
  // nested member: dseditgroup resolves nested groups and answers the same way (captured: root, a member
  // of com.apple.access_ssh only through its nested admin group, gets exit 0 "yes root is a member ...")
  r = checkRole(await fakeHost(t, { [N]: GOOD }, [N]));
  assert.equal(r.code, 1); assert.match(r.err, /is an admin/);
  // failures and unexpected answers refuse, never "reuse"
  for (const o of [
    '2||dseditgroup: directory services unavailable',      // the reviewer's case
    '64||Unable to find the user record',                   // captured for an unknown user
    '1|no ' + N + ' is NOT a member of admin|',             // right text, wrong status
    '67||',                                                 // right status, no text
    '67|no someone-else is NOT a member of admin|',         // another user's answer
    '67|no ' + N + ' is not a member of admin|',            // changed wording
    '0||',                                                  // success with no answer
    '0|maybe|',                                             // unexpected output on success
  ]) {
    r = await run(o);
    assert.equal(r.code, 1, o); assert.equal(r.out, '', o);
    assert.match(r.err, o.startsWith('0|') ? /cannot establish that .* is not an admin/ : /cannot establish/, o);
  }
});

test('the fake dscl answers as the real one does, including the exit 0 on "No such key" (#77)', async t => {
  const dir = await fakeHost(t, { [ROLE.name]: await readFile(HALF_CREATED, 'utf8') });
  // captured on the Air: stdout "", stderr "No such key: AuthenticationAuthority", exit 0
  assert.deepEqual(dscl(dir, '.', '-read', `/Users/${ROLE.name}`, 'AuthenticationAuthority'),
    { code: 0, out: '', err: 'No such key: AuthenticationAuthority\n' });
  const p = dscl(dir, '-plist', '.', '-read', `/Users/${ROLE.name}`, 'AuthenticationAuthority');
  assert.equal(p.code, 0); assert.match(p.out, /<dict\/>/);
  assert.equal(dscl(dir, '.', '-read', '/Users/_cortados-claw3d').code, 56);
});

test('role check: an absent AuthenticationAuthority passes; ShadowHash, Kerberos, SecureToken or an unreadable record refuse (#77)', async t => {
  // the real half-created record from the Air: every attribute as specified, no authority -> adopted
  assert.deepEqual(checkRole(await fakeHost(t, { [ROLE.name]: await readFile(HALF_CREATED, 'utf8') })),
    { code: 0, out: 'reuse', err: '' });
  const refused = [
    [';ShadowHash;HASHLIST:<SALTED-SHA512-PBKDF2,SRP-RFC5054-4096-SHA512-PBKDF2>'],
    [';Kerberosv5;;_cortados-office@LKDC:SHA1.0123456789ABCDEF;LKDC:SHA1.0123456789ABCDEF;'],
    [';ShadowHash;HASHLIST:<SALTED-SHA512-PBKDF2>', ';SecureToken;', ';Kerberosv5;;x@LKDC:SHA1.00;LKDC:SHA1.00;'],
    [''],
  ];
  for (const aa of refused) {
    const r = checkRole(await fakeHost(t, { [ROLE.name]: { ...GOOD, AuthenticationAuthority: aa } }));
    assert.equal(r.code, 1, JSON.stringify(aa)); assert.equal(r.out, '');
    assert.match(r.err, /has an AuthenticationAuthority/);
  }
  const unreadable = checkRole(await fakeHost(t, { [ROLE.name]: GOOD }, [], { failPlist: true }));
  assert.equal(unreadable.code, 1); assert.equal(unreadable.out, '');
});

test('role check: a re-run adopts the account a stopped --apply left behind only when it matches (#77)', async t => {
  // adopted: exactly the spec, written as dscl -create writes it
  assert.equal(checkRole(await fakeHost(t, { [ROLE.name]: GOOD })).out, 'reuse');
  // never adopted: a step that did not run (no Password, no IsHidden) or a value that differs
  const { Password, ...noPassword } = GOOD; const { IsHidden, ...notHidden } = GOOD;
  for (const [rec, why] of [[noPassword, /password is not/], [notHidden, /not hidden/],
    [{ ...GOOD, RealName: 'Other' }, /RealName is not/], [{ ...GOOD, UserShell: '/bin/sh' }, /shell is not/]]) {
    const r = checkRole(await fakeHost(t, { [ROLE.name]: rec }));
    assert.equal(r.code, 1); assert.equal(r.out, ''); assert.match(r.err, why);
  }
  // the second role user, never created, is still "create" next to the adopted first one
  assert.equal(checkRole(await fakeHost(t, { [ROLE.name]: GOOD }),
    { name: '_cortados-claw3d', uid: '481', home: '/opt/cortados-office/home/studio' }).out, 'create');
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

test('deploy-air.sh: every check comes before the first change, and the order is stage -> activate -> commit -> prune', async () => {
  const lines = (await readFile(join(HERE, 'deploy-air.sh'), 'utf8')).split('\n');
  const at = re => { const i = lines.findIndex(l => re.test(l)); assert.ok(i >= 0, String(re)); return i; };
  const installStart = at(/^# install \/ upgrade$/);
  const plan = at(/echo "  plan: /);
  const firstChange = lines.findIndex((l, i) => i > installStart && /^\s*(run|as_studio|create_user|switch_link|tx_run|write_root_file|new_secret) /.test(l));
  assert.ok(firstChange > plan, `first change at line ${firstChange + 1} precedes the plan at ${plan + 1}`);
  // between the install banner and the plan: reads and dies only
  for (let i = installStart; i < plan; i++) {
    const code = lines[i].replace(/^\s*#.*/, '').replace(/"(?:[^"\\]|\\.)*"/g, '""');   // messages are not commands
    assert.ok(!/(^|[\s;&|(])(run|git clone|npm|sudo (install|mv|rm|ln|dscl|launchctl|tee|chown|chmod)|clients (add|remove))\b/.test(code),
      `a change in the preflight: ${lines[i]}`);
  }
  const build = at(/run build && touch \.cortados-build-ok/);
  const stageToken = at(/run sudo install -o "\$BRIDGE_USER" -g staff -m 0600 "\$TMP\/office-token" "\$STAGE\/office-token"/);
  const activate = at(/^tx_rc=0; tx_run \|\| tx_rc=\$\?$/);
  const revokeOld = at(/clients remove "\$LIVE_SLOT"/);
  const prune = at(/^step "9\. Prune/);
  assert.ok(build < activate && stageToken < activate && activate < revokeOld && revokeOld < prune);
  // the old credential is revoked nowhere else: never before the new release is healthy
  assert.equal(lines.filter(l => /provider\.clients remove/.test(l)).length, 2);   // the commit, and on_exit's pending (new) slot
  assert.ok(at(/clients remove "\$PENDING_CLIENT"/) < activate);         // defined in on_exit, which runs at exit only
  // launchers and plists are staged, never written over the live ones before activation
  assert.ok(lines.filter(l => /^write_root_file |\| write_root_file /.test(l.trim())).every(l => !/\/(bin|Library)\//.test(l)));
  for (const f of ['run-bridge.sh', 'run-studio.sh']) assert.ok(lines.some(l => l.includes(`"$STAGE/${f}|$ROOT/bin/${f}"`)), f);
  assert.ok(lines.some(l => l.includes('[[ $TOKEN_PLAN == keep ]] || TX_FILES+=("$STAGE/office-token|$OFFICE_TOKEN|$BRIDGE_USER")')));
  // no clone into a fixed, possibly existing directory: only into the per-commit build dir, after clearing a failed one
  const clones = lines.filter(l => /git clone/.test(l));
  assert.deepEqual(clones.map(l => l.trim()), ['as_studio git clone --no-checkout "$CLAW3D_REPO" "$BUILD"']);
  // a loaded daemon is booted out before it is bootstrapped again (tx_restart), and the stop is waited for
  assert.match(lines.join('\n'), /daemon_stop\(\) \{\n  run sudo launchctl bootout "system\/\$1"/);
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

// ---- round 2: a failed `tailscale serve status` is never "free" ------------------------------------
async function fakeTailscale(t, { code = 0, stdout = '' }) {
  const dir = await mkdtemp(join(tmpdir(), 'office-ts-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  await writeFile(join(dir, 'out'), stdout);
  const bin = join(dir, 'tailscale');
  await writeFile(bin, `#!/bin/bash\n[ "$1 $2 $3" = "serve status --json" ] || { echo "unexpected $*" >&2; exit 99; }\ncat "${join(dir, 'out')}"\necho "fake tailscale failing" >&2\nexit ${code}\n`);
  await chmod(bin, 0o755);
  return bin;
}
function serveState(bin, port = '8446', target = 'http://127.0.0.1:3000') {
  const r = spawnSync('bash', ['-c', '. "$LIB"; serve_state "$@"', 'ss', bin, port, target],
    { env: { PATH: '/usr/bin:/bin', LIB }, encoding: 'utf8' });
  return { code: r.status, out: r.stdout.trim() };
}

test('tailnet port check: a failed status command aborts instead of reading as a free port (round-2 finding)', async t => {
  // tailscaled down / logged out: non-zero exit, nothing on stdout. Before round 2 this became '{}' -> "free".
  assert.deepEqual(serveState(await fakeTailscale(t, { code: 1 })), { code: 1, out: '' });
  // a failure is a failure even when the command printed something
  assert.deepEqual(serveState(await fakeTailscale(t, { code: 1, stdout: '{}' })), { code: 1, out: '' });
  // an empty config is accepted only from a successful command
  assert.deepEqual(serveState(await fakeTailscale(t, { code: 0, stdout: '' })), { code: 0, out: 'free' });
  assert.deepEqual(serveState(await fakeTailscale(t, { code: 0, stdout: '{}\n' })), { code: 0, out: 'free' });
  assert.deepEqual(serveState(await fakeTailscale(t, { code: 0, stdout: JSON.stringify(AIR) })), { code: 0, out: 'free' });
  assert.deepEqual(serveState(await fakeTailscale(t, { code: 0, stdout: JSON.stringify(AIR) }), '8444'), { code: 0, out: 'other' });
});

test('deploy-air.sh: publish and unpublish read the serve config only through serve_state, and die when it fails', async () => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  assert.ok(!/\|\| echo '\{\}'/.test(text), 'no fallback to an empty config');
  assert.ok(!/serve status --json/.test(text), 'the status command runs only in deploy-lib.sh');
  assert.match(text, /state="\$\(serve_state "\$TS" "\$TAILNET_PORT" "http:\/\/127\.0\.0\.1:\$STUDIO_PORT"\)" \\\n\s+\|\| die "tailscale serve status failed/);
  // the state is computed before either branch uses it
  assert.ok(text.indexOf('|| die "tailscale serve status failed') < text.indexOf('if [[ $MODE == unpublish ]]'));
});

// ---- round 2: activation is a transaction, exercised with injected failures ------------------------
// A fake host in a temp dir: real symlinks and files, daemons as state files recording the release they
// were started with (link targets + launcher text). FAIL lists steps that fail ONCE; FAIL_ALWAYS every time.
const TX_HARNESS = join(HERE, 'fixtures', 'tx-harness.sh');

async function txHost(t, { upgrade }) {
  const D = await mkdtemp(join(tmpdir(), 'office-tx-'));
  t.after(() => rm(D, { recursive: true, force: true }));
  for (const d of ['builds/old', 'builds/new', 'adapters/old', 'adapters/new', 'bin', 'stage', 'secrets', 'daemons']) {
    await mkdir(join(D, d), { recursive: true });
  }
  for (const f of ['run-bridge.sh', 'run-studio.sh']) await writeFile(join(D, 'stage', f), `new ${f}\n`);
  await writeFile(join(D, 'stage', 'token'), 'new-token\n');
  if (upgrade) {
    for (const f of ['run-bridge.sh', 'run-studio.sh']) await writeFile(join(D, 'bin', f), `old ${f}\n`);
    await writeFile(join(D, 'secrets', 'token'), 'old-token\n');
    const { symlink } = await import('node:fs/promises');
    await symlink(join(D, 'builds/old'), join(D, 'current'));
    await symlink(join(D, 'adapters/old'), join(D, 'adapter'));
    runTx(D, '', '', 'for l in bridge studio; do daemon_start $l; done; : > "$D/log"; exit 0');
  }
  return D;
}
function runTx(D, fail = '', failAlways = '', prelude = '', unknown = '') {
  const r = spawnSync('/bin/bash', [TX_HARNESS], {
    env: { PATH: '/usr/bin:/bin', LIB, FAKE: D, FAIL: fail, FAIL_ALWAYS: failAlways, PRELUDE: prelude, UNKNOWN: unknown },
    encoding: 'utf8' });
  return { status: r.status, out: r.stdout.trim(), err: r.stderr };
}
async function snapshot(D) {
  const { readlink, readdir } = await import('node:fs/promises');
  const link = async p => { try { return (await readlink(join(D, p))).slice(D.length + 1); } catch { return null; } };
  const file = async p => { try { return (await readFile(join(D, p), 'utf8')).trim(); } catch { return null; } };
  const daemons = {};
  for (const l of await readdir(join(D, 'daemons'))) {
    daemons[l] = (await file(`daemons/${l}`)).split('\n').map(x => x.startsWith(D) ? x.slice(D.length + 1) : x);
  }
  const leftovers = [...await readdir(join(D, 'bin')), ...await readdir(join(D, 'secrets'))].filter(n => /\.(prev|next)$/.test(n));
  return { current: await link('current'), adapter: await link('adapter'), bridge: await file('bin/run-bridge.sh'),
    studio: await file('bin/run-studio.sh'), token: await file('secrets/token'), daemons, leftovers };
}
const OLD = { current: 'builds/old', adapter: 'adapters/old', bridge: 'old run-bridge.sh', studio: 'old run-studio.sh',
  token: 'old-token', leftovers: [],
  daemons: { bridge: ['builds/old', 'adapters/old', 'old run-bridge.sh', 'old-token'],
    studio: ['builds/old', 'adapters/old', 'old run-studio.sh', 'old-token'] } };
const NEW = { current: 'builds/new', adapter: 'adapters/new', bridge: 'new run-bridge.sh', studio: 'new run-studio.sh',
  token: 'new-token', leftovers: [],
  daemons: { bridge: ['builds/new', 'adapters/new', 'new run-bridge.sh', 'new-token'],
    studio: ['builds/new', 'adapters/new', 'new run-studio.sh', 'new-token'] } };

test('activation: a healthy upgrade switches everything, restarts both daemons, and drops the .prev copies', async t => {
  const D = await txHost(t, { upgrade: true });
  assert.deepEqual(await snapshot(D), OLD);
  const r = runTx(D);
  assert.equal(r.out, 'rc=0', r.err);
  assert.deepEqual(await snapshot(D), NEW);
  assert.deepEqual((await readFile(join(D, 'log'), 'utf8')).trim().split('\n'),
    ['stop bridge', 'start bridge', 'stop studio', 'start studio', 'health']);
});

test('activation: a failure at any step of an upgrade restores links, files, the credential and the old daemons (round-2 finding)', async t => {
  // The review's case first: the bridge is already restarted on the new release when Studio fails to bootstrap.
  for (const point of ['start:studio', 'health', 'stop:studio', 'stop:bridge', 'start:bridge', 'link:current', 'link:adapter',
    'backup:run-studio.sh', 'promote:run-bridge.sh', 'promote:token']) {
    const D = await txHost(t, { upgrade: true });
    const r = runTx(D, point);
    assert.equal(r.out, 'rc=1', `${point}: ${r.err}`);
    assert.match(r.err, /ROLLBACK/, point);
    assert.deepEqual(await snapshot(D), OLD, point);
  }
});

test('activation: a failed fresh install leaves no links, no installed files and no daemons', async t => {
  for (const point of ['health', 'start:studio', 'promote:token']) {
    const D = await txHost(t, { upgrade: false });
    const r = runTx(D, point);
    assert.equal(r.out, 'rc=1', `${point}: ${r.err}`);
    assert.deepEqual(await snapshot(D), { current: null, adapter: null, bridge: null, studio: null, token: null,
      daemons: {}, leftovers: [] }, point);
  }
});

test('activation: TERM during the health check rolls back before exiting', async t => {
  const D = await txHost(t, { upgrade: true });
  const r = runTx(D, 'term');
  assert.equal(r.status, 143); assert.equal(r.out, '');      // exited from the trap, never reached "rc="
  assert.match(r.err, /ROLLBACK/);
  assert.deepEqual(await snapshot(D), OLD);
});

test('activation: a rollback that cannot restart the old daemon reports 2 (investigate), never success', async t => {
  const D = await txHost(t, { upgrade: true });
  const r = runTx(D, 'health', 'start:studio');
  assert.equal(r.out, 'rc=2', r.err);
  assert.match(r.err, /com|studio did not start again/);
  const s = await snapshot(D);
  assert.deepEqual({ ...s, daemons: undefined }, { ...OLD, daemons: undefined });   // files and links restored anyway
  assert.deepEqual(s.daemons, { bridge: OLD.daemons.bridge });
});

// ---- fix round 2 (#77): a lookup that failed is never a safe answer --------------------------------
// Captured read-only on the Air (macOS 26), 2026-11-23:
//   dscl . -read /Users/nosuchuser_xyz123        exit 56, stdout "", stderr "<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)"
//   dscl . -list /Users UniqueID                 exit 0, "NAME<spaces>UID" per line (134 lines)
//   dscl . -list /Nosuch                         exit 185, "<dscl_cmd> DS Error: -14009 (eDSUnknownNodeName)"
//   launchctl print system/com.nosuch.xyz        exit 113, stderr 'Bad request.\nCould not find service "com.nosuch.xyz" in domain for system'
const NOT_FOUND = '56||<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)';

test('role check: create only on a confirmed not-found and a successfully read, free UID (fix round 2)', async t => {
  // the confirmed not-found signature, explicitly: create
  assert.deepEqual(checkRole(await fakeHost(t, {}, [], { read: NOT_FOUND })), { code: 0, out: 'create', err: '' });
  // UID taken by another user, the name confirmed absent: refused
  let r = checkRole(await fakeHost(t, { someone: GOOD }, [], { read: NOT_FOUND }));
  assert.equal(r.code, 1); assert.equal(r.out, ''); assert.match(r.err, /UID 480 is taken by 'someone'/);
  // the UID list fails or answers unexpectedly: refused, never "UID free"
  for (const list of [
    '2||DS Error: directory services unavailable',               // the reviewer's case
    '185||<dscl_cmd> DS Error: -14009 (eDSUnknownNodeName)',      // captured for a bad node
    '0||',                                                        // success, but no accounts at all
    '0|root 0\\nsomeone|',                                        // a line without a UID
    '0|root 0\\nsomeone 480 481|',                                // more than NAME UID
    '0|<dict/>|',                                                 // another format entirely
  ]) {
    r = checkRole(await fakeHost(t, {}, [], { list }));
    assert.equal(r.code, 1, list); assert.equal(r.out, '', list);
    assert.match(r.err, /cannot list the accounts' UIDs/, list);
  }
  // the account read fails with anything but the exact not-found signature: refused, never "absent"
  for (const read of [
    '2||DS Error: directory services unavailable',                // the reviewer's case
    '40||<dscl_cmd> DS Error: -14987 (eDSPermissionError)',
    '56||',                                                        // right status, no message
    '56||<dscl_cmd> DS Error: -14136 (eDSRecordNotFound) extra',   // changed message
    '56|RecordName: x|<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)',   // output on stdout too
    '1||<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)',          // right message, wrong status
  ]) {
    r = checkRole(await fakeHost(t, {}, [], { read }));
    assert.equal(r.code, 1, read); assert.equal(r.out, '', read);
    assert.match(r.err, /cannot establish whether _cortados-office exists/, read);
  }
});

test('the fake dscl\'s not-found matches the captured one, and its overrides answer as given', async t => {
  const dir = await fakeHost(t, {});
  assert.deepEqual(dscl(dir, '.', '-read', '/Users/nosuchuser_xyz123'),
    { code: 56, out: '', err: '<dscl_cmd> DS Error: -14136 (eDSRecordNotFound)\n' });
  const o = await fakeHost(t, {}, [], { list: '2||boom' });
  assert.deepEqual(dscl(o, '.', '-list', '/Users', 'UniqueID'), { code: 2, out: '', err: 'boom' });
});

async function fakeBin(t, name, script) {
  const dir = await mkdtemp(join(tmpdir(), 'office-bin-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  await writeFile(join(dir, name), script); await chmod(join(dir, name), 0o755);
  return dir;
}
function libCall(binDir, call, ...args) {
  const r = spawnSync('bash', ['-c', `. "$LIB"; ${call} "$@"`, 'x', ...args],
    { env: { PATH: `${binDir}:/usr/bin:/bin`, LIB }, encoding: 'utf8' });
  return { code: r.status, out: r.stdout.trim() };
}

test('root_test: yes / no only from test\'s own answer; a refused sudo or a test error is unknown (fix round 2)', async t => {
  // a fake sudo that runs the command (as the owner) — the real test semantics
  const ok = await fakeBin(t, 'sudo', '#!/bin/bash\nexec "$@"\n');
  assert.deepEqual(libCall(ok, 'root_test', '-e', '/bin/sh'), { code: 0, out: 'yes' });
  assert.deepEqual(libCall(ok, 'root_test', '-e', '/nonexistent/x'), { code: 0, out: 'no' });
  assert.deepEqual(libCall(ok, 'root_test', '-e', '/x', '-o'), { code: 1, out: '' });          // test's own error (2)
  // sudo refused (no password / not allowed): exit 1, the same status as "false" from a bare `sudo test`
  const refused = await fakeBin(t, 'sudo', '#!/bin/bash\necho "sudo: a password is required" >&2\nexit 1\n');
  assert.deepEqual(libCall(refused, 'root_test', '-e', '/nonexistent/x'), { code: 1, out: '' });
  const odd = await fakeBin(t, 'sudo', '#!/bin/bash\necho maybe\n');
  assert.deepEqual(libCall(odd, 'root_test', '-e', '/bin/sh'), { code: 1, out: '' });
});

// a fake sudo that runs the command as the caller, and one that refuses (no password / not allowed)
const SUDO_OK = '#!/bin/bash\nexec "$@"\n';
const SUDO_REFUSED = '#!/bin/bash\necho "sudo: a password is required" >&2\nexit 1\n';
// A fake sudo that runs the command "as root" (FAKE_ROOT=1), and a fake launchctl blind to the system
// domain unless run as root, as the real one is: on the Air (macOS 26, 2026-11-23) the owner's
// `launchctl print system/com.apple.mDNSResponder` exits 113 "Could not find service" while it runs.
// sudo as the fake root; `sudo -u USER` runs the command as the test user, without FAKE_ROOT
const SUDO_ROOT = '#!/bin/bash\nif [ "$1" = -u ]; then shift 2; else export FAKE_ROOT=1; fi\nexec "$@"\n';
const launchctlAsRoot = (code, out) => `#!/bin/bash
[ "$1" = print ] || exit 99
if [ "\${FAKE_ROOT:-}" != 1 ]; then
  printf 'Bad request.\\nCould not find service "%s" in domain for system\\n' "\${2#system/}" >&2; exit 113
fi
printf '%s' ${JSON.stringify(out)} >&2
exit ${code}
`;

test('launchd_state: not loaded only on launchctl\'s captured "Could not find service" (fix round 2)', async t => {
  const L = 'com.cortados.office-bridge';
  const fake = (code, err) => fakeBins(t, { sudo: SUDO_ROOT, launchctl: launchctlAsRoot(code, err) });
  const st = dir => libCall(dir, 'launchd_state', L).code;
  assert.equal(st(await fake(0, '')), 0);
  assert.equal(st(await fake(113, `Bad request.\nCould not find service "${L}" in domain for system\n`)), 1);
  assert.equal(st(await fake(113, `Bad request.\nCould not find service "other.label" in domain for system\n`)), 2);
  assert.equal(st(await fake(113, '')), 2);
  assert.equal(st(await fake(1, `Could not find service "${L}" in domain for system\n`)), 2);
  assert.equal(st(await fake(5, 'Input/output error\n')), 2);
});

test('launchd_state / launchd_running read the system domain as root; a refused sudo is unknown, never "not loaded" (#77 launchd-sudo)', async t => {
  const L = 'com.cortados.office-bridge';
  const RUNNING = `${'system/' + L} = {\n\tstate = running\n}\n`;
  // launchd answers "loaded and running" only to root
  const lc = `#!/bin/bash
[ "$1" = print ] || exit 99
if [ "\${FAKE_ROOT:-}" != 1 ]; then
  printf 'Bad request.\\nCould not find service "%s" in domain for system\\n' "\${2#system/}" >&2; exit 113
fi
printf '%s' ${JSON.stringify(RUNNING)}
`;
  const root = await fakeBins(t, { sudo: SUDO_ROOT, launchctl: lc });
  assert.equal(libCall(root, 'launchd_state', L).code, 0);         // the Air's bug: this read 1 (not loaded)
  assert.equal(libCall(root, 'launchd_running', L).code, 0);
  // a sudo that runs the command WITHOUT root (as the old code did): launchctl's blind answer
  const notRoot = await fakeBins(t, { sudo: SUDO_OK, launchctl: lc });
  assert.equal(libCall(notRoot, 'launchd_state', L).code, 1);
  assert.equal(libCall(notRoot, 'launchd_running', L).code, 1);
  // sudo refused: unknown (2), never the "confirmed not loaded" that would skip a stop or a restart
  const refused = await fakeBins(t, { sudo: SUDO_REFUSED, launchctl: lc });
  assert.equal(libCall(refused, 'launchd_state', L).code, 2);
  assert.equal(libCall(refused, 'launchd_running', L).code, 1);
  // a refused sudo that even prints launchctl's not-found text and exits 113 is still not an answer
  const mimic = await fakeBins(t, { launchctl: lc,
    sudo: `#!/bin/bash\nprintf 'Could not find service "${L}" in domain for system\\n'\nexit 113\n` });
  assert.equal(libCall(mimic, 'launchd_state', L).code, 2);
  // a sudo that answers something else (no rc line from the shell) is unknown too
  const odd = await fakeBins(t, { launchctl: lc, sudo: '#!/bin/bash\necho "state = running"\n' });
  assert.equal(libCall(odd, 'launchd_state', L).code, 2);
  assert.equal(libCall(odd, 'launchd_running', L).code, 1);
  // loaded but not running (e.g. waiting on KeepAlive's throttle) is not healthy
  const waiting = await fakeBins(t, { sudo: SUDO_ROOT, launchctl: lc.replace('state = running', 'state = waiting') });
  assert.equal(libCall(waiting, 'launchd_state', L).code, 0);
  assert.equal(libCall(waiting, 'launchd_running', L).code, 1);
  // a running state printed with a failing exit is not "running"
  const failed = await fakeBins(t, { sudo: SUDO_ROOT, launchctl: lc + 'exit 5\n' });
  assert.equal(libCall(failed, 'launchd_running', L).code, 1);
  assert.equal(libCall(failed, 'launchd_state', L).code, 2);
});

test('activation: a daemon or file whose state cannot be read stops the transaction, never reads as absent (fix round 2)', async t => {
  // before anything changes: nothing changed, the old release keeps running
  let D = await txHost(t, { upgrade: true });
  let r = runTx(D, '', '', '', 'loaded:studio');
  assert.equal(r.out, 'rc=1', r.err); assert.match(r.err, /cannot tell whether studio is loaded/);
  assert.deepEqual(await snapshot(D), OLD);
  assert.equal((await readFile(join(D, 'log'), 'utf8')).trim(), '');
  // an installed file that cannot be checked is never "absent" (whose undo would delete it): rolled back
  D = await txHost(t, { upgrade: true });
  r = runTx(D, '', '', '', 'exists:token');
  assert.equal(r.out, 'rc=1', r.err); assert.match(r.err, /cannot tell whether .*token exists/);
  assert.deepEqual(await snapshot(D), OLD);
  // a link that cannot be read is never "absent" (whose undo would remove it): rolled back, link kept
  D = await txHost(t, { upgrade: true });
  r = runTx(D, '', '', '', 'readlink:adapter');
  assert.equal(r.out, 'rc=1', r.err); assert.match(r.err, /switching .*adapter failed/);
  assert.deepEqual(await snapshot(D), OLD);
});

test('deploy-air.sh: root checks, daemon state and links go through the tri-state helpers (fix round 2)', async () => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  assert.ok(!/sudo test /.test(text), 'no bare `sudo test` (a refused sudo reads as false)');
  assert.ok(!/launchctl print "system\/\$1" >\/dev\/null 2>&1/.test(text), 'no exit-code-only launchctl print');
  assert.ok(!/fs_readlink\(\) \{ readlink "\$1" 2>\/dev\/null \|\| true; \}/.test(text), 'no readlink failure read as absent');
  assert.match(text, /as_root_test\(\) \{\n  local a; a="\$\(root_test "\$@"\)" \|\| die /);
  assert.match(text, /daemon_loaded\(\) \{ launchd_state "\$1"; \}/);
  const lib = await readFile(LIB, 'utf8');
  assert.match(lib, /holders="\$\(uid_holders "\$uid"\)" \|\| die /);
  assert.match(lib, /state="\$\(account_state "\$name"\)" \|\| die /);
});

// ---- fix round 3 -------------------------------------------------------------------------------
async function fakeBins(t, scripts) {
  const dir = await mkdtemp(join(tmpdir(), 'office-bin-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  for (const [name, script] of Object.entries(scripts)) {
    await writeFile(join(dir, name), script); await chmod(join(dir, name), 0o755);
  }
  return dir;
}
// a fake lsof answering as the real one does (captured on macOS 26, lsof 4.91): exit 0 with -Fn
// field lines, exit 1 with no output when nothing listens
const lsof = (code, out) => `#!/bin/bash\nprintf '%b' ${JSON.stringify(out)}\nexit ${code}\n`;
const LOOPBACK = 'p501\nf3\nn127.0.0.1:18789\np502\nf21\nn127.0.0.1:3000\nf22\nn[::1]:3000\n';

test('listeners_loopback_only: loopback only from a successful lsof seeing both ports; anything else fails (r3 finding 1)', async t => {
  const call = async (sudo, lsofScript) =>
    libCall(await fakeBins(t, { sudo, lsof: lsofScript }), 'listeners_loopback_only', '18789', '3000');
  assert.equal((await call(SUDO_OK, lsof(0, LOOPBACK))).code, 0);
  // the review's reproduction: a refused sudo was "no non-loopback listener"
  let r = await call(SUDO_REFUSED, lsof(0, LOOPBACK));
  assert.equal(r.code, 1); assert.match(r.out, /cannot list the listeners/);
  r = await call(SUDO_OK, lsof(1, ''));                                   // nothing listens: lsof's exit 1
  assert.equal(r.code, 1); assert.match(r.out, /cannot list the listeners/);
  r = await call(SUDO_OK, lsof(0, ''));
  assert.equal(r.code, 1); assert.match(r.out, /no listener/);
  r = await call(SUDO_OK, lsof(0, 'p501\nf3\nn*:18789\np502\nf21\nn127.0.0.1:3000\n'));   // wildcard
  assert.equal(r.code, 1); assert.match(r.out, /not loopback-only \(\*\)/);
  r = await call(SUDO_OK, lsof(0, 'p501\nf3\nn100.64.0.7:3000\np502\nf21\nn127.0.0.1:18789\n'));
  assert.equal(r.code, 1); assert.match(r.out, /not loopback-only/);
  r = await call(SUDO_OK, lsof(0, 'p502\nf21\nn127.0.0.1:3000\n'));        // the bridge's port unseen
  assert.equal(r.code, 1); assert.match(r.out, /no listener seen on port 18789/);
  // the columnar form the old check parsed, or any other shape, is not an answer
  r = await call(SUDO_OK, lsof(0, 'COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\nnode 501 x 3u IPv4 0x1 0t0 TCP 127.0.0.1:3000 (LISTEN)\n'));
  assert.equal(r.code, 1); assert.match(r.out, /answered unexpectedly/);
  r = await call(SUDO_OK, lsof(0, LOOPBACK + 'n127.0.0.1:9999\n'));
  assert.equal(r.code, 1); assert.match(r.out, /unexpected port/);
  r = await call(SUDO_OK, lsof(2, LOOPBACK));                              // an lsof error, output or not
  assert.equal(r.code, 1);
});

// office_healthy, cut from deploy-air.sh as written and run with fakes: every other check passes, so
// the outcome is the listener check's alone (or, with `proxy`, the gateway check's). Studio is a fake
// gateway proxy on a free loopback port (fakeStudioProxy); the lsof answers name it where they say 3000.
// Async (spawn, not spawnSync): the fake proxy answers from this process's event loop.
const ACCESS = 'a'.repeat(64);                    // the fake studio-access-token's content
async function officeHealthy(t, sudo, lsofScript, proxy = 'ok', claw3d = null) {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const m = text.match(/^http_code\(\) \{.*\n^office_healthy\(\) \{[\s\S]*?\n\}\n/m);
  assert.ok(m, 'office_healthy found');
  const studio = await fakeStudioProxy(t, proxy, claw3d);
  const bin = await fakeBins(t, {
    sudo, lsof: lsofScript.replaceAll(':3000', `:${studio.port}`),
    launchctl: '#!/bin/bash\n[ "${FAKE_ROOT:-}" = 1 ] || { echo "Could not find service" >&2; exit 113; }\n'
      + 'echo "system/x = {"\necho "\tstate = running"\necho "}"\n',
    curl: '#!/bin/bash\nfor a; do u="$a"; done\ncase "$u" in */cortados-login) printf 200 ;; *:18789/) printf 426 ;; */) printf 401 ;; esac\n',
  });
  const tokenFile = join(bin, 'studio-access-token');
  await writeFile(tokenFile, ACCESS + '\n'); await chmod(tokenFile, 0o600);
  const r = await spawnAsync('bash', ['-c', `set -euo pipefail; . "$LIB"; BRIDGE_LABEL=b STUDIO_LABEL=s STUDIO_USER=_cortados-claw3d BRIDGE_PORT=18789 STUDIO_PORT=${studio.port}
NODE=${JSON.stringify(process.execPath)} HERE=${JSON.stringify(HERE.replace(/\/$/, ''))} ACCESS_TOKEN=${JSON.stringify(tokenFile)}
${m[0]}
rc=0; why="$(office_healthy)" || rc=$?; echo "rc=$rc $why"`], { PATH: `${bin}:/usr/bin:/bin`, LIB });
  assert.ok(!r.stdout.includes(ACCESS) && !r.stderr.includes(ACCESS), 'the access token is never printed');
  return r.stdout.trim();
}
function spawnAsync(cmd, args, env) {
  return new Promise(resolve => {
    const c = spawn(cmd, args, { env });
    let stdout = '', stderr = '';
    c.stdout.on('data', d => { stdout += d; }); c.stderr.on('data', d => { stderr += d; });
    c.on('close', status => resolve({ status, stdout, stderr }));
  });
}
// A stand-in for Claw3D 51337f7's Studio on its gateway path: the access gate's cookie check on the
// upgrade (401 without it), then server/gateway-proxy.js's answers to a browser `connect`:
//   ok       the bridge's hello (the proxy injected the gateway token and forwarded the connect)
//   blocked  what the Air answered on 2026-11-24: studio.gateway_url_blocked, then close 1011
//   rejected the bridge refused the injected token (UNAUTHORIZED), then the upstream closed
//   silent   the connect is never answered (the probe's deadline decides)
//   claw3d   decided as the real Studio decides (#77 fix round 1), per connection: the upstream from
//            a line-for-line port of Claw3D 51337f7's loadUpstreamGatewaySettings (claw3dSettings
//            below) with `claw3d.env` as Studio's environment, then gateway-proxy.js's checks in its
//            order (claw3dAnswer). The upstream is "the bridge" only at ws://localhost|127.0.0.1:<bridgePort>,
//            and it says hello only to `claw3d.bridgeToken`, as bridge.mjs does.
// It records each connect frame it got, to check the probe sends no credential of its own.
async function fakeStudioProxy(t, mode, claw3d = null) {
  const { createServer } = await import('node:http');
  const { FrameReader, encodeFrame, acceptKey } = await import('./bridge.mjs');
  const connects = [];
  const server = createServer((req, res) => { res.writeHead(404); res.end(); });
  server.on('upgrade', (req, socket) => {
    const cookie = String(req.headers.cookie ?? '');
    if (req.url !== '/api/gateway/ws' || !cookie.split(';').some(c => c.trim() === `studio_access=${ACCESS}`)) {
      socket.end('HTTP/1.1 401 Unauthorized\r\nConnection: close\r\nContent-Length: 0\r\n\r\n'); return;
    }
    socket.write('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n' +
      `Sec-WebSocket-Accept: ${acceptKey(req.headers['sec-websocket-key'])}\r\n\r\n`);
    const send = f => socket.write(encodeFrame(1, JSON.stringify(f)));
    const close = code => { const b = Buffer.alloc(2); b.writeUInt16BE(code); socket.write(encodeFrame(8, b)); socket.end(); };
    // startUpstream runs per browser connection: the settings file is read now, not at server start
    const setup = mode === 'claw3d' ? claw3dSetup(claw3d) : null;
    if (mode === 'claw3d' ? !setup.error : mode !== 'blocked') send({ type: 'event', event: 'connect.challenge', payload: { nonce: 'n' } });
    const reader = new FrameReader();
    socket.on('error', () => {});
    socket.on('data', chunk => {
      let frames; try { frames = reader.push(chunk); } catch { socket.destroy(); return; }
      for (const { opcode, payload } of frames) {
        if (opcode !== 1) { socket.end(); return; }
        const f = JSON.parse(payload.toString('utf8'));
        if (f.method !== 'connect') continue;
        connects.push(f);
        if (mode === 'claw3d') {
          const a = claw3dAnswer(setup, f, claw3d);
          if (a.ok) send({ type: 'res', id: f.id, ok: true, payload: { type: 'hello-ok', protocol: 3 } });
          else { send({ type: 'res', id: f.id, ok: false, error: { code: a.code, message: a.code } }); close(a.close); }
        } else if (mode === 'ok') send({ type: 'res', id: f.id, ok: true, payload: { type: 'hello-ok', protocol: 3 } });
        else if (mode === 'blocked') {
          send({ type: 'res', id: f.id, ok: false, error: { code: 'studio.gateway_url_blocked',
            message: 'Upstream gateway URL is not in the allowed hosts list.' } });
          close(1011);
        } else if (mode === 'rejected') {
          send({ type: 'res', id: f.id, ok: false, error: { code: 'UNAUTHORIZED', message: 'Bridge authentication failed' } });
          close(1012);
        }
      }
    });
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(() => { server.closeAllConnections?.(); server.close(); });
  return { port: server.address().port, connects };
}
// Claw3D 51337f7 (github.com/iamlukethedev/Claw3D, the commit deploy-air.sh pins), modelled line for
// line, not loaded: server/studio-settings.js, the only place its gateway proxy takes its upstream from
// (server/index.js 91-95 passes loadUpstreamGatewaySettings(process.env) to createGatewayProxy).
//   resolveUserPath 8-16, resolveDefaultHomeDir 18-26, resolveStateDir 28-47: OPENCLAW_STATE_DIR,
//     MOLTBOT_STATE_DIR, CLAWDBOT_STATE_DIR, else <home>/.openclaw (or an existing .clawdbot/.moltbot);
//   resolveStudioSettingsPath 49-51: <stateDir>/claw3d/settings.json;
//   readOpenclawGatewayDefaults 64-83: <stateDir>/openclaw.json gateway.auth.token + gateway.port;
//   loadUpstreamGatewaySettings 85-112: the file's gateway {url, token, adapterType}; no token and an
//     openclaw adapter -> openclaw.json's; else url || DEFAULT_GATEWAY_URL (ws://localhost:18789), token.
// It reads no CLAW3D_GATEWAY_URL / CLAW3D_GATEWAY_TOKEN: the reason deploy-air.sh writes the file.
// Not loaded from upstream source on purpose: tests run no third-party code. Re-check these lines
// when CLAW3D_COMMIT moves.
const claw3dSettings = () => {
  const resolveUserPath = input => {
    const trimmed = String(input ?? '').trim();
    if (!trimmed) return trimmed;
    return resolvePath(trimmed.startsWith('~') ? trimmed.replace(/^~(?=$|[\\/])/, homedir()) : trimmed);
  };
  const resolveStateDir = env => {
    const override = env.OPENCLAW_STATE_DIR?.trim() || env.MOLTBOT_STATE_DIR?.trim() || env.CLAWDBOT_STATE_DIR?.trim();
    if (override) return resolveUserPath(override);
    const home = existsSync(homedir()) ? homedir() : tmpdir();
    const dirs = ['.openclaw', '.clawdbot', '.moltbot'].map(d => join(home, d));
    return dirs.find(d => existsSync(d)) ?? dirs[0];
  };
  const resolveStudioSettingsPath = env => join(resolveStateDir(env), 'claw3d', 'settings.json');
  const readJsonFile = p => (existsSync(p) ? JSON.parse(readFileSync(p, 'utf8')) : null);
  const isRecord = v => Boolean(v && typeof v === 'object');
  const readOpenclawGatewayDefaults = env => {
    try {
      const parsed = readJsonFile(join(resolveStateDir(env), 'openclaw.json'));
      if (!isRecord(parsed)) return null;
      const gateway = isRecord(parsed.gateway) ? parsed.gateway : null;
      if (!gateway) return null;
      const auth = isRecord(gateway.auth) ? gateway.auth : null;
      const token = typeof auth?.token === 'string' ? auth.token.trim() : '';
      const port = typeof gateway.port === 'number' && Number.isFinite(gateway.port) ? gateway.port : null;
      if (!token) return null;
      const url = port ? `ws://localhost:${port}` : '';
      return url ? { url, token, adapterType: 'openclaw' } : null;
    } catch { return null; }
  };
  const loadUpstreamGatewaySettings = env => {
    const settingsPath = resolveStudioSettingsPath(env);
    const parsed = readJsonFile(settingsPath);          // a malformed file throws, as in Claw3D
    const gateway = parsed && typeof parsed === 'object' ? parsed.gateway : null;
    const url = typeof gateway?.url === 'string' ? gateway.url.trim() : '';
    const token = typeof gateway?.token === 'string' ? gateway.token.trim() : '';
    const adapterType = typeof gateway?.adapterType === 'string' && gateway.adapterType.trim() ? gateway.adapterType.trim() : 'openclaw';
    if (!token && adapterType === 'openclaw') {
      const defaults = readOpenclawGatewayDefaults(env);
      if (defaults) return { url: url || defaults.url, token: defaults.token, adapterType, settingsPath };
    }
    return { url: url || 'ws://localhost:18789', token, adapterType, settingsPath };
  };
  return { resolveStateDir, resolveStudioSettingsPath, loadUpstreamGatewaySettings };
};
// gateway-proxy.js 71-108 (isUpstreamAllowed), with Studio's env passed in instead of process.env
function claw3dUpstreamAllowed(url, env) {
  const allowlist = (env.UPSTREAM_ALLOWLIST || '').trim();
  if (!allowlist) return env.NODE_ENV !== 'production';
  try {
    const allowed = allowlist.split(',').map(h => h.trim().toLowerCase()).filter(Boolean);
    return allowed.includes(new URL(url).hostname.toLowerCase());
  } catch { return false; }
}
// gateway-proxy.js 288-330 (startUpstream): the settings, then url_missing, url_blocked, url_invalid
function claw3dSetup({ env }) {
  let settings;
  try { settings = claw3dSettings().loadUpstreamGatewaySettings(env); } catch { return { error: 'studio.settings_load_failed' }; }
  const url = typeof settings?.url === 'string' ? settings.url.trim() : '';
  const token = typeof settings?.token === 'string' ? settings.token.trim() : '';
  const adapterType = typeof settings?.adapterType === 'string' && settings.adapterType.trim()
    ? settings.adapterType.trim().toLowerCase() : 'openclaw';
  if (!url) return { error: 'studio.gateway_url_missing' };
  if (!claw3dUpstreamAllowed(url, env)) return { error: 'studio.gateway_url_blocked' };
  try { new URL(url); } catch { return { error: 'studio.gateway_url_invalid' }; }
  return { url, token, adapterType };
}
// A setup error is sent as the connect's answer, then close 1011 (sendConnectError); else
// forwardConnectFrame 230-243: an openclaw upstream with no token and no browser credential is
// studio.gateway_token_missing; otherwise the token is injected and the upstream answers.
function claw3dAnswer(setup, frame, { bridgeToken, bridgePort = 18789 }) {
  if (setup.error) return { code: setup.error, close: 1011 };
  const auth = frame.params?.auth ?? {};
  const browserHasAuth = [auth.token, auth.password, auth.deviceToken].some(v => typeof v === 'string' && v.trim());
  if (setup.adapterType === 'openclaw' && !setup.token && !browserHasAuth) return { code: 'studio.gateway_token_missing', close: 1011 };
  const u = new URL(setup.url);
  if (!['localhost', '127.0.0.1'].includes(u.hostname) || Number(u.port) !== bridgePort) return { code: 'studio.upstream_error', close: 1011 };
  const sent = browserHasAuth ? auth.token : setup.token;
  return sent === bridgeToken ? { ok: true } : { code: 'UNAUTHORIZED', close: 1008 };
}
test('deploy-air.sh office_healthy: a refused sudo lsof fails the health check (rollback), never passes it (r3 finding 1)', async t => {
  assert.equal(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK)), 'rc=0');
  // a refused sudo now fails at the first root read, the daemon state: never a pass
  assert.match(await officeHealthy(t, SUDO_REFUSED, lsof(0, LOOPBACK)), /^rc=1 b is not running/);
  assert.match(await officeHealthy(t, SUDO_ROOT, lsof(1, '')), /^rc=1 /);
  assert.match(await officeHealthy(t, SUDO_ROOT, lsof(0, 'p1\nf3\nn*:3000\np2\nf3\nn127.0.0.1:18789\n')), /^rc=1 a listener on port \d+ is not loopback-only/);
  // a refused sudo for lsof alone (launchctl answered as root) still fails the check
  const lsofRefused = '#!/bin/bash\nif [ "$1" = -u ]; then shift 2; fi\nif [ "$1" = lsof ]; then echo "sudo: a password is required" >&2; exit 1; fi\nexport FAKE_ROOT=1\nexec "$@"\n';
  assert.match(await officeHealthy(t, lsofRefused, lsof(0, LOOPBACK)), /^rc=1 cannot list the listeners/);
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  assert.ok(!/sudo lsof/.test(text), 'lsof runs only in deploy-lib.sh (listeners_loopback_only)');
  assert.ok(!/\| *grep -q/.test(text), 'no `| grep -q` under pipefail (SIGPIPE reads as failure)');
});

// ---- #77 upstream allowlist: the Office could not reach its bridge while every check passed ----------
// deploy-air.sh's pins as it sets them, with ROOT moved under `root` (every Studio path hangs off it)
async function deployPins(root) {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const pins = text.split('\n').filter(l => /^(STUDIO_USER|BRIDGE_USER|BRIDGE_PORT|STUDIO_PORT|GATEWAY_HOST|GATEWAY_URL|GATEWAY_SECRET|BRIDGE_SECRET|ACCESS_TOKEN|STUDIO_STATE|STUDIO_SETTINGS|HEALTH_DEADLINE_S)=/.test(l));
  return { text, sh: `ROOT=${JSON.stringify(root)}\n${pins.join('\n')}` };
}
// run-studio.sh rendered by bash from deploy-air.sh's own heredoc, then RUN with NODE = a stub printing
// its environment: the output is exactly the environment Studio starts with. Returns it with the pins.
async function studioLauncher(t, root) {
  const { text, sh } = await deployPins(root);
  const heredoc = text.match(/^write_root_file run-studio\.sh 0755 <<EOF\n([\s\S]*?)\nEOF\n/m);
  assert.ok(heredoc, 'run-studio.sh heredoc found');
  const node = join(root, 'node-env');
  await writeFile(node, '#!/bin/bash\nexec /usr/bin/env\n'); await chmod(node, 0o755);
  const r = spawnSync('bash', ['-c', `set -euo pipefail\n${sh}\nNODE=${JSON.stringify(node)}
cat <<EOF\n${heredoc[1]}\nEOF\nprintf '%s\\n' "$STUDIO_STATE" "$STUDIO_SETTINGS" "$GATEWAY_URL" "$ACCESS_TOKEN" "$BRIDGE_SECRET" >&2`],
    { env: { PATH: '/usr/bin:/bin' }, encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
  const [stateDir, settingsPath, gatewayUrl, accessToken, bridgeSecret] = r.stderr.trim().split('\n');
  await mkdir(join(root, 'claw3d', 'current'), { recursive: true });
  await mkdir(join(accessToken, '..'), { recursive: true });
  await writeFile(accessToken, ACCESS + '\n');
  const file = join(root, 'run-studio.sh');
  await writeFile(file, r.stdout); await chmod(file, 0o755);
  const run = spawnSync('/bin/bash', [file], { env: { PATH: '/usr/bin:/bin' }, encoding: 'utf8' });
  assert.equal(run.status, 0, run.stderr);
  const env = Object.fromEntries(run.stdout.trim().split('\n').map(l => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]));
  return { launcher: r.stdout, env, stateDir, settingsPath, gatewayUrl, bridgeSecret };
}
// studio-settings.py as deploy-air.sh runs it: the token on stdin, never argv
function buildSettings(token, url, existing = '') {
  const args = ['-I', join(HERE, 'studio-settings.py'), '--url', url];
  if (existing) args.push('--existing', existing);
  return spawnSync('python3', args, { input: token + '\n', encoding: 'utf8', env: { PATH: '/usr/bin:/bin' } });
}
const BRIDGE_TOKEN = 'c'.repeat(64);              // the fake bridge-token's content

test('run-studio.sh: Studio\'s state dir is pinned, the allowlist is the settings URL\'s hostname, no dead CLAW3D_GATEWAY_* (#77 fix round 1)', async t => {
  const root = await mkdtemp(join(tmpdir(), 'office-launcher-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const L = await studioLauncher(t, root);
  // Claw3D's resolver (modelled) lands on the file deploy-air.sh writes, from the launcher's environment
  assert.equal(L.env.OPENCLAW_STATE_DIR, L.stateDir);
  assert.equal(claw3dSettings().resolveStudioSettingsPath(L.env), L.settingsPath);
  assert.equal(L.settingsPath, join(root, 'home', 'studio', '.openclaw', 'claw3d', 'settings.json'));
  // Claw3D 51337f7's proxy reads no CLAW3D_GATEWAY_*: none is exported, so none can mislead
  assert.deepEqual(Object.keys(L.env).filter(k => k.startsWith('CLAW3D_GATEWAY')), []);
  assert.ok(!/CLAW3D_GATEWAY_TOKEN=|cat "[^"]*gateway-token"/.test(L.launcher), 'the launcher reads no gateway token');
  assert.equal(L.env.NODE_ENV, 'production');   // the reason the allowlist is needed: production refuses an empty one
  // Claw3D compares each comma-separated entry with new URL(upstream).hostname
  assert.equal(L.env.UPSTREAM_ALLOWLIST, new URL(L.gatewayUrl).hostname);
  assert.equal(L.gatewayUrl, 'ws://localhost:18789');
  assert.equal(L.env.STUDIO_ACCESS_TOKEN, ACCESS);
  // both come from the one pin, so they cannot drift
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  assert.match(text, /^export UPSTREAM_ALLOWLIST="\$GATEWAY_HOST"$/m);
  assert.match(text, /^GATEWAY_URL="ws:\/\/\$GATEWAY_HOST:\$BRIDGE_PORT"$/m);
  assert.ok(!/exactly the loopback bridge|only the loopback bridge is allowed/i.test(text), 'the allowlist names a hostname, not a port');
  // publishing refuses an installed launcher or state from before the allowlist or the settings file
  assert.match(text, /grep -q 'UPSTREAM_ALLOWLIST' "\$ROOT\/bin\/run-studio\.sh" \|\| die /);
  assert.match(text, /grep -q 'OPENCLAW_STATE_DIR' "\$ROOT\/bin\/run-studio\.sh" \|\| die /);
  assert.match(text, /as_root_test -s "\$STUDIO_SETTINGS" \|\| die /);
});

test('studio-settings.py: Claw3D\'s loader reads back the URL and the bridge token; UI state kept; junk refused; the token never printed (#77 fix round 1)', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'office-settings-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const env = { OPENCLAW_STATE_DIR: join(dir, 'state') };
  const path = join(dir, 'state', 'claw3d', 'settings.json');
  await mkdir(join(path, '..'), { recursive: true });
  const r = buildSettings(BRIDGE_TOKEN, 'ws://localhost:18789');
  assert.equal(r.status, 0, r.stderr);
  await writeFile(path, r.stdout);
  const got = claw3dSettings().loadUpstreamGatewaySettings(env);
  assert.deepEqual({ url: got.url, token: got.token, adapterType: got.adapterType },
    { url: 'ws://localhost:18789', token: BRIDGE_TOKEN, adapterType: 'openclaw' });
  // without the file Claw3D falls back to the default URL with NO token: the live Air's state
  await rm(path);
  assert.deepEqual(claw3dSettings().loadUpstreamGatewaySettings(env).token, '');
  // an upgrade over a file Studio's UI wrote: its state is kept, the whole gateway entry replaced
  const old = join(dir, 'old.json');
  await writeFile(old, JSON.stringify({ version: 1, avatars: { a: { hue: 3 } }, activeFloorId: 'f2',
    gateway: { url: 'ws://elsewhere:1', token: 'stale-token-0000000', adapterType: 'hermes', profiles: { hermes: { url: 'ws://x:2', token: '' } } } }));
  const up = buildSettings(BRIDGE_TOKEN, 'ws://localhost:18789', old);
  assert.equal(up.status, 0, up.stderr);
  assert.deepEqual(JSON.parse(up.stdout), { version: 1, avatars: { a: { hue: 3 } }, activeFloorId: 'f2',
    gateway: { url: 'ws://localhost:18789', token: BRIDGE_TOKEN, adapterType: 'openclaw' } });
  // an empty existing copy (a fresh install) is no file at all
  await writeFile(old, '');
  assert.equal(buildSettings(BRIDGE_TOKEN, 'ws://localhost:18789', old).stdout, r.stdout);
  // refused, never overwritten blind, and no value in any message
  for (const content of ['[1]', '{not json', '"x"']) {
    await writeFile(old, content);
    const bad = buildSettings(BRIDGE_TOKEN, 'ws://localhost:18789', old);
    assert.equal(bad.status, 2, content); assert.equal(bad.stdout, '');
    assert.ok(!bad.stderr.includes(BRIDGE_TOKEN));
  }
  for (const [token, url] of [['', 'ws://localhost:18789'], ['short', 'ws://localhost:18789'], ['x y'.repeat(8), 'ws://localhost:18789'],
    [BRIDGE_TOKEN, 'http://localhost:18789'], [BRIDGE_TOKEN, 'ws://localhost']]) {
    const bad = buildSettings(token, url);
    assert.equal(bad.status, 1, `${token} ${url}`); assert.equal(bad.stdout, '');
    assert.ok(!bad.stderr.includes(BRIDGE_TOKEN));
  }
});

test('deploy-air.sh stage_studio_settings: stages the bridge token for Studio, 0600, reading each file as its owner, never on argv (#77 fix round 1)', async t => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const fn = text.match(/^build_studio_settings\(\) \{[\s\S]*?\n\}\nstage_studio_settings\(\) \{[\s\S]*?\n\}\n/m);
  assert.ok(fn, 'stage_studio_settings found');
  const root = await mkdtemp(join(tmpdir(), 'office-stage-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const { sh } = await deployPins(root);
  const [bridgeSecret, settingsPath] = spawnSync('bash', ['-c', `${sh}\nprintf '%s\\n' "$BRIDGE_SECRET" "$STUDIO_SETTINGS"`],
    { encoding: 'utf8' }).stdout.trim().split('\n');
  await mkdir(join(bridgeSecret, '..'), { recursive: true });
  await writeFile(bridgeSecret, BRIDGE_TOKEN + '\n');
  for (const d of ['stage', 'tmp']) await mkdir(join(root, d));
  // sudo as root would: -u USER is logged (who read what), install's -o/-g dropped (not root here)
  const bin = await fakeBins(t, { sudo: `#!/bin/bash
if [ "$1" = -u ]; then echo "$2 \${@: -1}" >> "$SUDO_LOG"; shift 2; fi
if [ "$1" = install ]; then shift; a=(); while [ $# -gt 0 ]; do case "$1" in -o|-g) shift 2 ;; *) a+=("$1"); shift ;; esac; done; exec /usr/bin/install "\${a[@]}"; fi
exec "$@"
` });
  const run = () => spawnSync('bash', ['-c', `set -euo pipefail; . "$LIB"; ${sh}
HERE=${JSON.stringify(HERE.replace(/\/$/, ''))} TMP=${JSON.stringify(join(root, 'tmp'))} STAGE=${JSON.stringify(join(root, 'stage'))}
${fn[0]}
rc=0; stage_studio_settings || rc=$?; echo "rc=$rc"`], { env: { PATH: `${bin}:/usr/bin:/bin`, LIB, SUDO_LOG: join(root, 'sudo.log') }, encoding: 'utf8' });
  const staged = join(root, 'stage', 'studio-settings.json');
  const { stat, readdir } = await import('node:fs/promises');
  // a fresh install: no settings yet
  let r = run();
  assert.equal(r.stdout.trim(), 'rc=0', r.stderr);
  assert.ok(!r.stdout.includes(BRIDGE_TOKEN) && !r.stderr.includes(BRIDGE_TOKEN), 'the token is never printed');
  assert.equal((await stat(staged)).mode & 0o777, 0o600);
  assert.deepEqual(await readdir(join(root, 'tmp')), [], 'nothing left in TMP');
  await mkdir(join(settingsPath, '..'), { recursive: true });
  await writeFile(settingsPath, await readFile(staged));   // what the transaction installs
  const got = claw3dSettings().loadUpstreamGatewaySettings({ OPENCLAW_STATE_DIR: join(settingsPath, '..', '..') });
  assert.deepEqual([got.url, got.token, got.adapterType], ['ws://localhost:18789', BRIDGE_TOKEN, 'openclaw']);
  // an upgrade: Studio's UI state in the installed file survives, the gateway entry is rewritten
  await writeFile(settingsPath, JSON.stringify({ version: 1, avatars: { z: 1 }, gateway: { url: 'ws://localhost:1', token: 'old-old-old-old-old' } }));
  r = run();
  assert.equal(r.stdout.trim(), 'rc=0', r.stderr);
  assert.deepEqual(JSON.parse(await readFile(staged, 'utf8')),
    { version: 1, avatars: { z: 1 }, gateway: { url: 'ws://localhost:18789', token: BRIDGE_TOKEN, adapterType: 'openclaw' } });
  // each secret read as its own role user, never as root
  assert.deepEqual((await readFile(join(root, 'sudo.log'), 'utf8')).trim().split('\n'),
    [`_cortados-office ${bridgeSecret}`, `_cortados-claw3d ${settingsPath}`, `_cortados-office ${bridgeSecret}`]);
  // a bridge secret that cannot be read fails (pipefail), never stages a tokenless file
  await rm(staged); await rm(bridgeSecret);
  r = run();
  assert.equal(r.stdout.trim(), 'rc=1');
  assert.match(r.stderr, /cannot build the settings/);
  await assert.rejects(stat(staged));
  // the token reaches studio-settings.py on stdin only; the result is staged, then installed by the transaction
  assert.match(fn[0], /sudo -u "\$BRIDGE_USER" cat "\$BRIDGE_SECRET" \\\n\s+\| python3 -I "\$HERE\/studio-settings\.py" --url "\$GATEWAY_URL" --existing /);
  assert.match(text, /^TX_FILES\+=\("\$STAGE\/studio-settings\.json\|\$STUDIO_SETTINGS\|\$STUDIO_USER"\)/m);
  assert.match(text, /if \[\[ \$APPLY -eq 1 \]\]; then stage_studio_settings \|\| die /);
  assert.ok(text.indexOf('then stage_studio_settings ||') < text.indexOf('tx_rc=0; tx_run'));
  // an interrupted activation's .prev is caught, and no link is written through as root
  assert.match(text, /"\$OFFICE_TOKEN" "\$STUDIO_SETTINGS"; do/);
  assert.match(text, /for f in "\$STUDIO_STATE" "\$STUDIO_STATE\/claw3d" "\$STUDIO_SETTINGS"; do\n  if as_root_test -L "\$f"; then die /);
  assert.match(text, /"\$STUDIO_STATE" "\$STUDIO_STATE\/claw3d"\n/);   // created 0700, the Studio user's (step 1)
});

test('deploy-air.sh office_healthy: a connect through Studio\'s proxy must reach the bridge (#77 upstream allowlist)', async t => {
  assert.equal(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK), 'ok'), 'rc=0');
  // what the Air answered on 2026-11-24 while every other check passed
  assert.equal(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK), 'blocked'),
    "rc=1 Studio's gateway proxy does not reach the bridge (connect refused: studio.gateway_url_blocked)");
  assert.match(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK), 'rejected'), /^rc=1 .*connect refused: UNAUTHORIZED\)$/);
  // the access token cannot be read (a refused sudo for cat alone): fails, never skips the check
  const catRefused = '#!/bin/bash\nif [ "$1" = -u ]; then shift 2; fi\nif [ "$1" = cat ]; then echo "sudo: a password is required" >&2; exit 1; fi\nexport FAKE_ROOT=1\nexec "$@"\n';
  assert.match(await officeHealthy(t, catRefused, lsof(0, LOOPBACK), 'ok'), /^rc=1 Studio's gateway proxy does not reach the bridge \(/);
  // the token reaches the probe on stdin only: never as an argument
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  assert.match(text, /gw="\$\(sudo -u "\$STUDIO_USER" cat "\$ACCESS_TOKEN" 2>\/dev\/null \| "\$NODE" "\$HERE\/gateway-check\.mjs" --port "\$STUDIO_PORT" --timeout-ms 5000 2>\/dev\/null\)"/);
});

test('gateway-check.mjs: ok only on the bridge\'s hello; a refused upgrade, silence or no Studio fails (#77 upstream allowlist)', async t => {
  const { probe } = await import('./gateway-check.mjs');
  const studio = await fakeStudioProxy(t, 'ok');
  assert.equal(await probe({ port: studio.port, token: ACCESS }), 'ok');
  // the probe sends no credential of its own: Studio's proxy injects its gateway token, as for a browser
  assert.equal(studio.connects.length, 1);
  assert.equal(studio.connects[0].params?.auth, undefined);
  assert.equal(await probe({ port: studio.port, token: 'b'.repeat(64) }), 'gateway upgrade refused (HTTP 401)');
  assert.equal(await probe({ port: studio.port, token: '' }), 'no usable access token on stdin');
  assert.equal(await probe({ port: studio.port, token: 'bad token; x=y' }), 'no usable access token on stdin');
  const silent = await fakeStudioProxy(t, 'silent');
  assert.equal(await probe({ port: silent.port, token: ACCESS, timeoutMs: 300 }), 'no answer within 300 ms');
  const blocked = await fakeStudioProxy(t, 'blocked');
  assert.equal(await probe({ port: blocked.port, token: ACCESS }), 'connect refused: studio.gateway_url_blocked');
  // nothing listening
  const { createServer } = await import('node:net');
  const s = createServer(); await new Promise(r => s.listen(0, '127.0.0.1', r));
  const free = s.address().port; await new Promise(r => s.close(r));
  assert.equal(await probe({ port: free, token: ACCESS }), 'cannot reach Studio');
});

test('activation: Studio\'s proxy answering studio.gateway_url_blocked fails the health check and rolls back (#77 upstream allowlist)', async t => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const m = text.match(/^http_code\(\) \{.*\n^office_healthy\(\) \{[\s\S]*?\n\}\n/m);
  for (const [mode, rc] of [['blocked', 'rc=1'], ['ok', 'rc=0']]) {
    const D = await txHost(t, { upgrade: true });
    const studio = await fakeStudioProxy(t, mode);
    const bin = await fakeBins(t, {
      sudo: SUDO_ROOT, lsof: lsof(0, LOOPBACK.replaceAll(':3000', `:${studio.port}`)),
      launchctl: '#!/bin/bash\n[ "${FAKE_ROOT:-}" = 1 ] || exit 113\necho "\tstate = running"\n',
      curl: '#!/bin/bash\nfor a; do u="$a"; done\ncase "$u" in */cortados-login) printf 200 ;; *:18789/) printf 426 ;; */) printf 401 ;; esac\n',
    });
    const tokenFile = join(bin, 'studio-access-token');
    await writeFile(tokenFile, ACCESS + '\n'); await chmod(tokenFile, 0o600);
    // the harness's fake health_check replaced by deploy-air.sh's own office_healthy, once
    const prelude = `BRIDGE_LABEL=b STUDIO_LABEL=s STUDIO_USER=_cortados-claw3d BRIDGE_PORT=18789 STUDIO_PORT=${studio.port}
NODE=${JSON.stringify(process.execPath)} HERE=${JSON.stringify(HERE.replace(/\/$/, ''))} ACCESS_TOKEN=${JSON.stringify(tokenFile)}
${m[0]}
health_check() { local why; why="$(office_healthy)" || { echo "health check: $why" >&2; return 1; }; echo health >> "$D/log"; }`;
    const r = await spawnAsync('/bin/bash', [TX_HARNESS],
      { PATH: `${bin}:/usr/bin:/bin`, LIB, FAKE: D, FAIL: '', FAIL_ALWAYS: '', PRELUDE: prelude, UNKNOWN: '' });
    assert.equal(r.stdout.trim(), rc, r.stderr);
    if (mode === 'blocked') {
      assert.match(r.stderr, /health check: Studio's gateway proxy does not reach the bridge \(connect refused: studio\.gateway_url_blocked\)/);
      assert.match(r.stderr, /ROLLBACK/);
      assert.deepEqual(await snapshot(D), OLD);
    } else {
      assert.deepEqual(await snapshot(D), NEW);
    }
  }
});

test('deploy-air.sh office_healthy against Studio\'s real settings logic: no settings file is gateway_token_missing, never a pass (#77 fix round 1)', async t => {
  const root = await mkdtemp(join(tmpdir(), 'office-e2e-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const L = await studioLauncher(t, root);
  const claw3d = { env: L.env, bridgeToken: BRIDGE_TOKEN };
  const fail = code => `rc=1 Studio's gateway proxy does not reach the bridge (connect refused: ${code})`;
  // what the Air would have answered next once the allowlist was fixed: Claw3D falls back to the
  // default URL with no token (it ignores CLAW3D_GATEWAY_TOKEN)
  assert.equal(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK), 'claw3d', claw3d), fail('studio.gateway_token_missing'));
  // the settings file deploy-air.sh provisions: the bridge's hello
  await mkdir(join(L.settingsPath, '..'), { recursive: true });
  await writeFile(L.settingsPath, buildSettings(BRIDGE_TOKEN, L.gatewayUrl).stdout);
  assert.equal(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK), 'claw3d', claw3d), 'rc=0');
  // the launcher before 70fdedb (no allowlist): blocked, as seen live on 2026-11-24
  const noAllow = { ...L.env }; delete noAllow.UPSTREAM_ALLOWLIST;
  assert.equal(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK), 'claw3d', { ...claw3d, env: noAllow }), fail('studio.gateway_url_blocked'));
  // a stale token (the bridge secret rotated, the settings not rewritten): the bridge refuses it
  await writeFile(L.settingsPath, buildSettings('d'.repeat(64), L.gatewayUrl).stdout);
  assert.equal(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK), 'claw3d', claw3d), fail('UNAUTHORIZED'));
  // the allowlist names a hostname, not a port: another localhost port passes it, and only the
  // settings URL keeps Studio on the bridge (nothing answers there, so the check fails)
  await writeFile(L.settingsPath, buildSettings(BRIDGE_TOKEN, 'ws://localhost:9').stdout);
  assert.equal(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK), 'claw3d', claw3d), fail('studio.upstream_error'));
});

test('activation: a deploy without Studio\'s settings file fails the probe and rolls back; the settings file is rolled back with the release (#77 fix round 1)', async t => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const m = text.match(/^http_code\(\) \{.*\n^office_healthy\(\) \{[\s\S]*?\n\}\n/m);
  const good = buildSettings(BRIDGE_TOKEN, 'ws://localhost:18789').stdout;
  const oldFile = JSON.stringify({ version: 1, avatars: { kept: true }, gateway: { url: 'ws://localhost:18789', token: BRIDGE_TOKEN, adapterType: 'openclaw' } });
  const cases = [
    // 70fdedb's deploy: no settings file staged or installed
    { name: 'no settings file', staged: null, installed: null, rc: 'rc=1', code: 'studio.gateway_token_missing' },
    { name: 'fresh settings', staged: good, installed: null, rc: 'rc=0' },
    { name: 'upgrade, settings with a token the bridge refuses', staged: buildSettings('e'.repeat(64), 'ws://localhost:18789').stdout,
      installed: oldFile, rc: 'rc=1', code: 'UNAUTHORIZED' },
    { name: 'upgrade', staged: good, installed: oldFile, rc: 'rc=0' },
  ];
  for (const c of cases) {
    const D = await txHost(t, { upgrade: true });
    const L = await studioLauncher(t, D);
    await mkdir(join(L.settingsPath, '..'), { recursive: true });
    if (c.installed) await writeFile(L.settingsPath, c.installed);
    if (c.staged) await writeFile(join(D, 'stage', 'studio-settings.json'), c.staged);
    const studio = await fakeStudioProxy(t, 'claw3d', { env: L.env, bridgeToken: BRIDGE_TOKEN });
    const bin = await fakeBins(t, {
      sudo: SUDO_ROOT, lsof: lsof(0, LOOPBACK.replaceAll(':3000', `:${studio.port}`)),
      launchctl: '#!/bin/bash\n[ "${FAKE_ROOT:-}" = 1 ] || exit 113\necho "\tstate = running"\n',
      curl: '#!/bin/bash\nfor a; do u="$a"; done\ncase "$u" in */cortados-login) printf 200 ;; *:18789/) printf 426 ;; */) printf 401 ;; esac\n',
    });
    const tokenFile = join(bin, 'studio-access-token');
    await writeFile(tokenFile, ACCESS + '\n'); await chmod(tokenFile, 0o600);
    // deploy-air.sh's own TX_FILES entry for the settings file, when this deploy stages one
    const prelude = `BRIDGE_LABEL=b STUDIO_LABEL=s STUDIO_USER=_cortados-claw3d BRIDGE_PORT=18789 STUDIO_PORT=${studio.port}
NODE=${JSON.stringify(process.execPath)} HERE=${JSON.stringify(HERE.replace(/\/$/, ''))} ACCESS_TOKEN=${JSON.stringify(tokenFile)}
${c.staged ? `TX_FILES+=(${JSON.stringify(`${D}/stage/studio-settings.json|${L.settingsPath}`)})` : ''}
${m[0]}
health_check() { local why; why="$(office_healthy)" || { echo "health check: $why" >&2; return 1; }; echo health >> "$D/log"; }`;
    const r = await spawnAsync('/bin/bash', [TX_HARNESS],
      { PATH: `${bin}:/usr/bin:/bin`, LIB, FAKE: D, FAIL: '', FAIL_ALWAYS: '', PRELUDE: prelude, UNKNOWN: '' });
    assert.equal(r.stdout.trim(), c.rc, `${c.name}: ${r.stderr}`);
    const settings = await readFile(L.settingsPath, 'utf8').catch(() => null);
    if (c.rc === 'rc=1') {
      assert.match(r.stderr, new RegExp(`health check: Studio's gateway proxy does not reach the bridge \\(connect refused: ${c.code.replaceAll('.', '\\.')}\\)`), c.name);
      assert.match(r.stderr, /ROLLBACK/, c.name);
      assert.deepEqual(await snapshot(D), OLD, c.name);
      assert.equal(settings, c.installed, `${c.name}: the previous settings file is back (or none)`);
    } else {
      assert.deepEqual(await snapshot(D), NEW, c.name);
      assert.equal(settings, c.staged, c.name);
    }
    const { readdir } = await import('node:fs/promises');
    assert.deepEqual((await readdir(join(L.settingsPath, '..'))).filter(n => /\.(prev|next)$/.test(n)), [], `${c.name}: no leftovers`);
  }
});

test('deploy-air.sh health_check: an overall deadline bounds it, whatever each check costs; a pass is confirmed 10 s later (#77 fix round 1)', async t => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  assert.match(text, /^HEALTH_DEADLINE_S=60$/m);
  const fn = text.match(/^health_check\(\) \{[\s\S]*?\n\}\n/m);
  assert.ok(fn, 'health_check found');
  const dir = await mkdtemp(join(tmpdir(), 'office-health-'));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const log = join(dir, 'log');
  const run = body => spawnSync('bash', ['-c', `set -euo pipefail; APPLY=1 HEALTH_DEADLINE_S=2 LOG=${JSON.stringify(log)}\n${body}\n${fn[0]}
rc=0; health_check || rc=$?; echo "rc=$rc"`], { env: { PATH: '/usr/bin:/bin' }, encoding: 'utf8' });
  // a check that always fails slowly: given up at the deadline, not after a fixed number of tries
  const t0 = Date.now();
  let r = run('office_healthy() { echo try >> "$LOG"; sleep 1; echo "the probe timed out"; return 1; }');
  const took = Date.now() - t0;
  assert.equal(r.stdout.trim(), 'rc=1');
  assert.match(r.stderr, /health check \(deadline 2 s\): the probe timed out/);
  assert.ok(took < 7000, `took ${took} ms`);
  assert.ok((await readFile(log, 'utf8')).trim().split('\n').length <= 2);
  // a healthy release: checked, 10 s, checked again
  await rm(log);
  r = run('office_healthy() { echo check >> "$LOG"; }\nsleep() { echo "sleep $1" >> "$LOG"; }');
  assert.equal(r.stdout.trim().split('\n').pop(), 'rc=0', r.stderr);
  assert.deepEqual((await readFile(log, 'utf8')).trim().split('\n'), ['check', 'sleep 10', 'check']);
});

test('link_target: empty only on confirmed absence; a failed or impossible read is an error (r3 finding 2)', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'office-links-'));
  t.after(async () => { await chmod(join(dir, 'locked'), 0o755).catch(() => {}); await rm(dir, { recursive: true, force: true }); });
  const { symlink } = await import('node:fs/promises');
  await mkdir(join(dir, 'builds', 'old'), { recursive: true });
  await symlink(join(dir, 'builds', 'old'), join(dir, 'current'));
  await symlink(join(dir, 'nowhere'), join(dir, 'dangling'));
  await writeFile(join(dir, 'plain'), 'x');
  await mkdir(join(dir, 'locked')); await symlink(join(dir, 'builds', 'old'), join(dir, 'locked', 'current'));
  await chmod(join(dir, 'locked'), 0o000);
  const none = await fakeBins(t, {});
  const lt = (p, bin = none) => libCall(bin, 'link_target', p);
  assert.deepEqual(lt(join(dir, 'current')), { code: 0, out: join(dir, 'builds', 'old') });
  assert.deepEqual(lt(join(dir, 'dangling')), { code: 0, out: join(dir, 'nowhere') });
  assert.deepEqual(lt(join(dir, 'absent')), { code: 0, out: '' });                       // fresh install
  assert.deepEqual(lt(join(dir, 'no', 'such', 'current')), { code: 0, out: '' });        // nor its directory
  assert.equal(lt(join(dir, 'plain')).code, 1);                                          // not a link
  assert.equal(lt(join(dir, 'builds')).code, 1);
  assert.equal(lt(join(dir, 'locked', 'current')).code, 1);                              // unsearchable: not "absent"
  const broken = await fakeBins(t, { readlink: '#!/bin/bash\necho "readlink: I/O error" >&2\nexit 1\n' });
  assert.equal(lt(join(dir, 'current'), broken).code, 1);                                // readlink failed
  const empty = await fakeBins(t, { readlink: '#!/bin/bash\nexit 0\n' });
  assert.equal(lt(join(dir, 'current'), empty).code, 1);
});

test('deploy-air.sh: the prune\'s rollback targets and every planning read fail closed (r3 finding 2 + sweep)', async () => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  assert.ok(!/readlink[^\n]*\|\| true/.test(text), 'no readlink failure read as "no link"');
  assert.match(text, /CURRENT_BUILD="\$\(link_target "\$ROOT\/claw3d\/current"\)" \\\n  \|\| die /);
  assert.match(text, /PREVIOUS_ADAPTER="\$\(link_target "\$ROOT\/adapter"\)" \\\n  \|\| die /);
  assert.match(text, /fs_readlink\(\) \{ link_target "\$1"; \}/);
  // both are read before the first change (the order test above finds that line)
  const lines = text.split('\n');
  const first = lines.findIndex(l => /^step "1\. /.test(l));
  assert.ok(lines.findIndex(l => /^CURRENT_BUILD=/.test(l)) < first && lines.findIndex(l => /^PREVIOUS_ADAPTER=/.test(l)) < first);
  // the prune keeps exactly the new and those two
  assert.match(text, /"\$b" != "\$BUILD" && "\$b" != "\$CURRENT_BUILD"/);
  assert.match(text, /"\$a" != "\$ADAPTER_DIR" && "\$a" != "\$PREVIOUS_ADAPTER"/);
  // a failed git status is not a clean checkout
  assert.ok(!/\[\[ -z "\$\(git /.test(text));
  assert.match(text, /DIRTY="\$\(git -C "\$REPO" status --porcelain -- integrations\/office-claw3d\)" \|\| die /);
  // the remaining `|| true`s are the ones the sweep accepted (each feeds a positive match or is display-only)
  const ors = lines.filter(l => /\|\| true/.test(l)).map(l => l.trim());
  assert.equal(ors.length, 9, ors.join('\n'));
});

// ---- #77 launchd-sudo: role-user commands never inherit the owner's cwd ---------------------------
// On the Air the owner ran the script from ~/office-deploy-20261123; step 4's `git clone` as
// _cortados-claw3d died: "Unable to read current working directory: Permission denied".
test('deploy-air.sh: every role-user command runs from /, whatever the caller\'s cwd (#77 launchd-sudo)', async t => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const lines = text.split('\n');
  const cdRoot = lines.findIndex(l => l === 'cd /');
  assert.ok(cdRoot > 0, 'a top-level `cd /`');
  assert.ok(lines.findIndex(l => /^REPO=/.test(l)) < cdRoot && lines.findIndex(l => /^HERE=/.test(l)) < cdRoot,
    'HERE and REPO are resolved to absolute paths before the cd');
  lines.forEach((l, i) => { if (/sudo -u|as_studio/.test(l) && !/^\s*#/.test(l)) assert.ok(i > cdRoot, `before the cd: ${l}`); });
  assert.ok(!/^\s*cd [^/"]/m.test(text), 'no relative cd');

  // Behaviour: the script's prologue up to the `cd /`, then as_studio and switch_link as written, run
  // from a directory the caller can enter but not read; the fake sudo records the cwd it starts in.
  const W = await mkdtemp(join(tmpdir(), 'office-cwd-'));
  const cwd = join(W, 'owner-home');
  await mkdir(cwd); await chmod(cwd, 0o100);
  t.after(async () => { await chmod(cwd, 0o700); await rm(W, { recursive: true, force: true }); });
  const bin = await fakeBins(t, { sudo: `#!/bin/bash\necho "$(pwd -P) $*" >> ${JSON.stringify(join(W, 'sudo.log'))}\n` });
  const prologue = lines.slice(0, cdRoot + 1).join('\n');
  const asStudio = text.match(/^as_studio\(\) \{[\s\S]*?; \}\n/m);
  const switchLink = text.match(/^switch_link\(\) \{[\s\S]*?\n\}\n/m);
  assert.ok(asStudio && switchLink, 'as_studio and switch_link found');
  const r = spawnSync('/bin/bash', ['-c', `${prologue}
APPLY=1; run() { "$@"; }
${asStudio[0]}${switchLink[0]}
as_studio git clone --no-checkout x y
switch_link /a /b "$STUDIO_USER"
echo done`, join(HERE, 'deploy-air.sh')], { cwd, env: { PATH: `${bin}:/usr/bin:/bin` }, encoding: 'utf8' });
  assert.equal(r.stdout.trim(), 'done', r.stderr);
  const log = (await readFile(join(W, 'sudo.log'), 'utf8')).trim().split('\n');
  assert.equal(log.length, 3);
  for (const l of log) assert.match(l, /^\/ -u _cortados-claw3d /, l);
});

// ---- #77 fix round 2: root never writes through a path a role user controls ---------------------
// The role user is modelled by the fake sudo: `sudo -u USER cmd` runs cmd under macOS sandbox-exec with
// every write below PROTECTED denied, as a role user may not write root's files; plain `sudo cmd` (root)
// runs unconfined. A planted link therefore reaches the sentinel only when root follows it.
async function roleHost(t) {
  const { realpath, symlink } = await import('node:fs/promises');
  const W = await realpath(await mkdtemp(join(tmpdir(), 'office-role-')));   // the sandbox matches real paths
  t.after(() => rm(W, { recursive: true, force: true }));
  const { text, sh } = await deployPins(W);
  const settings = spawnSync('bash', ['-c', `${sh}\nprintf '%s' "$STUDIO_SETTINGS"`], { encoding: 'utf8' }).stdout;
  const P = join(W, 'root-owned');   // stands for any root-owned directory, e.g. /etc
  await mkdir(P); await mkdir(join(W, 'stage')); await mkdir(join(settings, '..'), { recursive: true });
  await writeFile(join(P, 'secret'), 'root-only\n');
  await writeFile(join(P, 'settings.json'), 'root settings\n');
  await writeFile(join(W, 'stage', 'studio-settings.json'), '{"new": true}\n');
  await writeFile(settings, '{"old": true}\n'); await chmod(settings, 0o640);
  const bin = await fakeBins(t, { sudo: `#!/bin/bash
if [ "$1" = -u ]; then u="$2"; shift 2; echo "$u $*" >> "$SUDO_LOG"
  exec /usr/bin/sandbox-exec -p "(version 1)(allow default)(deny file-write* (subpath \\"$PROTECTED\\"))" "$@"; fi
echo "root $*" >> "$SUDO_LOG"; exec "$@"
` });
  // deploy-air.sh's own primitives and its own TX_FILES entry for the settings file
  const role = text.match(/^role_write\(\) \{[\s\S]*?^promote_as\(\).*\n/m)?.[0] ?? '';   // absent before round 2
  const fs = text.match(/^fs_readlink\(\)[\s\S]*?(?=^daemon_loaded\(\))/m);
  const entry = text.match(/^TX_FILES\+=\("\$STAGE\/studio-settings\.json[^\n]*/m);
  assert.ok(fs && entry, 'fs_* primitives and the settings TX_FILES entry found');
  const snap = async () => {
    const { readdir, lstat, readlink } = await import('node:fs/promises');
    const out = {};
    for (const n of (await readdir(P)).sort()) out[n] = await readFile(join(P, n), 'utf8');
    const dir = join(settings, '..'); const st = {};
    for (const n of (await readdir(dir)).sort()) {
      const l = await lstat(join(dir, n));
      st[n] = l.isSymbolicLink() ? `-> ${await readlink(join(dir, n))}` : `${(l.mode & 0o777).toString(8)} ${await readFile(join(dir, n), 'utf8')}`;
    }
    return { protectedDir: out, studioDir: st };
  };
  const tx = (mutate, health = 'return 0') => spawnSync('/bin/bash', ['-c', `set -euo pipefail; . "$LIB"; ${sh}
STAGE=${JSON.stringify(join(W, 'stage'))}; APPLY=1; run() { "$@"; }
${role}${fs[0]}
daemon_loaded() { return 1; }; daemon_start() { :; }; daemon_stop() { :; }; health_check() { ${health}; }
TX_LINKS=(); TX_LABELS=(); TX_FILES=()
${entry[0]}
${mutate}
rc=0; tx_run || rc=$?; echo "rc=$rc"`], { env: { PATH: `${bin}:/usr/bin:/bin`, LIB, PROTECTED: P, SUDO_LOG: join(W, 'sudo.log') },
    encoding: 'utf8' });
  return { W, P, settings, snap, tx, symlink };
}

test('activation as the Studio user: a planted settings.json.next / .prev link never lets root write outside the file (#77 fix round 2)', async t => {
  // the review's reproduction: links planted after the preflight, pointing at a root-owned file
  for (const health of ['return 0', 'return 1']) {
    const H = await roleHost(t);
    const before = await H.snap();
    const r = H.tx(`ln -s ${JSON.stringify(join(H.P, 'secret'))} "$STUDIO_SETTINGS.next"
ln -s ${JSON.stringify(join(H.P, 'secret'))} "$STUDIO_SETTINGS.prev"`, health);
    const after = await H.snap();
    assert.deepEqual(after.protectedDir, before.protectedDir, `${health}: nothing outside the intended file is written`);
    if (health === 'return 0') {
      assert.equal(r.stdout.trim(), 'rc=0', r.stderr);
      assert.equal(after.studioDir['settings.json'], '600 {"new": true}\n');
    } else {
      // a failed release: the previous settings come back exactly, content and mode
      assert.equal(r.stdout.trim(), 'rc=1', r.stderr);
      assert.equal(after.studioDir['settings.json'], before.studioDir['settings.json']);
    }
    assert.ok(!('settings.json.prev' in after.studioDir), `${health}: .prev dropped`);
    assert.deepEqual(Object.keys(after.studioDir).filter(n => /\.next\.\w+$/.test(n)), [], `${health}: no temp file left`);
    // root only read: its STAGE (cat) and existence checks (test); every write went through sudo -u
    const rootCmds = readFileSync(join(H.W, 'sudo.log'), 'utf8').trim().split('\n').filter(l => l.startsWith('root '));
    for (const l of rootCmds) assert.match(l, /^root (cat \S+\/stage\/studio-settings\.json|\/bin\/sh -c test )/, l);
  }
});

test('activation as the Studio user: claw3d/ swapped for a link to a root-owned directory after the preflight is never written (#77 fix round 2)', async t => {
  const H = await roleHost(t);
  const before = await H.snap();
  const r = H.tx(`d="$(dirname "$STUDIO_SETTINGS")"; mv "$d" "$d.moved"; ln -s ${JSON.stringify(H.P)} "$d"`);
  assert.equal(r.stdout.trim(), 'rc=1', r.stderr);   // the Studio user may not write there: refused, rolled back
  assert.match(r.stderr, /installing .*settings\.json failed/);
  const { readdir } = await import('node:fs/promises');
  const after = {};
  for (const n of (await readdir(H.P)).sort()) after[n] = await readFile(join(H.P, n), 'utf8');
  assert.deepEqual(after, before.protectedDir, 'the root-owned directory is untouched: no .prev, no .next, no new settings');
  // the real settings file, moved aside by the swap, is untouched too
  assert.equal(await readFile(join(H.settings, '..') + '.moved/settings.json', 'utf8'), '{"old": true}\n');
});

test('deploy-air.sh: no root write inside a role user\'s directory; secrets there are written as the user (#77 fix round 2)', async () => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const roleOwned = /\$(BRIDGE_HOME|STUDIO_HOME)\/|\$STUDIO_STATE|\$STUDIO_SETTINGS|\$ROOT\/claw3d\/|\$BRIDGE_SECRET|\$ACCESS_TOKEN|\$OFFICE_TOKEN/;
  for (const l of text.split('\n')) {
    if (/^\s*#/.test(l)) continue;
    const m = l.match(/\bsudo (install|cp|mv|rm|tee|ln|chown|chmod|tar)\b(.*)/);
    if (m) assert.ok(!roleOwned.test(m[2]), `root writes into a role user's directory: ${l.trim()}`);
  }
  // every TX_FILES entry whose destination lies in a role user's directory names that user
  const entries = [...text.matchAll(/TX_FILES\+?=\(([\s\S]*?)\)/g)].flatMap(m => [...m[1].matchAll(/"([^"]*)"/g)].map(x => x[1]));
  for (const e of entries) {
    const [, dest, user = ''] = e.split('|');
    if (roleOwned.test(dest)) assert.match(user, /^\$(BRIDGE|STUDIO)_USER$/, e);
    else assert.equal(user, '', e);
  }
  assert.match(text, /run role_write_from "\$BRIDGE_USER" "\$TMP\/bridge-token" "\$BRIDGE_SECRET"/);
  assert.match(text, /run role_write_from "\$STUDIO_USER" "\$TMP\/access-token" "\$ACCESS_TOKEN"/);
  assert.match(text, /run sudo -u "\$STUDIO_USER" install -d -m 0700 "\$STUDIO_HOME\/secrets" "\$STUDIO_STATE" "\$STUDIO_STATE\/claw3d"\n/);
});

test('role_write: written as the user, 0600, by an exclusive temp file and a rename; a failure leaves no temp file (#77 fix round 2)', async t => {
  const H = await roleHost(t);
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const role = text.match(/^role_write\(\) \{[\s\S]*?^promote_as\(\).*\n/m);
  assert.ok(role, 'role_write found');
  const dest = join(H.W, 'home', 'bridge', 'secrets', 'bridge-token');
  await mkdir(join(dest, '..'), { recursive: true });
  await H.symlink(join(H.P, 'secret'), dest + '.next');   // a stale or planted .next is never used
  // only `sudo -u` is allowed here: a root write fails the call outright
  const bin = await fakeBins(t, { sudo: `#!/bin/bash
[ "$1" = -u ] || { echo "root write" >&2; exit 97; }
shift 2; exec /usr/bin/sandbox-exec -p "(version 1)(allow default)(deny file-write* (subpath \\"$PROTECTED\\"))" "$@"
` });
  const run = (d, src) => spawnSync('/bin/bash', ['-c', `set -euo pipefail\n${role[0]}\nrc=0; role_write_from u "$SRC" "$DEST" || rc=$?; echo "rc=$rc"`],
    { env: { PATH: `${bin}:/usr/bin:/bin`, SRC: src, DEST: d, PROTECTED: H.P }, encoding: 'utf8' });
  const src = join(H.W, 'token'); await writeFile(src, 'f'.repeat(64) + '\n');
  let r = run(dest, src);
  assert.equal(r.stdout.trim(), 'rc=0', r.stderr);
  const { stat, readdir } = await import('node:fs/promises');
  assert.equal(await readFile(dest, 'utf8'), 'f'.repeat(64) + '\n');
  assert.equal((await stat(dest)).mode & 0o777, 0o600);
  assert.equal(await readFile(join(H.P, 'secret'), 'utf8'), 'root-only\n');
  assert.deepEqual((await readdir(join(dest, '..'))).sort(), ['bridge-token', 'bridge-token.next']);   // the planted link, untouched
  // a destination the user may not write (here: inside the root-owned directory): fails, no temp file anywhere
  r = run(join(H.P, 'secret'), src);
  assert.equal(r.stdout.trim(), 'rc=1');
  assert.deepEqual((await readdir(H.P)).sort(), ['secret', 'settings.json']);
  assert.equal(await readFile(join(H.P, 'secret'), 'utf8'), 'root-only\n');
  // a source that cannot be read: fails before anything is created, and the temp file is removed
  r = run(dest + '-2', join(H.W, 'no-such-file'));
  assert.equal(r.stdout.trim(), 'rc=1');
  assert.deepEqual((await readdir(join(dest, '..'))).sort(), ['bridge-token', 'bridge-token.next']);
});

test('stage_studio_settings: a failed stage leaves no token-bearing file in TMP (#77 fix round 2)', async t => {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  // round 2 splits it in two (the steps, then the wipe); 1bde7eb has the one function
  const fn = text.match(/^(?:build_studio_settings\(\) \{[\s\S]*?\n\}\n)?stage_studio_settings\(\) \{[\s\S]*?\n\}\n/m);
  assert.ok(fn, 'stage_studio_settings found');
  const root = await mkdtemp(join(tmpdir(), 'office-stagefail-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const { sh } = await deployPins(root);
  const [bridgeSecret, settingsPath] = spawnSync('bash', ['-c', `${sh}\nprintf '%s\\n' "$BRIDGE_SECRET" "$STUDIO_SETTINGS"`],
    { encoding: 'utf8' }).stdout.trim().split('\n');
  await mkdir(join(bridgeSecret, '..'), { recursive: true }); await writeFile(bridgeSecret, BRIDGE_TOKEN + '\n');
  await mkdir(join(settingsPath, '..'), { recursive: true }); await writeFile(settingsPath, '{"version": 1, "avatars": {}}\n');
  for (const d of ['stage', 'tmp']) await mkdir(join(root, d));
  // the staging install into root's STAGE fails (a refused sudo, a full disk): both TMP files held the token
  const bin = await fakeBins(t, { sudo: `#!/bin/bash
if [ "$1" = -u ]; then shift 2; fi
if [ "$1" = install ]; then echo "install: disk full" >&2; exit 1; fi
exec "$@"
` });
  const r = spawnSync('bash', ['-c', `set -euo pipefail; . "$LIB"; ${sh}
HERE=${JSON.stringify(HERE.replace(/\/$/, ''))} TMP=${JSON.stringify(join(root, 'tmp'))} STAGE=${JSON.stringify(join(root, 'stage'))}
${fn[0]}
rc=0; stage_studio_settings || rc=$?; echo "rc=$rc"`], { env: { PATH: `${bin}:/usr/bin:/bin`, LIB }, encoding: 'utf8' });
  assert.equal(r.stdout.trim(), 'rc=1', r.stderr);
  const { readdir } = await import('node:fs/promises');
  assert.deepEqual(await readdir(join(root, 'tmp')), [], 'no token-bearing file left in TMP');
  assert.deepEqual(await readdir(join(root, 'stage')), []);
  assert.ok(!r.stderr.includes(BRIDGE_TOKEN));
});

test('wipe_rm and on_exit: secrets are overwritten and removed on every exit; a link is removed, never written through (#77 fix round 2)', async t => {
  const { symlink, readdir } = await import('node:fs/promises');
  const W = await mkdtemp(join(tmpdir(), 'office-wipe-'));
  t.after(() => rm(W, { recursive: true, force: true }));
  await writeFile(join(W, 'a'), 'secret-a\n'); await writeFile(join(W, 'empty'), '');
  await writeFile(join(W, 'target'), 'keep me\n'); await symlink(join(W, 'target'), join(W, 'link'));
  const r = spawnSync('bash', ['-c', `. "$LIB"; rc=0; wipe_rm "$W/a" "$W/empty" "$W/link" "$W/absent" || rc=$?; echo "rc=$rc"`],
    { env: { PATH: '/usr/bin:/bin', LIB, W }, encoding: 'utf8' });
  assert.equal(r.stdout.trim(), 'rc=0', r.stderr);
  assert.deepEqual((await readdir(W)).sort(), ['target']);
  assert.equal(await readFile(join(W, 'target'), 'utf8'), 'keep me\n');
  // the overwrite happens before the unlink: a second hard link shows the zeroed content
  await writeFile(join(W, 'b'), 'secret-b\n');
  const { link } = await import('node:fs/promises'); await link(join(W, 'b'), join(W, 'b-witness'));
  spawnSync('bash', ['-c', '. "$LIB"; wipe_rm "$W/b"'], { env: { PATH: '/usr/bin:/bin', LIB, W } });
  assert.equal(await readFile(join(W, 'b-witness'), 'utf8'), '\0'.repeat(9));
  // on_exit: every secret file deploy-air.sh makes in TMP is registered, and wiped before TMP goes
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const reg = text.match(/^SECRET_TMP=\(([^)]*)\)/m);
  assert.ok(reg, 'SECRET_TMP registered');
  const names = reg[1].trim().split(/\s+/);
  // every literal name under $TMP ($TMP/$NODE_TARBALL, the public Node tarball, is not literal)
  const used = new Set([...text.matchAll(/\$TMP\/([\w.-]+)/g)].map(m => m[1]));
  assert.ok(used.has('studio-settings.json') && used.has('bridge-token'));
  for (const n of used) assert.ok(names.includes(n), `$TMP/${n} is not registered in SECRET_TMP`);
  assert.ok(text.indexOf('SECRET_TMP=(') < text.indexOf('trap on_exit EXIT'));
  assert.match(text, /cleanup\(\) \{[\s\S]*?wipe_rm "\$\{SECRET_TMP\[@\]\/#\/\$TMP\/\}"[\s\S]*?rm -rf "\$TMP"\n  fi\n\}/);
  assert.ok(!/\brm -P\b/.test(text.replace(/^\s*#.*$/gm, '')), 'no rm -P left (a no-op on macOS)');
});

// ---- #77 fix round 3: a signal (INT/TERM/HUP) wipes the secrets and revokes the pending client ----
// Reason: bash does not run the EXIT trap when an untrapped signal kills it. These send REAL signals to
// /bin/bash running deploy-air.sh's own cleanup block (cut from the script's text, so the test runs
// what ships) and a fake step that blocks until signalled.
function cleanupBlock() {
  const text = readFileSync(join(HERE, 'deploy-air.sh'), 'utf8');
  const a = text.indexOf('\n', text.indexOf('\n[[ $APPLY -eq 1 ]] || echo "  DRY RUN') + 1) + 1;   // the top-level one
  const b = text.indexOf('# ---- 1. role users');
  assert.ok(a > 0 && b > a, 'cleanup block found');
  return text.slice(a, b);
}
// A stand-in for `python3 -m cortados_router.provider.clients`: logs its arguments.
async function fakeRepo(W) {
  const p = join(W, 'repo', 'cortados_router', 'provider');
  await mkdir(p, { recursive: true });
  await writeFile(join(W, 'repo', 'cortados_router', '__init__.py'), '');
  await writeFile(join(p, '__init__.py'), '');
  await writeFile(join(p, 'clients.py'),
    'import os, sys\nopen(os.environ["W"] + "/clients.log", "a").write(" ".join(sys.argv[1:]) + "\\n")\n');
  return join(W, 'repo');
}
async function untilExists(path, ms = 10000) {
  const end = Date.now() + ms;
  while (!existsSync(path)) {
    if (Date.now() > end) throw new Error(`${path} never appeared`);
    await new Promise(r => setTimeout(r, 25));
  }
}
function spawnSignalled(cmd, args, env, readyPath, signals) {
  return new Promise((resolveP, reject) => {
    const c = spawn(cmd, args, { env });
    let stdout = '', stderr = '';
    c.stdout.on('data', d => { stdout += d; }); c.stderr.on('data', d => { stderr += d; });
    c.on('close', (status, signal) => resolveP({ status, signal, stdout, stderr }));
    untilExists(readyPath).then(async () => {
      for (const s of signals) { c.kill(s); await new Promise(r => setTimeout(r, 50)); }
    }, e => { c.kill('SIGKILL'); reject(e); });
  });
}
const SIGNALS = [['SIGTERM', 143], ['SIGINT', 130], ['SIGHUP', 129]];

test('deploy-air.sh: a signal during the secrets steps wipes every secret in TMP and revokes the pending client (#77 fix round 3)', async t => {
  const block = cleanupBlock();
  for (const [sig, code] of SIGNALS) {
    // a second signal during the cleanup must neither re-run nor cut it short
    for (const signals of [[sig], [sig, sig, 'SIGTERM']]) {
      const W = await mkdtemp(join(tmpdir(), 'office-sig-'));
      t.after(() => rm(W, { recursive: true, force: true }));
      const repo = await fakeRepo(W);
      const script = `set -euo pipefail
. "$LIB"
APPLY=1; REPO=${JSON.stringify(repo)}
${block}
echo "$TMP" > "$W/tmpdir"
(umask 077; echo office-secret > "$TMP/office-token"; echo settings-secret > "$TMP/studio-settings.json")
PENDING_CLIENT=office-b
: > "$W/ready"
while :; do sleep 0.1; done
`;
      const r = await spawnSignalled('/bin/bash', ['-c', script], { PATH: '/usr/bin:/bin', LIB, W }, join(W, 'ready'), signals);
      const label = `${signals.join('+')}: ${r.stderr}`;
      const tmp = readFileSync(join(W, 'tmpdir'), 'utf8').trim();
      t.after(() => rm(tmp, { recursive: true, force: true }));
      assert.equal(r.status, code, label);
      assert.ok(!existsSync(tmp), `TMP and its secrets are gone (${label})`);
      assert.equal(readFileSync(join(W, 'clients.log'), 'utf8'), 'remove office-b\n', label);   // once
    }
  }
});

test('activation: a real signal during activation rolls back, then wipes TMP and revokes the pending client (#77 fix round 3)', async t => {
  const block = cleanupBlock();
  for (const [sig, code] of SIGNALS) {
    const D = await txHost(t, { upgrade: true });
    const repo = await fakeRepo(D);
    const prelude = `APPLY=1; REPO=${JSON.stringify(repo)}
${block}
echo "$TMP" > "$D/tmpdir"; (umask 077; echo office-secret > "$TMP/office-token"); PENDING_CLIENT=office-b`;
    const r = await spawnSignalled('/bin/bash', [TX_HARNESS],
      { PATH: '/usr/bin:/bin', LIB, FAKE: D, FAIL: 'block', FAIL_ALWAYS: '', PRELUDE: prelude, UNKNOWN: '', W: D },
      join(D, 'blocking'), [sig, sig]);
    const tmp = readFileSync(join(D, 'tmpdir'), 'utf8').trim();
    t.after(() => rm(tmp, { recursive: true, force: true }));
    assert.equal(r.status, code, `${sig}: ${r.stderr}`);
    assert.equal(r.stdout.trim(), '', sig);           // exited from the handler, never reached "rc="
    assert.match(r.stderr, /ROLLBACK/, sig);
    assert.equal((r.stderr.match(/ROLLBACK/g) || []).length, 1, `${sig}: the rollback ran once`);
    assert.deepEqual(await snapshot(D), OLD, sig);
    assert.ok(!existsSync(tmp), sig);
    assert.equal(readFileSync(join(D, 'clients.log'), 'utf8'), 'remove office-b\n', sig);
  }
});

test('activation: a signal during the rollback of a failed activation waits for it to finish, then exits 128+signal (#77 fix round 3)', async t => {
  const D = await txHost(t, { upgrade: true });
  const repo = await fakeRepo(D);
  const prelude = `APPLY=1; REPO=${JSON.stringify(repo)}
${cleanupBlock()}
echo "$TMP" > "$D/tmpdir"; (umask 077; echo office-secret > "$TMP/office-token"); PENDING_CLIENT=office-b
eval "orig_$(declare -f tx_rollback)"
tx_rollback() { kill -TERM $$; kill -TERM $$; orig_tx_rollback; }`;
  const r = spawnSync('/bin/bash', [TX_HARNESS],
    { env: { PATH: '/usr/bin:/bin', LIB, FAKE: D, FAIL: 'health', FAIL_ALWAYS: '', PRELUDE: prelude, UNKNOWN: '', W: D }, encoding: 'utf8' });
  const tmp = readFileSync(join(D, 'tmpdir'), 'utf8').trim();
  t.after(() => rm(tmp, { recursive: true, force: true }));
  assert.equal(r.status, 143, r.stderr);
  assert.equal(r.stdout.trim(), '');
  assert.deepEqual(await snapshot(D), OLD);
  assert.ok(!existsSync(tmp));
  assert.equal(readFileSync(join(D, 'clients.log'), 'utf8'), 'remove office-b\n');
});

test('activation: after a committed activation the script\'s own signal handlers are back, never cleared (#77 fix round 3)', async t => {
  const D = await txHost(t, { upgrade: true });
  const repo = await fakeRepo(D);
  // a signal in step 8, after tx_run: the pending client is still set until the commit clears it
  const prelude = `APPLY=1; REPO=${JSON.stringify(repo)}
${cleanupBlock()}
echo "$TMP" > "$D/tmpdir"; (umask 077; echo office-secret > "$TMP/office-token"); PENDING_CLIENT=office-b
eval "orig_$(declare -f tx_commit)"
tx_commit() { orig_tx_commit; kill -TERM $$; }`;
  const r = spawnSync('/bin/bash', [TX_HARNESS],
    { env: { PATH: '/usr/bin:/bin', LIB, FAKE: D, FAIL: '', FAIL_ALWAYS: '', PRELUDE: prelude, UNKNOWN: '', W: D }, encoding: 'utf8' });
  const tmp = readFileSync(join(D, 'tmpdir'), 'utf8').trim();
  t.after(() => rm(tmp, { recursive: true, force: true }));
  assert.equal(r.status, 143, r.stderr);
  assert.deepEqual(await snapshot(D), NEW);   // committed: no rollback
  assert.ok(!existsSync(tmp));
  assert.equal(readFileSync(join(D, 'clients.log'), 'utf8'), 'remove office-b\n');
});

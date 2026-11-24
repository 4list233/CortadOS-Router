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
  assert.ok(lines.some(l => l.includes('[[ $TOKEN_PLAN == keep ]] || TX_FILES+=("$STAGE/office-token|$OFFICE_TOKEN")')));
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
  assert.equal(r.status, 1); assert.equal(r.out, '');      // exited from the trap, never reached "rc="
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
const SUDO_ROOT = '#!/bin/bash\nexport FAKE_ROOT=1\nexec "$@"\n';
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
// the outcome is the listener check's alone
async function officeHealthy(t, sudo, lsofScript) {
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  const m = text.match(/^http_code\(\) \{.*\n^office_healthy\(\) \{[\s\S]*?\n\}\n/m);
  assert.ok(m, 'office_healthy found');
  const bin = await fakeBins(t, {
    sudo, lsof: lsofScript,
    launchctl: '#!/bin/bash\n[ "${FAKE_ROOT:-}" = 1 ] || { echo "Could not find service" >&2; exit 113; }\n'
      + 'echo "system/x = {"\necho "\tstate = running"\necho "}"\n',
    curl: '#!/bin/bash\nfor a; do u="$a"; done\ncase "$u" in *:18789/) printf 426 ;; *:3000/) printf 401 ;; */cortados-login) printf 200 ;; esac\n',
  });
  const r = spawnSync('bash', ['-c', `set -euo pipefail; . "$LIB"; BRIDGE_LABEL=b STUDIO_LABEL=s BRIDGE_PORT=18789 STUDIO_PORT=3000
${m[0]}
rc=0; why="$(office_healthy)" || rc=$?; echo "rc=$rc $why"`],
  { env: { PATH: `${bin}:/usr/bin:/bin`, LIB }, encoding: 'utf8' });
  return r.stdout.trim();
}
test('deploy-air.sh office_healthy: a refused sudo lsof fails the health check (rollback), never passes it (r3 finding 1)', async t => {
  assert.equal(await officeHealthy(t, SUDO_ROOT, lsof(0, LOOPBACK)), 'rc=0');
  // a refused sudo now fails at the first root read, the daemon state: never a pass
  assert.match(await officeHealthy(t, SUDO_REFUSED, lsof(0, LOOPBACK)), /^rc=1 b is not running/);
  assert.match(await officeHealthy(t, SUDO_ROOT, lsof(1, '')), /^rc=1 /);
  assert.match(await officeHealthy(t, SUDO_ROOT, lsof(0, 'p1\nf3\nn*:3000\np2\nf3\nn127.0.0.1:18789\n')), /^rc=1 a listener on port 3000 is not loopback-only/);
  // a refused sudo for lsof alone (launchctl answered as root) still fails the check
  const lsofRefused = '#!/bin/bash\nif [ "$1" = lsof ]; then echo "sudo: a password is required" >&2; exit 1; fi\nexport FAKE_ROOT=1\nexec "$@"\n';
  assert.match(await officeHealthy(t, lsofRefused, lsof(0, LOOPBACK)), /^rc=1 cannot list the listeners/);
  const text = await readFile(join(HERE, 'deploy-air.sh'), 'utf8');
  assert.ok(!/sudo lsof/.test(text), 'lsof runs only in deploy-lib.sh (listeners_loopback_only)');
  assert.ok(!/\| *grep -q/.test(text), 'no `| grep -q` under pipefail (SIGPIPE reads as failure)');
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

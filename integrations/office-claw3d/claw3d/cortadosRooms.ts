// CortadOS rooms for Claw3D (issue #77, integrations/office-claw3d/FLOORS-DESIGN.md in CortadOS-Router).
// One room (a pod of desks) per orchestrator on the one office floor: the orchestrator at the head desk, its child
// tasks and their helpers at the team desks, a finished task at a report desk beside the head desk.
// Active only while the CortadOS office bridge feeds this Studio (cortadosRoomsActive); every other gateway, the stock
// layout (the rooms are saved under their own namespace) and every desk a person assigned by hand are left exactly as
// stock Claw3D has them.
// Self-contained at runtime (type-only imports): the CortadOS repo unit-tests this file under Node's type stripping.
import type { FurnitureItem } from "@/features/retro-office/core/types";

// ---- the floor plan (owner decision E6, 2026-11-24: 8 rooms x 6 desks) ----
// 4 x 2 rooms fill the local office canvas (district.ts LOCAL_OFFICE_CANVAS_WIDTH x _HEIGHT, 1800 x 720). Each room is
// open towards the middle corridor (no wall there) and walled on its back; its side walls stop CORRIDOR_GAP short of
// the open edge. Reason (office round 2 fix): Claw3D pads every wall by 15 px on its 25 px navigation grid
// (navigation.ts buildNavGrid), so full-height side walls closed the 20 px gap between the two rows and sealed each
// column of rooms: a person spawned in another column (RetroOffice3D pickSpawnPoint: x 100..900, y 100..600) got no
// path to their desk and wandered near the spawn. With the gap, the corridor across the floor is open (rooms.test.mjs
// checks the layout on Claw3D's own grid). Seats are 100 px apart vertically, as in the stock office.
export const CORTADOS_ROOMS = 8;
export const CORTADOS_TEAM_DESKS = 6;
// Beside the head desk: where a finished child task reports back (E8). Two fit on the head row; more finished at once
// stand where Claw3D leaves an unseated person (see FLOORS-DESIGN.md "Overflow").
export const CORTADOS_REPORT_DESKS = 2;
const COLS = 4;
const ROOM_W = 430;
const ROOM_H = 340;
const COL_PITCH = 450;
// The bottom row starts on Claw3D's 25 px navigation grid (375, not 370): off the grid, its desks 100 px apart filled
// every grid row between them and closed the aisle to the right-hand seats (rooms.test.mjs reachability check).
const ROW_Y = [10, 375];
const WALL = 8; // core/constants.ts WALL_THICKNESS
export const CORTADOS_CORRIDOR_GAP = 60;
const DESK_X = [25, 165, 305]; // three 100 px desks per row inside a 430 px room
const ROW_DESK_Y = [50, 150, 250]; // head row, then two team rows
export const CORTADOS_UID_PREFIX = "cortados_";
// Bumped whenever the floor's furniture changes, so a floor saved by an earlier build is rebuilt (isCortadosLayout).
// 3: office round 2 fix (side walls open to the corridor; the server room has racks drawn from the bridge, no desks).
const LAYOUT_VERSION = 3;

// ---- the server room (office round 2, owner 2026-11-27): a rack per host and the S1 console ----
// It takes the floor's last room slot (bottom right), so 7 rooms are left for orchestrators ([ESCALATE] B8 in
// OFFICE-CORTADOS-DESIGN.md: the alternative is a wider floor). Nobody sits there: hosts and S1 are not people (office
// round 2 fix, owner 2026-11-27). The bridge sends the room's state in its hello and office.snapshot frames
// (adapter.mjs mapServers); cortadosNoteGatewayFrame keeps it and objects/cortadosSigns.tsx draws a rack per host with
// its sign and the S1 console with its sign.
export const CORTADOS_SERVER_ROOM = 7;
export const CORTADOS_ORCHESTRATOR_ROOMS = CORTADOS_SERVER_ROOM;
export const CORTADOS_SERVERS_GROUP = "servers";
export const CORTADOS_RACKS_MAX = 8; // adapter.mjs SERVER_RACKS_MAX (live.SERVER_RACKS_MAX)

type Slot = "head" | "report" | "desk";

const deskUid = (room: number, slot: Slot, index: number) => `${CORTADOS_UID_PREFIX}r${room}_${slot}${index}`;

// ---- room colours (office round 2): adapter.mjs ROOM_COLORS / roomHash, a test keeps the two equal ----
// Claw3D's own clothing colours (src/lib/avatars/profile.ts AGENT_AVATAR_CLOTHING_COLOR_OPTIONS), so the room's people
// can wear their room's colour: seven for the orchestrator rooms, slate for the rest.
export const CORTADOS_ROOM_COLORS = ["#7090ff", "#34d399", "#f59e0b", "#f43f5e", "#8b5cf6", "#f5f5f4", "#2d3748"];
export const CORTADOS_NEUTRAL_ROOM_COLOR = "#64748b";
/** FNV-1a 32-bit of a room key (adapter.mjs roomHash). */
export function cortadosRoomHash(key: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < key.length; i += 1) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}
/** A room's colour from its group key: stable across polls and room moves; grey for the rooms nobody orchestrates. */
export const cortadosRoomColor = (group: string): string =>
  group === "unattributed" || group === "lobby" || group === CORTADOS_SERVERS_GROUP
    ? CORTADOS_NEUTRAL_ROOM_COLOR
    : CORTADOS_ROOM_COLORS[cortadosRoomHash(group) % CORTADOS_ROOM_COLORS.length];

/** Where room `room` sits on the canvas (the same arithmetic the layout uses). */
export function cortadosRoomRect(room: number): { x: number; y: number; w: number; h: number; top: boolean } {
  const top = room < COLS;
  return { x: 10 + (room % COLS) * COL_PITCH, y: ROW_Y[top ? 0 : 1], w: ROOM_W, h: ROOM_H, top };
}

const deskWithChair = (uid: string, x: number, y: number): FurnitureItem[] => [
  { _uid: uid, type: "desk_cubicle", x, y },
  { _uid: `${uid}_chair`, type: "chair", x: x + 20, y: y - 10, facing: 180 },
  { _uid: `${uid}_computer`, type: "computer", x: x + 20, y: y - 13 },
  { _uid: `${uid}_keyboard`, type: "keyboard", x: x + 30, y: y - 5 },
];

/** The CortadOS floor: 7 orchestrator rooms, each with 1 head desk, 2 report desks and 6 team desks, and the server
 * room (walls only: its racks are drawn from the bridge's data), in a fixed _uid scheme. */
export function buildCortadosRoomsLayout(): FurnitureItem[] {
  const items: FurnitureItem[] = [];
  for (let room = 0; room < CORTADOS_ROOMS; room += 1) {
    const { x: ox, y: oy, top } = cortadosRoomRect(room);
    const wall = (name: string, x: number, y: number, w: number, h: number): FurnitureItem =>
      ({ _uid: `${CORTADOS_UID_PREFIX}r${room}_wall_${name}_v${LAYOUT_VERSION}`, type: "wall", x, y, w, h });
    // the back wall away from the corridor, and both side walls, which stop CORRIDOR_GAP short of the open edge
    const sideY = top ? oy : oy + CORTADOS_CORRIDOR_GAP;
    const sideH = ROOM_H - CORTADOS_CORRIDOR_GAP;
    items.push(wall("back", ox, top ? oy : oy + ROOM_H - WALL, ROOM_W, WALL));
    items.push(wall("left", ox, sideY, WALL, sideH));
    items.push(wall("right", ox + ROOM_W - WALL, sideY, WALL, sideH));
    if (room === CORTADOS_SERVER_ROOM) continue;
    items.push(...deskWithChair(deskUid(room, "report", 0), ox + DESK_X[0], oy + ROW_DESK_Y[0]));
    items.push(...deskWithChair(deskUid(room, "head", 0), ox + DESK_X[1], oy + ROW_DESK_Y[0]));
    items.push(...deskWithChair(deskUid(room, "report", 1), ox + DESK_X[2], oy + ROW_DESK_Y[0]));
    for (let d = 0; d < CORTADOS_TEAM_DESKS; d += 1) {
      items.push(...deskWithChair(deskUid(room, "desk", d), ox + DESK_X[d % 3], oy + ROW_DESK_Y[1 + Math.floor(d / 3)]));
    }
  }
  return items;
}

/** Whether a layout is the CURRENT CortadOS floor: it holds this build's versioned wall uids. A floor saved by an
 * earlier build (closed side walls, desks in the server room) is not, so it is rebuilt once: nobody can edit the rooms
 * layout (the editor is hidden), so nothing of the owner's is lost. */
export const CORTADOS_LAYOUT_MARK = `${CORTADOS_UID_PREFIX}r${CORTADOS_SERVER_ROOM}_wall_back_v${LAYOUT_VERSION}`;
export const isCortadosLayout = (items: FurnitureItem[] | null | undefined): boolean =>
  Array.isArray(items) && items.some((item) => item?._uid === CORTADOS_LAYOUT_MARK);

// ---- the rooms' own storage (CortadOS #77 review round 1) ----
// Claw3D saves the furniture under its storageNamespace (persistence.ts: the key itself for "default", else
// "<key>:<namespace>"), 300 ms after any change. While the rooms are on, the layout is read and saved under a
// namespace of its own, so the owner's stock layout is never written: switching the rooms off shows it exactly as it
// was. Desks assigned by hand are Studio's settings, kept per gateway by desk uid and never touched here: a stock
// desk's assignment applies again as soon as that desk is back on the floor.
const LAYOUT_NAMESPACE = "cortados-rooms";

export const cortadosLayoutNamespace = (storageNamespace: string): string =>
  storageNamespace === "default" ? LAYOUT_NAMESPACE : `${storageNamespace}:${LAYOUT_NAMESPACE}`;

export const isCortadosLayoutNamespace = (namespace: string): boolean =>
  namespace === LAYOUT_NAMESPACE || namespace.endsWith(`:${LAYOUT_NAMESPACE}`);

/** The rooms' initial layout from what their namespace holds: the saved floor, else a fresh one. The stock
 * migrations (gym, QA lab, jukebox...) never run on it. */
export const cortadosInitialLayout = (saved: FurnitureItem[] | null): FurnitureItem[] =>
  isCortadosLayout(saved) ? (saved as FurnitureItem[]) : buildCortadosRoomsLayout();

// ---- who belongs where: the bridge's agent id grammar (adapter.mjs agentId) ----
// office-<group>-orchestrator | -task-<task id> | -sub-<task id>-<ref> | -routed-<ref>. A group is a session tag
// (16-64 lowercase hex), a named origin (letters, at most 15: never a session tag) or "unattributed". "servers" is
// refused as a bot origin by the adapter, and refused here too: the server room is never an orchestrator's room.
const RUN = "[A-Za-z0-9_-]{8,64}";
const CORTADOS_ID = new RegExp(`^office-([0-9a-f]{16,64}|[a-z]{2,15})-(orchestrator|task-${RUN}|sub-${RUN}|routed-${RUN})$`);
// A finished child task's nameplate subtitle (adapter.mjs RETURN_LABEL): it reports at a desk beside its orchestrator.
export const CORTADOS_RETURN_SUBTITLE = /^(done|failed|cancelled) · /;

export const isCortadosAgentId = (id: string): boolean => {
  const m = CORTADOS_ID.exec(id);
  return m !== null && m[1] !== CORTADOS_SERVERS_GROUP;
};

export function cortadosPlaceOf(agent: { id: string; subtitle?: string | null }):
  { group: string; slot: Slot } | null {
  const m = CORTADOS_ID.exec(agent.id);
  if (!m || m[1] === CORTADOS_SERVERS_GROUP) return null;
  const slot: Slot = m[2] === "orchestrator" ? "head"
    : m[2].startsWith("task-") && CORTADOS_RETURN_SUBTITLE.test(agent.subtitle ?? "") ? "report" : "desk";
  return { group: m[1], slot };
}

// ---- nameplates (office round 2 fix, owner 2026-11-27: plates piled up and stock cut names to the first word) ----
// Stock Claw3D (objects/agents.tsx formatAgentNameplateText) shows a name of more than 10 characters as its first word
// only, so every "Office round 2 › ..." read "Office". For our people: a room's head (its orchestrator) and the person
// under the pointer get the full two-line plate (the name, cut with "…", over the subtitle); everyone else a short
// one-line label: the task part of the name (after "›"), cut to CORTADOS_PLATE_SHORT characters.
export const CORTADOS_PLATE_FULL = 26;
export const CORTADOS_PLATE_SUBTITLE = 38;
export const CORTADOS_PLATE_SHORT = 12;
export type CortadosNameplate = { title: string; subtitle: string | null; full: boolean };
const cut = (s: string, max: number): string => (s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s);
const oneLine = (s: unknown): string => (typeof s === "string" ? s.replace(/\s+/g, " ").trim() : "");

/** The plate of one of our people, or null for anyone else (stock plate). */
export function cortadosNameplate(agentId: string, name: unknown, subtitle: unknown, hovered: boolean):
  CortadosNameplate | null {
  const m = CORTADOS_ID.exec(agentId);
  if (!m || m[1] === CORTADOS_SERVERS_GROUP) return null;
  const full = oneLine(name);
  if (m[2] === "orchestrator" || hovered) {
    const sub = oneLine(subtitle);
    return { title: cut(full, CORTADOS_PLATE_FULL), subtitle: sub ? cut(sub, CORTADOS_PLATE_SUBTITLE) : null, full: true };
  }
  const at = full.lastIndexOf("›");
  const part = at >= 0 ? full.slice(at + 1).trim() : full;
  return { title: cut(part || full, CORTADOS_PLATE_SHORT), subtitle: null, full: false };
}

// ---- door signs and floor tints (office round 2): one per room in use, plus the server room always ----
export type CortadosRoomSign = { room: number; group: string | null; label: string; color: string;
  x: number; y: number; w: number; h: number; top: boolean };
const SIGN_MAX = 40; // characters on a door sign: the plate is 3/4 of a room wide
const fixedLabel = (group: string): string | null =>
  group === "unattributed" ? "Unattributed" : group === "lobby" ? "No orchestrator" : null;
const signText = (s: string): string => (s.length > SIGN_MAX ? `${s.slice(0, SIGN_MAX - 1)}…` : s);

/**
 * The signs to draw: each room the allocator gave a group (state.roomByGroup), named after the group's orchestrator
 * (the head-desk person's name: the session name, else the project) or the room's fixed label, in the room's colour;
 * and the server room, always. `rect` is the room's floor on the canvas, for the tint.
 */
export function cortadosRoomSigns(agents: Array<{ id: string; name?: string | null }>, state: CortadosRoomState):
  CortadosRoomSign[] {
  const heads = new Map<string, string>();
  for (const agent of agents) {
    const m = CORTADOS_ID.exec(agent.id);
    if (m && m[2] === "orchestrator" && typeof agent.name === "string" && agent.name.trim()) {
      heads.set(m[1], agent.name.trim());
    }
  }
  const out: CortadosRoomSign[] = [];
  for (const [group, room] of state.roomByGroup) {
    if (group === CORTADOS_SERVERS_GROUP) continue;
    const label = heads.get(group) ?? fixedLabel(group);
    if (label === null) continue;
    out.push({ room, group, label: signText(label), color: cortadosRoomColor(group), ...cortadosRoomRect(room) });
  }
  out.push({ room: CORTADOS_SERVER_ROOM, group: CORTADOS_SERVERS_GROUP, label: "Server room",
    color: CORTADOS_NEUTRAL_ROOM_COLOR, ...cortadosRoomRect(CORTADOS_SERVER_ROOM) });
  return out.sort((a, b) => a.room - b.room);
}

// ---- the server room's state, from the bridge (office round 2 fix) ----
// The bridge sends it as `snapshot.cortadosServers` in its hello and `payload.servers` in every office.snapshot event
// (adapter.mjs OfficeGateway). GatewayBrowserClient.handleMessage hands every frame to cortadosNoteGatewayFrame (one
// line, cortados-rooms.patch); anything that is not one of those two frames is ignored. Validated field by field, fixed
// words and short strings only; a malformed room is dropped (the last good one stays), never drawn as given.
export type CortadosServerState = "up" | "down" | "unknown";
export type CortadosRack = { host: string; name: string; state: CortadosServerState; note: string };
export type CortadosConsole = { name: string; state: CortadosServerState; alert: boolean; working: boolean; note: string };
export type CortadosServerRoom = { racks: CortadosRack[]; s1: CortadosConsole | null };
const SERVER_STATES = new Set(["up", "down", "unknown"]);
const HOST_RE = /^[a-z][a-z0-9-]{0,31}$/;
const TEXT_MAX = 80;
const shortString = (v: unknown): string | null =>
  typeof v === "string" && v.length > 0 && v.length <= TEXT_MAX ? v : null;
const isObject = (v: unknown): v is Record<string, unknown> => v !== null && typeof v === "object" && !Array.isArray(v);

/** The server room from a bridge payload, or null when it is not one. */
export function cortadosParseServerRoom(value: unknown): CortadosServerRoom | null {
  if (!isObject(value) || !Array.isArray(value.racks) || value.racks.length > CORTADOS_RACKS_MAX) return null;
  const racks: CortadosRack[] = [];
  for (const r of value.racks) {
    if (!isObject(r) || typeof r.host !== "string" || !HOST_RE.test(r.host) || !SERVER_STATES.has(r.state as string)) {
      return null;
    }
    const name = shortString(r.name), note = shortString(r.note);
    if (name === null || note === null) return null;
    racks.push({ host: r.host, name, state: r.state as CortadosServerState, note });
  }
  let s1: CortadosConsole | null = null;
  if (value.s1 !== null && value.s1 !== undefined) {
    const s = value.s1;
    if (!isObject(s) || !SERVER_STATES.has(s.state as string) || typeof s.alert !== "boolean" ||
        typeof s.working !== "boolean") return null;
    const name = shortString(s.name), note = shortString(s.note);
    if (name === null || note === null) return null;
    s1 = { name, state: s.state as CortadosServerState, alert: s.alert, working: s.working, note };
  }
  return { racks, s1 };
}

let serverRoom: CortadosServerRoom | null = null;
const serverRoomListeners = new Set<() => void>();

/** GatewayBrowserClient's hook: keeps the server room from our bridge's hello or office.snapshot frame. */
export function cortadosNoteGatewayFrame(frame: unknown): void {
  if (!isObject(frame)) return;
  let raw: unknown;
  if (frame.type === "event" && frame.event === "office.snapshot" && isObject(frame.payload)) {
    raw = frame.payload.servers;
  } else if (frame.type === "res" && isObject(frame.payload) && frame.payload.type === "hello-ok" &&
      frame.payload.adapterType === "cortados-office" && isObject(frame.payload.snapshot)) {
    raw = frame.payload.snapshot.cortadosServers;
  } else {
    return;
  }
  if (raw === undefined) return;
  const next = raw === null ? null : cortadosParseServerRoom(raw);
  if (next === null && raw !== null) return; // malformed: keep the last good room
  serverRoom = next;
  for (const listener of serverRoomListeners) listener();
}
export const subscribeCortadosServerRoom = (listener: () => void): (() => void) => {
  serverRoomListeners.add(listener);
  return () => {
    serverRoomListeners.delete(listener);
  };
};
export const getCortadosServerRoom = (): CortadosServerRoom | null => serverRoom;

// Where the server room's racks and console stand (canvas px): racks in one row along the room's open side, spread
// over its width and facing into the room; the console between them and the back wall. Reason: the back wall of a
// bottom-row room is the one nearest the camera, where the room's own sign stands; racks against it hid their signs
// behind it (preview 2026-11-27). `front` is the canvas y direction the racks face (+1 south, -1 north).
const RACK_W = 45; // core/geometry.ts ITEM_FOOTPRINT server_rack
const RACK_D = 90;
const RACK_EDGE_GAP = 30;
export type CortadosServerSpot = { x: number; y: number; w: number; d: number };
export function cortadosServerRoomPlan(racks: number):
  { racks: CortadosServerSpot[]; console: CortadosServerSpot; front: 1 | -1 } {
  const { x: ox, y: oy, w, h, top } = cortadosRoomRect(CORTADOS_SERVER_ROOM);
  const n = Math.max(0, Math.min(CORTADOS_RACKS_MAX, Math.floor(racks)));
  const inner = w - 2 * WALL - 20;
  const pitch = n > 0 ? Math.min(100, inner / n) : 100;
  const start = ox + WALL + 10 + (inner - pitch * n) / 2;
  const rackY = top ? oy + h - RACK_EDGE_GAP - RACK_D : oy + RACK_EDGE_GAP;
  return {
    racks: Array.from({ length: n }, (_, i) => ({ x: start + i * pitch + (pitch - RACK_W) / 2, y: rackY, w: RACK_W, d: RACK_D })),
    console: { x: ox + w / 2 - 30, y: top ? rackY - 40 - 34 : rackY + RACK_D + 40, w: 60, d: 34 },
    front: top ? -1 : 1,
  };
}

/** On only while our bridge is the connected gateway: the hello's adapterType ("cortados-office") is normalised to
 * "openclaw" by Studio (settings.ts normalizeGatewayAdapterType), so the gate is the roster itself: every local agent
 * carries the bridge's id grammar. Remote-office agents ("remote:") are not ours to place and are ignored. */
export function cortadosRoomsActive(agents: Array<{ id: string }>, gatewayStatus: string | undefined): boolean {
  if (gatewayStatus !== "connected") return false;
  const local = agents.filter((agent) => !agent.id.startsWith("remote:"));
  return local.length > 0 && local.every((agent) => isCortadosAgentId(agent.id));
}

/** The layout switch, sticky: while a gateway is connected and lists local agents it is cortadosRoomsActive; with no
 * gateway or nobody on it (a reconnect, a quiet fleet) it keeps its last value, so the floor does not flip between
 * the rooms and the stock office on every reconnect. */
export function cortadosLayoutOn(
  previous: boolean,
  agents: Array<{ id: string }>,
  gatewayStatus: string | undefined,
): boolean {
  if (gatewayStatus !== "connected" || !agents.some((agent) => !agent.id.startsWith("remote:"))) return previous;
  return cortadosRoomsActive(agents, gatewayStatus);
}

// ---- desk allocation: sticky, so a room and a seat never move while their people stay ----
export type CortadosRoomState = { roomByGroup: Map<string, number>; deskByAgent: Map<string, string> };
export const createCortadosRoomState = (): CortadosRoomState => ({ roomByGroup: new Map(), deskByAgent: new Map() });

const UID_RE = new RegExp(`^${CORTADOS_UID_PREFIX}r(\\d+)_(head|report|desk)(\\d+)$`);

/**
 * Desks for the agents nobody placed by hand. `deskItems` are the layout's desk_cubicle items in Claw3D's index order
 * (assignedDeskIndexByAgentId's indices); `manual` is Studio's deskAssignmentByDeskUid (it always wins: those desks and
 * agents are skipped). Groups take the lowest free orchestrator room in the order they first appear (agents.list
 * order: the adapter emits rooms in the provider tree's order, active first); a person keeps a desk while it stays
 * theirs and the right kind (head / report / team), and otherwise takes the lowest free one of that kind in its room.
 * No room or no free desk: no entry, so Claw3D treats the person as stock treats anyone unassigned (FLOORS-DESIGN.md
 * "Overflow"). `holds`: every seated person is held at the desk (deskHoldByAgentId), so an idle or waiting person
 * sits in the room instead of wandering off; a finished, failed task is held too and walks to its report desk.
 */
export function allocateCortadosDesks(
  agents: Array<{ id: string; subtitle?: string | null }>,
  deskItems: Array<{ _uid: string }>,
  manual: Record<string, string>,
  state: CortadosRoomState,
): { desks: Record<string, number>; holds: Record<string, boolean> } {
  const indexByUid = new Map<string, number>();
  const free = new Map<string, string[]>(); // "room:slot" -> desk uids, ascending
  const manualAgents = new Set<string>();
  deskItems.forEach((item, index) => {
    const owner = manual[item._uid];
    if (owner) {
      manualAgents.add(owner);
      return;
    }
    const m = UID_RE.exec(item._uid);
    if (!m) return;
    indexByUid.set(item._uid, index);
    const key = `${m[1]}:${m[2]}`;
    (free.get(key) ?? free.set(key, []).get(key)!).push(item._uid);
  });
  for (const list of free.values()) list.sort((a, b) => Number(UID_RE.exec(a)![3]) - Number(UID_RE.exec(b)![3]));
  const rooms = new Set<number>();
  for (const key of free.keys()) rooms.add(Number(key.split(":")[0]));

  const places = new Map<string, { group: string; slot: Slot }>();
  const groups: string[] = [];
  for (const agent of agents) {
    if (manualAgents.has(agent.id)) continue;
    const place = cortadosPlaceOf(agent);
    if (!place) continue;
    places.set(agent.id, place);
    if (!groups.includes(place.group)) groups.push(place.group);
  }
  // forget the groups and people that left, then keep every room still held (never the server room)
  for (const group of [...state.roomByGroup.keys()]) {
    const room = state.roomByGroup.get(group)!;
    if (!groups.includes(group) || !rooms.has(room) || room >= CORTADOS_ORCHESTRATOR_ROOMS) {
      state.roomByGroup.delete(group);
    }
  }
  for (const id of [...state.deskByAgent.keys()]) if (!places.has(id)) state.deskByAgent.delete(id);
  const taken = new Set(state.roomByGroup.values());
  for (const group of groups) {
    if (state.roomByGroup.has(group)) continue;
    const room = [...rooms].sort((a, b) => a - b).find((r) => r < CORTADOS_ORCHESTRATOR_ROOMS && !taken.has(r));
    if (room === undefined) continue; // more orchestrators than rooms: this group's people stay unseated
    state.roomByGroup.set(group, room);
    taken.add(room);
  }
  // keep seats that are still right, then seat the rest in agents.list order
  const used = new Set<string>();
  const want = (id: string) => {
    const place = places.get(id)!;
    const room = state.roomByGroup.get(place.group);
    return room === undefined ? null : `${room}:${place.slot}`;
  };
  for (const [id, uid] of [...state.deskByAgent]) {
    const m = UID_RE.exec(uid);
    if (!m || !indexByUid.has(uid) || want(id) !== `${m[1]}:${m[2]}` || used.has(uid)) {
      state.deskByAgent.delete(id);
    } else {
      used.add(uid);
    }
  }
  for (const id of places.keys()) {
    if (state.deskByAgent.has(id)) continue;
    const key = want(id);
    const uid = key === null ? undefined : (free.get(key) ?? []).find((u) => !used.has(u));
    if (uid === undefined) continue;
    state.deskByAgent.set(id, uid);
    used.add(uid);
  }
  const desks: Record<string, number> = {};
  const holds: Record<string, boolean> = {};
  for (const [id, uid] of state.deskByAgent) {
    desks[id] = indexByUid.get(uid)!;
    holds[id] = true;
  }
  return { desks, holds };
}

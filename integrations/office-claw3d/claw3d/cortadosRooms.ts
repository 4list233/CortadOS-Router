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
// open towards the middle corridor (no wall there: a wall's navigation padding would close an 80 px corridor) and
// walled on the other three sides. Seats are 100 px apart vertically, as in the stock office, so paths stay open.
export const CORTADOS_ROOMS = 8;
export const CORTADOS_TEAM_DESKS = 6;
// Beside the head desk: where a finished child task reports back (E8). Two fit on the head row; more finished at once
// stand where Claw3D leaves an unseated person (see FLOORS-DESIGN.md "Overflow").
export const CORTADOS_REPORT_DESKS = 2;
const COLS = 4;
const ROOM_W = 430;
const ROOM_H = 340;
const COL_PITCH = 450;
const ROW_Y = [10, 370];
const WALL = 8; // core/constants.ts WALL_THICKNESS
const DESK_X = [25, 165, 305]; // three 100 px desks per row inside a 430 px room
const ROW_DESK_Y = [50, 150, 250]; // head row, then two team rows
export const CORTADOS_UID_PREFIX = "cortados_";

type Slot = "head" | "report" | "desk";

const deskUid = (room: number, slot: Slot, index: number) => `${CORTADOS_UID_PREFIX}r${room}_${slot}${index}`;

const deskWithChair = (uid: string, x: number, y: number): FurnitureItem[] => [
  { _uid: uid, type: "desk_cubicle", x, y },
  { _uid: `${uid}_chair`, type: "chair", x: x + 20, y: y - 10, facing: 180 },
  { _uid: `${uid}_computer`, type: "computer", x: x + 20, y: y - 13 },
  { _uid: `${uid}_keyboard`, type: "keyboard", x: x + 30, y: y - 5 },
];

/** The CortadOS floor: 8 rooms, each with 1 head desk, 2 report desks and 6 team desks, in a fixed _uid scheme. */
export function buildCortadosRoomsLayout(): FurnitureItem[] {
  const items: FurnitureItem[] = [];
  for (let room = 0; room < CORTADOS_ROOMS; room += 1) {
    const ox = 10 + (room % COLS) * COL_PITCH;
    const top = room < COLS;
    const oy = ROW_Y[top ? 0 : 1];
    const wall = (name: string, x: number, y: number, w: number, h: number): FurnitureItem =>
      ({ _uid: `${CORTADOS_UID_PREFIX}r${room}_wall_${name}`, type: "wall", x, y, w, h });
    // the back wall away from the corridor, and both side walls
    items.push(wall("back", ox, top ? oy : oy + ROOM_H - WALL, ROOM_W, WALL));
    items.push(wall("left", ox, oy, WALL, ROOM_H));
    items.push(wall("right", ox + ROOM_W - WALL, oy, WALL, ROOM_H));
    items.push(...deskWithChair(deskUid(room, "report", 0), ox + DESK_X[0], oy + ROW_DESK_Y[0]));
    items.push(...deskWithChair(deskUid(room, "head", 0), ox + DESK_X[1], oy + ROW_DESK_Y[0]));
    items.push(...deskWithChair(deskUid(room, "report", 1), ox + DESK_X[2], oy + ROW_DESK_Y[0]));
    for (let d = 0; d < CORTADOS_TEAM_DESKS; d += 1) {
      items.push(...deskWithChair(deskUid(room, "desk", d), ox + DESK_X[d % 3], oy + ROW_DESK_Y[1 + Math.floor(d / 3)]));
    }
  }
  return items;
}

/** Whether a layout is the CortadOS floor (it holds the fixed _uid scheme). */
export const isCortadosLayout = (items: FurnitureItem[] | null | undefined): boolean =>
  Array.isArray(items) && items.some((item) => typeof item?._uid === "string" && item._uid.startsWith(CORTADOS_UID_PREFIX));

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
// (16-64 lowercase hex), a named origin (letters, at most 15: never a session tag) or "unattributed".
const RUN = "[A-Za-z0-9_-]{8,64}";
const CORTADOS_ID = new RegExp(`^office-([0-9a-f]{16,64}|[a-z]{2,15})-(orchestrator|task-${RUN}|sub-${RUN}|routed-${RUN})$`);
// A finished child task's nameplate subtitle (adapter.mjs RETURN_LABEL): it reports at a desk beside its orchestrator.
export const CORTADOS_RETURN_SUBTITLE = /^(done|failed|cancelled) · /;

export const isCortadosAgentId = (id: string): boolean => CORTADOS_ID.test(id);

export function cortadosPlaceOf(agent: { id: string; subtitle?: string | null }):
  { group: string; slot: Slot } | null {
  const m = CORTADOS_ID.exec(agent.id);
  if (!m) return null;
  const slot: Slot = m[2] === "orchestrator" ? "head"
    : m[2].startsWith("task-") && CORTADOS_RETURN_SUBTITLE.test(agent.subtitle ?? "") ? "report" : "desk";
  return { group: m[1], slot };
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
 * agents are skipped). Groups take the lowest free room in the order they first appear (agents.list order: the
 * adapter emits rooms in the provider tree's order, active first); a person keeps a desk while it stays theirs and
 * the right kind (head / report / team), and otherwise takes the lowest free one of that kind in its room. No room or
 * no free desk: no entry, so Claw3D treats the person as stock treats anyone unassigned (FLOORS-DESIGN.md "Overflow").
 * `holds`: every seated person is held at the desk (deskHoldByAgentId), so an idle or waiting person sits in the
 * room instead of wandering off; a finished, failed task is held too and walks to its report desk.
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
  // forget the groups and people that left, then keep every room still held
  for (const group of [...state.roomByGroup.keys()]) {
    if (!groups.includes(group) || !rooms.has(state.roomByGroup.get(group)!)) state.roomByGroup.delete(group);
  }
  for (const id of [...state.deskByAgent.keys()]) if (!places.has(id)) state.deskByAgent.delete(id);
  const taken = new Set(state.roomByGroup.values());
  for (const group of groups) {
    if (state.roomByGroup.has(group)) continue;
    const room = [...rooms].sort((a, b) => a - b).find((r) => r < CORTADOS_ROOMS && !taken.has(r));
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

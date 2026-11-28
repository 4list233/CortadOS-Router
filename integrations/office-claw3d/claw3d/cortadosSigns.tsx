// CortadOS rooms: door signs, floor tints and the server room (office round 2, CortadOS-Router claw3d/cortados-rooms.patch).
// Each room in use gets a tint of its colour on its floor and a sign above its back wall with its name (the
// orchestrator's Claude Code session name, else the room's label), and the server room always has its sign. The server
// room holds a rack per host (its sign: name, up / stale / down / unknown, how long ago) and the S1 console (its sign: the
// served model and its latency line), drawn from the bridge's server-room data (cortadosRooms.ts
// cortadosNoteGatewayFrame); nobody sits there. Display only: no handler, nothing clickable, nothing sent anywhere.
import { Billboard, Text } from "@react-three/drei";
import { memo, useSyncExternalStore } from "react";
import { SCALE, WALL_THICKNESS } from "@/features/retro-office/core/constants";
import { toWorld } from "@/features/retro-office/core/geometry";
import {
  CORTADOS_SERVER_STATE_COLOR as STATE_COLOR,
  cortadosServerRoomPlan,
  getCortadosServerRoom,
  subscribeCortadosServerRoom,
  type CortadosRoomSign,
  type CortadosServerRoom,
} from "@/features/retro-office/core/cortadosRooms";

// Heights in world units (the nameplates float at 1.05 above a figure): the sign reads above the back wall, over the
// people; the tint sits just above the floor so it never flickers with it.
const SIGN_Y = 1.9;
const TINT_Y = 0.004;
const TINT_OPACITY = 0.16;
// The server room: racks about a person's height, their signs above them and below the room's own sign.
const RACK_H = 1.15;
const RACK_SIGN_Y = 1.5;
const CONSOLE_H = 0.42;
const CONSOLE_SIGN_Y = 1.35;

function Plate({ width, title, note, color, y }: { width: number; title: string; note: string; color: string; y: number }) {
  return (
    <Billboard position={[0, y, 0]}>
      <mesh position={[0, 0, -0.002]}>
        <planeGeometry args={[width, 0.46]} />
        <meshBasicMaterial color="#080c14" transparent opacity={0.9} />
      </mesh>
      <mesh position={[-width / 2 + 0.018, 0, -0.001]}>
        <planeGeometry args={[0.036, 0.46]} />
        <meshBasicMaterial color={color} />
      </mesh>
      <Text position={[0, 0.08, 0.001]} fontSize={0.18} color="#f3ecd2" anchorX="center" anchorY="middle"
        maxWidth={width - 0.12} font={undefined}>
        {title}
      </Text>
      <Text position={[0, -0.12, 0.001]} fontSize={0.1} color={color} anchorX="center" anchorY="middle"
        maxWidth={width - 0.12} font={undefined}>
        {note}
      </Text>
    </Billboard>
  );
}

const ServerRoom = memo(function ServerRoom({ room }: { room: CortadosServerRoom }) {
  const plan = cortadosServerRoomPlan(room.racks.length);
  const consoleColor = room.s1 === null ? STATE_COLOR.unknown
    : room.s1.alert ? STATE_COLOR.down : room.s1.working ? STATE_COLOR.up : "#f59e0b";
  return (
    <group>
      {room.racks.map((rack, i) => {
        const spot = plan.racks[i];
        if (!spot) return null;
        const [x, , z] = toWorld(spot.x + spot.w / 2, spot.y + spot.d / 2);
        const color = STATE_COLOR[rack.state];
        const pitchWorld = (plan.racks[1] ? plan.racks[1].x - plan.racks[0].x : 100) * SCALE;
        return (
          <group key={`cortados-rack-${rack.host}`} position={[x, 0, z]}>
            <mesh position={[0, RACK_H / 2, 0]} castShadow receiveShadow>
              <boxGeometry args={[spot.w * SCALE, RACK_H, spot.d * SCALE]} />
              <meshStandardMaterial color="#111827" roughness={0.6} metalness={0.3} />
            </mesh>
            {/* the front faces into the room (plan.front): a status strip and drive lights */}
            <mesh position={[0, RACK_H * 0.82, plan.front * ((spot.d * SCALE) / 2 + 0.002)]}
              rotation={[0, plan.front > 0 ? 0 : Math.PI, 0]}>
              <planeGeometry args={[spot.w * SCALE * 0.8, 0.06]} />
              <meshStandardMaterial color={color} emissive={color} emissiveIntensity={0.9} />
            </mesh>
            {[0.25, 0.42, 0.59].map((h) => (
              <mesh key={h} position={[0, RACK_H * h, plan.front * ((spot.d * SCALE) / 2 + 0.002)]}
                rotation={[0, plan.front > 0 ? 0 : Math.PI, 0]}>
                <planeGeometry args={[spot.w * SCALE * 0.7, 0.025]} />
                <meshStandardMaterial color="#334155" emissive={rack.state === "up" ? "#1e3a8a" : "#000000"}
                  emissiveIntensity={0.6} />
              </mesh>
            ))}
            <Plate width={Math.max(0.7, Math.min(1.7, pitchWorld * 0.95))} title={rack.name} note={rack.note}
              color={color} y={RACK_SIGN_Y} />
          </group>
        );
      })}
      {room.s1 ? (() => {
        const spot = plan.console;
        const [x, , z] = toWorld(spot.x + spot.w / 2, spot.y + spot.d / 2);
        return (
          <group key="cortados-s1-console" position={[x, 0, z]}>
            <mesh position={[0, CONSOLE_H / 2, 0]} castShadow receiveShadow>
              <boxGeometry args={[spot.w * SCALE, CONSOLE_H, spot.d * SCALE]} />
              <meshStandardMaterial color="#1f2937" roughness={0.7} metalness={0.2} />
            </mesh>
            <mesh position={[0, CONSOLE_H + 0.12, 0]} rotation={[plan.front > 0 ? 0.35 : -0.35, 0, 0]}>
              <boxGeometry args={[spot.w * SCALE * 0.9, 0.24, 0.03]} />
              <meshStandardMaterial color={consoleColor} emissive={consoleColor} emissiveIntensity={0.55} />
            </mesh>
            <Plate width={3.2} title={room.s1.name} note={room.s1.note} color={consoleColor} y={CONSOLE_SIGN_Y} />
          </group>
        );
      })() : null}
    </group>
  );
});

export const CortadosRoomSigns = memo(function CortadosRoomSigns({ signs }: { signs: CortadosRoomSign[] }) {
  // the server room's racks and S1 console, as the bridge last sent them (the GatewayBrowserClient hook keeps them)
  const serverRoom = useSyncExternalStore(subscribeCortadosServerRoom, getCortadosServerRoom, () => null);
  return (
    <group>
      {signs.map((sign) => {
        const [cx, , cz] = toWorld(sign.x + sign.w / 2, sign.y + sign.h / 2);
        const wallY = sign.top ? sign.y + WALL_THICKNESS : sign.y + sign.h - WALL_THICKNESS;
        const [sx, , sz] = toWorld(sign.x + sign.w / 2, wallY);
        const width = sign.w * SCALE * 0.72;
        return (
          <group key={`cortados-sign-${sign.room}`}>
            <mesh rotation={[-Math.PI / 2, 0, 0]} position={[cx, TINT_Y, cz]}>
              <planeGeometry args={[sign.w * SCALE, sign.h * SCALE]} />
              <meshBasicMaterial color={sign.color} transparent opacity={TINT_OPACITY} depthWrite={false} />
            </mesh>
            <Billboard position={[sx, SIGN_Y, sz]}>
              <mesh position={[0, 0, -0.002]}>
                <planeGeometry args={[width, 0.42]} />
                <meshBasicMaterial color="#080c14" transparent opacity={0.88} />
              </mesh>
              <mesh position={[0, -0.19, -0.001]}>
                <planeGeometry args={[width, 0.05]} />
                <meshBasicMaterial color={sign.color} />
              </mesh>
              <Text
                position={[0, 0.02, 0.001]}
                fontSize={0.2}
                color="#f3ecd2"
                anchorX="center"
                anchorY="middle"
                maxWidth={width - 0.16}
                font={undefined}
              >
                {sign.label}
              </Text>
            </Billboard>
          </group>
        );
      })}
      {serverRoom ? <ServerRoom room={serverRoom} /> : null}
    </group>
  );
});

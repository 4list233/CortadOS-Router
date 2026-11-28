// CortadOS rooms: door signs and floor tints (office round 2, CortadOS-Router claw3d/cortados-rooms.patch).
// Each room in use gets a tint of its colour on its floor and a sign above its back wall with its name (the
// orchestrator's Claude Code session name, else the room's label), and the server room always has its sign. The data
// comes from cortadosRooms.ts cortadosRoomSigns; drawn only while the CortadOS rooms are on. Display only: no handler,
// nothing clickable, nothing sent anywhere.
import { Billboard, Text } from "@react-three/drei";
import { memo } from "react";
import { SCALE, WALL_THICKNESS } from "@/features/retro-office/core/constants";
import { toWorld } from "@/features/retro-office/core/geometry";
import type { CortadosRoomSign } from "@/features/retro-office/core/cortadosRooms";

// Heights in world units (the nameplates float at 1.05 above a figure): the sign reads above the back wall, over the
// people; the tint sits just above the floor so it never flickers with it.
const SIGN_Y = 1.9;
const TINT_Y = 0.004;
const TINT_OPACITY = 0.16;

export const CortadosRoomSigns = memo(function CortadosRoomSigns({ signs }: { signs: CortadosRoomSign[] }) {
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
    </group>
  );
});

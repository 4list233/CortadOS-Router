# Office art: origin and licence

Every image in this folder is **original to this repository** (CortadOS-Router, issue #77,
2026-11-22). No third-party asset is included.

- `assets/sprites/` (`<family>.png`, `<family>-suit.png`, `sheet.png`, `sheet.json`) and
  `assets/tiles/` (`floor-wood.png`, `floor-carpet.png`, `wall.png`) are drawn pixel by pixel as
  text grids in `../sprites.mjs` and encoded by `../build-assets.mjs`. They are not copied, traced
  or derived from any logo, mascot or other artwork. They only *evoke* a provider through colour and
  a generic motif:
  - `claude`: a round orange bean with a cream highlight (not Anthropic's logo or any Claude mascot);
  - `codex`: a white robot with a `>_` prompt on its screen face (not an OpenAI mark);
  - `grok`: a charcoal robot with a visor slit and a red sensor (not an xAI mark);
  - `ollama`: a cream pixel llama (not Ollama's logo drawing);
  - `local`: a green circuit-chip creature with gold pins;
  - `unknown`: a grey hooded figure with a question mark.
  Product names are used only as labels for which provider a sprite stands for. They imply no
  endorsement, and the marks belong to their owners.
- `assets/tiles/floor-wood.png` uses the colour of Claw3D's own office floor (`#c8a97e`, upstream
  `src/features/retro-office/scene/environment.tsx`) so the sprites match the 3D office. Only the
  colour value is shared. The tile is drawn here, and no Claw3D file is copied.
- `assets/preview.png` is rendered by `../build-assets.mjs` from the invented fixture
  `../fixtures/sample-office.json`. It contains no real session data.

Licence: the art is licensed under the same terms as the rest of this repository: the
GNU General Public License v3.0 (`/LICENSE`). Copyright © 2026 the CortadOS-Router authors.

Regenerate with `node integrations/office-claw3d/build-assets.mjs --write`. Do not hand-edit the
PNGs: `sprites.test.mjs` checks that the committed files match the generated ones byte for byte.

Claw3D itself (https://github.com/iamlukethedev/Claw3D) is MIT-licensed, © 2026 Luke The Dev. This
repository does not vendor it. `../claw3d/avatar-profile-hint.patch` is a 6-line change to one
Claw3D file. It is applied at deploy time to a pinned upstream checkout, and that patched file
stays under Claw3D's MIT licence.

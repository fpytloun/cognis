# Noto Emoji

`NotoColorEmoji.ttf` is the Noto Color Emoji font published by Google. The
vendored copy is the font distributed by Debian's `fonts-noto-color-emoji`
package and validated with Pango/WeasyPrint. It contains recognizable artwork
for flags, symbols, modifiers, keycaps, and ZWJ sequences.

- Upstream: https://github.com/google/fonts/tree/main/ofl/notoemoji
- Font source: https://github.com/googlefonts/noto-emoji
- License: SIL Open Font License 1.1 (`OFL.txt`)
- Vendored file SHA-256:
  `93cdc4ee9aa40e2afceecc63da0ca05ec7aab4bec991ece51a6b52389f48a477`

The Rich Deliverables standalone/PDF renderer embeds this file as an offline
data URL. It is used only for intact emoji grapheme spans; no runtime network
fetch is performed.

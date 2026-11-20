# Tag Designer

Design what your 2.13" tags show (text in any font, pictures, boxes, lines) and send it to one tag or several at once over Bluetooth.

## Start it
**The app:** double-click **Tag Designer.app** in the `ATC_TLSR_Paper` folder (drag it to the Dock to keep it handy). The first start takes a couple of minutes to set up. The designer opens in its own window, and a small Terminal window opens beside it: keep that open while you use the app, because macOS gives Bluetooth permission to Terminal, not to a home-made app like this one. Closing the designer window ends both.

**In the browser instead:** double-click **Tag Designer.command**.

Only one copy runs at a time; starting it again just shows the one that is running.

To try it without any tags: in Terminal, `cd ~/ATC_TLSR_Paper/tag_designer && ../stock_backup/.venv/bin/python server.py --demo` (simulated tags; what they would show is saved as PNGs in `tag_designer/tests/demo_screens/`).

## Using it
- **Add** text, a picture (or drop/paste one onto the tag), a box or a line. Drag to move, drag the blue corner to resize, arrow keys nudge.
- **Text**: select a text item (or double-click it on the tag) and type in the "What it says" box on the right. "Add a field…" inserts live values: tag name, battery % or volts, time, date, day, signal, MAC. They are filled in separately for each tag when you send (so one design can show each tag's own name and battery); time and date are the moment you press Send. Templates "Tag info" and "Clock style" use them. **Your own fields**: choose "New field…" in that menu (or "Add field" under Send to tags), e.g. `room`, then type each tag's value under "Your fields for each tag". Write `Meeting in {room}` once, send to several tags, and each shows its own room. "Preview as" under the tag picture shows the design as any one tag will get it. Values are kept in `tag_designer/tag_fields.json`. Any Mac font, size, bold/italic, alignment, black or white. "Add font file…" takes a .ttf/.otf/.woff (kept in `tag_designer/fonts/`, or just drop font files in that folder).
- **Pictures** are turned into black and white. "Smooth" and "Crisp" shading suit photos, "None" suits logos; adjust cut-off, brightness and contrast while watching the preview.
- The preview is exactly the tag's 250 × 122 black-and-white pixels.
- **Grid and Snap** (under the tag picture): "Grid" draws guide lines every 2 to 25 pixels (pick the spacing), with pink dashed lines through the middle of the screen. The grid is only on your screen and never goes to the tag. With "Snap" ticked, dragging puts the nearest edge of an item on a grid line, and an item close to the middle of the screen jumps to the exact middle (a solid pink line shows it). Resizing boxes, pictures and lines snaps their corner too. Hold ⌘ while dragging to place freely. With Snap on, Shift + arrow keys move one grid step. The Center buttons on the right still centre the selected item in one click.
- **Save** keeps designs in `tag_designer/designs/`; **Open** brings them back. **Templates** has a few starting points.
- **Send to tags**: Scan, tick one or more tags, press Send. Tags are done one after another, about 10 to 20 seconds each.
- **Show clock** puts a tag back on its clock screen.
- **What each tag shows**: after a scan, the app reads each tag's screen and shows it under the tag (tags on firmware version 2). "Read again" refreshes it, "Edit this" loads it as your design so you can change it. Every picture you send is read back and checked. For tags on older firmware it shows the last picture this app sent instead.
- **Tag settings**: if the first picture shows upside down or as a negative, tick the matching box. "Send test pattern" helps check.

## The tag's own fonts

At the top of the Font list, under *The tag's own fonts (pixel-exact)*, are the bitmap fonts built into the firmware: **Dialog 16** (the tag's default, used for its name and battery lines), **DSEG14 40** (the big clock digits), **Special Elite 30** and **Roboto Black 80**. They are drawn exactly pixel for pixel as the tag draws them (checked against the firmware's own drawing code). Sizes go in whole steps (16, 32, 48…). Characters the font doesn't have (like ° or ~) are left out, and the panel lists them. The template *Tag's own clock look* copies the tag's clock screen; use the field {time24} for the tag's 24-hour time. The fonts were taken from the firmware source with `tools/extract_tag_fonts.py`.

## Screen types and colour

- **Screen** (under the design) picks what you are designing for: this project's PickSmart 2.13in LCD, ATC e-paper tags (2.13in black/white or black/white/red, 2.13in 212×104, 1.54in 200×200 black/white/red), and some common colour sizes (2.9in, 3.5in, 4.2in; red or yellow) for designing and Export PNG.
- Each tag in the list has a small screen-type menu (saved in `tag_screens.json`). When you tick tags, the design switches to their screen size and colours. Tags with a different screen size are skipped when sending, with a note. Switching straight back to the previous screen restores the layout exactly.
- On colour screens, text, boxes, lines and the background can be Red (or Yellow). Pictures have *Dark parts* and *Light parts* colours, and photos can use *Full colour*.
- **Gallery → Colour** has 18 two-colour backgrounds and 14 two-colour pictures. After adding one, pick the colour for each part (*Dark parts*, *Colour parts*). On black/white screens the colour parts show as black unless you change them.
- The tag firmware still shows only black and white, so red or yellow is sent as black for now.

## Gallery and angles

- **Gallery…** (left side) has ready-made **Backgrounds** (fill the whole tag, placed behind everything), **Frames** (a border, placed on top, middle stays clear) and **Pictures** (small signs and icons). Click one to add it.
- **My pictures**: press *Add your own…* to keep pictures you use often. They are saved in `gallery/mine/`. You can also drop your own `.svg`/`.png` files straight into `gallery/backgrounds`, `gallery/frames` or `gallery/pictures`.
- **Angle**: text, boxes and pictures can be turned to any angle (slider, number box, or the 15° / 90° buttons; *Straight* puts it back). Pictures can also be flipped left-right or upside down.
- For a dark version of a background, select it and tick *Swap black and white*.

## Editing a tag's design later

Every time you send a design, Tag Designer keeps its layers (text, pictures, boxes,
lines, in their order) for each tag it reached, in `tag_designs/` on this Mac. Each tag in
the list then has an **Edit layers** button: it reopens exactly those layers, so you can
change one word or move one picture and send it again, instead of starting over.
Fields such as `{name}` stay as fields. The test pattern does not replace the kept layers.
(**Edit this** under the screen preview still opens what the tag shows as one flat picture.)

## Password lock

Without a password, anyone nearby with a free Bluetooth app can change a tag:
its picture, its settings, even its firmware. Firmware v3 adds a password lock.

1. Update each tag to firmware v3 once: in Terminal, `cd ~/ATC_TLSR_Paper/atc_port` and
   `./ota.sh --name ESL_xxxxxx` (the tag's name).
2. In Tag Designer, open **Password lock** (right side, under Tag settings), type a
   password of at least 10 characters twice and press **Save password**. It is kept
   in your Mac's Keychain, never in a file.
3. Tick the tags and press **Lock selected**. Each tag then shows "🔒 locked".

After that the app unlocks a tag by itself every time it sends, and `./ota.sh`
does too. Other phones and computers are ignored. The password itself never goes
over the air: the tag sends a random question and the app answers it with a key
made from the password (AES-128 challenge and answer), so a recorded answer is
useless later.

- **Change password**: type the current one and the new one. Tags move to the new
  password the next time the app talks to them (the Mac remembers the last two
  old ones for that).
- **Remove password** takes the lock off the ticked tags.
- **Password lost, or a different Mac**: on a new Mac, type the same password and
  everything works. If the password is truly lost, wire the tag to the USB-UART
  adapter as for `./flash.sh` and run `./unlock_usb.sh` in `atc_port`. It erases only
  the password (flash 0x7A000 to 0x7BFFF).
- After 6 wrong answers in a row a tag allows one try per 30 seconds.

Use a password nobody can guess (a few unrelated words). Someone who records a
successful unlock could try guesses offline, so a short or common password is weak.

## Which tags can take designs
Only tags running this project's firmware (they advertise as `ESL_…`). Tags still on the original PickSmart firmware (`NEMR…`) show as "original firmware"; put the new firmware on them with `atc_port/ota.sh --name NEMR…`.

The firmware needs version 2 (`atc_port/ATC_Paper_picksmart.bin` from 2026-11-17, sha256 4df38ac6…) for a picture to **stay** on screen. The first version redraws its clock every minute, over your picture. The app labels such tags "older firmware"; update them with `cd atc_port && ./ota.sh --name ESL_…`.

The screen is an LCD that keeps its picture only while the tag has power. After a battery change the tag starts on its clock screen; send the design again.

## How it talks to the tag
The ATC_TLSR_Paper picture service (service `13187b10-eba9-a3ba-044e-83d3217d9a38`, characteristic `4b646063-6264-f3a7-8941-e65356ea82fe`): `00 FF` clear, `02 00 00` position, `03 …` data in MTU-sized chunks, `01` show. Version 2 adds `05 00` (back to the clock), answers `02` when the characteristic is read, and adds screen read-back: `06 HI LO` fills a 180-byte window from the screen buffer, read from characteristic `4b646064-6264-f3a7-8941-e65356ea82fe`. The 4000-byte buffer holds pixel (x, y) in bit `0x80 >> (y % 8)` of byte `(249 − x) × 16 + y / 8`, set = white.

Tests (no hardware): `../stock_backup/.venv/bin/python tests/test_designer.py`. Log: `tag_designer/tag_designer.log`.

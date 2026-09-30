# Guide Calculator for DaVinci Resolve

A script that works out pixel-exact guide positions for your timeline and adds them to Resolve's viewer. Pick equal columns and rows, Rule of Thirds, Golden Ratio or Fibonacci, see them on a Resolve-style preview, then copy the values or press **Send guides**.

Every value is measured from the center of the frame with +Y up, the same way Resolve's rulers and its Add Guide dialog read. At 1920 × 1080 with 3 columns and 3 rows you get vertical guides at −320 and +320 and horizontal guides at +180 and −180.

## Install

1. Download [GuideCalculator.py](https://github.com/lhasnen97/guide-calculator/releases/latest/download/GuideCalculator.py).
2. Copy it into Resolve's Scripts folder:
   ```bash
   cp ~/Downloads/GuideCalculator.py ~/Library/Application\ Support/Blackmagic\ Design/DaVinci\ Resolve/Fusion/Scripts/Utility/
   ```
3. In Resolve, open **Workspace › Scripts › GuideCalculator**. If it isn't listed yet, restart Resolve.

The first time, the script downloads the Qt libraries its window needs (PySide6, about 110 MB) and a notification tells you when it's ready. After that the window opens right away with your timeline's resolution filled in.

### Requirements

- macOS 13 or later
- DaVinci Resolve 21.1 or later. Older versions don't ship their own Python, so they need Python 3 with PySide6 installed (`pip3 install PySide6`).
- An internet connection for the first run

Built and tested on DaVinci Resolve Studio 21.1. See [Free vs Studio](#free-vs-studio).

## What it does

- **From Timeline**: reads the current timeline's resolution when you open it from the Scripts menu, or the project's resolution if no timeline is open.
- **Grids**: Equal (columns and rows set separately, 2 to 20 each), Rule of Thirds, Golden Ratio (lines at 1/φ² and 1/φ) and Fibonacci (3/8 and 5/8).
- **Axes**: make both sets of guides, or only vertical, or only horizontal.
- **Preview**: drawn like Resolve's viewer, with rulers, a black frame, 1px guides and safe-area outlines (action safe 93%, title safe 90%, per EBU R 95). Hover a guide to see its value and click it to copy.
- **Copy**: any single value, one axis, or everything as text, JSON, CSV or a numbered list.
- **Presets**: 1080p, UHD, DCI 4K, 720p, 6K, 2.39:1 scope and Instagram 4:5 and 1:1. Save your own sizes and they stay for next time (`~/.guidecalc/presets.json`).

## Send guides to Resolve

Resolve's scripting API has no functions for viewer guides, so **Send guides** uses the menu the way you would:

1. Clears the guides you're replacing (only the axis you picked, when *Replace existing guides* is on).
2. Opens **View › Guides › Add Guide…** and switches it to pixels.
3. Types the position and reads it back. If the value didn't take, it tries again, and cancels rather than add a guide in the wrong place.
4. Sets the orientation and presses Add, then moves on to the next guide.

**Clear** runs Clear All Guides, or Clear All Vertical/Horizontal Guides when only one axis is selected.

It takes a little over a second per guide, so a 4 × 3 grid (five guides) takes about eight seconds. Keep your hands off the mouse and keyboard while it runs. The first time, macOS asks you to allow control under **System Settings › Privacy & Security › Accessibility**.

## Free vs Studio

Blackmagic documents the scripting API as the same in the free version and in Studio, and the script mostly uses the Scripts menu and the View › Guides menu. One part depends on a setting Blackmagic lists under Studio: the **From Timeline** button inside the window connects to Resolve from outside, which needs **Preferences › System › General › External scripting** set to Local. When you open the script from the Scripts menu, the resolution is read inside Resolve instead, so that part doesn't need the setting.

It hasn't been tested on the free version yet.

## How it runs

Resolve 21.1 runs Scripts-menu scripts in its own Python, which has no PySide6, and Resolve's own Qt 5 would clash with PySide6's Qt 6 in the same process. So the script reads the resolution inside Resolve, then opens the window in a separate process:

- If a system Python with PySide6 exists (`/usr/local/bin/python3`, Homebrew or python.org), it uses that.
- Otherwise it uses Resolve's own Python with a private copy of PySide6 6.10.2 in `~/Library/Application Support/GuideCalculator/`. The first run downloads it from PyPI with `curl` and checks each file against PyPI's published SHA-256 hash (319 MB unpacked). A small `Guide Calculator.app` wrapper in the same folder lets the window come to the front.

Setup messages and errors go to `~/Library/Logs/GuideCalculator.log`.

## Uninstall

```bash
rm ~/Library/Application\ Support/Blackmagic\ Design/DaVinci\ Resolve/Fusion/Scripts/Utility/GuideCalculator.py
rm -rf ~/Library/Application\ Support/GuideCalculator ~/.guidecalc
```

Not affiliated with Blackmagic Design.

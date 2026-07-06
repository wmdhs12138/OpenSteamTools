# OpenSteamTools

OpenSteamTools is an open source SteamTools alternative. This version is still
in beta, but the basics work.

## Supported platforms

- Windows
- Linux native Steam
- Linux Flatpak Steam
- macOS path detection is included, but not heavily tested

On Linux, the app looks for Steam config directories in this order:

1. `$STEAM_CONFIG_DIR`
2. `~/.steam/steam/config`
3. `~/.local/share/Steam/config`
4. `~/.var/app/com.valvesoftware.Steam/.local/share/Steam/config`

The Lua files are installed to `stplug-in` under the detected Steam config
directory. Manifest files are installed to `depotcache`.

## Install from source

Install Python 3.10+ and Firefox, then install the Python dependencies:

```bash
python -m pip install -r requirements.txt
```

On Arch Linux, if PySide6 is unavailable for your Python version from `pip`,
install the distro package instead:

```bash
sudo pacman -S pyside6
```

If you use a virtual environment with the distro package, create it with system
site packages enabled:

```bash
python -m venv --system-site-packages .venv
source .venv/bin/activate
```

Run the app:

```bash
python OpenSteamtools.py
```

The GUI is built with Qt6/PySide6 and can run on Wayland without requiring
Tkinter or XWayland.

The mod downloader uses Selenium with Firefox. If `geckodriver` is installed in
your `PATH`, it will be used automatically. Selenium Manager may also be able to
download a matching driver automatically. You can override paths explicitly:

```bash
STEAM_CONFIG_DIR="$HOME/.steam/steam/config" \
GECKODRIVER="/usr/bin/geckodriver" \
FIREFOX_BINARY="/usr/bin/firefox" \
python OpenSteamtools.py
```

## Build

```bash
pyinstaller OpenSteamtools.spec
```

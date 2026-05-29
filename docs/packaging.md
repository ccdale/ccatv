# ccatv packaging notes

This document covers the current M5 packaging baseline for Debian-family and Arch Linux systems.

## Contents

- Arch Linux template package via [archlinux/PKGBUILD](/home/chris/src/ccatv/archlinux/PKGBUILD)
- Debian manual package assembly using `dpkg-deb`

## Arch Linux

The repository now ships an initial PKGBUILD template at [archlinux/PKGBUILD](/home/chris/src/ccatv/archlinux/PKGBUILD).

Typical workflow:

```bash
cd archlinux
makepkg -si
```

Notes:

- the package installs the Python application, `ccatv-service`, `ccatv-web`, and systemd user units (`ccatv.service`, `ccatv-api.service`, `ccatv-web.service`)
- review `depends` and `makedepends` against the target machine before publishing

## Debian-family systems

The repo does not yet ship full debhelper packaging metadata. The supported M5 path is a small manual `dpkg-deb` build.

Install runtime dependencies from `pyproject.toml` using apt:

```bash
sudo apt update
sudo apt install -y python3 python3-flask python3-platformdirs systemd
```

Dependency mapping:

- `flask` -> `python3-flask`
- `platformdirs` -> `python3-platformdirs`

Suggested flow:

```bash
cd /home/chris/src/ccatv
uv build
PKGVER=$(uv version --short)
rm -rf .pkgroot
mkdir -p .pkgroot/usr/lib/systemd/user
cp systemd/ccatv.service .pkgroot/usr/lib/systemd/user/
cp systemd/ccatv-api.service .pkgroot/usr/lib/systemd/user/
cp systemd/ccatv-web.service .pkgroot/usr/lib/systemd/user/
python -m installer --destdir=.pkgroot --prefix=/usr/local dist/*.whl
```

Add a control file:

```bash
mkdir -p .pkgroot/DEBIAN
cat > .pkgroot/DEBIAN/control <<'EOF'
Package: ccatv
Version: __PKGVER__
Section: video
Priority: optional
Architecture: all
Maintainer: ccatv maintainers
Depends: python3, systemd
Description: ccatv scheduler daemon and CLI tools
EOF

sed -i "s/__PKGVER__/${PKGVER}/" .pkgroot/DEBIAN/control
```

Set Debian runtime package dependencies from `pyproject.toml`:

```bash
sed -i 's/^Depends:.*/Depends: python3, python3-flask, python3-platformdirs, systemd/' .pkgroot/DEBIAN/control
```

Build the package:

```bash
dpkg-deb --build .pkgroot "ccatv_${PKGVER}_all.deb"
```

Install it:

```bash
sudo dpkg -i "ccatv_${PKGVER}_all.deb"
systemctl --user daemon-reload
systemctl --user enable --now ccatv.service
# HTTP API transport unit (requires ~/.config/ccatv/web.env service token)
systemctl --user enable --now ccatv-api.service
# optional web frontend unit (requires ~/.config/ccatv/web.env web token)
systemctl --user enable --now ccatv-web.service
```

## Operational follow-up after installation

Regardless of package format:

1. run `ccatv setup` to populate `~/.config/ccatv/runtime.json` and `~/.config/dvbstreamer/userconfig.json`
2. confirm the service starts and stays healthy with `systemctl --user status ccatv.service`
3. inspect logs with `journalctl --user-unit ccatv.service`
4. optionally run `loginctl enable-linger $USER` so the service persists across logouts

## Current limitation

These packaging instructions are intentionally minimal and operational. They do not yet cover:

- post-install user creation hooks
- automatic migration of existing per-user configs
- debhelper metadata
- repository publishing/signing
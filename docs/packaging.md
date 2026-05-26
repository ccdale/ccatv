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

- the package installs the Python application, `ccatv-service`, `ccatv-web`, and the systemd user unit
- review `depends` and `makedepends` against the target machine before publishing

## Debian-family systems

The repo does not yet ship full debhelper packaging metadata. The supported M5 path is a small manual `dpkg-deb` build.

Suggested flow:

```bash
cd /home/chris/src/ccatv
uv build
rm -rf .pkgroot
mkdir -p .pkgroot/usr/lib/systemd/user
cp systemd/ccatv.service .pkgroot/usr/lib/systemd/user/
python -m installer --destdir=.pkgroot --prefix=/usr dist/*.whl
```

Add a control file:

```bash
mkdir -p .pkgroot/DEBIAN
cat > .pkgroot/DEBIAN/control <<'EOF'
Package: ccatv
Version: 0.1.173
Section: video
Priority: optional
Architecture: all
Maintainer: ccatv maintainers
Depends: python3, systemd
Description: ccatv scheduler daemon and CLI tools
EOF
```

Build the package:

```bash
dpkg-deb --build .pkgroot ccatv_0.1.173_all.deb
```

Install it:

```bash
sudo dpkg -i ccatv_0.1.173_all.deb
systemctl --user daemon-reload
systemctl --user enable --now ccatv.service
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
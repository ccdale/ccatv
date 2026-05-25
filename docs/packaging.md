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

- the package installs the Python application, the `ccatv-service` entrypoint, and the systemd unit
- the package also creates the `/var/lib/ccatv` and `/var/lib/ccatv/recordings` directories
- review `depends` and `makedepends` against the target machine before publishing

## Debian-family systems

The repo does not yet ship full debhelper packaging metadata. The supported M5 path is a small manual `dpkg-deb` build.

Suggested flow:

```bash
cd /home/chris/src/ccatv
uv build
rm -rf .pkgroot
mkdir -p .pkgroot/usr/lib/systemd/system
mkdir -p .pkgroot/var/lib/ccatv/recordings
cp systemd/ccatv.service .pkgroot/usr/lib/systemd/system/
python -m installer --destdir=.pkgroot --prefix=/usr dist/*.whl
```

Add a control file:

```bash
mkdir -p .pkgroot/DEBIAN
cat > .pkgroot/DEBIAN/control <<'EOF'
Package: ccatv
Version: 0.1.153
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
dpkg-deb --build .pkgroot ccatv_0.1.153_all.deb
```

Install it:

```bash
sudo dpkg -i ccatv_0.1.153_all.deb
sudo systemctl daemon-reload
sudo systemctl enable --now ccatv.service
```

## Operational follow-up after installation

Regardless of package format:

1. create the `ccatv` system user if your package manager hook does not do it automatically
2. run `ccatv setup` as that user with `XDG_CONFIG_HOME=/var/lib/ccatv/.config`
3. confirm the service starts and stays healthy with `systemctl status ccatv.service`
4. inspect logs with `journalctl -u ccatv.service`

## Current limitation

These packaging instructions are intentionally minimal and operational. They do not yet cover:

- post-install user creation hooks
- automatic migration of existing per-user configs
- debhelper metadata
- repository publishing/signing
#!/bin/bash

set -e

cd /home/chris/src/ccatv
# remove earlier builds
rm -rf dist .pkgroot
uv build
PKGVER=$(uv version --short)
mkdir -p .pkgroot/usr/lib/systemd/user
cp systemd/ccatv.service .pkgroot/usr/lib/systemd/user/
python -m installer --destdir=.pkgroot --prefix=/usr dist/*.whl

# Add a control file
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

# Set Debian runtime package dependencies from pyproject.toml
sed -i 's/^Depends:.*/Depends: python3, python3-flask, python3-platformdirs, systemd/' .pkgroot/DEBIAN/control

# build the deb
dpkg-deb --build .pkgroot "ccatv_${PKGVER}_all.deb"

# install it
sudo dpkg -i "ccatv_${PKGVER}_all.deb"
systemctl --user daemon-reload
# systemctl --user enable --now ccatv.service

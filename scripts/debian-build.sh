#!/bin/bash

set -e

cd /home/chris/src/ccatv
# remove earlier builds
rm -rf dist .pkgroot
uv build
PKGVER=$(uv version --short)
mkdir -p .pkgroot/usr/lib/systemd/user
cp systemd/ccatv.service .pkgroot/usr/lib/systemd/user/
cp systemd/ccatv-api.service .pkgroot/usr/lib/systemd/user/
cp systemd/ccatv-web.service .pkgroot/usr/lib/systemd/user/
python3 -m installer --destdir=.pkgroot --prefix=/usr/local dist/*.whl

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

# Regenerate API/web tokens and rewrite shared web env defaults on every update.
CCATV_CONFIG_DIR="${HOME}/.config/ccatv"
CCATV_WEB_ENV="${CCATV_CONFIG_DIR}/web.env"
mkdir -p "${CCATV_CONFIG_DIR}"

CCATV_SERVICE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
CCATV_WEB_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')

cat > "${CCATV_WEB_ENV}" <<EOF
CCATV_SERVICE_AUTH_TOKEN=${CCATV_SERVICE_TOKEN}
CCATV_WEB_AUTH_TOKEN=${CCATV_WEB_TOKEN}
CCATV_API_BIND_HOST=127.0.0.1
CCATV_API_PORT=8787
CCATV_WEB_LISTEN_HOST=0.0.0.0
CCATV_WEB_LISTEN_PORT=5000
CCATV_WEB_SERVICE_HOST=127.0.0.1
CCATV_WEB_SERVICE_PORT=8787
EOF
chmod 600 "${CCATV_WEB_ENV}"

systemctl --user daemon-reload
systemctl --user enable --now ccatv.service ccatv-api.service ccatv-web.service

echo "Wrote ${CCATV_WEB_ENV} with regenerated service/web tokens."

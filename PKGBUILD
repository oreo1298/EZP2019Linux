# Maintainer: EZP2019Linux contributors
#
# Build and install from a clone of this repository:
#   git clone https://github.com/oreo1298/EZP2019Linux.git
#   cd EZP2019Linux
#   makepkg -si

pkgname=ezp2019linux
pkgver=1.0.1
pkgrel=1
pkgdesc="Modern GUI and CLI for the EZP2019/EZP2019+ USB SPI flash and EEPROM programmer"
arch=('any')
url="https://github.com/oreo1298/EZP2019Linux"
license=('MIT')
depends=('python' 'pyside6' 'qt6-svg' 'qt6-wayland' 'python-pyusb' 'libusb')
makedepends=('python-build' 'python-installer' 'python-setuptools' 'python-wheel')
checkdepends=('python-pytest')
optdepends=('polkit: install the udev rule from the GUI')
install=ezp2019linux.install
source=()
sha256sums=()

# The PKGBUILD lives in the repository root (packaging/arch/PKGBUILD links to it);
# find the checkout from wherever makepkg was started.
_srcroot() {
  local dir="$startdir"
  while [[ ! -f "$dir/pyproject.toml" && "$dir" != / ]]; do
    dir="$(dirname "$dir")"
  done
  if [[ ! -f "$dir/pyproject.toml" ]]; then
    echo "Run makepkg inside the EZP2019Linux source folder." >&2
    return 1
  fi
  printf '%s\n' "$dir"
}

prepare() {
  local root
  root="$(_srcroot)" || return 1
  rm -rf "$srcdir/$pkgname"
  mkdir -p "$srcdir/$pkgname"
  # Copy the checkout, leaving out git data and makepkg's own work folders.
  tar -C "$root" \
      --exclude=./.git --exclude=./src --exclude=./pkg \
      --exclude=./packaging/arch/src --exclude=./packaging/arch/pkg \
      --exclude='*.pkg.tar*' --exclude='__pycache__' --exclude='*.egg-info' \
      --exclude=./build --exclude=./dist \
      -cf - . | tar -C "$srcdir/$pkgname" -xf -
}

build() {
  cd "$srcdir/$pkgname"
  python -m build --wheel --no-isolation
}

check() {
  cd "$srcdir/$pkgname"
  python -m pytest -q tests/test_core.py tests/test_fileio.py tests/test_transport.py
}

package() {
  cd "$srcdir/$pkgname"
  python -m installer --destdir="$pkgdir" dist/*.whl
  install -Dm644 packaging/udev/70-ezp2019linux.rules \
    "$pkgdir/usr/lib/udev/rules.d/70-ezp2019linux.rules"
  install -Dm644 packaging/desktop/ezp2019linux.desktop \
    "$pkgdir/usr/share/applications/ezp2019linux.desktop"
  install -Dm644 ezp2019linux/data/ezp2019linux.svg \
    "$pkgdir/usr/share/icons/hicolor/scalable/apps/ezp2019linux.svg"
  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 README.md docs/PROTOCOL.md -t "$pkgdir/usr/share/doc/$pkgname/"
}

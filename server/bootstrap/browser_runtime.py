"""The shared libraries and fonts the headless browser needs, with no package manager (W3).

Chrome for Testing's headless shell links 21 libraries a Databricks Apps
container does not have (NSS, GLib, ATK, D-Bus, X11/xcb, GBM, ALSA, expat,
xkbcommon), and the Apps runtime (Ubuntu 22.04, glibc 2.35) has no package
manager to add them. These are the Ubuntu 20.04 packages that provide them and
their own dependencies, pinned by sha256 from Launchpad's permanent archive and
listed in the artifact manifest, so the toolchain mirror stages them like every
other tool.

Ubuntu 20.04 because its glibc (2.31) is older than any runtime we support and
its packages are xz-compressed, which the standard library can unpack: Debian
12's libexpat needs glibc 2.36, and Ubuntu 22.04 packages are zstd.

They are unpacked next to the browser and reach only the browser's own process,
through ``LD_LIBRARY_PATH`` and ``FONTCONFIG_FILE``. Attendee shells and agents
never see either variable: their environment is an allowlist.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import tarfile
import tempfile

LAUNCHPAD_FILES = "https://launchpad.net/ubuntu/+archive/primary/+files/"

#: (package, version, filename, sha256) — Ubuntu 20.04 amd64, 5.6 MB in all.
RUNTIME_DEBS = (
    ("fonts-liberation", "1:1.07.4-11", "fonts-liberation_1.07.4-11_all.deb", "d359cba9c3ac8a40fb57773881e20827845e9fab1b2d6fd25cde4b7bade7d57f"),
    ("libasound2", "1.2.2-2.1ubuntu2.5", "libasound2_1.2.2-2.1ubuntu2.5_amd64.deb", "5ea02b4590b210bb2d4a180007184c10e8941d0ee1f025361950b0d0084578a6"),
    ("libatk-bridge2.0-0", "2.34.2-0ubuntu2~20.04.1", "libatk-bridge2.0-0_2.34.2-0ubuntu2~20.04.1_amd64.deb", "2fcfc11cdb476274e1c11f0321c2dcabe97186dab45eb9ca91ee9dc1f6906603"),
    ("libatk1.0-0", "2.35.1-1ubuntu2", "libatk1.0-0_2.35.1-1ubuntu2_amd64.deb", "3bfb6d72e35771481be152dcc96d39b899d6ff246c995658c181114357f46656"),
    ("libatspi2.0-0", "2.36.0-2", "libatspi2.0-0_2.36.0-2_amd64.deb", "482f7e0c7081e9fd16b7cd71d3fc937271b1f722fdcfb0c40f934e108e9fdb80"),
    ("libbsd0", "0.10.0-1", "libbsd0_0.10.0-1_amd64.deb", "4f668025fe923a372eb7fc368d6769fcfff6809233d48fd20fc072917cd82e60"),
    ("libdbus-1-3", "1.12.16-2ubuntu2.3", "libdbus-1-3_1.12.16-2ubuntu2.3_amd64.deb", "624e7f78a22f483477bf806c3c4278ce3d5c6707d1176a646baf1dfc453a0982"),
    ("libdrm2", "2.4.107-8ubuntu1~20.04.2", "libdrm2_2.4.107-8ubuntu1~20.04.2_amd64.deb", "9b01d73313841abe8e3f24c2715edced675fbe329bbd10be912a5b135cd51fb6"),
    ("libexpat1", "2.2.9-1ubuntu0.8", "libexpat1_2.2.9-1ubuntu0.8_amd64.deb", "021d56318dba1f753dc8284623161fad3051e4ec2f69ad84c16d649b1ac7d0c0"),
    ("libffi7", "3.3-4", "libffi7_3.3-4_amd64.deb", "4584aa8fef1bf5086168ce2f7078cd2ebd78fdc4cc0d86d958d795d4e0b0f50d"),
    ("libgbm1", "21.2.6-0ubuntu0.1~20.04.2", "libgbm1_21.2.6-0ubuntu0.1~20.04.2_amd64.deb", "58b610bf61b00e2fb599d44c5247850ea422ff712085639224b0a1cbcee95e50"),
    ("libglib2.0-0", "2.64.6-1~ubuntu20.04.9", "libglib2.0-0_2.64.6-1~ubuntu20.04.9_amd64.deb", "459d8f08c5e3e8c0454b1cb16660fc7a11274729c83bd41886e5a1388feeddbd"),
    ("libnspr4", "2:4.35-0ubuntu0.20.04.1", "libnspr4_4.35-0ubuntu0.20.04.1_amd64.deb", "da98b529dfe37aedbf7d23d2edf5faae4d817c40e5a60932e791ba4cdec4dd88"),
    ("libnss3", "2:3.98-0ubuntu0.20.04.2", "libnss3_3.98-0ubuntu0.20.04.2_amd64.deb", "1e711911ad3ae2de8a19bc6cb048fa81e2e2c2c09d0aad186b7c14d78517bb16"),
    ("libpcre3", "2:8.39-12ubuntu0.1", "libpcre3_8.39-12ubuntu0.1_amd64.deb", "98c25f98cc8df949e774a7ebaff509c37a6baea4dab83786a3c1efecb850bfae"),
    ("libwayland-server0", "1.18.0-1ubuntu0.1", "libwayland-server0_1.18.0-1ubuntu0.1_amd64.deb", "c1470c26861afaaafda54c9b9802b55a7bcc873841c239d4a844f6f5160e2363"),
    ("libx11-6", "2:1.6.9-2ubuntu1.6", "libx11-6_1.6.9-2ubuntu1.6_amd64.deb", "20d7c0a8ea7a138d49d777a5db8e652071a3c47c78136450ec7417b5232b84ac"),
    ("libxau6", "1:1.0.9-0ubuntu1", "libxau6_1.0.9-0ubuntu1_amd64.deb", "58a0d78302a35e4584f96cd598af16b563ae7aae4af589e2a7cee6dc6666d979"),
    ("libxcb-randr0", "1.14-2", "libxcb-randr0_1.14-2_amd64.deb", "508da595aaca39deb9ab09e3aad5619ad7b7703032413d39a226a72a28f36765"),
    ("libxcb1", "1.14-2", "libxcb1_1.14-2_amd64.deb", "3fcab5cc6a70bcb1e4157748f9c626be21bc18b4c8459447e4c213cba98b9831"),
    ("libxcomposite1", "1:0.4.5-1", "libxcomposite1_0.4.5-1_amd64.deb", "d87e5d52d4bfa714bd56bb3103999e045b4f50638917736e544b67db361d0c8e"),
    ("libxdamage1", "1:1.1.5-2", "libxdamage1_1.1.5-2_amd64.deb", "77f4088c85b973a05a9e12825db9fc8e0cc5a577bed57ac29f4427e187c2896f"),
    ("libxdmcp6", "1:1.1.3-0ubuntu1", "libxdmcp6_1.1.3-0ubuntu1_amd64.deb", "8a612b0fb60a41b92698f87258bc5ec6467da88e38d3de79411e02921c42af87"),
    ("libxext6", "2:1.3.4-0ubuntu1", "libxext6_1.3.4-0ubuntu1_amd64.deb", "a3c546490c0ae0f9247cf8f2919fc7b99b386a538ac91ae48a4ebb96a2a69834"),
    ("libxfixes3", "1:5.0.3-2", "libxfixes3_5.0.3-2_amd64.deb", "ee3d380e5f825048a381fea92e8215bc65662fa4ce06346579fbaada2b1f7acc"),
    ("libxi6", "2:1.7.10-0ubuntu1", "libxi6_1.7.10-0ubuntu1_amd64.deb", "462b5067ba80ab8dad197c83d1da7b4b1b51b0b392138e20f03029e8d33f9565"),
    ("libxkbcommon0", "0.10.0-1", "libxkbcommon0_0.10.0-1_amd64.deb", "b704ce1751dd938025b88a84c4054d4f36651661e473efeb3da8ed39aaf05868"),
    ("libxrandr2", "2:1.5.2-0ubuntu1", "libxrandr2_1.5.2-0ubuntu1_amd64.deb", "e6b232c3b2e6f8858aee0f1e46d2cec1575de1b85cb9cfdff93b8b951b15ff97"),
    ("libxrender1", "1:0.9.10-1", "libxrender1_0.9.10-1_amd64.deb", "bfda6dc44a39d6500ac2974b3113105477adce457699aadd99f022d5a4858c1c"),
)

_LIBRARY_DIR = re.compile(r"(?:usr/)?lib/x86_64-linux-gnu/(?:[A-Za-z0-9._+-]+/)*")
_LIBRARY = re.compile(r"lib[A-Za-z0-9._+-]*\.so(?:\.[0-9.]+)?")
_FONT = re.compile(r"usr/share/fonts/truetype/liberation/(Liberation[A-Za-z0-9-]*\.ttf)")
_SAFE_NAME = re.compile(r"[A-Za-z0-9._+-]+")

FONTS_CONF = """<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>{fonts}</dir>
  <dir>/usr/share/fonts</dir>
  <cachedir>{cache}</cachedir>
  <alias><family>sans-serif</family><prefer><family>Liberation Sans</family></prefer></alias>
  <alias><family>serif</family><prefer><family>Liberation Serif</family></prefer></alias>
  <alias><family>monospace</family><prefer><family>Liberation Mono</family></prefer></alias>
</fontconfig>
"""


class ExtractError(RuntimeError):
    """A runtime package could not be unpacked safely."""


def artifact_name(package: str) -> str:
    """The manifest name for one package, e.g. ``browser_runtime_libnss3``."""
    return "browser_runtime_" + re.sub(r"[^a-z0-9]+", "_", package.lower()).strip("_")


def artifact_names() -> list[str]:
    return [artifact_name(package) for package, *_ in RUNTIME_DEBS]


def source_url(filename: str) -> str:
    return LAUNCHPAD_FILES + filename


def runtime_digest() -> str:
    """Content address of the whole set: a changed pin unpacks into a new directory."""
    return hashlib.sha256("\n".join(sha for *_, sha in RUNTIME_DEBS).encode()).hexdigest()


def runtime_env(runtime: str) -> dict[str, str]:
    """The two variables only the browser process gets."""
    return {
        "LD_LIBRARY_PATH": os.path.join(runtime, "lib"),
        "FONTCONFIG_FILE": os.path.join(runtime, "fonts.conf"),
    }


def _data_tar(deb: bytes) -> tuple[str, bytes]:
    """The ``data.tar.*`` member of a Debian package (an ``ar`` archive)."""
    if not deb.startswith(b"!<arch>\n"):
        raise ExtractError("not a Debian package")
    offset = 8
    while offset + 60 <= len(deb):
        header = deb[offset:offset + 60]
        if header[58:60] != b"`\n":
            raise ExtractError("corrupt Debian package header")
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as error:
            raise ExtractError("corrupt Debian package member size") from error
        start = offset + 60
        if size < 0 or start + size > len(deb):
            raise ExtractError("truncated Debian package")
        if name.startswith("data.tar"):
            return name, deb[start:start + size]
        offset = start + size + (size & 1)
    raise ExtractError("Debian package has no data archive")


def _members(deb_path: str):
    """``(path, member, archive)`` for each file and link in a package's data archive."""
    with open(deb_path, "rb") as handle:
        name, data = _data_tar(handle.read())
    if name not in {"data.tar", "data.tar.xz", "data.tar.gz", "data.tar.bz2"}:
        raise ExtractError(f"unsupported package compression {name}")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            for member in archive.getmembers():
                path = member.name[2:] if member.name.startswith("./") else member.name
                if path.startswith("/") or ".." in path.split("/"):
                    raise ExtractError(f"package path escapes its root: {member.name[:80]}")
                if member.isfile() or member.issym():
                    yield path, member, archive
    except (tarfile.TarError, EOFError, OSError) as error:
        raise ExtractError(f"cannot read the data archive: {error}") from error


def _write(path: str, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(content)


def unpack(debs: list[str], runtime: str) -> None:
    """Unpack the libraries and fonts from ``debs`` into ``runtime``, atomically.

    Only shared objects under the multiarch library directories and the
    Liberation fonts are taken, flattened by basename into ``lib/`` and
    ``fonts/``. Links are recreated only between files of that flat set, so
    nothing in a package can write or point outside the runtime directory.
    """
    files: dict[str, bytes] = {}
    links: dict[str, str] = {}
    for deb in debs:
        for path, member, archive in _members(deb):
            font = _FONT.fullmatch(path)
            directory, _, base = path.rpartition("/")
            if font and member.isfile():
                key = f"fonts/{font.group(1)}"
            elif _LIBRARY.fullmatch(base) and _LIBRARY_DIR.fullmatch(directory + "/"):
                key = f"lib/{base}"
            else:
                continue
            if member.issym():
                target = os.path.basename(member.linkname)
                if _SAFE_NAME.fullmatch(target) and target not in {".", ".."}:
                    links.setdefault(key, target)
                continue
            content = archive.extractfile(member).read()
            if files.get(key, content) != content:
                raise ExtractError(f"two packages ship different {base}")
            files[key] = content
    if not any(key.startswith("lib/") for key in files):
        raise ExtractError("the runtime packages contained no libraries")

    parent = os.path.dirname(runtime)
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".browser-runtime-", dir=parent)
    try:
        for sub in ("lib", "fonts", "fontcache"):
            os.mkdir(os.path.join(staging, sub))
        for key, content in sorted(files.items()):
            _write(os.path.join(staging, key), content)
        for key, target in sorted(links.items()):
            # A link whose basename is also a real file (libfreebl3.so ->
            # nss/libfreebl3.so) is already satisfied by that file.
            if key in files or (f"lib/{target}" not in files and f"lib/{target}" not in links):
                continue
            os.symlink(target, os.path.join(staging, key))
        with open(os.path.join(staging, "fonts.conf"), "w") as handle:
            handle.write(FONTS_CONF.format(fonts=os.path.join(runtime, "fonts"),
                                           cache=os.path.join(runtime, "fontcache")))
        if os.path.isdir(runtime):
            shutil.rmtree(runtime, ignore_errors=True)
        os.replace(staging, runtime)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


__all__ = ["ExtractError", "RUNTIME_DEBS", "artifact_name", "artifact_names", "runtime_digest",
           "runtime_env", "source_url", "unpack"]

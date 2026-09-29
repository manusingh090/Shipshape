# vendor/: everything the build would otherwise download

The brief asks for `docker compose up` to bring up the portal with the network off. A normal Python image build downloads two things: a base image from Docker Hub and packages from PyPI. Both are kept here instead, so the build needs nothing from outside the repository. None of these files is Shipshape code; they're unmodified third-party releases, listed below with their checksums.

`docker-compose.yml` builds with `network: none`, so if a file here goes missing the build stops with an error instead of quietly fetching it.

## What's here

| File | What it is |
| --- | --- |
| `python-3.12-slim-amd64.tar.bz2` | The root filesystem of the official `python:3.12-slim` image for amd64 (Intel and AMD machines): Python 3.12.14 on Debian 13 (trixie). |
| `python-3.12-slim-arm64.tar.bz2` | The same image for arm64 (Apple Silicon Macs, ARM servers). |
| `wheels/` | The seven Python packages the portal runs on, as wheels: Django, gunicorn, whitenoise, Pillow and tzdata from `requirements.txt`, and asgiref and sqlparse, which Django needs. Pillow has one wheel per processor type; the rest work everywhere. |

The Dockerfile starts `FROM scratch` and unpacks the archive for the machine's processor with `ADD`, the way the official Debian images are built. It's bzip2 rather than gzip or xz because Docker unpacks bzip2 itself: xz needs an `xz` program on the machine running Docker, which isn't always there. Then `pip install --no-index --find-links=wheels` installs the packages without looking anywhere else. The running image doesn't contain this folder.

Both archives were made from one pinned image, the multi-platform index `python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f` (published 19 September 2026). Each is a `docker export` of that image, unchanged except that the empty `.dockerenv` marker file was left out (Docker creates it in every running container anyway).

## Checksums (SHA-256)

```
d1ffb3e750776334f18425b0c067adb06bbc3d2dc24c8297dc747518d7c38b57  python-3.12-slim-amd64.tar.bz2
a48f26fd1ccb56e8dd7df4c8a3d3873fbab7d18102708979f2dc1fd2e7bf0766  python-3.12-slim-arm64.tar.bz2
fe386d1c2bff7259ea95929266d12a8cf9a8b5a1c2598402967d8792e7a7c094  wheels/asgiref-3.12.1-py3-none-any.whl
f04fb3b36ee119e1af4fa1d397d5fd6cf12700f49321e84d4f4c642c5b1973db  wheels/django-5.2.17-py3-none-any.whl
bd249d0b3f7972f7432f0a6b6ff3b3ee2d129f70cd1ff6c09a9dd9e29a2b88e3  wheels/gunicorn-26.2.0-py3-none-any.whl
d9c7f76c0673154f044e9d78c8655fb4213f6ca31a836df48b40fe5d187717b9  wheels/pillow-12.3.0-cp312-cp312-manylinux_2_27_aarch64.manylinux_2_28_aarch64.whl
78cb2c6865a35ab8ff8b75fd122f6033b92a62c82801110e48ddd6c936a45d91  wheels/pillow-12.3.0-cp312-cp312-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl
b861c0288ce2fa56209a9a6412d2e066ac664b3873b89c26c9d8415e8e32996f  wheels/sqlparse-0.6.0-py3-none-any.whl
c2169a8b0a7a5e9674da5a135ccdfb2b3e671b333ed9fed17b41f73c34476e81  wheels/tzdata-2026.4-py2.py3-none-any.whl
fc5e8c572e33ebf24795b47b6a7da8da3c00cff2349f5b04c02f28d0cc5a3cc2  wheels/whitenoise-6.12.0-py3-none-any.whl
```

To check them: `cd src/vendor && sha256sum -c` with the block above saved to a file, or compare the wheels with the hashes PyPI publishes for each release.

## Licenses

Each package keeps its own license, inside its wheel: Django, asgiref and sqlparse are BSD, gunicorn and whitenoise are MIT, tzdata is Apache 2.0, and Pillow uses the MIT-CMU license. Python is under the PSF License, and the Debian packages in the base system carry their licenses in `/usr/share/doc/<package>/copyright` inside the archive. The MIT license at the top of this repository covers Shipshape's own code only.

## Refreshing them

Only needed to move to newer versions, and it needs the internet. From the repository root:

```bash
# The base system, for both processor types. Pin the new index digest first.
IMAGE=python@sha256:<new index digest>
docker pull --platform linux/amd64 $IMAGE && docker pull --platform linux/arm64 $IMAGE
for arch in amd64 arm64; do
  c=$(docker create --platform linux/$arch $IMAGE)
  docker export $c | bzip2 -9 > src/vendor/python-3.12-slim-$arch.tar.bz2
  docker rm $c
done

# The wheels, after changing the versions in src/requirements.txt.
rm src/vendor/wheels/*.whl
for arch in x86_64 aarch64; do
  pip download -r src/requirements.txt -d src/vendor/wheels --only-binary=:all: \
    --implementation cp --python-version 3.12 --abi cp312 \
    --platform manylinux_2_28_$arch --platform manylinux_2_27_$arch --platform manylinux2014_$arch
done
```

If the Python version changes, update `PYTHON_VERSION` in the Dockerfile, the archive names and this file. Then rebuild with `docker compose build --no-cache` and update the checksums above.

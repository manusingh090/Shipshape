"""Ed25519 signatures (RFC 8032), in plain Python.

The standard library has no public-key signatures and the stack takes no new
dependencies, so this follows the RFC's own definition, section 5.1. It is
slow by the standards of C (a few milliseconds a signature), which is plenty
for signing certificates. tests/test_records.py checks it against the RFC's
published test vectors.

Only what records need: sign, verify, and deriving a public key.
"""

import hashlib

p = 2 ** 255 - 19
q = 2 ** 252 + 27742317777372353535851937790883648493  # the group order, L in the RFC


def _inv(x):
    return pow(x, p - 2, p)


d = -121665 * _inv(121666) % p
SQRT_M1 = pow(2, (p - 1) // 4, p)


def _sha512(data):
    return hashlib.sha512(data).digest()


def _sha512_modq(data):
    return int.from_bytes(_sha512(data), "little") % q


# Points are (X, Y, Z, T) in extended homogeneous coordinates, x = X/Z, y = Y/Z, xy = T/Z.

def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    C = 2 * P[3] * Q[3] * d % p
    D = 2 * P[2] * Q[2] % p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % p, G * H % p, F * G % p, E * H % p)


def _mul(s, P):
    Q = (0, 1, 1, 0)  # the neutral element
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q):
    return (P[0] * Q[2] - Q[0] * P[2]) % p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % p == 0


def _recover_x(y, sign):
    if y >= p:
        return None
    x2 = (y * y - 1) * _inv(d * y * y + 1)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p != 0:
        x = x * SQRT_M1 % p
    if (x * x - x2) % p != 0:
        return None
    if (x & 1) != sign:
        x = p - x
    return x


_gy = 4 * _inv(5) % p
_gx = _recover_x(_gy, 0)
BASE = (_gx, _gy, 1, _gx * _gy % p)


def _compress(P):
    zinv = _inv(P[2])
    x, y = P[0] * zinv % p, P[1] * zinv % p
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decompress(data):
    if len(data) != 32:
        return None
    y = int.from_bytes(data, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % p)


def _expand(secret):
    if len(secret) != 32:
        raise ValueError("An Ed25519 secret key is 32 bytes.")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret):
    a, _ = _expand(secret)
    return _compress(_mul(a, BASE))


def sign(secret, message):
    a, prefix = _expand(secret)
    A = _compress(_mul(a, BASE))
    r = _sha512_modq(prefix + message)
    R = _compress(_mul(r, BASE))
    h = _sha512_modq(R + A + message)
    s = (r + h * a) % q
    return R + int.to_bytes(s, 32, "little")


def verify(public, message, signature):
    if len(public) != 32 or len(signature) != 64:
        return False
    A = _decompress(public)
    R = _decompress(signature[:32])
    if A is None or R is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= q:
        return False
    h = _sha512_modq(signature[:32] + public + message)
    return _equal(_mul(s, BASE), _add(R, _mul(h, A)))

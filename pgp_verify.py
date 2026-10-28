"""Vérification de signatures OpenPGP en Python pur (RSA), sans gpg.

Woob signe ses modules bancaires avec GPG, mais ne vérifie la signature que si
`gpg` est installé sur l'ordinateur : sans lui (cas de la plupart des utilisateurs)
le module est installé sans aucun contrôle, et avec le `gpg` de Git pour Windows
la vérification échoue. L'agent vérifie donc lui-même : même résultat partout.

Périmètre volontairement étroit : signature détachée v4, clés RSA, SHA-1/2. Tout
le reste (DSA, EdDSA, signature v3…) est refusé : on échoue toujours « fermé ».
Module pur, testé (tests/test_pgp_verify.py).
"""
import hashlib
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PKT_SIGNATURE = 2
PKT_PUBLIC_KEY = 6
PKT_PUBLIC_SUBKEY = 14
ALGO_RSA = 1
SIG_BINARY, SIG_TEXT = 0x00, 0x01
SUBPKT_ISSUER_KEYID = 16
SUBPKT_ISSUER_FPR = 33

HASHES = {
    2: hashes.SHA1, 8: hashes.SHA256, 9: hashes.SHA384, 10: hashes.SHA512, 11: hashes.SHA224,
}


class PgpError(Exception):
    pass


def _packets(data):
    """Découpe un flux OpenPGP en (tag, corps) : formats ancien et nouveau."""
    i, n = 0, len(data)
    while i < n:
        ctb = data[i]
        if not ctb & 0x80:
            raise PgpError('flux OpenPGP invalide')
        i += 1
        if ctb & 0x40:  # nouveau format
            tag = ctb & 0x3F
            if i >= n:
                raise PgpError('paquet tronqué')
            first = data[i]
            i += 1
            if first < 192:
                length = first
            elif first < 224:
                length = ((first - 192) << 8) + data[i] + 192
                i += 1
            elif first == 255:
                length = struct.unpack('>I', data[i:i + 4])[0]
                i += 4
            else:
                raise PgpError('paquet à longueur partielle non géré')
        else:  # ancien format
            tag = (ctb & 0x3C) >> 2
            kind = ctb & 0x03
            if kind == 0:
                length, i = data[i], i + 1
            elif kind == 1:
                length, i = struct.unpack('>H', data[i:i + 2])[0], i + 2
            elif kind == 2:
                length, i = struct.unpack('>I', data[i:i + 4])[0], i + 4
            else:
                raise PgpError('paquet de longueur indéfinie non géré')
        if i + length > n:
            raise PgpError('paquet tronqué')
        yield tag, data[i:i + length]
        i += length


def _mpi(buf, i):
    bits = struct.unpack('>H', buf[i:i + 2])[0]
    size = (bits + 7) // 8
    if i + 2 + size > len(buf):
        raise PgpError('entier tronqué')
    return int.from_bytes(buf[i + 2:i + 2 + size], 'big'), i + 2 + size


def parse_keyring(data):
    """{identifiant long (8 octets) : (empreinte, clé publique RSA)} pour chaque clé RSA."""
    keys = {}
    for tag, body in _packets(data):
        if tag not in (PKT_PUBLIC_KEY, PKT_PUBLIC_SUBKEY):
            continue
        if len(body) < 6 or body[0] != 4 or body[5] != ALGO_RSA:
            continue  # seules les clés v4 RSA servent à signer les modules
        n, i = _mpi(body, 6)
        e, _ = _mpi(body, i)
        fingerprint = hashlib.sha1(b'\x99' + struct.pack('>H', len(body)) + body).digest()
        keys[fingerprint[-8:]] = (fingerprint, rsa.RSAPublicNumbers(e, n).public_key())
    return keys


def _parse_signature(sigdata):
    sigs = [body for tag, body in _packets(sigdata) if tag == PKT_SIGNATURE]
    if len(sigs) != 1:
        raise PgpError('une seule signature attendue')
    body = sigs[0]
    if len(body) < 10 or body[0] != 4:
        raise PgpError('version de signature non gérée')
    sigtype, pubalgo, hashalgo = body[1], body[2], body[3]
    if sigtype not in (SIG_BINARY, SIG_TEXT):
        raise PgpError('type de signature non géré')
    if pubalgo != ALGO_RSA:
        raise PgpError('algorithme de signature non géré')
    if hashalgo not in HASHES:
        raise PgpError('algorithme de hachage non géré')
    hashed_len = struct.unpack('>H', body[4:6])[0]
    hashed = body[6:6 + hashed_len]
    j = 6 + hashed_len
    unhashed_len = struct.unpack('>H', body[j:j + 2])[0]
    unhashed = body[j + 2:j + 2 + unhashed_len]
    j += 2 + unhashed_len
    left16 = body[j:j + 2]
    value, _ = _mpi(body, j + 2)
    issuers = set()
    for area in (hashed, unhashed):
        k = 0
        while k < len(area):
            size = area[k]
            k += 1
            if size >= 192:  # longueurs étendues : ignorées, aucune n'est utile ici
                break
            sub = area[k:k + size]
            k += size
            if sub and (sub[0] & 0x7F) == SUBPKT_ISSUER_KEYID and len(sub) == 9:
                issuers.add(bytes(sub[1:9]))
            elif sub and (sub[0] & 0x7F) == SUBPKT_ISSUER_FPR and len(sub) == 22 and sub[1] == 4:
                issuers.add(bytes(sub[-8:]))
    trailer = bytes(body[:6 + hashed_len]) + b'\x04\xff' + struct.pack('>I', 6 + hashed_len)
    return sigtype, hashalgo, trailer, value, issuers, left16


def verify_detached(keyring_data, data, sigdata):
    """True si `sigdata` est une signature valide de `data` par une clé du trousseau."""
    try:
        keys = parse_keyring(keyring_data)
        sigtype, hashalgo, trailer, value, issuers, left16 = _parse_signature(sigdata)
        if sigtype == SIG_TEXT:
            data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
        candidates = [keys[k][1] for k in issuers if k in keys]
        if not candidates:
            return False
        digest = hashlib.new(HASHES[hashalgo].name.replace('-', ''), data + trailer).digest()
        if digest[:2] != left16:
            return False
        for key in candidates:
            size = (key.key_size + 7) // 8
            try:
                signature = value.to_bytes(size, 'big')
            except OverflowError:
                continue
            try:
                key.verify(signature, data + trailer, padding.PKCS1v15(), HASHES[hashalgo]())
                return True
            except InvalidSignature:
                continue
        return False
    except (PgpError, struct.error, IndexError, ValueError):
        return False

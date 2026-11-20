"""
Password lock for tags running this project's firmware (v3 and later).

A locked tag refuses pictures, settings, clock changes and Bluetooth firmware
updates until the connection proves it knows the tag's key:
    write 10          the tag makes a random 16-byte challenge; read it back
                      from the screen characteristic as "10 state challenge salt"
                      (challenge all zeros = no try allowed right now)
    write 11 answer   answer = AES-128(key, challenge); one try per challenge,
                      3 wrong answers per connection, and after 6 wrong answers
                      in a row only one try per 30 s
    write 12 k salt   store a key: k itself when the tag has none, otherwise
                      k XOR AES-128(old key, challenge ^ 5A..5A) once unlocked
    write 13          remove the key (once unlocked)
Reading the picture characteristic returns "03 state": 0 no password,
1 locked, 2 unlocked in this connection. The unlock ends with the connection.

Keys: one app password -> master = PBKDF2-SHA256(password, 600000 rounds);
each tag's key = HMAC-SHA256(master, "tag:" + salt)[:16], with a fresh random
salt every time a tag is locked (the tag stores it and hands it out; it is not
secret). So a device pretending to be an unlocked tag only ever gets a key that
is useless on the real tags. Only the master (never the password) is kept, in
the macOS Keychain. A tag whose key is lost can be reset over the USB-UART
adapter with ./unlock_usb.sh (erases flash sectors 0x7A000 and 0x7B000).
"""
import hashlib
import hmac
import json
import os
import subprocess
import sys

EPD_CHAR = "4b646063-6264-f3a7-8941-e65356ea82fe"
SCREEN_CHAR = "4b646064-6264-f3a7-8941-e65356ea82fe"
LOCK_SECTORS = (0x7A000, 0x7B000)
NONE, LOCKED, OPEN = 0, 1, 2
PBKDF2_ROUNDS = 600000
MIN_PASSWORD = 10
KEEP_PREVIOUS = 2  # current + 2 older passwords = the 3 tries a tag allows per connection
KEYCHAIN_SERVICE = "Tag Designer tag lock"
KEYCHAIN_ACCOUNT = "tag-keys"


class LockError(Exception):
    pass


class WrongPassword(LockError):
    pass


# ---------------------------------------------------------------- AES-128 ----
def _sbox():
    s, p, q = [0] * 256, 1, 1
    while True:  # standard construction from the multiplicative inverse
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ (((q << 1) | (q >> 7)) & 0xFF) ^ (((q << 2) | (q >> 6)) & 0xFF) \
            ^ (((q << 3) | (q >> 5)) & 0xFF) ^ (((q << 4) | (q >> 4)) & 0xFF)
        s[p] = x ^ 0x63
        if p == 1:
            break
    s[0] = 0x63
    return s


SBOX = _sbox()


def _xt(x):
    return ((x << 1) ^ (0x1B if x & 0x80 else 0)) & 0xFF


def aes128(key, block):
    """AES-128 encryption of one 16-byte block (FIPS-197)."""
    if len(key) != 16 or len(block) != 16:
        raise ValueError("AES-128 needs a 16-byte key and block")
    k = list(key)
    s = [a ^ b for a, b in zip(block, k)]
    rcon = 1
    for rnd in range(1, 11):
        k[0] ^= SBOX[k[13]] ^ rcon
        k[1] ^= SBOX[k[14]]
        k[2] ^= SBOX[k[15]]
        k[3] ^= SBOX[k[12]]
        for i in range(4, 16):
            k[i] ^= k[i - 4]
        rcon = _xt(rcon)
        t = [SBOX[s[(i + 4 * (i & 3)) & 15]] for i in range(16)]
        if rnd < 10:
            for c in range(0, 16, 4):
                a0, a1, a2, a3 = t[c:c + 4]
                al = a0 ^ a1 ^ a2 ^ a3
                t[c:c + 4] = [a0 ^ al ^ _xt(a0 ^ a1), a1 ^ al ^ _xt(a1 ^ a2),
                              a2 ^ al ^ _xt(a2 ^ a3), a3 ^ al ^ _xt(a3 ^ a0)]
        s = [a ^ b for a, b in zip(t, k)]
    return bytes(s)


# ------------------------------------------------------------------ keys ----
def master_from_password(password):
    if len(password) < MIN_PASSWORD:
        raise LockError("Use a password of at least %d characters." % MIN_PASSWORD)
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), b"PickSmart tag lock v1", PBKDF2_ROUNDS)


def tag_key(master, salt):
    """The 16-byte key of one tag, from the salt it was locked with."""
    return hmac.new(master, b"tag:" + bytes(salt), hashlib.sha256).digest()[:16]


def master_id(master):
    """4 bytes naming a master; they start every salt, so the app knows which of its
    passwords a tag was locked with and never spends the tag's tries guessing."""
    return hmac.new(master, b"id", hashlib.sha256).digest()[:4]


def new_salt(master):
    return master_id(master) + os.urandom(12)


def _store_file():
    return os.environ.get("TAG_LOCK_FILE")


def load_masters():
    """{"current": bytes or None, "previous": [bytes]} from the Keychain."""
    raw = None
    path = _store_file()
    if path:
        try:
            with open(path) as f:
                raw = f.read()
        except OSError:
            raw = None
    elif sys.platform == "darwin":
        r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                            "-a", KEYCHAIN_ACCOUNT, "-w"], capture_output=True, text=True)
        if r.returncode == 44:  # errSecItemNotFound: no password saved yet
            raw = None
        elif r.returncode != 0:  # locked or refused Keychain: never treat that as "no password"
            raise LockError("Could not read the tag password from the Keychain (%s). Unlock the Mac's "
                            "Keychain and try again." % (r.stderr.strip() or "error %d" % r.returncode))
        else:
            raw = r.stdout.strip()
    if not raw:
        return {"current": None, "previous": []}
    try:
        d = json.loads(raw)
        cur = bytes.fromhex(d["current"]) if d.get("current") else None
        prev = [bytes.fromhex(h) for h in d.get("previous", [])][:KEEP_PREVIOUS]
    except (ValueError, TypeError, KeyError, AttributeError):
        raise LockError("The saved tag password is damaged; not touching it.")
    return {"current": cur, "previous": prev}


def save_masters(current, previous=()):
    raw = json.dumps({"current": current.hex() if current else None,
                      "previous": [p.hex() for p in previous][:KEEP_PREVIOUS]})
    path = _store_file()
    if path:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(raw)
        os.chmod(path, 0o600)
        return
    if sys.platform != "darwin":
        raise LockError("Saving the password needs the macOS Keychain (or TAG_LOCK_FILE).")
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE,
                        "-a", KEYCHAIN_ACCOUNT, "-w", raw], capture_output=True, text=True)
    if r.returncode != 0:
        raise LockError("The Keychain refused to save the password: " + (r.stderr.strip() or "unknown error"))


def set_app_password(password):
    """Make password the app's password. The old one is kept as 'previous' so
    tags still locked with it are moved to the new one when next used."""
    m = load_masters()
    new = master_from_password(password)
    prev = [p for p in ([m["current"]] if m["current"] else []) + m["previous"] if p != new]
    save_masters(new, prev)
    return new


def check_app_password(password):
    m = load_masters()
    return m["current"] is not None and hmac.compare_digest(master_from_password(password), m["current"])


# --------------------------------------------------------- talking to tags ----
async def read_status(client):
    """(firmware version, lock state). Version < 3 means no lock support."""
    char = client.services.get_characteristic(EPD_CHAR)
    if char is None:
        return 0, NONE
    try:
        v = bytes(await client.read_gatt_char(char))
    except Exception:
        return 0, NONE
    if not v:
        return 0, NONE
    return v[0], (v[1] if v[0] >= 3 and len(v) > 1 else NONE)


async def _challenge(client):
    """(state, challenge, salt) from a fresh 10 command."""
    char = client.services.get_characteristic(EPD_CHAR)
    screen = client.services.get_characteristic(SCREEN_CHAR)
    await client.write_gatt_char(char, b"\x10", response=True)
    w = bytes(await client.read_gatt_char(screen))
    if len(w) < 34 or w[0] != 0x10:
        raise LockError("The tag did not answer the password check.")
    chal, salt = w[2:18], w[18:34]
    if w[1] != NONE and not any(chal):
        raise LockError("The tag is refusing password tries for now (too many wrong ones). "
                        "Wait a minute and try again.")
    return w[1], chal, salt


async def _answer(client, master_or_key, is_key=False):
    """One unlock try. Returns (key, challenge) on success; None if the tag has no password."""
    state, chal, salt = await _challenge(client)
    if state == NONE:
        return None
    key = master_or_key if is_key else tag_key(master_or_key, salt)
    char = client.services.get_characteristic(EPD_CHAR)
    await client.write_gatt_char(char, b"\x11" + aes128(key, chal), response=True)
    _, st = await read_status(client)
    if st != OPEN:
        raise WrongPassword("wrong key")
    return key, chal


async def unlock(client, tag_name="", masters=None):
    """Unlock a locked tag with the app's password. Returns ("none"|"open", key, session
    challenge). A tag still on an older app password is moved to the current one."""
    masters = masters or load_masters()
    ver, state = await read_status(client)
    if ver < 3 or state == NONE:
        return "none", None, None
    if state == OPEN:
        return "open", None, None
    keys = ([masters["current"]] if masters["current"] else []) + masters["previous"]
    if not keys:
        raise LockError("This tag has a password, and this Mac doesn't know it yet. "
                        "Enter it in Tag Designer under Password lock.")
    keys = keys[:1 + KEEP_PREVIOUS]
    _, _, salt = await _challenge(client)  # just to see the salt; an unanswered challenge costs nothing
    named = [m for m in keys if master_id(m) == salt[:4]]
    if named:
        keys = named[:1]  # the password this tag was locked with: one try, no guessing
    for i, m in enumerate(keys):  # otherwise at most the 3 tries a tag allows per connection
        try:
            got = await _answer(client, m)
        except WrongPassword:
            continue
        if got is None:
            return "none", None, None
        key, chal = got
        if m != masters["current"] and masters["current"]:  # unlocked with an old password: move it to the current one
            key = await _change_key(client, key, chal, masters["current"])
        return "open", key, chal
    raise WrongPassword("%s's password is not one this Mac knows. Enter the password it was locked "
                        "with (Tag Designer, Password lock), or reset it with ./unlock_usb.sh."
                        % (tag_name or "This tag"))


async def _change_key(client, old_key, session, master):
    """Give the tag a new key from master and a fresh salt. Returns the new key."""
    salt = new_salt(master)
    new_key = tag_key(master, salt)
    char = client.services.get_characteristic(EPD_CHAR)
    if old_key is None:  # tag had no password: the key goes as it is
        payload = new_key
    else:
        mask = aes128(old_key, bytes(b ^ 0x5A for b in session))
        payload = bytes(a ^ b for a, b in zip(new_key, mask))
    await client.write_gatt_char(char, b"\x12" + payload + salt, response=True)
    # check it: a fresh challenge answered with the new key (a wrong answer re-locks the tag,
    # and a tag whose flash write failed still has its old key and salt)
    try:
        got = await _answer(client, new_key, is_key=True)
    except WrongPassword:
        got = None
    if got is None:
        raise LockError("The tag did not take the new password (it keeps its old one).")
    return new_key


async def lock(client, tag_name="", masters=None):
    """Put the app's current password on the tag (unlocking it first if needed)."""
    masters = masters or load_masters()
    if not masters["current"]:
        raise LockError("Set the app's password first.")
    ver, state = await read_status(client)
    if ver < 3:
        raise LockError("This tag's firmware has no password lock. Update it with ./ota.sh first.")
    if state == NONE:
        await _change_key(client, None, None, masters["current"])  # checks the new key itself
    else:  # already locked: unlocking proves (or moves it to) the current password
        how, key, chal = await unlock(client, tag_name, masters)
        if how != "open" or key is None:
            raise LockError("Could not unlock the tag to change its password; try again.")
    return True


async def remove(client, tag_name="", masters=None):
    """Take the password off a tag (it must be unlockable)."""
    how, _, _ = await unlock(client, tag_name, masters)
    if how == "none":
        return False
    char = client.services.get_characteristic(EPD_CHAR)
    await client.write_gatt_char(char, b"\x13", response=True)
    _, st = await read_status(client)
    if st != NONE:
        raise LockError("The tag kept its password.")
    return True

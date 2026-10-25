"""Set (or change) the LED live page login. Stores only a salted PBKDF2 hash in auth.json.

    python3 set_password.py you@example.com          # asks for the password
    python3 set_password.py --off                     # remove the login
The running service picks up the change on the next request (no restart needed).
"""
import getpass, hashlib, json, os, secrets, sys

HERE = os.path.dirname(os.path.abspath(__file__))
AUTH_FILE = os.path.join(HERE, "auth.json")

if len(sys.argv) < 2:
    sys.exit(__doc__)
if sys.argv[1] == "--off":
    if os.path.exists(AUTH_FILE):
        os.remove(AUTH_FILE)
    print("login removed")
    sys.exit()

user = sys.argv[1]
if sys.stdin.isatty():
    pw = getpass.getpass("Password: ")
else:
    # read raw bytes: 'utf-8-sig' drops the invisible byte-order mark Windows PowerShell adds to piped text
    pw = sys.stdin.buffer.readline().decode("utf-8-sig").rstrip("\r\n")
pw = pw.lstrip("﻿")
if len(pw) < 8:
    sys.exit("password must be at least 8 characters")
salt = secrets.token_bytes(16)
iterations = 200_000
digest = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations).hex()
fd = os.open(AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(dict(user=user, salt=salt.hex(), iterations=iterations, hash=digest), f)
print(f"login set for {user} (stored as a salted hash in {AUTH_FILE})")

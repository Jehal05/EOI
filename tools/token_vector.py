#!/usr/bin/env python3
"""Independent reference implementation of the platform token format. Prints the shared
contract-test vector that the Rust, Go, Python and Java implementations must all accept."""
import base64, hashlib, hmac, json
SECRET = b"contract-test-secret-0123456789"
payload = json.dumps({"sub": "vector", "role": "operator", "jur": ["Northern"], "exp": 4102444800}, separators=(",", ":")).encode()
p = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
sig = hmac.new(SECRET, p.encode(), hashlib.sha256).hexdigest()
print(f"{p}.{sig}")

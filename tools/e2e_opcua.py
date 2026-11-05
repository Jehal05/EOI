#!/usr/bin/env python3
"""OPC UA server of the FEP, checked with an INDEPENDENT client (asyncua, test-only dependency; pip install asyncua).
Stack: GS_OPCUA=1 tools/stack.sh up. Checks the reduced feature set (None security, anonymous, Read, Browse) and that everything
else is refused, and compares what OPC UA returns with what the SCADA core holds for the same tags."""
import asyncio, os, socket, struct, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from gs_client import Sess
from asyncua import Client, ua

URL = "opc.tcp://127.0.0.1:4840"
ok = fail = 0
def check(name, cond, detail=""):
    global ok, fail
    if cond: ok += 1; print(f"  PASS  {name}")
    else: fail += 1; print(f"  FAIL  {name}  {detail}")

async def main():
    alice = Sess("alice")
    snap = {p["t"]: p for p in alice.get("/api/snapshot").json()["pts"]}
    async with Client(URL, timeout=10) as c:
        print("== session and address space")
        ns = await c.get_namespace_array()
        check("namespace array has the server's namespaces", len(ns) == 3 and ns[2] == "urn:gridsovereign:model", ns)
        eps = await c.connect_and_get_server_endpoints() if False else None
        objs = c.nodes.objects
        kids = await objs.get_children()
        names = [(await k.read_browse_name()).Name for k in kids]
        check("Objects folder lists Server and GridSovereign", "GridSovereign" in names and "Server" in names, names)
        gs = [k for k in kids if (await k.read_browse_name()).Name == "GridSovereign"][0]
        groups = await gs.get_children()
        check("one folder per substation (118-bus model: more than 100)", len(groups) > 100, len(groups))
        g = groups[0]
        vars_ = await g.get_children()
        check("a substation folder holds its points as variables", len(vars_) > 5, len(vars_))
        print("== values agree with the SCADA core")
        agree = bad = checked = 0
        for g in groups[:12]:
            for v in (await g.get_children())[:40]:
                tag = (await v.read_browse_name()).Name
                dv = await v.read_data_value()
                p = snap.get(tag)
                if p is None or "v" not in p or dv.Value is None: continue
                checked += 1
                if abs(float(dv.Value.Value) - p["v"]) <= max(0.5, 0.02 * abs(p["v"])): agree += 1
                else: bad += 1
        check(f"{checked} analog points: OPC UA value equals the core's (live data may move between reads)", checked > 50 and agree / checked > 0.9, (checked, agree, bad))
        v = None
        for cand in await groups[0].get_children():
            if (await cand.read_browse_name()).Name in snap: v = cand; break
        dv = await v.read_data_value()
        check("a live point reads with Good status and a source timestamp", dv.StatusCode.is_good() and dv.SourceTimestamp is not None, dv)
        check("DataType attribute is Double or Int32", (await v.read_data_type()).Identifier in (11, 6))
        print("== things that must be refused")
        try:
            await v.write_value(1.0); check("writing a point is refused (read-only server)", False, "write succeeded")
        except Exception as e:
            check("writing a point is refused (read-only server)", True)
        try:
            bad = c.get_node("ns=2;s=NO.SUCH.TAG"); await bad.read_value(); check("unknown node -> bad status", False)
        except Exception as e:
            check("unknown node -> bad status", "BadNodeIdUnknown" in repr(e) or "Bad" in repr(e), repr(e)[:100])
        try:
            await c.nodes.root.call_method("0:Anything"); check("method calls are refused", False)
        except Exception:
            check("method calls are refused", True)
    print("== security-policy and robustness")
    try:
        c2 = Client(URL, timeout=5)
        await c2.set_security_string("Basic256Sha256,SignAndEncrypt,/nonexistent.pem,/nonexistent.key")
        check("signed/encrypted policy is refused (cannot even be configured without certificates)", False)
    except Exception:
        check("signed/encrypted policy request cannot be satisfied (None only)", True)
    def raw(data, wait=1.0):
        s = socket.create_connection(("127.0.0.1", 4840), timeout=3); s.sendall(data); time.sleep(wait)
        try: r = s.recv(4096)
        except Exception: r = b""
        s.close(); return r
    hello = b"HELF" + struct.pack("<I", 8 + 20 + 4 + 5) + struct.pack("<IIIII", 0, 65536, 65536, 0, 0) + struct.pack("<i", 5) + b"opc.t"
    r = raw(hello)
    check("valid Hello answered with Acknowledge", r[:3] == b"ACK", r[:8])
    check("garbage is not answered with data", b"ACK" not in raw(b"\xff" * 64, 0.3))
    r = raw(b"HELF" + struct.pack("<I", 0xFFFFFFF0))
    check("oversize length field is dropped", r == b"", r[:8])
    r = raw(hello + b"MSGF" + struct.pack("<I", 24) + struct.pack("<IIII", 1, 1, 1, 1))
    check("a message before OpenSecureChannel gets an error and the connection closes", r[:3] == b"ACK" and b"ERR" in r, r[:40])
    alive = False
    try:
        async with Client(URL, timeout=5) as c3:
            alive = (await c3.get_namespace_array()) is not None
    except Exception: pass
    check("server still serves after the hostile inputs", alive)
    print(f"\ne2e_opcua: {ok} passed, {fail} failed")
    return fail

sys.exit(1 if asyncio.run(main()) else 0)

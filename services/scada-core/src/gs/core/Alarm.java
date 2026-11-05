package gs.core;

import java.util.*;

public final class Alarm {
    public static final String ACTIVE_UNACK = "ACTIVE_UNACK", ACTIVE_ACK = "ACTIVE_ACK", RTN_UNACK = "RTN_UNACK", CLOSED = "CLOSED";
    public long id, raiseTs, ackTs, rtnTs, group;
    public String tag, type, text, substation, state = ACTIVE_UNACK, ackBy;
    public int prio, level;            // level: limit level for limit alarms
    public boolean latching;           // event alarms (trip): no return-to-normal, closed by ack
    public boolean firstOut, consequential, shelved;
    public double value;

    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id); m.put("tag", tag); m.put("type", type); m.put("prio", (long) prio); m.put("text", text);
        m.put("sub", substation); m.put("state", state); m.put("raised", raiseTs);
        if (ackBy != null) { m.put("ackBy", ackBy); m.put("ackTs", ackTs); }
        if (rtnTs != 0) m.put("rtn", rtnTs);
        if (group != 0) m.put("group", group);
        if (firstOut) m.put("firstOut", true);
        if (consequential) m.put("consequential", true);
        if (shelved) m.put("shelved", true);
        m.put("v", value);
        return m;
    }
}

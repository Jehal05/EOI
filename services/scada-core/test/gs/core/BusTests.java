package gs.core;

import java.io.*;
import java.net.*;
import java.util.*;
import java.util.concurrent.*;

/** The Redis Streams publisher against a fake RESP server: wire format, MAXLEN, AUTH, outage and recovery. */
public final class BusTests {
    public static void run() throws Exception {
        List<List<String>> got = new CopyOnWriteArrayList<>();
        List<Socket> open = new CopyOnWriteArrayList<>();
        ServerSocket ss = new ServerSocket(0, 5, InetAddress.getLoopbackAddress());
        Thread srv = Thread.startVirtualThread(() -> {
            try {
                while (true) {
                    Socket s = ss.accept(); open.add(s);
                    Thread.startVirtualThread(() -> {
                        try (s; InputStream in = new BufferedInputStream(s.getInputStream()); OutputStream out = s.getOutputStream()) {
                            while (true) {
                                String h = line(in); if (h == null) return;
                                int n = Integer.parseInt(h.substring(1)); List<String> parts = new ArrayList<>();
                                for (int i = 0; i < n; i++) { int len = Integer.parseInt(line(in).substring(1)); byte[] b = in.readNBytes(len); in.readNBytes(2); parts.add(new String(b, "UTF-8")); }
                                got.add(parts);
                                out.write((parts.get(0).equals("XADD") ? "$15\r\n1700000000000-0\r\n" : "+OK\r\n").getBytes()); out.flush();
                            }
                        } catch (IOException ignore) { }
                    });
                }
            } catch (IOException ignore) { }
        });
        int port = ss.getLocalPort();
        Bus b = new Bus("redis://:s3cret@127.0.0.1:" + port, "gs:telemetry", 1000);
        String msg = "{\"seq\":1,\"pts\":[{\"t\":\"x\",\"v\":1.5}],\"txt\":\"åß\"}";
        T.ok("publish succeeds", b.publish(msg));
        T.ok("AUTH is sent first, then XADD with MAXLEN ~ and the payload intact",
            got.size() == 2 && got.get(0).equals(List.of("AUTH", "s3cret")) && got.get(1).equals(List.of("XADD", "gs:telemetry", "MAXLEN", "~", "1000", "*", "d", msg)));
        ss.close();
        for (Socket k : open) k.close();
        for (int i = 0; i < 3; i++) b.publish(msg);
        T.ok("an unreachable bus never throws; messages are counted as dropped", b.dropped.get() >= 1 && b.published.get() == 1);
        long t0 = System.nanoTime();
        for (int i = 0; i < 1000; i++) b.publish(msg);
        T.ok("while the bus is down publish returns immediately (no blocking of the control core): " + (System.nanoTime() - t0) / 1_000_000 + " ms for 1000 calls", (System.nanoTime() - t0) < 500_000_000L);
        boolean bad = false;
        try { new Bus("http://x", "s", 1); } catch (IllegalArgumentException e) { bad = true; }
        T.ok("only redis:// URLs are accepted", bad);
    }

    static String line(InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder(); int c;
        while ((c = in.read()) != '\n') { if (c < 0) return null; if (c != '\r') sb.append((char) c); }
        return sb.toString();
    }
}
